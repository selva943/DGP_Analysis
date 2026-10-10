# DGP Dashboard — Live Database-Connected Project Guide

This document explains how the current file-based pipeline works and how to turn
the dashboard into a **live, database-backed application** where the browser
reads fresh data from a database through an API instead of downloading a
pre-built, encrypted static bundle.

It is written to be actionable: schema DDL, ETL jobs, API endpoints, frontend
swap-in points, auth, and a phased migration checklist.

---

## 1. Current architecture (analysis)

The project is a **batch ETL + static site** pipeline. There is no server and
no database. Every run reads spreadsheets/CSVs from disk, computes results, and
writes Excel outputs. The dashboard is a single HTML file plus pre-encrypted
`*.bin` chunks served as static files.

### 1.1 Scripts

| Script | Role | Inputs | Outputs |
|---|---|---|---|
| `Rename.py` | Normalize `Input/` filenames (`SBD_report*.CSV`) | `Input/*` | renamed files |
| `find_easy_sbf.py` | PLAN: gap per DSE+Retailer+VisitDate | Calls Plan, Name Master, Easy SBF List, SBD reports | `Output/Easy_SBF_Gap_<YYYYMMDD>.xlsx` + `History/` copy |
| `achieved_sbf.py` | ACHIEVED: compare plan vs current SBD | `History/Easy_SBF_Gap_*.xlsx`, SBD reports, masters | `Output|History/Achieved_SBF_<ts>.xlsx` |
| `find_opening_easy_sbf.py` | Monthly opening baseline | SBD reports, Name Master, Easy SBF List | `Output|History/Opening_Easy_SBF_<YYYYMM>.xlsx` |
| `build_dashboard.py` | Build static encrypted site | `updating_files/*`, `History/*`, `Input/AR.csv`, parquet snapshots, `_dashboard_template.html` | `Output/site/` (index.html + `dse-data/`) |
| `monthly_reset.py` | Archive previous months, reset site | `Output/`, `History/` | `History/archive/<YYYY>/<MM>/` |

### 1.2 Data flow

```
Input/SBD_report*.CSV ─┐
updating_files/*.xlsx ─┼─► find_easy_sbf.py ─► History/Easy_SBF_Gap_*.xlsx ─┐
Input/AR.csv ──────────┘                                                      │
                                                                              ▼
History/Achieved_SBF_*.xlsx ──────────────────────────────────────────► build_dashboard.py
History/Opening_Easy_SBF_*.xlsx ──────────────────────────────────────►        │
History/2026/*/analytics.parquet ─────────────────────────────────────►        │
                                                                              ▼
                                                   Output/site/index.html + dse-data/*.bin
                                                              (AES-GCM + PBKDF2, static)
                                                              deployed to Netlify
```

### 1.3 Business keys and rules (must be preserved in the DB)

- **Unique key:** `dse_code + retailer_code + visit_date`. `retailer_code` is
  **never** unique — a retailer under two DSEs is two records.
- **WS channel exclusion:** any SBD row whose `channel_description` contains
  `WS` is dropped.
- **Valid DSEs only:** DSEs must exist in the Name Master.
- **SBD joins happen at retailer level first**, then attach to the DSE+Retailer
  mapping to avoid many-to-many inflation.
- **Easy SBF matching** is done against `Easy_SBF_List.xlsx` after normalizing
  reason text (strip `(n)`, collapse spaces, uppercase).

### 1.4 Source columns

| Source | Columns used |
|---|---|
| `updating_files/Name_master.xlsx` | `DSE_Code`, `DSE_Name`, `ZSM` (cluster), `ASM`, `Branch`, `BU` (channel) |
| `updating_files/Calls_plan.xlsx` | `UserCode` (dse), `RetailerCode`, `Vist Date`, `RetailerName` |
| `updating_files/Easy_SBF_List.xlsx` | `easy_sbf` |
| `updating_files/Retailer Listing Report.csv` | `retailer_code`, `payment_mode` |
| `Input/SBD_report*.CSV` | `RetailerCode`, `DseCode`/`UserCode`, `ChannelDescription`, `NoSale` |
| `Input/AR.csv` | `dse_code`, `retailer_code`, `aging_days`, `ar_status`, `balance_amount`, `net_amount` |
| `History/Easy_SBF_Gap_*.xlsx` | `dse_code`, `retailer_code`, `visit_date`, `total_gap`, `easy_sbf_gap`, `easy_sbf` |
| `History/Achieved_SBF_*.xlsx` | `dse_code`, `retailer_code`, `visit_date`, `historical_easy_sbf_gap`, `achieved_sbf_gap`, `achieved_sbf`, `still_open_easy_sbf_gap`, `still_open_easy_sbf`, `new_sbf_gap`, `new_sbf`, `comparison_status` |
| `History/Opening_Easy_SBF_*.xlsx` | `dse_code`, `retailer_code`, `month`, `total_gap`, `easy_sbf_gap`, `easy_sbf` |
| `History/2026/*/analytics.parquet` | `business_date`, `user_code`, `retailer_code`, `retailer_name`, `gap_count`, `easy_count`, `hard_count`, `with_sale_count`, `easy_sold_count`, `planned_today`, `suggested_today` |

