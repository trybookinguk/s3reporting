# TryBooking UK Account Tiers — Reference

How the nightly tier run turns S3 booking/account extracts into account tiers,
retention priorities and tier‑movement alerts, and writes them back to Zoho CRM.

> Source of truth is the code. Key files: `zoho_tiers.py` (orchestrator),
> `modules/account_processor.py`, `modules/tier_calculator.py` (v1),
> `modules/tier_calculator_v2.py` (v2), `modules/tier_codes.py`,
> `modules/retention_priority.py`, `modules/tier_snapshot.py` /
> `tier_history.py` / `tier_state.py` / `tier_movement_email.py`,
> `modules/utils/config.py`, `modules/utils/zoho_api.py`.

---

## 1. End‑to‑end flow

```
S3 bucket  ──►  local cache / SQLite warehouse  ──►  zoho_tiers.py  ──►  Zoho CRM (upsert)
(RDS extracts)   (prepare_data.py, 02:00)          (02:45 Mon–Fri)        + reports CSVs
                                                          │
                                                          ├─► retention_priority table (warehouse) ─► DuckDB (dashboard)
                                                          └─► tier_snapshot / tier_history (SQLite) ─► tier‑movement emails (T1/T2)
```

- `prepare_data.py` (02:00 daily) refreshes the S3 cache and the SQLite
  warehouse. Downstream jobs set `CACHE_TRUST_TODAY=1` so they trust that
  morning's cache without re‑hitting S3.
- `zoho_tiers.py` (02:45, Mon–Fri) computes tiers and upserts to Zoho.
- A second `prepare_data.py --materialise-only` (03:30) re‑materialises DuckDB so
  the dashboard shows that day's retention priority.

---

## 2. Data sources (S3)

**Bucket:** `produk-rdsextracts-438255373632` (`config.py`).

The tier run uses **Accounts + bookings** (Users is *not* used here):

| Data | S3 key pattern | Notes |
|---|---|---|
| Accounts snapshot | `{year}/{month}/{prefix}-Accounts-TBUK.csv` | → `account_lookup` keyed by `Id` (LastEventCreation, Industry, Postcode, DateTimeCreated, AccountName, AccountStatus, LastLogIn, SubIndustry) |
| Monthly bookings | `{year}/{month}/{prefix}-BookingData-TBUK.csv` | current‑month‑to‑date |
| Full history | `…BookingDataAll-TBUK.csv` | located dynamically (checks prev‑month and current‑month folders, takes newest) |

On the **1st of the month** the run targets the *previous* month's folder (the
current‑month file isn't finalised yet).

**Two load paths** (`main(use_combined=…)`, default `False`):
- **Warehouse path (default, memory‑safe):** streams bookings from the SQLite
  warehouse; builds a 365‑day per‑account aggregate (industry report) and a
  ~90‑day Successful‑bookings frame (rapid‑drop). This 90‑day frame is what
  `process_accounts` and the industry report see.
- **Combined‑pickle path (`--combined`, reference only):** loads the full de‑duped
  BookingDataAll + current‑month union into one frame. OOMs the 4 GB Pi — don't
  use in production.

---

## 3. Tier models

Two taxonomies coexist; `TIER_SYSTEM` (env, default `v1`) selects which drives
`Current_Tier`/`Previous_Tier`.

### v1 (legacy) — higher is better
Labels: **Key Account, High Value, Tier 4, Tier 3, Tier 2, Tier 1, NIL**.
Percentile cut‑points (`TIER_PERCENTILES`): Key Account ≥99, High Value ≥95,
Tier 4 ≥75, Tier 3 ≥50, Tier 2 ≥25; qualified but below Tier 2 → `Tier 1`; no
current activity → `NIL`.

### v2 (composite) — Tier 1 is best
Labels: **Tier 1…Tier 5**, plus off‑scale **Free** and **Nil**.
Numeric codes (`tier_codes.py`): Tier 1=1 … Tier 5=5, Free=6, Nil=7.

### Owned tiers
Only **Tier 1** and **Tier 2** are "owned" (`TIER_OWNERS`), both →
`joan@trybooking.co.uk`, `kathryn@trybooking.co.uk`. Movement emails fire only
when a move touches an owned tier.

