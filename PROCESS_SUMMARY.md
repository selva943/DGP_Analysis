# Easy SBF Identification — Process Summary

## What this pipeline does

`find_easy_sbf.py` finds the **Easy SBF (Sales Blocking Factors)** gap for the
next scheduled visit by joining the Calls Plan with the SBD reports, then
scoring each retailer's No Sale reasons against the Easy SBF master list.

## Critical business rule — DSE + Retailer matching

A retailer can be assigned to multiple DSEs, so **RetailerCode is NEVER a
unique identifier**. The unique business key is:

```
DSE_CODE + RETAILER_CODE + VISIT_DATE
```

- `SM12271|SEMDU_49061` and `SM12272|SEMDU_49061` are **two different records**.
- The pipeline NEVER groups only by `retailer_code` for the final result and
  never uses `drop_duplicates(subset=["retailer_code"])`.
- SBD reports contain the retailer but may not contain the DSE, so the valid
  DSE + Retailer relationships come from the Calls Plan (after Name Master
  validation), and SBD data is attached at **retailer level only**.

## Inputs

| File | Folder | Purpose |
|------|--------|---------|
| `SBD_report*.CSV` (multiple, merged) | `Input/` | No Sale reasons per retailer |
| `Calls_plan.xlsx` | `updating_files/` | Who visits which retailer on which date |
| `Easy_SBF_List.xlsx` | `updating_files/` | Master list of Easy SBF reasons |
| `Name_master.xlsx` | `updating_files/` | Only valid DSEs appear in output |

## Pipeline stages

1. **Read & merge** — every `SBD_report*.CSV` is read and merged into one frame.
2. **Column normalization** — columns auto-mapped (e.g. `Vist Date` → `visit_date`,
   `UserCode` → `dse_code`, `RetailerCode` → `retailer_code`); no exact spelling required.
3. **Code normalization** — trim, strip tabs/hidden chars, collapse duplicate
   spaces, remove `.0` float artifacts, uppercase (`sm12271` → `SM12271`).
4. **Target Visit Date** — `MIN(Visit_date WHERE Visit_date > today)`.
   Today's visits are ignored. No future date → script stops gracefully, no output.
5. **Filter target date** — keep only the target date's records from Calls Plan.
6. **Filter valid DSEs** — keep DSEs present in the Name Master only.
7. **Remove exact duplicates** — only identical `DSE + Retailer + Visit Date`
   rows collapse; a retailer under two DSEs stays as two rows.
8. **Exclude WS channel** — drop SBD rows whose channel contains `WS`
   (`Semi WS Traditional`, `WS Non-Feeder ...`, `WS Feeder ...`); only
   non-WS channels (Small / Large / Convenience / Pharmacy / etc.) remain.
9. **Match retailers (layered)** — aggregate SBD No Sale reasons to
   `retailer_code` level FIRST, then merge that retailer-level summary into the
   DSE + Retailer mapping. No many-to-many multiplication.
10. **Process No Sale** — split on `|`, strip count markers like `(1)`, `(12)`.
11. **Count & match** — `total_gap` = number of reasons,
    `easy_sbf_gap` = number matching the Easy SBF master,
    `easy_sbf` = deduped, comma-separated matched reason text.

## Three-layer data model

| Layer | Scope | Content |
|-------|-------|---------|
| Layer 1 — Visit Mapping | Calls Plan | `dse_code`, `retailer_code`, `visit_date` |
| Layer 2 — Retailer SBF | SBD (retailer level) | `total_gap`, `easy_sbf_gap`, `easy_sbf` |
| Layer 3 — Final Output | DSE + Retailer + Visit Date | merge of Layer 1 and Layer 2 |

The final result always preserves every DSE + Retailer relationship from the
Calls Plan.

## How many rows the pipeline produces

The output is the **intersection of the following filters**:

| Filter stage | Records |
|---|---|
| Calls Plan (full) | **201,078** |
| Planned for target date `08-08-2026` | **7,548** |
| … with DSE present in Name Master | **7,548** |
| … after exact-duplicate removal | **7,548** |
| SBD reports merged (all channels) | **87,340** |
| SBD rows in non-WS channels (KEPT) | **83,389** |
| SBD rows with WS/Wholesale channel (EXCLUDED) | **3,951** |
| DSE + Retailer combos with matching non-WS SBD | **7,016** ← output rows |

Rows are dropped because:

- **Only one target date** is used — the rest of the month's visits are excluded.
- **Inner join** — DSE + Retailer combos planned on `08-08` but with **no
  non-WS SBD report row yet** are dropped (SBD reports are month-to-date and
  may not yet cover every retailer).
- **WS channel excluded** — WS/Wholesale channel rows are deliberately removed.
- **DSEs not in the Name Master** are dropped.

If more SBD reports (more branches/stores) are dropped into `Input/` and renamed,
the output count grows.

## Output

`Output/Easy_SBF_Gap_<YYYYMMDD>.xlsx` — the filename carries the **target
visit date** (e.g. `Easy_SBF_Gap_20260810.xlsx` for a 10-08-2026 target).
The workbook has two sheets, `Easy_SBF_Gap` (one row per DSE + Retailer +
Visit Date) and `Run_Info` (execution metadata). A byte-identical copy is
written to `History/`. Re-running the same target date appends `_1`, `_2`, ...
so no previous result is ever overwritten. Manual `--date` runs use the same
target-date filename logic.

`Easy_SBF_Gap` columns:

| Column | Meaning |
|--------|---------|
| `dse_code` | DSE from Calls Plan (validated against Name Master) |
| `retailer_code` | Retailer visited on the target date |
| `visit_date` | Target visit date (`DD-MM-YYYY`) |
| `total_gap` | Total No Sale reasons |
| `easy_sbf_gap` | Reasons found in the Easy SBF master |
| `easy_sbf` | Comma-separated matched reason names (deduped) |

Sample row:

```
dse_code      SEALR_MS001
retailer_code SEALR_67506
visit_date    08-08-2026
total_gap     111
easy_sbf_gap  9
easy_sbf      Guard 3in1 Cart 2s, Guard Carts 1s Rs 15, Guard Razor Rs28, ...
```

Latest run totals (non-WS channels, AUTO target date `10-08-2026`): **7,141**
output rows at DSE + Retailer level, `total_gap` = **309,612**, `easy_sbf_gap` =
**55,192**.

## Run

```bash
python rename.py          # normalizes Input filenames to SBD_report*.CSV
python find_easy_sbf.py   # generates the output
```

Optional overrides: `--date DD-MM-YYYY` (or `YYYY-MM-DD`) forces a specific
Visit Date instead of the AUTO `MIN(Visit_date > today)` rule; `-h` prints help.

## Config (top of `find_easy_sbf.py`)

- `INPUT_DIR` / `UPDATING_DIR` / `OUTPUT_DIR` / `HISTORY_DIR` — source and
  destination folders; `Output/` and `History/` are auto-created
- `OUTPUT_FILE_PREFIX` — prefix of the timestamped workbook name
- `COLUMN_ALIASES` — tolerant column mapping (extend if sources change)
- `CHANNEL_WS_PATTERN` — channel substring that is **excluded** (any channel
  containing `WS` is dropped; all other channels are kept)

## Verified edge cases

- No future visit date → logs *"No future Visit Date found"*, **Process terminated.**, no output file, exit 0.
- Missing Calls Plan / empty Input → clear error, exit 1.
- Empty files, blank dates, blank No Sale, blank codes → handled without crashing.
- One retailer under multiple DSEs → one output row per DSE (never merged).
- Exact duplicate DSE + Retailer + Visit Date rows → collapsed to one logical record.
