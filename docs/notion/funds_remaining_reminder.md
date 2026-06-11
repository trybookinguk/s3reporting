# funds_remaining_reminder.py

**Category:** CRM action
**Schedule:** Run manually when needed

## What it does

Tags users in GetVero who have unspent funds sitting in their TryBooking account from past events — prompting them to request a payout or use the funds for a new event.

## Who it affects

Tags are applied in GetVero. A CSV audit trail is saved locally. No email is sent directly by this script.

## How to run manually

**Always test first:**
```bash
export TEST_MODE=1
python3 funds_remaining_reminder.py
```

Live run:
```bash
python3 funds_remaining_reminder.py
```

## Inputs

- S3: Accounts, Users, RiskReport
- Vero API (write)

## Outputs

- GetVero: users tagged as `fundsremaining-verified` or `fundsremaining-notverified`
- CSV: audit trail saved locally

## Technical notes

- `TEST_MODE=1` skips all live Vero writes — always review the CSV first
- Two tags are applied: verified (funds confirmed present) and not-verified (funds may be present but data is unclear)
- API client: `modules/utils/vero_api.py`