### 1.5 What the dashboard consumes (payload built in `build_dashboard.py`)

```jsonc
{
  "meta":    { "generated", "plan_dates", "achieved_dates", "snapshot_days",
               "n_customers", "n_dse", "all_brands", "channels",
               "opening_month", "opening_customers", "opening_only_dses" },
  "tree":    { "name":"All", "metrics":{...}, "brands":{...}, "kids":[ ... ] },
  "dse_customers": { "SEALR_MS001": ["SEALR_67506", ...] },
  "opening": { "SEALR_MS001": { "Guard Razor Rs28": 4, ... } },
  "customers": { "SEALR_MS001|SEALR_67506": { ...customer record... } },
  "dates":   { "plan":[...], "achieved":[...], "snap":[...] },
  "levels":  ["cluster","asm","branch","dse"]
}
```

`metrics` per tree node:

```jsonc
{ "dse":0, "cust":0, "planGap":0, "planEasy":0, "ach":0, "open":0,
  "new":0, "hist":0, "arBal":0,
  "byDate": { "2026-09-01": {"planGap":0,"planEasy":0,"ach":0,"open":0,"new":0,"hist":0} } }
```

Customer record:

```jsonc
{ "dse","dse_name","ret","name","channel","plan_date_count",
  "plan": {"<date>": {"total","easy","sbf":[...]}},
  "ach":  {"<date>": {"historical","achieved","open","new",
                       "achieved_sbf":[],"open_sbf":[],"new_sbf":[],"status"}},
  "total_easy","total_gap","achieved","open","new","hist",
  "ar": { "balance","net","aging","docs","status":[{"s","bal"}] } | null,
  "latest_sbf":[],"latest_easy","latest_date",
  "opening":[],"opening_gap","payment_mode",
  "snap": [ {"date","with_sale","easy_sold","gap","easy","planned","suggested"} ] }
```

### 1.6 Client-side data loading (`_dashboard_template.html`)

The template currently:
1. `fetchManifest()` → `dse-data/manifest.json` (KDF params + per-file IVs).
2. `deriveAesKey(pwd)` → PBKDF2-SHA256 → AES-GCM key.
3. `decryptFile("core")` → decrypt + `DecompressionStream("deflate-raw")` → `DATA`.
4. `loadDseChunk(dse)` → lazy-fetch and decrypt `dse-data/c/<dse>.bin`, then
   `Object.assign(DATA.customers, recs)`.
5. `init()` → builds nav/filters from `DATA`.

**The live version replaces steps 1–4 with API calls; `DATA` keeps the same
shape so the rendering code (nav, tree, KPIs, charts, drill-down) is unchanged.**

### 1.7 Why it is not "live"

- Data is snapshotted at `build_dashboard.py` runtime; the site never changes
  until someone re-runs the pipeline and redeploys.
- No queries — the entire tree/brand rollup is precomputed server-side.
- No incremental loading of new SBD/plan data without a full rebuild.
- No shared concurrency, no per-user auth — one static password for everyone.
- Excel/parquet files are the source of truth, not a transactional store.

---

## 2. Target architecture

```
CSV / XLSX feeds ──►  ETL / ingestion jobs  ──►  DATABASE (PostgreSQL)
                       (scheduled)                    │
                                                      ▼
                              REST / GraphQL API (FastAPI)  ──►  Browser dashboard
                                - auth (JWT)                     (same UI, live fetch)
                                - tree rollup queries
                                - per-DSE customer pagination
```

