# What Data We Have

A guide to the data TryBooking gives us and what it can tell you. The focus here is what questions each report can answer — for exact field names and formats, see the companion **S3 Report Schemas** reference.

---

## The big picture

Every day, TryBooking's platform exports a set of CSV files to a secure store. Our scripts read those files to build reports, update the CRM, and feed the dashboard. There are about 16 reports in total, but most questions come from a handful of them.

A useful way to think about the data is **how reachable it is**:

- **Accounts** and **Users** are small — a few tens of thousands of rows. You can open them in a spreadsheet and get real answers with filters and pivot tables, no code required.
- **Bookings** and the **balance/money** reports are large (millions of rows, gigabytes). These are what the scripts and dashboard are built for — they're not practical to open by hand, but they answer the bigger revenue and financial questions.

---

## What you can explore yourself (in a spreadsheet)

### Accounts — our customers

`Accounts` is **one row per account** — an account being an organisation that uses TryBooking (a school, theatre, charity, club). It's small enough to open directly, and it's the richest report for understanding *who* our customers are. Each row carries the account's name, status, join date, industry and sub-industry, region/postcode, fee rates, tier, activity rating, and when it was last active.

**What it can tell you — straight from a pivot table or filter:**
- How many accounts we have, broken down by **industry**, **sub-industry**, **region**, or **status** (active vs closed).
- How many accounts joined in a given period (by join date), and how that's trending.
- Which accounts have gone quiet — sort by last-activity date.
- The spread of **tiers** and **activity ratings** across the base.
- Which accounts are on which **fee rates**.

This is the first place to look for almost any "how many / what kind of customers do we have" question.

### Users — the people on those accounts

`Users` is **one row per person**. An account can have several users, and one of them is the **account owner** (their role says so, and their username is their login email). Also small enough to work with directly.

**What it can tell you:**
- Who owns or has access to a given account.
- What **email domains** our customers use — a useful read on where sign-ups come from (e.g. lots of `.sch.uk`, or a particular reseller's domain).
- Which users are active vs dormant, from their last-login dates.
- How many users sit on an account.

**Tip:** Accounts and Users share an account ID, so if you're comfortable with a lookup (e.g. `VLOOKUP`/`XLOOKUP`), you can join them — for example, to get the owner's email next to each account's industry and tier without touching any code.

---

## What the scripts and dashboard are for (too big to open by hand)

### Bookings — every ticket transaction

`BookingData` (current month) and `BookingDataAll` (full history) are **one row per ticket transaction** — which account, which event, how many tickets, the money, the fees, the payment method, the buyer's location. This is where revenue lives.

**What it can tell you:**
- Revenue and ticket volumes by account, industry, region, or event.
- Which segments are growing or shrinking over time.
- How people are paying (card, PayPal, Apple/Google Pay, box-office card reader).
- Seasonality and event-type patterns.

`BookingDataAll` is the entire history and is over 2 GB — it's not something to open in a spreadsheet, which is exactly why we have scripts and the dashboard for it.

### Balances & money held

`RiskReport` is the one we rely on: per account, how much is **available** now (`Balance`), how much including pending (`FullBalance`), and how exposed they are against upcoming events. `AccountMovement` / `AccountMovementDaily` are the underlying ledger movements (including amounts mid-transfer), and `AccountBalance` is a simpler balance snapshot.

**What it can tell you:**
- How much an account can be paid out right now.
- Whether an account is "exposed" — owes more for future events than it currently holds.
- Whether an account has requested its funds.

---

## The rest

Exported and available if a specific question calls for them, but not part of day-to-day analysis:

| Report | What it is |
|---|---|
| **Account-Transactions** | The billing ledger — individual charges and credits against accounts. |
| **TransactionReconciliation** | Each transaction matched to the payment provider (card type, payout). |
| **GatewayMovement** | Money totals grouped by payment provider (Stripe, PayPal, etc.). |
| **Transfers** | Records of bank transfers out to accounts. |
| **Donations** / **DonationCampaigns** | Donation transactions and campaign pages (little used in the UK). |
| **ScanAppUsers** | Lists the users who have used the ticket-scanning app, by event. |

---

## A few things worth knowing

- **The data is a day behind, at best.** The files refresh overnight, so the freshest any report gets is the previous day. If the dashboard looks stale, the first thing to check is whether TryBooking's overnight export actually ran — the data is only ever as fresh as the file they give us.
- **The 1st of the month is special.** When a month ends, that month's bookings file stops updating and the new month's files don't appear until the 2nd, so anything running on the 1st deliberately reads the previous month.
- **It's regulated data.** These files hold personal and financial information, so they're only handled on approved machines — that's why the scripts run on the Pi rather than on a personal laptop. If you're working with extracts in a spreadsheet, keep them on approved kit too.
- **Mind revenue vs fees.** "Revenue" is the ticket value (what the buyer paid for tickets); "fees" are separate (what TryBooking charges). When a figure looks wrong it's usually a revenue-vs-fees, or VAT-inclusive-vs-exclusive, mix-up.

---

*Exact field names and formats: `docs/s3_report_schemas.md`.*
