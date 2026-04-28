# Tier System v2

## Purpose

The tier system assigns every TryBooking UK account a value tier based on the revenue they generate for the business. It is used to prioritise account management, inform retention strategy, and segment accounts for reporting within Zoho CRM.

## Why v2?

The previous system (v1) used five metrics across three qualification paths, which made tiers difficult to explain and predict. It also scored ticket quantity as a core metric, meaning free-only accounts could reach Key Account or High Value status on volume alone.

v2 replaces this with a single revenue-focused composite score. Free accounts are now explicitly labelled rather than mixed in with paying accounts.

## How It Works

### Step 1: Classify the account

Every account falls into one of three categories:

| Category | Rule | What it means |
| --- | --- | --- |
| **Paid activated** | >= 10 tickets in the period AND revenue > £0 | Actively generating revenue — scored and tiered |
| **Free** | >= 10 tickets in the period AND £0 revenue | Active but generating no revenue — tracked separately |
| **Nil** | < 10 lifetime tickets | Never meaningfully used the platform |

### Step 2: Score paid activated accounts

Only paid activated accounts are scored. The composite score is a weighted average of three percentile-ranked metrics:

$$S = 0.55A_{p} + 0.35B_{p} + 0.10C_{p}$$

| Weight | Metric | Why it matters |
| --- | --- | --- |
| **55% (A)** | Revenue Current (rolling 12 months, ex-VAT) | The primary measure of current value to the business. Weighted highest because recent revenue is the strongest signal of an account's importance right now. |
| **35% (B)** | Revenue Lifetime (all time, ex-VAT) | Captures established accounts that may have a quieter year but have historically been significant. Prevents a single slow period from dramatically dropping a long-standing high-value account. |
| **10% (C)** | Years Loyalty (years with at least one transaction) | A lightweight tiebreaker that rewards longevity. Two accounts with identical revenue will favour the one that has been with us longer — they represent a more stable, proven relationship. Kept low to avoid over-rewarding accounts that have simply existed for a long time without generating meaningful revenue. |

**Lower score = better.** Percentiles are inverted so that the strongest accounts score closest to 0, matching the tier numbering where Tier 1 is the best.

All revenue figures are **ex-VAT** (divided by 1.20) so that scores reflect actual value to the business rather than the gross amount including tax.

### Step 3: Assign tier bands

Paid activated accounts are ranked by their composite score and placed into bands:

| Tier | Percentile Band | What it means |
| --- | --- | --- |
| **Tier 1** | Top 2% | The most valuable accounts. Highest revenue, longest track record. |
| **Tier 2** | 3–10% | Strong accounts generating significant revenue. |
| **Tier 3** | 11–25% | Solid mid-tier accounts with meaningful contribution. |
| **Tier 4** | 26–50% | Lower-revenue accounts, still actively paying. |
| **Tier 5** | 51–100% | Smallest revenue contributors among active paid accounts. |

Free and Nil accounts are not ranked — they receive no composite score or percentile values.

### Why ticket quantity is not in the score

Ticket quantity correlates 0.75 with revenue — they largely measure the same thing. Including both means you're effectively double-counting revenue. Removing tickets from the score only changes ~3% of account tiers, and the accounts that move are exactly the ones you'd want to move:

- **Promoted:** Low-volume, high-value accounts (avg £3.60/ticket) — correctly recognised for their revenue despite fewer tickets.
- **Demoted:** High-volume, low-value accounts (avg £0.30/ticket) — correctly deprioritised as their ticket volume was masking low revenue contribution.

### Why free accounts are separate

Free accounts generate no revenue but can still issue significant ticket volumes. Including them in the same scoring pool as paid accounts would:

1. Dilute the percentile rankings (a free account taking a Tier 3 slot pushes a paying account down)
2. Misrepresent their value — high ticket volume is not the same as high business value
3. Allocate team resource to accounts that don't contribute to revenue

The **Free** tier makes these accounts visible without distorting the paid tier rankings. A separate high-engagement free accounts report (1,000+ tickets) is produced alongside the tier output for targeted follow-up.

## YoY Movement

Each account's tier is compared to the previous period (prior rolling 12 months). Movement is labelled as:

- **Improved 2+ tiers** / **Improved 1 tier** — account has moved to a better tier
- **No Change** — same tier as previous period
- **Dropped 1 tier** / **Dropped 2+ tiers** — account has moved to a worse tier

Movement between Free, Nil, and numbered tiers is also tracked (e.g. Free → Tier 4 = improvement).

## Output CSV Columns

| Column | Description |
| --- | --- |
| Account_Name | AccountId (string, Zoho lookup key) |
| Account_Display_Name | Human-readable account name |
| Current_Tier | Tier 1–5, Free, or Nil |
| Previous_Tier | Tier 1–5, Free, or Nil |
| Tier_Movement | Improved 2+/1, No Change, Dropped 1/2+ |
| Composite_Score | Weighted score (0–100, lower = better). NaN for Free/Nil |
| Previous_Composite_Score | Same for previous period |
| Revenue_Current | Rolling 12m fees ex-VAT (£) |
| Revenue_Lifetime | Total lifetime fees ex-VAT (£) |
| Tickets_Current | Rolling 12m ticket count |
| Years_Loyalty | Years with transactions |
| A_Percentile | Revenue current percentile (0 = best). NaN for Free/Nil |
| B_Percentile | Lifetime revenue percentile (0 = best). NaN for Free/Nil |
| C_Percentile | Years loyalty percentile (0 = best). NaN for Free/Nil |

## Migration from v1

Based on a comparison of all 8,022 accounts with transaction history:

| v1 Tier | → Tier 1 | → Tier 2 | → Tier 3 | → Tier 4 | → Tier 5 | → Free | → Nil | Total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Key Account** | **35** | 27 | 3 | 1 | 0 | 3 | 0 | 69 |
| **High Value** | 28 | **123** | 36 | 9 | 6 | 22 | 0 | 224 |
| **Tier 4** | 0 | 106 | **421** | 303 | 67 | 146 | 0 | 1,043 |
| **Tier 3** | 0 | 0 | 17 | **476** | 448 | 250 | 0 | 1,191 |
| **Tier 2** | 0 | 0 | 0 | 7 | **760** | 235 | 0 | 1,002 |
| **Tier 1** | 0 | 0 | 0 | 0 | **312** | 154 | 0 | 466 |
| **NIL** | 0 | 0 | 0 | 0 | 0 | 0 | **4,027** | 4,027 |

v1's numbering was inverted (Key Account was the best, Tier 1 was the lowest). Reading across each row, the bold value shows accounts that landed in the equivalent v2 tier. 88% of paid accounts stayed in the same tier. 810 accounts moved to the new Free tier — these were previously spread across all v1 tiers based on ticket volume despite generating no revenue.
