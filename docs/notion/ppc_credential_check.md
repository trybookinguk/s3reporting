# PPC Credential Check
`test_ppc_setup.py`

**Category:** Utility
**Schedule:** Run manually when needed

## What it does

Checks that the AWS and Google Analytics credentials are correctly configured **before** running the PPC Attribution Report. Run this first if the PPC report is failing or you've just set up new credentials.

## How to run manually

```bash
python3 test_ppc_setup.py
```

## Inputs

- AWS credentials and GA4 credentials (`GA4_PROPERTY_ID`, `GA4_SERVICE_ACCOUNT_KEY`) from the environment

## Outputs

- Console output: a pass/fail for each credential and connection it checks

## Technical notes

- Safe to run at any time — read-only, makes no changes and sends nothing
- Pairs with the [PPC Attribution Report](ppc_attribution_report.md)
