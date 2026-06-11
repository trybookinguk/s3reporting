# Box Office Account Breakdown
`account_box_office_cardpresent.py`

**Category:** Utility
**Schedule:** Run manually when needed

## What it does

Shows a breakdown of Box Office card-present revenue for **one specific account** over the last 365 days. Useful when someone asks "how much has account X taken through the card reader this year?"

## How to run manually

Pass the account's TryBooking ID:

```bash
ACCOUNT_ID=12345 python3 account_box_office_cardpresent.py
```

## Inputs

- S3: BookingData
- `ACCOUNT_ID` — the TryBooking account ID to report on (environment variable)

## Outputs

- Console summary + CSV

## Technical notes

- Rolling 365-day window
- Read-only — makes no changes to any system
