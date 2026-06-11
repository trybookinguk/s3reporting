# pricing_scenarios_2026.py / pricing_model_comparison.py

**Category:** Planning & analysis
**Schedule:** Run manually when needed

## What they do

These two scripts work together to model what revenue would look like under different fee structures.

- **`pricing_scenarios_2026.py`** — models three scenarios (online, Stripe, box office card) against actual booking data, using scenario groups defined in `pricing_scenarios.yml`
- **`pricing_model_comparison.py`** — compares the outputs of multiple models side by side to highlight the differences

## Who receives it

CSVs saved to `output/`. No email is sent.

## How to run manually

Run the scenarios first:
```bash
python3 pricing_scenarios_2026.py
```

Then compare:
```bash
python3 pricing_model_comparison.py
```

For a specific year:
```bash
export YEAR=2025
python3 pricing_model_comparison.py
```

## Inputs

- S3: BookingData

## Outputs

- CSVs: scenario outputs saved to `output/`

## Technical notes

- Scenario groups are defined in `pricing_scenarios.yml` — edit this file to model different fee structures
- `pricing_scenarios_2026.py` is intended to be made generic for future years (currently uses 2025 booking data as the baseline)
