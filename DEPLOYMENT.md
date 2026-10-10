# DGP Live Dashboard — Deployment Guide

Three layers, one repository:

```
frontend/  +  app/ (FastAPI)  ->  Supabase (PostgreSQL)
```

The API serves the frontend itself, so in production you deploy **one thing**
(the FastAPI app) and point it at a Supabase project.

---

## 1. What you need

| Item | Notes |
|---|---|
| Python 3.10+ | 3.14 works |
| Supabase project | You already have one |
| The DGP project folder | `Input/`, `updating_files/`, `History/`, `Output/`, `_dashboard_template.html` |
| A machine that stays on | The ETL + dashboard import run there |
| HTTPS in front of the app | See §5 — do not skip on a public network |

The `History/` folder is the real source of truth (hundreds of MB). The app
only needs `History/` + `_dashboard_template.html` to import bundles; it needs
the full folder tree to run the ETL scripts.

---

## 2. First-time setup

```powershell
cd G:\profile\Desktop\DGP_analysis\liveapp
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create `liveapp\.env` (copy `.env.example`):

```
DGP_DATABASE_URL=postgresql://postgres.<PROJECT_REF>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:5432/postgres
DGP_JWT_SECRET=<64+ random chars>
DGP_TOKEN_TTL_MINUTES=720
DGP_ETL_STEP_TIMEOUT=3600
DGP_SKIP_SOURCE_SCRIPTS=0
DGP_EXCLUDED_VISIT_MONTHS=2026-08
DGP_EXCLUDED_VISIT_DATES=2026-10-15
DGP_CORS_ORIGINS=
```

Generate a secret:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Notes:
- Use the **Session pooler** (port `5432`). The transaction pooler (`6543`)
  recycles connections mid-import and can kill a long bundle decrypt.
- URL-encode the password (`@` -> `%40`).
- `DGP_CORS_ORIGINS` stays **blank**. The frontend is served by this same app,
  so it is same-origin and needs no CORS. Never set `*`.

First launch creates every table in Supabase and seeds the 11 users
automatically. Nothing to import.

---

## 3. Running it

### Development

```powershell
cd G:\profile\Desktop\DGP_analysis\liveapp
python main.py                     # reload on, port 8000
python main.py --port 8080
python main.py --no-reload         # faster
```

### Production (single process)

```powershell
python main.py --no-reload --host 127.0.0.1 --port 8000
```

Bind to `127.0.0.1` and let a reverse proxy terminate TLS — do not expose
uvicorn directly to the internet.

### Production (multiple workers)

The bundle import is CPU/IO heavy and holds a DB connection. Multiple workers
are fine because imports are short; avoid them if you also run the ETL from the
web UI.

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4
```

### Windows: run as a service (auto-start)