---

## 4. Scoring factors

Time windows (Europe/London): `CUTOFF_365 = today−365d`, `CUTOFF_730 =
today−730d`; event‑frequency windows anchor to the 1st of the month −365/−730d.

Revenue metric: `Revenue = BookingFee + CardFee + ProcessingFee + TicketFee`
(VAT‑inclusive in v1; v2 divides by `VAT_RATE = 1.20`). Per account the aggregator
produces lifetime / current (≥CUTOFF_365) / previous ([CUTOFF_730, CUTOFF_365))
revenue, ticket counts, and `years_loyalty` (distinct calendar years with a
booking).

| Factor | File | What it produces |
|---|---|---|
| **Revenue factor** | `revenue_factor.py` | Peer‑relative (industry quintile) revenue drop severity: severe <0.25 ratio → 3, significant <0.50 → 2, moderate <0.80 → 1, else stable. Needs ≥100 accounts in an industry for quintiles. Lifecycle stage by account age (weeks). |
| **Event frequency** | `account_processor.py` (bands) | Distinct active months → `Inactive`(0) / `Annual`(1) / `Seasonal`(2–4) / `Regular`(5–9) / `Continuous`(10–12), current and previous period. |
| **Activity rating** | `activity_rating.py` | 10 ratings (Active Paid/Free, At Risk, Churned, Outreach, Re‑Activated, Never Logged In/Transacted, Suspended/Closed, …). Recent login = 180d, churned login = 365d; "meaningful" = ≥10 tickets or ≥£100. At‑risk/churned thresholds vary by frequency pattern. |
| **Retention priority** | `retention_priority.py` | Composite CS worklist score — see §6. |
| **Rapid‑drop** | `account_processor.py` (inline) | 4‑week vs prior 8‑week revenue drop for high‑tier Continuous/Regular accounts; severity 3/2/1 at <0.25/<0.50/<0.75. Summer suppression for Education. (`rapid_drop_detector.py` is the standalone version; the production path re‑implements it inline.) |

---

## 5. Tier calculation

### v1 — `determine_tier_from_percentiles(a_pct, b_pct, c_years, d_pct, e_pct, has_activity)`
Per‑account inputs (`account_processor.py`): a = current tickets pct, b = current
revenue pct, c = `years_loyalty` (raw), d = lifetime revenue pct, e = avg
revenue/year pct, `has_activity` = current tickets ≥ 10. Percentiles are computed
over accounts with the metric > 0.

Logic: no activity → `NIL`. Otherwise three independent paths, keep the best tier
reached:
1. Tickets percentile alone vs `TIER_PERCENTILES`.
2. Current revenue percentile alone.
3. Loyalty + lifetime + avg: requires `years_loyalty ≥ MIN_YEARS_BY_TIER[tier]`
   **and** both lifetime and avg‑revenue percentiles ≥ the tier cut.
Qualified but sub‑threshold defaults to `Tier 1`. `MIN_YEARS_BY_TIER`: Key 8,
High 7, T4 5, T3 3, T2 2, T1 1. `Current_Tier` and `Previous_Tier` come from two
calls (current vs previous windows).

### v2 — `calculate_composite_tiers(account_metrics, vat_rate=1.20)`
A single weighted composite, ranked **only among paid‑activated accounts**
(≥10 tickets AND revenue>0):

```
composite = 0.55·revenue_current_pct + 0.35·revenue_lifetime_pct + 0.10·years_loyalty_pct
```

Percentiles are **inverted** (lower composite = stronger). `years_loyalty` capped
at 5 (accounts at the cap get best loyalty percentile). Bands by composite
percentile rank: Tier 1 ≤2%, Tier 2 ≤10%, Tier 3 ≤25%, Tier 4 ≤50%, Tier 5 ≤100%
(most‑exclusive wins). `Free` = activated but £0 revenue; `Nil` = not activated.
v2 also emits `Tier_Movement` (Improved 2+/1, No Change, Dropped 1/2+) and
composite diagnostics.

