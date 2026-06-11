# Managing Report Emails

**Who this is for:** anyone who needs to add or remove people from the automated report emails. **No coding or technical knowledge required.**

## Where the list lives

All report recipients are controlled by a single file in SharePoint:

> **Platform Data → `report_recipients.json`**

(It sits in the top level of the Platform Data folder.)

You edit this file, save it, and the change takes effect on the next time each report runs. You do **not** need to tell anyone or touch any code.

## How to edit it

1. Open the **Platform Data** folder in SharePoint.
2. Find **`report_recipients.json`** and open it (it opens in the browser as plain text — you may need to choose "Open → In browser" or download, edit, and re-upload).
3. Make your change (see below).
4. Save / upload back to the same place, keeping the same filename.

## What the file looks like

Each report has its own block. `to` is the main recipients; `cc` is people copied in. Here is the weekly new-accounts report as an example:

```json
  "weekly_new_accounts": {
    "to": ["jules@trybooking.co.uk", "kathryn@trybooking.co.uk"],
    "cc": ["louise@trybooking.co.uk"]
  },
```

## Adding someone

Add their email address inside the square brackets, in quotes, with a comma separating it from the one before:

```json
  "weekly_new_accounts": {
    "to": ["jules@trybooking.co.uk", "kathryn@trybooking.co.uk", "sam@trybooking.co.uk"],
    "cc": ["louise@trybooking.co.uk"]
  },
```

## Removing someone

Delete their email address and the comma next to it:

```json
  "weekly_new_accounts": {
    "to": ["jules@trybooking.co.uk"],
    "cc": ["louise@trybooking.co.uk"]
  },
```

## The rules (important)

- Every email address must be wrapped in **"double quotes"**.
- Put a **comma between** addresses, but **not after the last one**.
- If nobody should be copied in, leave the cc empty like this: `"cc": []`
- Don't rename the blocks (`weekly_new_accounts`, etc.) — those are how the system knows which report is which.
- Keep the curly braces `{ }` and square brackets `[ ]` as they are.

> **Tip:** if you want to check the file is still valid after editing, paste it into a free online "JSON validator" — it will tell you if a quote or comma is missing.

## Which block is which report

| Block name in the file | Report |
|---|---|
| `weekly_new_accounts` | Weekly New Accounts Report (Tuesday) |
| `weekly_salesiq` | Weekly SalesIQ Report (Tuesday) |
| `monthly_commission_summary` | Monthly Commission Report — overall summary copy |

> **Note on the commission report:** each salesperson is emailed their *own* commission report at their **Zoho login email** (the address on their Zoho user account) — not via this file. The `monthly_commission_summary` block only controls who gets the **overall summary copy** (whoever manages commissions).

## If something goes wrong

If the file gets broken (a missing quote or comma), the system **automatically falls back** to a built-in default list, so reports will still go out — they just won't reflect your change until the file is fixed. If a report stops arriving for someone you added, the file is probably invalid — check it in a JSON validator, or ask an engineer.

## Testing a change safely

If you want to confirm a change before it reaches real inboxes, an engineer can run the report in **test mode**, which sends the email only to the test address (`henry@trybooking.co.uk`) regardless of the list. This is a good check before a big change to an important report.
