# Deployment

Two machines are involved, and keeping them straight is the whole trick:

| Where | What runs | What it needs |
|---|---|---|
| **Build machine** (your laptop) | `Rename.py` → `find_easy_sbf.py` → `achieved_sbf.py` → `find_opening_easy_sbf.py` → `build_dashboard.py` → `publish_site.py` | The data files (`Input/`, `history/`, `updating_files/`) and pandas |
| **Vercel** | `api/index.py` → `liveapp/app` (FastAPI) | A PostgreSQL connection string and nothing else |

Data never travels through Vercel. `publish_site.py` writes straight to
Supabase from the build machine; the deployed function only ever *reads*.
That is why the function bundle is small and why no data files are committed.

---

## 1. One-time setup

### Supabase

1. Create the project, then **Project settings → Database → Connection string**.
2. Use the **Session pooler** (port `5432`). ETL jobs hold a connection open for
   a long time and the transaction pooler (6543) will drop them.
3. URL-encode the password if it contains `@ : / ? #`.

The app creates its own tables on first start (`Base.metadata.create_all`), so
there is no migration step. Deploy once before the first publish and the schema
will be in place.

### Vercel

1. **Add New → Project**, then import `selva943/DGP_Analysis`.
2. Leave every build setting alone. This repo has no framework preset to guess
   at: `vercel.json` already points at `api/index.py`, and the root
   `requirements.txt` is the dependency list Vercel installs.
3. **Deploy.**

### Environment variables

Vercel is configured entirely through environment variables — there is no
`.env` file on the server, because `.env` is gitignored and never deployed.

Project → Settings → Environment Variables. Set these for **Production** (and
**Preview** too, if you want previews to work).

| Variable | Required | Value |
|---|---|---|
| `DGP_DATABASE_URL` | **yes** | `postgresql://postgres.<ref>:<password>@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres` |
| `DGP_JWT_SECRET` | **yes** | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DGP_IMPORT_ENABLED` | recommended | `0` — see below |
| `DGP_LOCAL_PROCESSING_ENABLED` | recommended | `0` — see below |
| `DGP_BOOTSTRAP_ADMIN_USERNAME` | first deploy | your admin username |
| `DGP_BOOTSTRAP_ADMIN_PASSWORD` | first deploy | a real password |
| `DGP_BOOTSTRAP_ADMIN_DISPLAY_NAME` | optional | label shown in the UI |
| `DGP_CORS_ORIGINS` | no | leave blank; the frontend is same-origin |
| `DGP_TOKEN_TTL_MINUTES` | no | `720` (12 h) default |

After adding any variable, redeploy — Vercel only applies env changes to new
deployments.

#### Why `DGP_IMPORT_ENABLED=0`

Importing a bundle writes tens of thousands of rows and takes ~45 s or more.
The function timeout is 60 s, so the request would be killed partway through and
leave the database inconsistent. With it off the admin panel explains the
alternative instead of failing.

#### Why `DGP_LOCAL_PROCESSING_ENABLED=0`

"Run ETL scripts" spawns a Python subprocess from the project root. Vercel has
no writable checkout and no interpreter that outlives the request, so the call
could only ever 500. With it off the button is replaced by the
`publish_site.py` instructions.

Both flags default to `1`, so a local machine behaves exactly as before.

### The first login

`liveapp/seed_users.json` is gitignored — it holds plaintext passwords and has
no business in git. So the serverless deployment starts with no users at all.

On the **first** start, `seed.bootstrap_admin()` creates a single admin from
`DGP_BOOTSTRAP_ADMIN_USERNAME` / `DGP_BOOTSTRAP_ADMIN_PASSWORD`, and **only if
the `users` table is completely empty**. From then on it is a no-op, so:

- rotating the password variable later does not change any existing login;
- the app can never resurrect a deleted account;
- the password only ever exists in Vercel's environment settings.

Sign in, and you are admin. Add viewers through the `users` table.

> If you previously deployed this project, check whether an old admin row is
> already there. If so the bootstrap will correctly skip, and the password you
> set in Vercel will not apply — reset that row's `password_hash` or delete the
> user to re-bootstrap.

---

## 2. Publishing new data

Run on the build machine, with `liveapp/.env` pointing at the **same**
Supabase project. There is no `.env` on Vercel to copy, so keep this one local
and out of any shared backup.

```bash
# 1. Put the new reports in Input/ and updating_files/
python Rename.py                 # normalise filenames

# 2. The three analyses
python find_easy_sbf.py          # Easy_SBF_Gap_<date>.xlsx  -> output/ + history/
python achieved_sbf.py           # Achieved_SBF_<stamp>.xlsx -> output/ + history/
python find_opening_easy_sbf.py  # Opening_Easy_SBF_<month>.xlsx

