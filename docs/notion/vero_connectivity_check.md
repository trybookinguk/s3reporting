# Vero Connectivity Check
`test_vero_api.py`

**Category:** Utility
**Schedule:** Run manually when needed

## What it does

Checks that the GetVero API connection is working **before** running either of the CRM action scripts (Event Completion Reminders or Funds Remaining Reminders). Run this first if a CRM tagging script is failing.

## How to run manually

```bash
python3 test_vero_api.py
```

## Inputs

- `VERO_API_KEY` from the environment

## Outputs

- Console output: a pass/fail for the Vero connection

## Technical notes

- Runs in test mode — no live writes to GetVero
- Pairs with [Event Completion Reminders](event_completion_reminders.md) and [Funds Remaining Reminders](funds_remaining_reminders.md)
