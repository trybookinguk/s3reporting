# Validating the warehouse migration

The warehouse path (default) must produce the same output as the legacy
combined-pickle path (`--combined`). The Pi can't run `--combined` (it OOMs),
so **equivalence is validated on your Mac**; the Pi only proves the new path
runs within memory.

`deploy/compare_outputs.py` does the diffing (money compared within ±0.01,
ints/strings/tiers exact).

## On the Mac — tier job equivalence

Source the env first (`set -a && source .env && set +a`). Build the warehouse +
combined pickle once, then run the tier job both ways with `--dry-run`
(no Zoho/SharePoint writes) and `TEST_MODE=true` (emails to test recipient):

```bash
# 1. Build the warehouse AND the combined pickle (one-off, needs RAM)
python3 prepare_data.py --combined

# 2. Reference run (legacy full-frame path) → its own reports dir
REPORTS_DIR=./val_old TEST_MODE=true python3 zoho_tiers.py --combined --dry-run

# 3. Warehouse run → separate reports dir
REPORTS_DIR=./val_new TEST_MODE=true python3 zoho_tiers.py --dry-run

# 4. Diff the tier + industry CSVs
python3 deploy/compare_outputs.py tiers ./val_old ./val_new
```

Expected: `ALL EQUIVALENT (within tolerance)`. Investigate any:
- **tier-label flips** — a score landed on a percentile boundary; check the
  account's revenue/ticket inputs match between paths.
- **penny diffs** — rounding placement; should be within ±0.01 already.
- **missing/extra accounts** — dedupe or filter divergence (see below).

## Dedupe-survivor spot-check

Confirms the warehouse upsert kept the same row the pickle's `keep='last'` did,
for IDs present in both BookingDataAll and current-month BookingData:

```bash
python3 deploy/compare_outputs.py dedupe \
  --pickle .cache/prepared/combined_booking.pkl \
  --db .cache/prepared/warehouse.db --sample 500
```

Expected: `OK — N sampled shared IDs have identical survivors`.

## On the Pi — memory smoke-test

The Pi runs only the warehouse path. Confirm it stays bounded:

```bash
set -a && source /root/s3reporting/.env && set +a
cd /root/s3reporting
/usr/bin/time -v python3 prepare_data.py 2>&1 | tee /root/logs/val-prepare.log
/usr/bin/time -v python3 zoho_tiers.py --dry-run 2>&1 | tee /root/logs/val-tiers.log
```

In the output of `/usr/bin/time -v`, check **"Maximum resident set size"** —
expect a few hundred MB, not ~2 GB. No `Killed`. Watch live in another shell
with `watch -n2 free -h`.

> The first `prepare_data.py` does the one-time BookingDataAll seed (streamed,
> slow but bounded). Subsequent runs just upsert the daily delta.

## Note

`generate_dashboard_data.py` is NOT yet migrated (Stage 3) — it still OOMs on
the Pi. Validate and smoke-test the tier job first; the dashboard follows.
