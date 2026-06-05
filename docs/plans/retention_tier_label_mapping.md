# Retention priority — shared tier-label mapping (§3 / §8.1)

The retention dashboard shows two tier-related things side by side: the
**current tier** (a label) and the **retention priority** (a category). They are
produced by two different code paths with two different tier taxonomies, so this
note records why they nonetheless refer to the *same accounts* and stay coherent.

## The two taxonomies

| Source | Where | Tier labels |
| --- | --- | --- |
| **v1 pandas pipeline** (`account_processor.py`) | drives `retention_priority.py` | `Key Account`, `High Value`, `Tier 4`, `Tier 3`, `Tier 2`, `Tier 1`, `NIL` |
| **v2 composite** (`tier_calculator_v2.py`, and the dashboard's `getAccountMetricsDuck`) | what CS sees as "current tier" | `Tier 1`–`Tier 5`, `Free`, `Nil` |

`retention_priority.py::calculate_retention_priorities()` keys its tier-weight
and tier-drop boosts off the **v1** labels. The dashboard renders the **v2**
labels. These are *not* the same strings.

## Why side-by-side display is still consistent

We do **not** join priority to a row by tier label — we join by `account_id`:

1. `process_accounts` produces one row per account, `Account_Name` == AccountId,
   carrying both the v1 tier and the computed `Retention_Priority`.
2. `zoho_tiers.py` overlays v2 labels onto that same frame
   (`_apply_v2_tiers`, keyed by AccountId) before pushing to Zoho — so the v1
   and v2 labels for a given account describe the *same* account.
3. `warehouse.write_retention_priority()` snapshots `(account_id,
   retention_priority, retention_priority_score)` into the SQLite warehouse.
4. `prepare_data.py` copies that across to DuckDB as `retention_agg`.
5. The dashboard's `getRetentionDuck()` joins `account_metrics`
   (which computes the **v2** tier for display) to `retention_agg` **by
   `account_id`**.

So the priority value is computed against v1 labels in Python, but it is
attached to the dashboard row by account id, and the *displayed* tier is the v2
label the dashboard computes itself. Parity-by-construction: the priority is the
exact figure the team already trusts (the same one written to Zoho), not a
re-implementation.

## Approximate v1 ↔ v2 correspondence (for human sanity-checking only)

Not used by code — purely to reassure a reader that "Key Account" and "Tier 5"
roughly line up when eyeballing the report:

| v1 (priority input) | v2 (displayed) |
| --- | --- |
| Key Account | Tier 5 |
| High Value | Tier 4 |
| Tier 4 | Tier 3 |
| Tier 3 | Tier 2 |
| Tier 2 / Tier 1 | Tier 1 |
| NIL | Free / Nil |

(The composite v2 calculator is score-based, so the boundaries aren't a strict
1:1 — treat the table as indicative.)

## Timing — same-day, via an end-of-chain re-materialise

The dashboard reads **only** the DuckDB file, which is built by
`prepare_data.py`'s materialise. The naive ordering was a perpetual 1-day lag:

- `prepare_data.py` (02:00) materialises DuckDB — but it's the *first* nightly job.
- `zoho_tiers.py` (02:45) writes retention priority into **SQLite** — *after* the
  DuckDB snapshot was already taken. That write only reaches DuckDB at the *next*
  morning's materialise → 24h stale, and blank for the first day after deploy.

Note this lag is **unique to retention priority**: everything else the dashboard
shows — tier, previous_tier, tier_movement, activity_rating — is computed *in
DuckDB* (`warehouse_duck.ts`'s v2 composite scoring) from the bookings/accounts
data that step 1 already materialised. Industry comes from the accounts snapshot.
None of those depend on a downstream job. Retention priority is the one
dashboard-visible value produced *after* the materialise.

**Fix:** a second, materialise-only run at the end of the weekday chain
(`prepare_data.py --materialise-only`, ~03:30, after `zoho_tiers`). It rebuilds
DuckDB from the now-updated SQLite, so retention priority is **same-day**. The
02:00 full run still materialises so weekends (when `zoho_tiers` doesn't run)
stay fresh off the previous weekday's priority. See `deploy/pi-crontab` steps 1
and 5.