- **Source of truth:** relational tables in PostgreSQL (SQL Server/MySQL also
  fine; DDL below is PostgreSQL-flavored).
- **Ingestion:** keep the existing Python parsing logic, but write rows to the
  DB (upsert) instead of (or in addition to) Excel.
- **Serving:** a small API exposes exactly the JSON shapes the template already
  understands, so the frontend change is small.
- **Auth:** server-side login issuing a JWT; the static password scheme is
  retired.

---

## 3. Database schema

```sql
-- ---------- Reference / master data ----------
CREATE TABLE name_master (
    dse_code     text PRIMARY KEY,
    dse_name     text,
    cluster      text,              -- ZSM
    asm          text,
    branch       text,
    channel      text               -- BU
);

CREATE TABLE retailer (
    retailer_code text PRIMARY KEY,
    retailer_name text,
    payment_mode  text
);

CREATE TABLE easy_sbf_master (
    reason_key    text PRIMARY KEY,  -- normalized (upper, no "(n)")
    reason_text   text NOT NULL
);

-- ---------- Ingestion staging (raw feed mirror) ----------
CREATE TABLE sbd_report (
    id           bigserial PRIMARY KEY,
    report_file  text NOT NULL,
    dse_code     text,
    retailer_code text,
    channel_description text,
    no_sale      text,
    loaded_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE calls_plan (
    user_code     text,
    retailer_code text,
    visit_date    date,
    PRIMARY KEY (user_code, retailer_code, visit_date)
);

-- ---------- Derived fact tables (what the dashboard reads) ----------
CREATE TABLE easy_sbf_gap (          -- PLAN
    dse_code     text NOT NULL,
    retailer_code text NOT NULL,
    visit_date   date NOT NULL,
    total_gap    integer NOT NULL DEFAULT 0,
    easy_sbf_gap integer NOT NULL DEFAULT 0,
    easy_sbf     text[]  NOT NULL DEFAULT '{}',   -- matched reason texts
    run_id       bigint,
    PRIMARY KEY (dse_code, retailer_code, visit_date)
);

CREATE TABLE achieved_sbf (          -- ACHIEVED
    dse_code     text NOT NULL,
    retailer_code text NOT NULL,
    visit_date   date NOT NULL,
    historical_easy_sbf_gap integer NOT NULL DEFAULT 0,
    achieved_sbf_gap        integer NOT NULL DEFAULT 0,
    achieved_sbf        text[] NOT NULL DEFAULT '{}',
    still_open_easy_sbf_gap integer NOT NULL DEFAULT 0,
    still_open_easy_sbf     text[] NOT NULL DEFAULT '{}',
    new_sbf_gap             integer NOT NULL DEFAULT 0,
    new_sbf             text[] NOT NULL DEFAULT '{}',
    comparison_status   text,
    run_id       bigint,
    PRIMARY KEY (dse_code, retailer_code, visit_date)
);

CREATE TABLE opening_easy_sbf (      -- monthly locked baseline
    dse_code     text NOT NULL,
    retailer_code text NOT NULL,
    month        text NOT NULL,      -- 'MM-YYYY'
    total_gap    integer NOT NULL DEFAULT 0,
    easy_sbf_gap integer NOT NULL DEFAULT 0,
    easy_sbf     text[]  NOT NULL DEFAULT '{}',
    PRIMARY KEY (dse_code, retailer_code, month)
);

CREATE TABLE customer_snapshot (     -- from analytics.parquet
    business_date date NOT NULL,
    dse_code      text NOT NULL,
    retailer_code text NOT NULL,
    retailer_name text,
    gap_count        integer,
    easy_count       integer,
    hard_count       integer,
    with_sale_count  integer,
    easy_sold_count  integer,
    planned_today    boolean,
    suggested_today  boolean,
    PRIMARY KEY (business_date, dse_code, retailer_code)
);

CREATE TABLE aged_receivable (
    dse_code      text NOT NULL,
    retailer_code text NOT NULL,
    aging_days    integer,
    ar_status     text,
    balance_amount numeric(14,2),
    net_amount     numeric(14,2)
);

-- Run metadata (mirrors each workbook's Run_Info sheet)
CREATE TABLE etl_run (
    run_id      bigserial PRIMARY KEY,
    job         text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    target_date date,
    status      text
);

CREATE INDEX ON sbd_report (retailer_code);
CREATE INDEX ON easy_sbf_gap (visit_date);
CREATE INDEX ON achieved_sbf (visit_date);
CREATE INDEX ON customer_snapshot (dse_code, retailer_code);
```

