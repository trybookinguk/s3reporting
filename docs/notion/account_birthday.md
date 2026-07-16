# Account Birthday
`account_birthday.py`

**Category:** CRM action
**Status:** 🆕 **New script** — not yet added to the cron, review before scheduling
**Schedule:** Not scheduled (run manually / test first)

## What it does

Tags accounts in GetVero on their sign-up anniversary, triggering a "thank you" email that shows lifetime stats — total events run and total tickets sold since the account joined TryBooking.

## Who it affects

An event is sent to GetVero for the account owner only (not Finance — this is a relationship touchpoint, not a funds action). A CSV audit trail is saved locally. No email is sent directly by this script — GetVero's campaign engine sends it off the `Account_Birthday` event.

## How to run manually

**Always test first:**
```bash
export TEST_MODE=1
python3 account_birthday.py
```

Run for a specific date (useful for catching up after a missed run, or testing a known anniversary date):
```bash
export TEST_DATE=2026-05-15
python3 account_birthday.py
```

Live run:
```bash
python3 account_birthday.py
```

## Inputs

- S3: Accounts (for `DateTimeCreated`), lifetime BookingDataAll + current-month BookingData, Users
- Vero API (write)

## Outputs

- GetVero: `Account_Birthday` event tracked per account owner
- CSV: audit trail saved locally (`account_birthday_<date>.csv`)

## Technical notes

- `TEST_MODE=1` skips all live Vero writes — always review the CSV first
- An account "has a birthday" when today's month/day matches `Accounts.DateTimeCreated`, and the account is at least one full year old (accounts created earlier this calendar year don't have a first anniversary yet)
- Lifetime stats use `load_combined_booking_data()` (de-duped union of BookingDataAll + current month), filtered to successful transactions only:
  - `total_tickets_sold` = lifetime sum of `TicketQuantity`
  - `total_events_run` = count of distinct events whose last session has already completed
- Data sent to Vero: `account_id`, `total_events_run`, `total_tickets_sold`, `years_active` — no financial amounts
- Email template: `emails/Account_Birthday.html`
- API client: `modules/utils/vero_api.py` (`track_event`, called directly per user rather than through `batch_track_events`, since that helper's `data_fields` list is hardcoded to the event-completion payload shape)
