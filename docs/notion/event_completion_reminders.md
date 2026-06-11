# Event Completion Reminders
`event_completion_reminders.py`

**Category:** CRM action
**Schedule:** Run manually when needed

## What it does

Tags users in GetVero after their events complete so they receive automated follow-up messages — prompting them to request their funds, leave a review, or come back and create another event.

## Who it affects

Tags are applied in GetVero. A CSV audit trail is saved locally for review. No email is sent directly by this script — the messages are sent by GetVero's campaign engine.

## How to run manually

**Always test first:**
```bash
export TEST_MODE=1
python3 event_completion_reminders.py
```

Run for a specific date (useful for catching up after a missed run):
```bash
export TEST_DATE=2026-05-15
python3 event_completion_reminders.py
```

Live run:
```bash
python3 event_completion_reminders.py
```

## Inputs

- S3: Accounts, BookingData, Users, RiskReport
- Vero API (write)

## Outputs

- GetVero: user tags applied
- CSV: audit trail saved locally

## Technical notes

- `TEST_MODE=1` skips all live Vero writes — review the CSV output before going live
- The script checks account exposure before tagging — accounts with insufficient balance to cover future events are skipped
- Five event types are tagged differently: paid/requested, paid/not-requested, free, exposed, and no upcoming events
- See `docs/event_completion_reminders_VERO_DATA.md` for the exact fields sent to Vero
- API client: `modules/utils/vero_api.py`