# 3. Build the bundle from history/
python build_dashboard.py        # -> output/site/
python zip_site.py               # -> output/site.zip   (only needed for manual import)

# 4. Publish to the database
python publish_site.py           # writes a new active dataset version
```

Step 4 is what makes the change visible on Vercel. The deployment never needs
restarting — it reads the active version on each request.

To roll back, re-run `publish_site.py` against an older `output/site/`. Every
publish is a new row in `dataset_version`, so nothing is overwritten.

### Month rollover

```bash
python monthly_reset.py              # dry run, prints what it would move
python monthly_reset.py --apply      # archive stale months into history/archive/<YYYY>/<MM>/
```

It is a dry run by default on purpose — check the plan before applying.

---

## 3. What is in the deployed bundle

`vercel.json` builds one function and rewrites every path to it:

```json
"functions": { "api/index.py": { "maxDuration": 60, "memory": 1024, "includeFiles": "**/*" } },
"rewrites":  [{ "source": "/(.*)", "destination": "/api/index.py" }]
```

`includeFiles: "**/*"` forces the whole uploaded tree into the function,
because the app lives in `liveapp/` and is reached through a runtime
`sys.path` entry that Vercel's import tracer cannot follow on its own.
`api/index.py` re-exports it explicitly to make the dependency visible.
`includeFiles` takes a single glob string, not an array.

`maxDuration` is 60 s, which is the Hobby ceiling. On Pro it can be raised.
`memory` only applies if Fluid compute is off — see Troubleshooting.

`.vercelignore` decides what gets uploaded in the first place. It excludes the
~450 MB of data directories, the ETL scripts, the docs, and anything secret.
This matters most for `vercel --prod` from a terminal, which deploys the
working directory rather than the committed files; a Git-connected deploy only
ever sees what is in the repository anyway.

**Keep in the bundle** (do not add these to `.vercelignore`):

| Path | Why |
|---|---|
| `liveapp/**` | the whole application and its frontend |
| `_dashboard_template.html` | the dashboard UI; `main.py` serves it at `/_dashboard_template.html` |
| `api/**` | the function entrypoint |

The root `requirements.txt` is the lean serverless set — no pandas, no
pyarrow. `pandas` is only reachable through `process_runner → builder`, which
is the ETL path that `DGP_LOCAL_PROCESSING_ENABLED=0` disables. The full set,
including pandas, lives in `liveapp/requirements.txt` for local use.

---

## 4. Troubleshooting

**`ModuleNotFoundError: No module named 'app'`** — `api/index.py` adds
`liveapp/` and the repo root to `sys.path` before importing. If you renamed
either folder, that list is the first thing to fix.

**Everything 500s immediately** — usually a missing or wrong `DGP_DATABASE_URL`.
Without it the app falls back to SQLite, which needs a writable directory;
serverless has none, so it raises a startup error saying exactly that. Check
the function logs.

**No one can log in** — the `users` table is empty. Confirm
`DGP_BOOTSTRAP_ADMIN_USERNAME` / `_PASSWORD` are set for the environment you
deployed to, then redeploy. If users already exist the bootstrap is skipped by
design.

**Blank page, login works** — `_dashboard_template.html` did not make it into
the bundle. Confirm it is not in `.vercelignore`.

**Admin panel shows "disabled" hints** — that is `DGP_IMPORT_ENABLED=0` and
`DGP_LOCAL_PROCESSING_ENABLED=0` working as intended on serverless. Publish
with `publish_site.py`.

**Cold start is slow** — every start runs `create_all` against the pooler.
New Vercel projects default to `iad1` (Washington, D.C.) while this Supabase
pooler is in Tokyo, so pin the function to the region next to the database:

```json
"functions": { "api/index.py": { "regions": ["nrt1"] } }
```

`nrt1` is Tokyo and matches an `ap-northeast-1` pooler. Use whichever region
matches your own connection string. The same value can be set under
Project → Settings → Functions → Function Regions instead of editing the file.
Hobby plans allow a single region, so this is safe on any plan.

**"memory" in `vercel.json` has no effect** — with Fluid compute enabled (the
default for new projects), memory cannot be set in `vercel.json`. Set it in
Project → Settings → Functions instead. `maxDuration` still works.

---

## 5. Secret hygiene

- `liveapp/.env` and `liveapp/seed_users.json` are gitignored. Both hold
  plaintext credentials. If either has been pasted into a chat, an email or a
  shared document, rotate it: a new database password in Supabase, and a new
  `DGP_JWT_SECRET` in Vercel (which invalidates every issued token).
- `DGP_JWT_SECRET` must be at least 32 characters. The app logs a loud warning
  at startup if it is left at the default.
- Never commit `.env`. `.gitignore` and `.vercelignore` both exclude it; the
  committed `liveapp/.env.example` is a template with empty values.