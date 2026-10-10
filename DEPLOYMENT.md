# Deployment

## Read this first: the Root Directory trap

**Vercel → Project Settings → General → Root Directory must be the repository
root (empty, or `/`). Do not set it to `liveapp`.**

This single setting caused every failed deployment so far. With the Root
Directory set to `liveapp`:

- `api/index.py` is never uploaded, so it is never the entrypoint;
- the repository-root `requirements.txt` is never uploaded, so Vercel installs
  whatever it guesses instead — including missing packages;
- the repository-root `vercel.json` is never read, so edits to it have **no
  effect at all**.

You can tell this has happened from the runtime log. A correct deployment says:

    could not import "api/index.py"

A deployment with the Root Directory set to `liveapp` says:

    could not import "app/main.py"

If you ever see `app/main.py` in the log, fix the Root Directory. Nothing in
the code will help until you do.

---

## The "Multiple applications detected" prompt

On import, Vercel may offer to deploy this repository as **Services** and
suggest a three-service `vercel.json`. **Decline it and set the Application
Preset to `Other`.**

This repository contains exactly one application. The detector is
pattern-matching three files that are all the same app:

| What Vercel calls an app | What it actually is |
|---|---|
| `DGP_Analysis` (root) | the same FastAPI app |
| `liveapp` (`liveapp/main.py`) | the local dev launcher, same app |
| `app` (`liveapp/app/main.py`) | the same FastAPI app, one folder deeper |

Do not add a `services` key to `vercel.json`. Per Vercel's own documentation:

> "A project builds as services only when this setting is selected and a
> `services` key is present in `vercel.json`." — and "You don't need services if
> your project uses a single framework."

The moment `services` is present, the top-level `functions`, `framework`,
`buildCommand`, `installCommand`, `devCommand`, `ignoreCommand` and
`outputDirectory` keys **become invalid** and must move inside a service. The
existing `vercel.json` is written for the normal (non-services) mode and is
correct as-is.

Vercel scans for a Python entrypoint in `app.py`, `index.py`, `server.py`,
`main.py`, `wsgi.py` and `asgi.py` at the project root, plus the same
filenames inside `src/`, `app/` and `api/`. At the repository root this
repository has **exactly one** match:

```
api/index.py     <-- the entrypoint
```

`liveapp/main.py` and `liveapp/app/main.py` sit under `liveapp/`, which is not
a scanned directory, so they are never candidates. That is precisely why the
Root Directory must stay at the repository root — set it to `liveapp` and
`liveapp/app/main.py` becomes the entrypoint instead.

---

## Architecture

Two tiers, and keeping them straight is the whole design:

| Where | What runs | What it needs |
|---|---|---|
| **Build machine** (your laptop) | `Rename.py` → `find_easy_sbf.py` → `achieved_sbf.py` → `find_opening_easy_sbf.py` → `build_dashboard.py`, then `zip_site.py` and/or `publish_site.py` | The data files (`Input/`, `history/`, `updating_files/`) and pandas |
| **Vercel** | `api/index.py` → `liveapp/app` (FastAPI) | A PostgreSQL connection string and nothing else |

Data never has to travel through Vercel. `publish_site.py` writes straight to
Supabase from the build machine and the deployed function only ever *reads*.
That is why the function bundle is ~400 KB.