`text[]` can be replaced by a `jsonb` column or a child table
`easy_sbf_gap_item(dse_code, retailer_code, visit_date, reason_text)` if you
prefer normalized SBF items. Arrays keep the template's shape simplest.

---

## 4. Ingestion / ETL design

Reuse the parsing functions already in the scripts (normalization, WS filter,
SBF matching). Replace the Excel writers with DB upserts.

### 4.1 Jobs

| Job | Replaces | Trigger | Upsert target |
|---|---|---|---|
| `load_reference` | masters | on file change / daily | `name_master`, `retailer`, `easy_sbf_master` |
| `load_sbd` | SBD read | after new `SBD_report*.CSV` | `sbd_report` (staging) |
| `build_plan` | `find_easy_sbf.py` | daily (auto target date) | `easy_sbf_gap` |
| `build_achieved` | `achieved_sbf.py` | daily (today-2) | `achieved_sbf` |
| `build_opening` | `find_opening_easy_sbf.py` | monthly | `opening_easy_sbf` |
| `load_ar` | `AR.csv` | daily | `aged_receivable` |
| `load_snapshots` | parquet | daily | `customer_snapshot` |

### 4.2 Key upsert patterns

```sql
-- PLAN upsert; a re-run for the same date refreshes in place (no _1/_2 files)
INSERT INTO easy_sbf_gap
    (dse_code, retailer_code, visit_date, total_gap, easy_sbf_gap, easy_sbf, run_id)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (dse_code, retailer_code, visit_date) DO UPDATE SET
    total_gap = EXCLUDED.total_gap,
    easy_sbf_gap = EXCLUDED.easy_sbf_gap,
    easy_sbf = EXCLUDED.easy_sbf,
    run_id = EXCLUDED.run_id;
```

The whole pipeline reduces to: load staging → `INSERT ... SELECT` transforms →
upsert facts. The WS filter and normalization move into SQL or stay in Python.

### 4.3 SQL for the two hard business rules

```sql
-- Layer 2: aggregate No Sale to retailer level, excluding WS channels
WITH retailer_nosale AS (
    SELECT retailer_code, string_agg(no_sale, '|') AS joined
    FROM sbd_report
    WHERE channel_description !~* 'WS'
      AND coalesce(no_sale,'') <> ''
    GROUP BY retailer_code
)
-- Layer 3: attach to each planned DSE+Retailer relationship (no fan-out)
SELECT cp.user_code AS dse_code, cp.retailer_code, cp.visit_date, rn.joined
FROM calls_plan cp
JOIN name_master nm ON nm.dse_code = cp.user_code
JOIN retailer_nosale rn ON rn.retailer_code = cp.retailer_code;
```

---

## 5. Backend API

Implement with FastAPI (Python, reuses pandas logic) or Fastify/Express. The
API must return **exactly the `DATA` shape** so the template can stay almost
unchanged.

### 5.1 Endpoints

| Method | Path | Returns | Replaces |
|---|---|---|---|
| `POST` | `/api/login` | `{ token }` (JWT) | password unlock / PBKDF2 |
| `GET` | `/api/meta` | `DATA.meta` | `core.bin.meta` |
| `GET` | `/api/tree` | `DATA.tree` (rollups) | `core.bin.tree` |
| `GET` | `/api/dates` | `DATA.dates` | `core.bin.dates` |
| `GET` | `/api/opening-index` | `DATA.opening` | `core.bin.opening` |
| `GET` | `/api/dse/{dse}/customers` | customer records map | `c/<dse>.bin` |
| `GET` | `/api/customers/{dse}/{ret}` | single customer record | record lookup |
| `GET` | `/api/brands?path=...` | node brand summary | `tree.brands` |
| `GET` | `/api/health` | `{ ok: true }` | — |

### 5.2 Tree rollup example (per level)

Because `metrics` are additive, the server can compute rollups on the fly with
a recursive query or (better) materialize them:

