#!/usr/bin/env python3
"""
2026 Pricing Scenario Modeller

Models 2025 revenue under different fee structures, varying one segment at a
time. Three independent comparison groups:

  - online           (vary online, hold stripe + BO at baseline)
  - stripe           (vary stripe, hold online + BO at baseline)
  - box_office_card  (vary BO card, hold online + stripe at baseline)

Each group is written to its own CSV. The baseline ("do nothing") for the
varying segment is included in the YAML config as the first entry of each
group, so it appears as a row in the output for direct comparison.

Fees are calculated from first principles per row:
    fee = (PaymentReceived * percent / 100) + (TicketQuantity * pence / 100)

If a rule is `includes_vat: true`, percent and pence are divided by 1.2 before
being applied so all reported fees are ex VAT.

Box Office Cash is always £0. Stripe Connect transactions (online OR box
office) use the Stripe rule.

Usage:
    python pricing_scenarios_2026.py --config pricing_scenarios.yml
"""

import argparse
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.utils.data_loader import load_booking_data, filter_successful_transactions

VAT_DIVISOR = 1.2
ANALYSIS_YEAR = 2025

# Baseline rules used to hold non-varying segments constant.
BASELINE_RULES = {
    "online":          {"percent": 5.0, "pence": 15, "includes_vat": True},
    "stripe":          {"percent": 0.0, "pence": 75, "includes_vat": True},
    "box_office_card": {"percent": 5.0, "pence": 15, "includes_vat": True},
}

GROUPS = ("online", "stripe", "box_office_card")


def classify_transactions(df: pd.DataFrame) -> pd.DataFrame:
    """Add a Segment column: online | stripe | box_office_card | bo_cash."""
    payment_upper = df["PaymentType"].astype("string").fillna("").str.upper().str.strip()
    is_box_office = payment_upper.str.contains("CARDPRESENT", na=False) | (payment_upper == "CASH")
    is_cash = payment_upper == "CASH"

    gateway_col = next((c for c in ["Gateway Group", "GatewayGroup"] if c in df.columns), None)
    if gateway_col:
        gateway_lower = df[gateway_col].astype("string").fillna("").str.lower().str.strip()
        is_stripe = gateway_lower.str.contains("stripe connect", na=False)
    else:
        is_stripe = pd.Series(False, index=df.index)

    # Box Office wins over Stripe: any card-present txn is BO Card regardless
    # of gateway. Stripe rule applies to online Stripe Connect only.
    df["Segment"] = np.select(
        [
            is_box_office & is_cash,
            is_box_office & ~is_cash,
            is_stripe,
        ],
        ["bo_cash", "box_office_card", "stripe"],
        default="online",
    )
    return df


def normalise_rule(rule: dict) -> tuple[float, float]:
    """Return (percent_ex_vat, pence_ex_vat) for a single rule."""
    percent = float(rule["percent"])
    pence = float(rule["pence"])
    if rule.get("includes_vat", False):
        percent /= VAT_DIVISOR
        pence /= VAT_DIVISOR
    return percent, pence


def calculate_fees(df: pd.DataFrame, rules: dict) -> pd.Series:
    """Calculate per-row ex-VAT fees given rules for all segments."""
    payment = df["PaymentReceived"].fillna(0).astype("float64").to_numpy()
    tickets = df["TicketQuantity"].fillna(0).astype("float64").to_numpy()
    segment = df["Segment"].to_numpy()

    online_pct, online_p = normalise_rule(rules["online"])
    stripe_pct, stripe_p = normalise_rule(rules["stripe"])
    boc_pct, boc_p = normalise_rule(rules["box_office_card"])

    pct = np.select(
        [segment == "online", segment == "stripe", segment == "box_office_card"],
        [online_pct, stripe_pct, boc_pct],
        default=0.0,
    )
    pence = np.select(
        [segment == "online", segment == "stripe", segment == "box_office_card"],
        [online_p, stripe_p, boc_p],
        default=0.0,
    )

    fee = payment * (pct / 100.0) + tickets * (pence / 100.0)
    return pd.Series(fee, index=df.index)


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        config = yaml.safe_load(f)

    for group in GROUPS:
        if group not in config:
            raise ValueError(f"Config missing group '{group}'")
        if not config[group]:
            raise ValueError(f"Group '{group}' has no scenarios")
        for entry in config[group]:
            for field in ("name", "percent", "pence", "includes_vat"):
                if field not in entry:
                    raise ValueError(f"Group '{group}' entry missing '{field}': {entry}")
    return config