### How `TIER_SYSTEM` picks the path
- `v1` (default): `process_accounts`' tiers are kept as‑is; **no** `Tier_Movement`.
- `v2`: `_apply_v2_tiers` overlays **only** `Current_Tier`/`Previous_Tier`/
  `Tier_Movement` where v2 produced a value (v1 is the fallback for skipped
  accounts). All non‑tier fields (Rating, Event_Frequency_*, Retention_Priority…)
  always come from v1.
- The snapshot/movement‑email pipeline (§7) **always** runs v2, regardless of the
  flag.

---

## 6. Retention priority

`retention_priority.py`. Base score:

```
score = (tier_weight × 2) + rating_severity + revenue_score
```

- **tier_weight:** T1=5, T2=4, T3=3, T4=2, T5=1, Free/Nil=1 (legacy Key=5,
  High=4, NIL=1).
- **rating_severity:** At Risk=5, Outreach=3, Re‑Activated/New/Returned=2, Active
  (Paid/Free)/Inactive=0; Churned/Suspended/Closed/Unactivated/Never
  Logged‑in/Transacted = −1 (excluded).
- **revenue_score:** 0–3 from drop severity (Severe 3 … Stable 0).

Then rapid‑drop clean‑ups (new accounts, upgrades, Education‑in‑summer, still‑selling
accounts, Annual/Seasonal patterns) and a series of **boosts/floors**: tier‑drop
boost (+2/+4/+6), annual reachout floor 11, rapid‑drop floors 13/17, top‑tier‑drop
floors 13/16, a critical‑case set‑to‑19, and a school‑summer cap to ≤5. Hard cap 20.

**Categories** (`categorize_priorities`): Excluded (≤0) / Low 1–5 / Medium 6–10 /
High 11–15 / Very High 16–20. Excluded ratings get a blank `Retention_Priority`.

**Persisted to:**
- Warehouse table `retention_priority` (keyed by `account_id`, with category +
  score), via `warehouse.write_retention_priority` — in its own try/except so a
  warehouse hiccup can't block the Zoho push. DuckDB's next materialise copies it
  to `retention_agg` for the dashboard.
- Zoho fields `Retention_Priority` (category) and `Retention_Priority_Score`.

---

## 7. Zoho sync

**Fields upserted** (`zoho_tiers.py` → `process_accounts` output, minus
`_`‑prefixed columns, plus `Retention_Priority_Score`):
`Account_Name, Current_Tier, Previous_Tier, Ticket_Quantity,
Last_Year_Ticket_Quantity, Years_Loyalty, Event_Frequency_Current,
Event_Frequency_Previous, Rating, Months_Active, Retention_Priority,
Days_Since_Last_Booking, Last_Event_Date_Str, Annual_Pattern,
Retention_Priority_Score` — plus `Tier_Movement` in v2.

**Mechanism** (`modules/utils/zoho_api.py → upsert_to_zoho`): `POST
https://www.zohoapis.com/crm/v2/Accounts/upsert`, batches of 100,
`duplicate_check_fields = ["Account_Name"]`. **The match key is `Account_Name`,
which holds the TryBooking AccountId as a string.** OAuth via refresh token;
retries with backoff and 429 handling.

- **TEST_MODE:** `upsert_to_zoho` logs the payload and performs **no** write.
- **Deleted accounts:** this run logs the count of `Account Deleted` / `Closed`
  accounts; actual removal happens in `zoho_industry.py`. Accounts with
  non‑numeric names are dropped in `process_accounts`.

---

## 8. History, snapshots, movement emails

State lives in **SQLite** (`tier_state.py`, default
`/root/s3reporting/.cache/prepared/tier_state.db`), not SharePoint JSON — the
`token`/`drive_id` arguments on the loaders are legacy no‑ops.

- **tier_snapshot** — one row per account `{tier, composite_score, account_name}`
  + date; overwritten each run; used for day‑over‑day change detection. Only moves
  where the previous *or* current tier is Tier 1/Tier 2 are "email‑relevant"
  (departures and brand‑new accounts dropped).
- **tier_history** — columnar long‑form timeline (accounts × days, int tier codes
  + composite scores); appended one column/day; feeds the per‑account chart in the
  email and the cooldown/bouncer logic. `--rebuild-history` bulk‑replays it.

