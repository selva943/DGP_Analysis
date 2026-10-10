# Live DGP Dashboard

This is the live, database-backed version of the Easy SBF Deep-Dive Dashboard. It
serves the same UI, but reads fresh data from an API and adds role-based
authentication and an admin panel to process the source pipeline.

## Features

- **1 admin + 7 viewers** — role-based access enforced on every API route.
- **Live data** — the browser pulls from `/api/core`, `/api/meta`, `/api/tree`,
  `/api/dates`, `/api/opening-index` and `/api/dse/{dse}/customers`.
- **Report upload** — the admin imports a built `output/site` bundle (zip, or
  straight from the server folder) and it becomes the live dataset.
- **Supabase / PostgreSQL** — point `DGP_DATABASE_URL` at your Supabase
  Postgres instance and the app creates and refreshes the tables itself.
- **Admin "Process data"** — run the full file-based pipeline (Rename, find_easy_sbf,
  achieved_sbf, opening_easy_sbf), ingest into the database, rebuild the
  payload and publish a new active dataset version.
- **Zero-touch seed** — `app/seed.py` creates the eight demo users at first startup.
- **Drop-in UI** — the frontend reuses the template's rendering logic and only
  swaps the encrypted bundle loader for API calls.

## Project layout

```
liveapp/
  ├── app/                # FastAPI app, DB, auth, services, routers
  ├── frontend/           # SPA (index.html + app.js)
  ├── storage/            # SQLite DB (dgp.db) created on first run
  ├── seed_users.json     # Default credentials (change in production)
  ├── requirements.txt
  ├── main.py             # uvicorn entrypoint
  └── run_process.py       # standalone ETL runner (spawned by admin)
```

## Prerequisites

- Python 3.10+
- The original DGP project files (Input/, updating_files/, history/, Output/)
- A Supabase project (free tier is fine) — see below

## Supabase setup

The app stores all data in PostgreSQL, so a Supabase Postgres database works
directly. No schema import is needed: the tables are created automatically on
first startup.

1. In Supabase, open **Project settings → Database → Connection string → URI**.
2. Copy `liveapp/.env.example` to `liveapp/.env`.
3. Paste the connection string into `DGP_DATABASE_URL`, for example:

   ```
   DGP_DATABASE_URL=postgresql://postgres.YOUR_REF:YOUR_PASSWORD@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
   ```

   - Use the **Session pooler** (port `5432`) — the ETL keeps a connection open
     for a long time while it runs, and the transaction pooler (`6543`) can
     recycle connections mid-job.
   - URL-encode special characters in the password (`@` → `%40`).
4. Set `DGP_JWT_SECRET` to a long random string.
5. Start the app — tables are created and the 8 users are seeded on boot.

Each "Process data" run **deletes the existing rows** in the fact tables and
re-inserts the freshly computed data, so Supabase always holds exactly the
latest report. The previous dashboard snapshot stays queryable through the
version id, and viewers keep seeing the previous version until the new one is
published (no partial data is ever shown).

## Setup

1. Create a virtual environment (optional but recommended).
2. Install dependencies:

   ```bash
   pip install -r liveapp/requirements.txt
   ```

3. Review or override configuration via environment variables (prefix `DGP_`),
   or through `liveapp/.env`:
   - `DGP_DATABASE_URL` — Supabase/PostgreSQL URL; leave blank for SQLite (`storage/dgp.db`)
   - `DGP_JWT_SECRET` — set to a strong secret in production
   - `DGP_SKIP_SOURCE_SCRIPTS` — set to `1` if you only want to rebuild from
     existing files
   - `DGP_ETL_STEP_TIMEOUT` — per-script timeout in seconds (default 3600)

## Daily / monthly workflow

The ETL scripts run outside the app. See **Daily workflow** above — the admin
imports the built `output/site` bundle.

## Default credentials

There are no built-in credentials. Users are created in one of two ways, and
both are no-ops once the database already has users:

