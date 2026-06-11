# Box Office Account Ranking
`account_box_office_cardpresent.py`

**Category:** Utility
**Schedule:** Run manually when needed

## What it does

Ranks **every account** by how much it has taken on Box Office **card-present** transactions over the rolling last 365 days. Useful for spotting your biggest box-office clients, or for sizing the box-office segment overall. You can also pass a single account ID to get just that account's figures.

Cash is excluded — card-present only.

## How to run manually

Rank all accounts:
```bash
python3 account_box_office_cardpresent.py
```

Just one account:
```bash
python3 account_box_office_cardpresent.py 19815
# or: ACCOUNT_ID=19815 python3 account_box_office_cardpresent.py
```

## Inputs

- S3: BookingData (BookingDataAll + current month)

## Outputs

- `box_office_cardpresent_accounts.csv` — one row per account: fees (inc VAT), revenue, tickets, transactions, last transaction date — ranked by fees descending
- A summary + the top 15 printed to the console

## Technical notes

- Rolling 365 days, Europe/London, relative to today
- "Card present" = `PaymentType` contains `CARDPRESENT` (any spacing); Cash is not included
- Fees are inc VAT = sum of BookingFee + CardFee + ProcessingFee + TicketFee
- Read-only — makes no changes to any system