Publishing from the browser works too, over the same database, via chunked
upload — see [Importing from the browser](#importing-from-the-browser).

---

## 1. One-time setup

### Supabase

1. Create the project, then **Project settings → Database → Connection string**.
2. Use the **Session pooler** (port `5432`). ETL jobs hold a connection open for
   a long time and the transaction pooler (6543) will drop them.
3. URL-encode the password if it contains `@ : / ? #`.

The app creates its own tables on first start, so there is no migration step.
`upload_session` is the only table added since the original schema.

### Vercel

1. **Add New → Project**, import `selva943/DGP_Analysis`.
2. **Framework Preset: `Other`.** Do not pick FastAPI. `vercel.json` sets
   `"framework": null`, which forces file-based routing to `api/index.py` and
   removes any chance of the preset choosing a different entrypoint.
3. **Root Directory: leave empty** (see the trap above).
4. Leave Build/Install/Output Command on their defaults.
5. **Deploy.**

### Environment variables

There is no `.env` on the server — `.env` is gitignored and never deployed.
Set these under **Project → Settings → Environment Variables**, for Production
(and Preview too, if you want previews to work).

| Variable | Required | Value |
|---|---|---|
| `DGP_DATABASE_URL` | **yes** | `postgresql://postgres.<ref>:<password>@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres` |
| `DGP_JWT_SECRET` | **yes** | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DGP_BOOTSTRAP_ADMIN_USERNAME` | first deploy | your admin username |
| `DGP_BOOTSTRAP_ADMIN_PASSWORD` | first deploy | a real password |
| `DGP_BOOTSTRAP_ADMIN_DISPLAY_NAME` | no | label shown in the UI |
| `DGP_IMPORT_ENABLED` | no | `1` (default). Bundle import works on Vercel via chunked upload. |
| `DGP_LOCAL_PROCESSING_ENABLED` | no | `0` on Vercel — see [Why local processing is off](#why-local-processing-is-off) |
| `DGP_CORS_ORIGINS` | no | leave blank; the frontend is same-origin |
| `DGP_TOKEN_TTL_MINUTES` | no | `720` (12 h) default |

Add a variable, then **redeploy** — Vercel only applies env changes to new
deployments.

### Verify the deployment

Open **`https://<your-domain>/api/health`** straight after deploying. It needs
no login and answers even when the database is broken:

```json
{
  "status": "ok",
  "database_url_set": true,
  "jwt_secret_strong": true,
  "bootstrap_admin_configured": true,
  "database_ok": true,
  "database_error": null,
  "user_count": 1,
  "has_dataset": true
}
```

- `status: "degraded"` with HTTP 503 means the database is unreachable.
  `database_error` says why. The site still loads; only data endpoints fail.
- `has_dataset: false` means nothing has been published yet — normal on a fresh
  database. Run `python publish_site.py`.
- It never echoes a connection string, password or hostname.

### The first login

`liveapp/seed_users.json` is gitignored — it holds plaintext passwords and has
no business in git — so the deployment starts with no users.

On the **first** start `seed.bootstrap_admin()` creates a single admin from the
two `DGP_BOOTSTRAP_ADMIN_*` variables, and **only if the `users` table is
completely empty**. From then on it is a no-op, so:

- rotating the password variable later changes no existing login;
- the app can never resurrect a deleted account;
- the password only ever exists in Vercel's environment settings.

Add viewers by inserting rows into `users` with a bcrypt hash.

> If this project was deployed before, an old admin row may already exist. The
> bootstrap will correctly skip, and the password you set in Vercel will not
> apply. Delete that row to re-bootstrap, or update its `password_hash`.

---

## 2. Publishing new data

Run on the build machine, with `liveapp/.env` pointing at the **same** Supabase
project.

```bash
# 1. New reports into Input/ and updating_files/
python Rename.py                 # normalise filenames

# 2. The three analyses (write to output/ and history/)
python find_easy_sbf.py          # Easy_SBF_Gap_<date>.xlsx
python achieved_sbf.py           # Achieved_SBF_<stamp>.xlsx
python find_opening_easy_sbf.py  # Opening_Easy_SBF_<month>.xlsx

# 3. Build the bundle from history/
python build_dashboard.py        # -> output/site/
python zip_site.py               # -> output/site.zip  (for browser import)

# 4a. Publish straight to the database
python publish_site.py
```

Step 4a is enough: the deployment reads the active version on each request, so
no redeploy or restart is needed.

To roll back, re-run `publish_site.py` against an older `output/site/`. Every
publish is a new `dataset_version` row, so nothing is overwritten.

### Month rollover

```bash
python monthly_reset.py           # dry run, prints the plan
python monthly_reset.py --apply   # archive stale months to history/archive/<YYYY>/<MM>/
```

Dry run by default on purpose — read the plan before applying.

---

## 3. Importing from the browser

Works in production, including on Vercel. This is the same code path
`publish_site.py` uses, just driven from the admin panel.

Vercel caps a request body at **4.5 MB** and `output/site.zip` is already about
**4.1 MB**, so a single multipart POST would have almost no headroom and would
break as soon as a month of data is added. The admin panel therefore slices the
zip into 3 MB parts and posts them one at a time:

```
POST /api/admin/import-site/chunked/init     -> { upload_id, chunk_bytes }
POST /api/admin/import-site/chunked/part     (xN, one per slice)
POST /api/admin/import-site/chunked/commit   -> assembles, decrypts, publishes
```

Parts are staged in the **`upload_session` table, not `/tmp`**, because a
serverless instance is not guaranteed to be the same one for the next request.
The database is shared by every instance, so the upload survives being routed
around. Re-sending a slice overwrites it, so a retry is safe. A commit with a
gap returns 409 naming the missing indices.

Uncommitted parts are removed on commit (including a failed commit). They are
not garbage-collected otherwise — if an upload is abandoned, delete its rows
from `upload_session` when convenient.

### Why local processing is off

`DGP_LOCAL_PROCESSING_ENABLED` gates the **ETL scripts only**
(`Rename.py`, `find_easy_sbf.py`, `achieved_sbf.py`,
`find_opening_easy_sbf.py`). They read 330 MB of CSVs and XLSX from `Input/`
and `updating_files/`, and write Excel workbooks back.

That cannot run on Vercel, for physical reasons rather than configuration:

- the filesystem is read-only outside `/tmp`;
- the inputs are ~330 MB, against a **4.5 MB** request-body limit and a 500 MB
  `/tmp` cap — they cannot be uploaded in any practical number of requests;
- the run takes minutes and the subprocess dies with the invocation that
  started it.

So run the ETL on the build machine and publish with `publish_site.py`, or
build the bundle and import it from the browser. Both produce the identical
`dataset_version`. Everything else — login, roles, the dashboard, DSE drill-down,
viewer activity, CSV/XLSX export, dataset publishing — runs in production
exactly as it does in development.

With the flag off the admin panel replaces the ETL button with these
instructions rather than failing.

---

## 4. What is in the deployed bundle

```json
{
  "framework": null,
  "regions": ["nrt1"],
  "functions": { "api/index.py": { "maxDuration": 300 } },
  "rewrites": [{ "source": "/(.*)", "destination": "/api/index.py" }]
}
```

- **`"framework": null`** forces file-based routing. Without it, the presence of
  `fastapi` in `requirements.txt` lets Vercel apply a FastAPI framework preset,
  and a framework preset takes precedence over files under `/api`.
- **`regions: ["nrt1"]`** is Tokyo, matching an `ap-northeast-1` Supabase
  pooler. New projects default to `iad1` (Washington, D.C.). Use whichever
  region matches your connection string. One region, so it is fine on Hobby.
- **`maxDuration: 300`** is the Hobby ceiling with Fluid compute. Importing a
  large bundle is the slowest thing the function does.
- **`includeFiles` was removed on purpose.** Vercel's Python builder never reads
  it — it globs the whole uploaded tree with no tree-shaking — so four commits
  spent tuning it could not have changed anything. `.vercelignore` is what
  controls the upload.
- `memory` is absent because it cannot be set in `vercel.json` while Fluid
  compute is enabled. Set it under Project → Settings → Functions.

**Do not add these to `.vercelignore`:** `api/`, `liveapp/app/`,
`liveapp/frontend/`, `_dashboard_template.html`, `build_dashboard.py` (imported
by `liveapp/app/services/builder.py`), and `requirements.txt`.

---

## 5. Why the app used to return 500 on every request

Two separate causes, both now fixed. Recorded because the symptoms were
identical and the causes were not.

**A missing PostgreSQL driver.** On 2026-09-24 SQLAlchemy released 2.1, which
changed the default DBAPI for a bare `postgresql://` URL from psycopg2 to
psycopg (v3). `requirements.txt` only said `>=2.0`, so the deployment resolved
2.1 while the development machine stayed on 2.0, and every request died with:

    ModuleNotFoundError: No module named 'psycopg'

Fixed three ways so it cannot recur: both drivers are now installed, every
version has an upper bound, and `config.pin_postgres_driver()` rewrites a bare
`postgresql://` to an explicit `postgresql+psycopg://` or
`postgresql+psycopg2://` depending on what is actually importable. SQLAlchemy's
default no longer gets a vote.

**A fatal startup error.** Vercel runs FastAPI's lifespan startup through
uvicorn with `lifespan="auto"`. If the handler raises, the process exits before
the ASGI app is ever served, so *every* request becomes an anonymous
`500 FUNCTION_INVOCATION_FAILED` with nothing useful in the logs — the failure
happens before user logging is wired up.

`init_db()` connecting to the database at startup was exactly that risk. Startup
is now non-fatal: a database problem degrades the app to 503 on data endpoints
while the login page, the admin panel and `/api/health` keep working, which is
precisely the state you want when diagnosing a deployment.

---

## 6. Troubleshooting

**Everything is 500 and the log says `could not import "app/main.py"`** — the
Root Directory is set to `liveapp`. See the trap at the top.

**`ModuleNotFoundError: No module named '...'`** — the wrong dependency set was
installed, which also means the Root Directory is wrong. Once it is the
repository root, Vercel reads the root `requirements.txt`.

**`/api/health` returns 503** — read `database_error`. It is either a malformed
`DGP_DATABASE_URL` or Supabase refusing the connection. Note the pooler host
(`*.pooler.supabase.com`), not the direct `db.<ref>.supabase.co` host.

**`has_dataset: false` after publishing** — the build machine's `.env` points at
a different database than Vercel's environment variable. Compare the project
reference in both.

**Nobody can log in** — confirm `DGP_BOOTSTRAP_ADMIN_USERNAME` and
`_PASSWORD` are set for the environment you deployed to, then redeploy. If
users already exist the bootstrap is skipped by design.

**Blank page, login works** — `_dashboard_template.html` is missing from the
bundle. Confirm it is not in `.vercelignore`.

**Import fails with 413** — a single request body over 4.5 MB. The admin panel
chunks automatically; only a hand-rolled `POST /api/admin/import-site` with a
large file can hit this.

**Slow cold starts** — every start runs `create_all` against the pooler. Confirm
`regions` in `vercel.json` matches your Supabase region.

---

## 7. Secret hygiene

- `liveapp/.env` and `liveapp/seed_users.json` are gitignored. Both hold
  plaintext credentials. If either has been pasted into a chat, an email or a
  shared document, rotate it: a new database password in Supabase, and a new
  `DGP_JWT_SECRET` in Vercel (which invalidates every issued token).
- `DGP_JWT_SECRET` must be at least 32 characters. The app logs a loud warning
  at startup if it is left at the default; `/api/health` reports it as
  `jwt_secret_strong`.
- Never commit `.env`. `.gitignore` and `.vercelignore` both exclude it, and the
  committed `liveapp/.env.example` is a template with empty values.