def load_2025_data() -> pd.DataFrame:
    print("Loading booking data...")
    booking_all = load_booking_data(data_type="BookingDataAll")
    booking_current = load_booking_data(data_type="BookingData")

    dfs = [d for d in (booking_all, booking_current) if d is not None and len(d) > 0]
    if not dfs:
        raise RuntimeError("No booking data loaded")

    df = pd.concat(dfs, ignore_index=True)
    if "BookingTransactionId" in df.columns:
        df = df.drop_duplicates(subset=["BookingTransactionId"])
    print(f"  Loaded {len(df):,} total transactions")

    df = filter_successful_transactions(df)
    df["TransactionDate"] = pd.to_datetime(df["TransactionDate"], errors="coerce", utc=True)
    df = df[df["TransactionDate"].dt.year == ANALYSIS_YEAR].copy()
    print(f"  Filtered to {len(df):,} successful {ANALYSIS_YEAR} transactions")

    free_mask = df["PaymentReceived"].fillna(0) == 0
    df = df[~free_mask]
    print(f"  Excluded {free_mask.sum():,} free transactions, {len(df):,} remaining")

    df = classify_transactions(df)
    df["Month"] = df["TransactionDate"].dt.to_period("M").astype(str)
    return df


def build_group_results(df: pd.DataFrame, group: str, scenarios: list[dict]) -> pd.DataFrame:
    """
    For a single group, hold the other two segments at baseline and vary
    `group`'s rule across `scenarios`. The first scenario is treated as the
    baseline for % change calculations.
    """
    months = sorted(df["Month"].unique())
    rows = []
    baseline_total = None

    for i, scenario in enumerate(scenarios):
        rules = dict(BASELINE_RULES)
        rules[group] = {
            "percent": scenario["percent"],
            "pence": scenario["pence"],
            "includes_vat": scenario["includes_vat"],
        }

        fees = calculate_fees(df, rules)
        total = float(fees.sum())
        if i == 0:
            baseline_total = total

        row = {
            "scenario": scenario["name"],
            "total_fee_ex_vat": round(total, 2),
        }
        monthly = fees.groupby(df["Month"]).sum()
        for m in months:
            row[m] = round(float(monthly.get(m, 0.0)), 2)
        rows.append(row)

    results = pd.DataFrame(rows)
    results["pct_change_vs_baseline"] = (
        (results["total_fee_ex_vat"] - baseline_total) / baseline_total * 100
    ).round(2)

    cols = ["scenario", "total_fee_ex_vat", "pct_change_vs_baseline"] + months
    return results[cols]


def main():
    parser = argparse.ArgumentParser(description="Model 2025 revenue under different fee scenarios")
    parser.add_argument("--config", default="pricing_scenarios.yml", help="Path to YAML scenario config")
    parser.add_argument("--output-dir", default="output", help="Directory for output CSVs")
    args = parser.parse_args()

    config = load_config(args.config)
    print(f"Loaded scenarios: " + ", ".join(f"{g}={len(config[g])}" for g in GROUPS))

    df = load_2025_data()

    os.makedirs(args.output_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")

    for group in GROUPS:
        results = build_group_results(df, group, config[group])

        print("\n" + "=" * 70)
        print(f"{group.upper()} (ex VAT)")
        print("=" * 70)
        print(results[["scenario", "total_fee_ex_vat", "pct_change_vs_baseline"]].to_string(index=False))

        out_path = os.path.join(args.output_dir, f"pricing_{group}_{stamp}.csv")
        results.to_csv(out_path, index=False)
        print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
