# What Data We Have

A plain-English guide to the data TryBooking gives us, what each report is for, and the kinds of questions it can answer. No technical knowledge needed.

For the exact column-by-column detail, see the companion **S3 Report Schemas** reference — but you shouldn't need it to understand what's here.

---

## The big picture

Every day, TryBooking's platform exports a set of CSV files (spreadsheet-style data) to a secure store. Our scripts read those files to build reports, update the CRM, and feed the dashboard. There are about **16 different reports**, but most day-to-day questions come from just three or four of them.

Think of it in three buckets:

1. **Who our customers are** — the accounts and the people on them.
2. **What they're selling** — every ticket transaction.
3. **Money owed and held** — balances, what's been paid out, what's pending.

---

## The data you'll actually use

### 🎟️ Bookings — *"who sold what, when, for how much"*

**Reports:** `BookingData` (this month) and `BookingDataAll` (all history)

This is the heart of it: **one row per ticket transaction**. Every time someone buys tickets, it's a row here — which account, which event, how many tickets, how much money, what fees, what payment method, where the buyer was.

**Questions it answers:**
- How much revenue did an account / industry / region generate?
- Which events or account types are growing or shrinking?
- How were people paying (card, PayPal, Apple Pay, box-office card reader)?
- How many tickets were sold for an event?

**Good to know:** `BookingData` is just the current month and refreshes daily. `BookingDataAll` is the entire history — it's huge (over 2 GB), so it's handled carefully behind the scenes.

### 🏛️ Accounts — *"who our customers are"*

**Report:** `Accounts`

**One row per account** (an account = an organisation using TryBooking — a school, a theatre, a charity). Holds their name, status, when they joined, their industry, their fee rates, their tier and activity rating, and their location.

**Questions it answers:**
- How many active accounts do we have, by industry or region?
- When did an account join, and when did it last do anything?
- What tier / rating is an account on?
- What fee rates is an account on?

### 👤 Users — *"the people on each account"*

**Report:** `Users`

**One row per person.** An account can have several users; one of them is the **account owner**. Holds names, login email, role, and last-login info.

**Questions it answers:**
- Who owns / has access to an account?
- What email domains are our customers using? (acquisition insight)
- Which users are active vs dormant?

### 💷 Balances & risk — *"money we're holding and owe"*

**Reports:** `RiskReport` (preferred), `AccountBalance`, `AccountMovement` / `AccountMovementDaily`

These tell us how much money is sitting in each account, how much is tied up in upcoming events, and how much is in the middle of being paid out.

**Questions they answer:**
- How much does an account have available to be paid out?
- Is an account "exposed" (owes more for future events than it currently holds)?
- Has an account requested a transfer of its funds?

**Good to know:** `RiskReport` is the one we lean on — `Balance` is what's available now, `FullBalance` includes money still pending. `AccountMovementDaily` is a once-a-day snapshot used to spot funds movements.

---

## The rest (available, but rarely needed)

These are exported too, and can be drawn on if a question calls for it:

| Report | In one line |
|---|---|
| **Account-Transactions** | The billing ledger — individual charges and credits against accounts. |
| **TransactionReconciliation** | Each transaction matched to the payment provider (card type, payout). |
| **GatewayMovement** | Totals grouped by payment provider (Stripe, PayPal, etc.) rather than by account. |
| **Transfers** | Records of bank transfers out to accounts. |
| **Donations** / **DonationCampaigns** | Donation transactions and campaign pages (little used in the UK). |
| **Fundraising** | Fundraising/charity entity details (an Australian concept — minimal UK use). |
| **ScanAppUsers** | How much the ticket-scanning app was used per event. |

---

## A few things worth knowing

- **It's all yesterday's data, at best.** The files refresh overnight, so the freshest a report can be is the previous day. If the dashboard looks behind, the first thing to check is whether TryBooking's overnight export actually ran (the data can only be as fresh as the file they give us).
- **The 1st of the month is special.** When a month ends, that month's bookings file stops updating, and the new month's files don't appear until the 2nd. So anything running on the 1st deliberately looks at the previous month.
- **It's regulated data.** These files contain personal and financial information, so they're only handled on approved machines — that's why the scripts run on the Pi rather than on personal laptops.
- **Money fields can be confusing.** "Revenue" usually means the ticket value (what the buyer paid for tickets), and "fees" are separate (what TryBooking charges). When a number looks off, it's almost always a revenue-vs-fees, or inc-VAT-vs-ex-VAT, mix-up.

---

*Need the exact field names and formats? See `docs/s3_report_schemas.md`.*