```sql
CREATE MATERIALIZED VIEW node_metrics AS
SELECT nm.cluster, nm.asm, nm.branch, nm.dse_code,
       count(DISTINCT g.retailer_code) FILTER (WHERE g.visit_date IS NOT NULL) AS cust,
       sum(g.total_gap)    AS plan_gap,
       sum(g.easy_sbf_gap) AS plan_easy,
       sum(a.achieved_sbf_gap)        AS ach,
       sum(a.still_open_easy_sbf_gap) AS open,
       sum(a.new_sbf_gap)             AS new,
       sum(a.historical_easy_sbf_gap) AS hist
FROM name_master nm
LEFT JOIN easy_sbf_gap g ON g.dse_code = nm.dse_code
LEFT JOIN achieved_sbf a ON a.dse_code = nm.dse_code
                        AND a.retailer_code = g.retailer_code
                        AND a.visit_date = g.visit_date
GROUP BY nm.cluster, nm.asm, nm.branch, nm.dse_code;
```

A `byDate` breakdown adds a grouped query on the fact tables keyed by
`(cluster, asm, branch, dse, visit_date)`. Refresh the matview on each ETL run.

### 5.3 FastAPI skeleton

```python
# app.py
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from auth import issue_token, require_user
import db

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["https://dash.example.com"],
                   allow_methods=["*"], allow_headers=["*"])

@app.post("/api/login")
def login(body: dict):
    if not db.check_user(body.get("username"), body.get("password")):
        raise HTTPException(401, "Invalid credentials")
    return {"token": issue_token(body["username"])}

@app.get("/api/tree")
def tree(user = Depends(require_user)):
    return db.build_tree()          # returns the {name,metrics,brands,kids} tree

@app.get("/api/dse/{dse}/customers")
def dse_customers(dse: str, user = Depends(require_user)):
    return db.dse_customer_map(dse) # {"DSE|RET": {...record...}}
```

---

## 6. Frontend changes

Only the data-loading section of `_dashboard_template.html` changes. Keep the
rest of the rendering code intact.

### 6.1 Remove / replace

- **Remove:** `fetchManifest`, `deriveAesKey`, `decryptFile`,
  `DecompressionStream` usage, `DATA_BASE = "dse-data/"`, `AES_KEY`,
  `AAD_PREFIX`.
- **Replace the lock screen:** password box now posts to `/api/login` and
  stores a JWT in memory (or `sessionStorage`).

### 6.2 New loader

```js
const API = "/api";
let TOKEN = null;

async function api(path, opts = {}) {
  const r = await fetch(API + path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      ...(TOKEN ? { Authorization: "Bearer " + TOKEN } : {}),
      ...(opts.headers || {}),
    },
    cache: "no-store",
  });
  if (!r.ok) throw new Error(path + " HTTP " + r.status);
  return r.json();
}

async function login(username, password) {
  const { token } = await api("/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  TOKEN = token;
}

async function loadCore() {
  const [meta, tree, dates, opening] = await Promise.all([
    api("/meta"), api("/tree"), api("/dates"), api("/opening-index"),
  ]);
  // dse_customers comes from the tree (or a dedicated endpoint)
  DATA = { meta, tree, dates, opening, dse_customers: tree.dse_customers || {}, customers: {} };
}

function loadDseChunk(dse) {
  if (!custChunks[dse]) {
    custChunks[dse] = api("/dse/" + encodeURIComponent(dse) + "/customers")
      .then(recs => Object.assign(DATA.customers, recs))
      .catch(err => { delete custChunks[dse]; throw err; });
  }
  return custChunks[dse];
}

async function unlock() {
  await login(ev("user").value, ev("pwd").value);
  await loadCore();
  ev("lockScreen").classList.add("off");
  init();
}
```

`init()`, `render()`, `dseNeeded()`, and all render functions stay as-is — they
already consume `DATA.customers`, `DATA.tree`, `DATA.dates`, etc.

### 6.3 Optional optimization

Add `GET /api/dse/{dse}/customers?since=<date>` or pagination if customer
chunks grow large; the current per-DSE chunk pattern is already lazy, so a
1:1 replacement is enough to become live.

---

## 7. Security model (replace the static password)

| Concern | Current | Live target |
|---|---|---|
| Auth | single shared `SME#DDYYYYMM` password | per-user login, hashed (bcrypt/argon2) |
| Transport | static files on Netlify | HTTPS only |
| Authorization | none | role-based (cluster/region filters) |
| Data at rest | AES-GCM blobs | DB encryption + row-level security |
| Tokens | none | short-lived JWT + refresh, HTTP-only cookies if same-origin |

