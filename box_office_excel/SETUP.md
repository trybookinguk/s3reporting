# Box Office Terminals — Excel workbook setup

This rebuilds the reporting-dashboard box-office feature as a standalone,
macro-driven Excel workbook. **Single-owner editing** (one person maintains it);
**one terminal per hire row**; no live data feed.

## What's here

| File | What it is |
| --- | --- |
| `build_workbook.py` | Generates the workbook *structure* (sheets, tables, dropdowns, formulas). |
| `vba/BoxOfficeRules.bas` | The business-rule macros (double-booking guard, continuation, trial/payment). Imported as a module. |
| `vba/HiresSheet_Worksheet_Change.txt` | The event hook — pasted into the **Hires sheet's** code, not a module. |

openpyxl can't write VBA. The generator therefore produces a plain **`.xlsx`**
(a macro-declared `.xlsm` with no macro container is rejected as corrupt by
Excel — especially on Mac). You add the macros in Excel, then Save As `.xlsm`,
and Excel itself writes the valid macro-enabled file.

## Build steps (one-off)

1. **Generate the structure** (hand to the operator — writes a local file only):
   ```bash
   pip install --break-system-packages openpyxl   # if not already installed
   python3 box_office_excel/build_workbook.py
   ```
   → produces `box_office_excel/BoxOfficeTerminals.xlsx`.

2. **Open it in Excel.** You'll see sheets: `ReadMe`, `Hires`, `Terminals`
   (and a hidden `Lists`). The derived Terminals columns (Status, Available
   from, Utilisation, Future hires) are already live formulas.

3. **Add the macros** (Excel for Windows; Mac Excel also has the VBA editor):
   - Press **Alt + F11** to open the VBA editor.
   - **File → Import File…** → choose `vba/BoxOfficeRules.bas`. It appears under
     *Modules*.
   - In the Project pane, under *Microsoft Excel Objects*, **double-click
     `Sheet (Hires)`**. Paste the entire contents of
     `vba/HiresSheet_Worksheet_Change.txt` into that code pane.
   - Close the editor.

4. **Save as macro-enabled:** File → Save As → keep the type **Excel
   Macro-Enabled Workbook (\*.xlsm)**. (If Excel offers to strip macros, say no.)

5. **Enable macros** when reopening (yellow banner → *Enable Content*).

That's it — the guards are now live.

## How it behaves

- **Add a hire:** type into the next empty row of the `Hires` table. On commit the
  macro stamps a `HireId` + `ChangedBy`/`ChangedAt`, then runs the guard.
- **Double-booking:** if the chosen `Terminal` is already on another active,
  date-overlapping hire, the edit is **rejected and undone** with a message
  (unless it's a continuation — see below). Active = `confirmed`/`shipped`/`in_use`.
- **Trial:** set `IsTrial = TRUE` to bypass the payment requirement. Otherwise an
  active hire needs `PaymentReceived = TRUE`.
- **Continuation:** put the prior hire's `HireId` in `ContinuesFromHireId`. On
  editing that cell the macro copies the prior contact/address, sets
  `OutboundMethod = collect`, sets `HireFrom` to the day after the prior ends,
  and marks the **prior hire `completed`**. Overlap between the two linked hires
  is allowed (the kit never came back).
- **Terminals:** maintain your kit on the `Terminals` sheet (`TerminalId`,
  `Model`, `Retired`). Status / Available-from / Utilisation / Future-hires are
  formulas that read the Hires table — no macro needed.

## Limits (be aware)

- **One editor at a time.** This is not multi-user. Two people editing a shared
  copy will clobber each other — there is no concurrency control (the dashboard
  had it; Excel doesn't).
- **No live TryBooking data.** Accounts and terminals are entered by hand.
- **The guard runs on edit, not retroactively.** If you paste many rows at once
  or disable macros, bad data can land unchecked. Re-enable macros and re-touch a
  row to re-validate it.
- **`Application.Undo` only reverts the last single action** — if a rejected edit
  spanned an unusual multi-cell paste, double-check the row after a rejection.

## If you need to change the rules

The status sets and logic live in `vba/BoxOfficeRules.bas` (`IsActive`,
`IsEnded`, `ValidateHireRow`, `ApplyContinuationForRow`). Edit there, re-import,
re-save. The column list lives in `build_workbook.py` (`HIRE_COLUMNS`) if you
regenerate the structure.