Use [NSSM](https://nssm.cc):

```powershell
nssm install DGPApp "C:\path\to\.venv\Scripts\python.exe" `
  "G:\profile\Desktop\DGP_analysis\liveapp\main.py --no-reload --host 127.0.0.1 --port 8000"
nssm set DGPApp AppDirectory "G:\profile\Desktop\DGP_analysis\liveapp"
nssm set DGPApp Start SERVICE_AUTO_START
nssm start DGPApp
```

Or use **Task Scheduler** -> "Run whether user is logged on or not" ->
trigger "At startup".

### Linux: systemd

`/etc/systemd/system/dgp.service`:

```ini
[Unit]
Description=DGP Live Dashboard
After=network-online.target

[Service]
Type=simple
User=dgp
WorkingDirectory=/opt/dgp_analysis/liveapp
Environment="PATH=/opt/dgp_analysis/liveapp/.venv/bin"
ExecStart=/opt/dgp_analysis/liveapp/.venv/bin/uvicorn app.main:app \
          --host 127.0.0.1 --port 8000 --workers 4
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dgp
sudo journalctl -u dgp -f
```

---

## 4. Firewall (LAN access)

```powershell
New-NetFirewallRule -DisplayName "DGP Dashboard" -Direction Inbound `
  -LocalPort 8000 -Protocol TCP -Action Allow -Profile Private
```

Users then open `http://<server-ip>:8000`.

Use a **Private** profile only. Do not open this on Public networks.

---

## 5. HTTPS (required before this leaves your LAN)

The login token is stored in `localStorage` and sent as `Authorization: Bearer`.
Over plain HTTP it crosses the network in clear text — anyone on the same WiFi
can read it and log in as any user. Put a reverse proxy in front:

### nginx

```nginx
server {
    listen 443 ssl;
    server_name dgp.example.com;

    ssl_certificate     /etc/letsencrypt/live/dgp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dgp.example.com/privkey.pem;

    client_max_body_size 2048M;   # bundle zips are large

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;  # bundle import can take a while
    }
}

server {
    listen 80;
    server_name dgp.example.com;
    return 301 https://$host$request_uri;
}
```

`X-Forwarded-For` matters: the Viewer Activity log records the client IP from
it, so without this every row shows the proxy's address.

Get a certificate:

```bash
sudo certbot --nginx -d dgp.example.com
```

### IIS (Windows)

Install URL Rewrite + ARR, set up an HTTPS binding and a reverse proxy to
`127.0.0.1:8000`. Set the site binding to require SSL, and set
`client_max_body_size` equivalent to at least 2 GB in `web.config`
(`<requestFiltering><requestLimits maxAllowed="2147483648" />`).

---

## 6. Daily operation

```
build_dashboard.py  ->  Output/site/
zip_site.py         ->  Output/site.zip
Admin -> Import zip (or "Import from server folder")
```

Viewers just log in. Tracking starts from the first dashboard load.

If you also want the app to run the ETL itself, press **Run ETL scripts** in the
admin drawer. That spawns `Rename.py`, `find_easy_sbf.py`, `achieved_sbf.py`
and `find_opening_easy_sbf.py` against the folders beside `liveapp/`.

---

## 7. Security checklist

- [ ] `DGP_JWT_SECRET` replaced with a 48+ char random value
- [ ] Supabase password **rotated** if it was ever pasted into chat, email or a doc
- [ ] HTTPS terminated in front of the app
- [ ] uvicorn bound to `127.0.0.1`, not `0.0.0.0`
- [ ] `DGP_CORS_ORIGINS` left blank
- [ ] Firewall rule limited to the Private profile
- [ ] `liveapp/.env` not committed (`.gitignore` covers it)
- [ ] `liveapp/seed_users.json` not committed — it holds the shared plaintext password
- [ ] The shared employee password rotated to per-person credentials
- [ ] Supabase RLS reviewed if any table is ever exposed to the `anon` key
- [ ] `Output/site/` never published to a public static host — it is now
      **unencrypted** JSON (`build_dashboard.py` no longer derives a password)

### About the shared password

All 11 employees currently share one password, so the Viewer Activity log proves
*a* session happened but not reliably *who* did it. Per-person passwords make
the log a real audit trail. That is a change in `seed_users.json` (which stays
local and git-ignored) plus a reset of the existing rows.

---

## 8. Backups

The database is the only thing that matters. Everything else is reproducible
from `History/`.

Supabase: **Dashboard -> Backups** (free tier keeps ~7 days) or
`pg_dump`:

```bash
pg_dump "$DATABASE_URL" --format=custom --file dgp_$(date +%F).dump
```

```powershell
pg_dump $env:DGP_DATABASE_URL --format=custom --file dgp_2026-10-10.dump
```

`History/` and `Output/` are not backed up by the app — copy them yourself if
rebuilds matter.

At a monthly publishing cadence the dataset tables settle around 12 x ~61k
customer rows a year, which is comfortable on the free tier. If you ever switch
to daily imports, prune old versions:

```sql
DELETE FROM dashboard_customer WHERE version_id NOT IN
  (SELECT id FROM dataset_version WHERE is_active);
DELETE FROM dashboard_core WHERE version_id NOT IN
  (SELECT id FROM dataset_version WHERE is_active);
DELETE FROM dataset_version WHERE is_active IS FALSE;
```

Viewers only ever read the active version, so this is safe.

---

## 9. GitHub + Vercel (cloud deployment)

### How the split works

```
Build machine (Windows)                    Vercel                     Supabase
------------------------------------------  ------------------------   ----------
build_dashboard.py
publish_site.py  ----------------------------------> (nothing)  ---->  dataset tables
                                          api/index.py  ----reads---->  read API
browser -------------------------------------->  frontend + API
```

Vercel serves the **read path** only:

| Endpoint | Measured | Vercel limit |
|---|---|---|
| `GET /api/core` | ~2.1 s | 60 s |
| `GET /api/dse/{dse}/customers` | ~1.1 s | 60 s |
| `POST /api/login` | <1 s | 60 s |
| `POST /api/activity/open` | <0.8 s | 60 s |
| `POST /api/admin/import-site` | **47.5 s** | **disabled** |

The import writes tens of thousands of rows to Postgres, which does not fit in
a 60 s function. Publishing therefore happens on the build machine with
`publish_site.py`, which does exactly what the Admin upload button does.

### 1. Push to GitHub

```powershell
cd G:\profile\Desktop\DGP_analysis
git add -A
git status                    # confirm no .env, seed_users.json or data files
git commit -m "DGP live dashboard: Supabase backend, bundle publish, activity monitor"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

`.gitignore` already excludes `.env`, `seed_users.json`, the 107 MB SQLite file
and every data folder (~450 MB). What gets committed is ~43 files / ~430 KB,
including `_dashboard_template.html`, which the app serves.

Use a **private** repository.

### 2. Create the Vercel project

1. [vercel.com/new](https://vercel.com/new) -> import the GitHub repo
2. Framework preset: **Other**
3. Build command: leave empty
4. Output directory: `api`

`vercel.json` already sets the output directory, the rewrite rule, a 60 s
function limit and `includeFiles` for `_dashboard_template.html`.

Vercel installs `requirements.txt` by default. Use the lean set instead
(Project -> Settings -> Build & Development, or omit that step):

```
requirements-vercel.txt
```

That drops `pandas`/`pyarrow`, which only the ETL and the importer need, and
keeps the function small enough to deploy and quick to cold-start.

### 3. Environment variables

Project -> Settings -> Environment Variables. Add to **both** Production and
Preview:

| Name | Value |
|---|---|
| `DGP_DATABASE_URL` | the Supabase **Session pooler** URL (port 5432) |
| `DGP_JWT_SECRET` | a **new** strong secret for production |
| `DGP_TOKEN_TTL_MINUTES` | `720` |
| `DGP_IMPORT_ENABLED` | `0` |
| `DGP_CORS_ORIGINS` | leave empty (same-origin) |
| `DGP_EXCLUDED_VISIT_MONTHS` | `2026-08` |
| `DGP_EXCLUDED_VISIT_DATES` | `2026-10-15` |

Then **Redeploy**. The tables already exist in Supabase and the users are
already seeded, so there is nothing else to initialise.

With `DGP_IMPORT_ENABLED=0` the Admin drawer shows:

```
Browser import is disabled on this deployment.
python publish_site.py
```

instead of the upload form.

### 4. Publishing each month

On the build machine:

```powershell
cd G:\profile\Desktop\DGP_analysis
python build_dashboard.py
python publish_site.py
```

That is the entire routine. Viewers on the Vercel URL get the new version on
their next page load — no redeploy, because the data lives in Supabase.

### 5. Verify the deployment

```powershell
curl.exe -s -o NUL -w "%{http_code}" https://<your-domain>/api/login   # 405 = route alive
```

If `/` returns a JSON 404, `outputDirectory` is wrong. If the dashboard reports
"template not found", `includeFiles` did not pick up `_dashboard_template.html`.

### Limits worth knowing

- **Hobby: 100 s/day of function time, 1 GB RAM, cold starts.** Read paths are a
  few hundred milliseconds each, so 11 daily users is comfortable.
- **Supabase connections.** Each warm function instance holds one. The free tier
  allows ~15 concurrent — fine here.
- **Data never travels through Vercel**, so republishing costs 0 s of function
  time and does not count against the daily budget.
- The ETL scripts cannot run on Vercel at all (GBs of local files). Use
  `publish_site.py`, or **Run ETL scripts** on the build machine.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `500` on login after an `.env` edit | The app read the old config. Restart the process. |
| `409 "A processing job is already running"` | A crashed job left a row. It auto-fails after `DGP_ETL_STEP_TIMEOUT`; restart to clear it immediately. |
| `400 Could not decrypt this legacy encrypted bundle` | Old v2 bundle. Rebuild with the current `build_dashboard.py` (no longer encrypts). |
| `409 No dataset has been published yet` | Admin -> Import zip. |
| Dashboard blank, `progress bar spins forever` | Check the message under the bar; usually `/api/core` 401 (token expired -> log out and in). |
| Bundle upload fails immediately | Check the reverse proxy body-size limit. |
| `viewer_activity` empty | Table is created on startup. Confirm the app restarted after the upgrade. |
| Activity shows the proxy IP | Add `proxy_set_header X-Forwarded-For` (nginx). |

---

## 11. Rollback

```powershell
# stop
nssm stop DGPApp          # or: sudo systemctl stop dgp

# previous code
cd G:\profile\Desktop\DGP_analysis
git checkout <previous-tag>    # if you use git

# restart
python liveapp\main.py --no-reload
```

`init_db()` only creates tables; it never drops or alters them, so rolling the
code back cannot damage data.