- **First admin on a serverless deployment** — set these Vercel environment
  variables. The admin is created only when the `users` table is empty:

  | Variable | Value |
  |---|---|
  | `DGP_BOOTSTRAP_ADMIN_USERNAME` | the admin's username |
  | `DGP_BOOTSTRAP_ADMIN_PASSWORD` | the admin's password |
  | `DGP_BOOTSTRAP_ADMIN_DISPLAY_NAME` | optional label shown in the UI |

  Nothing is written to git, and later changing the password variable does not
  affect a database that already has users. Set a real password here, not a
  placeholder.

- **On the build machine** — `liveapp/seed_users.json` lists the initial users.
  It is gitignored because it holds plaintext passwords, so it exists only
  locally. Edit it before running the first local boot, and keep it out of any
  backup that leaves the machine.

Change passwords through the database or a fresh seed file; never commit one.

## Running

From `liveapp/`:

```bash
python main.py
# or: uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. Log in with an admin account, and if no dataset
exists, run the ETL once and publish it from the machine holding the files:

```bash
cd liveapp && python main.py          # terminal 1
python publish_site.py                # terminal 2, from the project root
```

`publish_site.py` writes straight into the configured database, so the running
app picks the new version up without a restart.

## Daily workflow (the only one you need)

1. Build the dashboard as usual — the ETL scripts produce `output/site/`
   (`index.html` + `dse-data/` with `manifest.json`, `core.bin`, `c/*.bin`).
2. Sign in as `admin` → **Admin** tab → **Import dashboard data**.
   - **Import zip** — zip the `output/site` folder and pick the file.
   - **Import from server folder** — one click if the app runs on the same
     machine as the ETL scripts; reads `output/site` directly.
3. The bundle is decrypted (AES-256-GCM + PBKDF2) and stored in the database as
   the active dataset version. It does **not** run the ETL scripts and does
   **not** touch `Input/`.
4. Viewers open the app and see the new data.

## Bundle format

`build_dashboard.py` writes raw-deflate compressed JSON — **no password, no
encryption**. Access is controlled by this app's login: an authenticated user
loads the core bundle, and each DSE's customer chunk is fetched on demand the
first time that DSE is viewed.

Older bundles (AES-256-GCM + PBKDF2, manifest `v: 2`) still import: the
importer detects them from the manifest and falls back to the old password
scheme. New bundles are manifest `v: 3`, `encrypted: false`.

**Never host `output/site` on a public URL.** The data is served through the
API, which verifies the session on every request.

## Admin panel

- **Import dashboard data** — zip upload or read `output/site` from disk.
- **Recent jobs** — last 10 runs; click a row to expand its log.
- **Run ETL scripts (optional)** — re-runs `Rename.py`, `find_easy_sbf.py`,
  `achieved_sbf.py`, `find_opening_easy_sbf.py`, ingests them into the database
  and publishes a payload. Use this when you want the data rebuilt from source
  files instead of importing a built site.
- **Dataset status** — confirms whether an active version is live.

A job that was interrupted is marked `failed` automatically once it exceeds
`DGP_ETL_STEP_TIMEOUT`, so a killed run can never block future imports.

## Notes on the pipeline

- The live app **reuses** the original batch scripts (`Rename.py`,
  `find_easy_sbf.py`, `achieved_sbf.py`, `find_opening_easy_sbf.py`) to stay
  identical to the proven file-based workflow.
- `build_dashboard.build()` is monkey-patched to read from the DB via
  `app.services.builder`, so the tree/brand rollups are identical to
  `build_dashboard.py`.
- The static site's encrypted `.bin` loader has been replaced with JWT + API
  calls; all rendering code and `DATA` shapes remain the same.
- When `DGP_DATABASE_URL` points to PostgreSQL, JSON columns are used for
  `easy_sbf`, `achieved_sbf`, etc. (the ORM maps `JSON` to the DB's JSON type).

## Troubleshooting

- **"No dataset has been published yet"** — login as admin and process data.
- **"Processing job is already running"** — wait for the previous run to finish
  or check the jobs list.
- **Windows** — Python subprocess uses `sys.executable` and runs the scripts in
  the original project directory.
- **Large datasets** — the ETL is CPU/IO heavy; the runner logs progress so you
  can tail it in the admin panel or console.
