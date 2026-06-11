# Wales & NI Sector Report
`analyse_wales_ni_sectors.py`

**Category:** On-demand report
**Schedule:** Run manually when needed

## What it does

Breaks down accounts in Wales and Northern Ireland by industry sector. Useful for regional sales activity or grant/partnership conversations that require sector evidence.

## Who receives it

CSVs saved locally. No email is sent.

## How to run manually

```bash
python3 analyse_wales_ni_sectors.py
```

## Inputs

- S3: BookingData, Accounts

## Outputs

- CSVs: `wales_ni_*.csv`

## Technical notes

- Focused subset of `regional_segmentation.py` — if you need the full UK picture, run that instead
