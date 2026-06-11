# Account Name Lookup
`check_account_names.py`

**Category:** Utility
**Schedule:** Run manually when needed

## What it does

Takes a list of account names and fuzzy-matches them against TryBooking data — useful for identifying duplicates, merging records, or looking up accounts when you only have a partial or slightly-wrong name.

## How to run manually

Put the names you want to look up in a `names.txt` file in the working directory (one name per line), then:

```bash
python3 check_account_names.py
```

## Inputs

- `names.txt` — one account name per line
- S3: Accounts, BookingData

## Outputs

- CSV with three groups of results: **matched**, **needs review** (close but not certain), and **no match**

## Technical notes

- Fuzzy matching handles typos and partial names, so a near-miss still surfaces as a "needs review" rather than being silently dropped
- Read-only — makes no changes to any system