- Never ship data blobs to unauthenticated clients; the API enforces the token
  on every route.
- Apply row-level security so a manager only sees their cluster.
- Keep the `dse_code` whitelist check (Name Master) as a foreign key, not just a
  runtime filter.

---

## 8. Deployment

| Piece | Suggested |
|---|---|
| Database | PostgreSQL (managed: RDS / Cloud SQL / Supabase) |
| API | FastAPI behind Uvicorn/Gunicorn, containerized |
| Scheduler | cron / APScheduler / GitHub Actions / Airflow for ETL |
| Frontend | same HTML (or React) hosted on Netlify/Vercel/nginx, calling the API |
| Secrets | env vars / secret manager (never in repo) |
| Observability | log `etl_run`, API request logs, health checks |

Environment variables for the API:

```
DATABASE_URL=postgresql://user:pass@host:5432/dgp
JWT_SECRET=...
JWT_TTL_MINUTES=60
CORS_ORIGINS=https://dash.example.com
```

---

## 9. Migration checklist (phased)

**Phase 0 — Inspect**
- [ ] Confirm all source columns with stakeholders (some files are large; sample rows).
- [ ] Freeze current Excel outputs as the regression baseline.

**Phase 1 — Database**
- [ ] Provision PostgreSQL.
- [ ] Apply the schema in §3.
- [ ] Write `load_reference` and load the three masters.

**Phase 2 — Backfill**
- [ ] Load historical `Easy_SBF_Gap_*.xlsx`, `Achieved_SBF_*.xlsx`,
      `Opening_Easy_SBF_*.xlsx`, `AR.csv`, and parquet snapshots.
- [ ] Verify row counts and totals match `PROCESS_SUMMARY.md`
      (e.g. plan rows, `total_gap`, `easy_sbf_gap`).

**Phase 3 — Live ETL**
- [ ] Port `find_easy_sbf.py`, `achieved_sbf.py`, `find_opening_easy_sbf.py`
      to upsert into the DB (keep the parsing/normalization logic).
- [ ] Schedule the jobs; write `etl_run` rows.

**Phase 4 — API**
- [ ] Implement the endpoints in §5 returning the exact `DATA` shapes.
- [ ] Add JWT auth and CORS.
- [ ] Add a golden test: API `/api/tree` equals the baseline `core.bin` tree.

**Phase 5 — Frontend**
- [ ] Replace the loader in `_dashboard_template.html` per §6.
- [ ] Point the lock screen at `/api/login`.
- [ ] Delete `dse-data/` and the encryption code from `build_dashboard.py`.

**Phase 6 — Cutover**
- [ ] Deploy API + DB, run ETL once, smoke-test the dashboard.
- [ ] Keep `build_dashboard.py` as an offline export fallback for a few weeks.
- [ ] Retire `monthly_reset.py` archiving in favor of DB backups.

---

## 10. Mapping: script → live component

| Current | Live replacement |
|---|---|
| `Rename.py` | ingestion normalization inside `load_sbd` |
| `find_easy_sbf.py` → `Easy_SBF_Gap_*.xlsx` | `build_plan` → `easy_sbf_gap` table |
| `achieved_sbf.py` → `Achieved_SBF_*.xlsx` | `build_achieved` → `achieved_sbf` table |
| `find_opening_easy_sbf.py` → `Opening_*.xlsx` | `build_opening` → `opening_easy_sbf` table |
| `Input/AR.csv` | `load_ar` → `aged_receivable` |
| `analytics.parquet` | `load_snapshots` → `customer_snapshot` |
| `build_dashboard.py` (precompute + encrypt) | API rollup queries + JWT |
| `dse-data/manifest.json` + `*.bin` | REST endpoints `/api/*` |
| `monthly_reset.py` | DB partitions + scheduled backups |
| static `SME#DDYYYYMM` password | per-user login + JWT |

---

## 11. Quick reference — keep these invariants

1. `dse_code + retailer_code + visit_date` is the composite key everywhere.
2. WS rows excluded; Name Master DSEs only.
3. SBD aggregated to retailer level before joining DSE+Retailer.
4. Easy SBF text normalized identically in ETL and matching.
5. `tree.metrics` are additive and include `byDate` breakdown.
6. Opening snapshot is the locked "Total gap" baseline for the brand table.
7. The API response shape must match the template's `DATA` object exactly.