**Suppression rules:**
- **First run** (no baseline snapshot): all moves suppressed (baseline saved);
  `TEST_MODE` bypasses for preview.
- **Bouncer** (`BOUNCER_WINDOW_DAYS=90`, `BOUNCER_MIN_ROUNDTRIPS=2`): ≥2
  round‑trips on the same owned‑tier pair → suppress moves *within* that pair; a
  breakout *out* of the pair is always sent.
- **Cooldown** (`MOVEMENT_COOLDOWN_DAYS=30`): a move survives only if it climbs
  into a tier not held in the window, or there was no owned‑tier change in the
  window.

**Emails** (`tier_movement_email.py`): one per account whose move touches
Tier 1/Tier 2, to the `TIER_OWNERS` for the tiers involved (To unioned, CCs
deduped). Subject `[Tier Drop]` (down) or `[Tier Change]`. Body =
`templates/tier_movement_email.html` with an inline matplotlib tier‑history chart,
industry, 365‑day tickets, last sale/event, current & lifetime revenue ranks (with
YoY), and a Zoho record link. **TEST_MODE** redirects all mail to
`henry@trybooking.co.uk`, drops CCs, and `[TEST]`‑prefixes the subject.

`--dry-run` skips all state writes (history/snapshot untouched) but still sends
email; `TEST_MODE` alone still writes state (it only redirects email).

---

## 9. Scheduling & run modes

- **Cron:** `deploy/pi-crontab` job #4 — `45 2 * * 1-5` (02:45 UTC, Mon–Fri),
  through `cron_wrapper.sh`, logging to `/root/logs/zoho-tiers.log`. Order:
  prepare‑data 02:00 → s3‑sharepoint 02:15 → zoho‑industry 02:30 → **zoho‑tiers
  02:45** → prepare‑data `--materialise-only` 03:30.
- **Flags:** `--dry-run` (no writes; email still sends — pair with
  `TEST_MODE=true`), `--rebuild-history` (+`--history-from/-to`,
  `--rebuild-from-scratch`; skips Zoho/email), `--combined` (legacy full‑pickle;
  OOMs the Pi).
- **Outputs** → `REPORTS_DIR` (Pi: `/root/s3reporting/reports`):
  `tier_updates_{ts}.csv`, `upcoming_annual_events_{date}.csv`, industry‑revenue
  CSVs. Plus the tier‑updates and upcoming‑annual‑events report emails and the
  per‑account movement emails.

---

## 10. Config quick reference (`modules/utils/config.py`)

| Constant | Value |
|---|---|
| `S3_BUCKET` | `produk-rdsextracts-438255373632` |
| `TIER_SYSTEM` (env) | default `v1` (Pi `.env` sets `v1`) |
| `TIER_PERCENTILES` (v1) | Key 99, High 95, T4 75, T3 50, T2 25 |
| `MIN_YEARS_BY_TIER` | Key 8, High 7, T4 5, T3 3, T2 2, T1 1 |
| `MIN_TICKETS_FOR_ACTIVE` | 10 |
| `YEARS_LOYALTY_CAP` | 5 |
| v2 weights / VAT / bands | 0.55 / 0.35 / 0.10; VAT 1.20; bands 2/10/25/50/100 % |
| `TIER_OWNERS` | Tier 1 & Tier 2 → joan@, kathryn@ |
| `MOVEMENT_COOLDOWN_DAYS` | 30 |
| `BOUNCER_WINDOW_DAYS` / `BOUNCER_MIN_ROUNDTRIPS` | 90 / 2 |
| `MIN_ACCOUNTS_FOR_QUINTILES` | 100 |
| `REVENUE_DROP_THRESHOLDS` | severe 0.25, significant 0.50, moderate 0.80 |
| `MIN_REVENUE_FOR_RAPID_DROP` | 200 |
| `MAX_PRIORITY_SCORE` | 20 |
| `DEFAULT_RECIPIENT` / `TEST_MODE_RECIPIENT` | henry@trybooking.co.uk |

> Note: a few thresholds exist both as a shared `config.py` table and a
> module‑local redefinition that is the one actually used on the production
> `process_accounts` path (event‑frequency bands, rapid‑drop). This doc describes
> the values that actually execute.
