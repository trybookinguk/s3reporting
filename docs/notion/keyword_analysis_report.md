# keyword_analysis_report.py

**Category:** On-demand report
**Schedule:** Run manually when needed

## What it does

Analyses event names to identify common types of events — balls, concerts, quiz nights, fetes, and so on — and shows how they trend over time. Useful for understanding what kinds of events TryBooking UK is used for.

## Who receives it

CSVs saved locally. No email is sent.

## How to run manually

Full analysis (all keywords):
```bash
python3 keyword_analysis_report.py
```

Focused on specific keywords:
```bash
python3 keyword_analysis_report.py --keywords "ball,concert,quiz" --top_n 20
```

Filter by industry:
```bash
python3 keyword_analysis_report.py --industry_filter "Arts"
```

## Inputs

- S3: BookingData

## Outputs

- `keyword_analysis_report_*.csv` — full keyword frequency table
- Focused variants per keyword group

## Technical notes

- Analysis logic in `modules/event_keyword_analysis.py`
- Keywords are matched against event names — case-insensitive, partial matches included
