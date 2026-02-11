#!/usr/bin/env python3
"""
Keyword Analysis Report for TryBooking UK.

Generates comprehensive keyword/event type analysis including:
- Keyword frequency and revenue breakdown
- Industry sector breakdown for each keyword
- Temporal patterns (session date and on-sale date)
- Lead time analysis
- Monthly event popularity
- Focused analysis for specific keywords (e.g., ball, concert, musical)

This report answers questions like:
- What sectors are balls/concerts coming from?
- What time of year are these events being run?
- What's the lead time for these events?
- What are the most popular events by month?

Usage:
    # Generate all keyword reports
    python3 keyword_analysis_report.py

    # Generate focused report for specific keywords
    python3 keyword_analysis_report.py --keywords ball,concert,musical

    # Custom output location
    python3 keyword_analysis_report.py --output reports/keyword_analysis.csv
"""

import argparse
import os
from datetime import datetime

import pandas as pd

from modules.event_keyword_analysis import (
    KEYWORD_INDUSTRY_FILTERS,
    analyse_detailed_temporal_patterns,
    analyse_keywords_by_industry,
    analyse_monthly_event_popularity,
    generate_focused_keyword_report,
    generate_keyword_analysis_csvs,
)

# Import shared modules
from modules.utils.config import UK_TZ
from modules.utils.data_loader import filter_successful_transactions, load_booking_data
from modules.utils.performance import timer_decorator


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate Keyword Analysis Report for TryBooking UK"
    )

    parser.add_argument(
        "--output",
        type=str,
        default="keyword_analysis_report.csv",
        help="Output CSV filename (default: keyword_analysis_report.csv)",
    )

    parser.add_argument(
        "--keywords",
        type=str,
        default=None,
        help="Comma-separated list of keywords for focused analysis (e.g., ball,concert,musical)",
    )

    parser.add_argument(
        "--top-n",
        type=int,
        default=100,
        help="Number of top keywords to analyse (default: 100)",
    )

    parser.add_argument(
        "--industry-filter",
        action="store_true",
        default=False,
        help="Enable industry-filtered temporal analysis for focused keywords "
        "(uses predefined industry group configuration)",
    )

    return parser.parse_args()


@timer_decorator
def load_booking_data_combined():
    """
    Load all booking data from S3.

    Returns:
        DataFrame with all booking transactions
    """
    print("Loading booking data from S3...")

    # Load all booking data (BookingDataAll + BookingData combined)
    booking_all_df = load_booking_data(data_type="BookingDataAll")
    booking_current_df = load_booking_data(data_type="BookingData")

    # Combine and deduplicate
    booking_df = pd.concat([booking_all_df, booking_current_df], ignore_index=True)
    if "BookingTransactionId" in booking_df.columns:
        booking_df = booking_df.drop_duplicates(subset=["BookingTransactionId"])

    # Filter to successful transactions only
    booking_df = filter_successful_transactions(booking_df)
    print(f"  Booking records loaded: {len(booking_df):,}")

    # Parse dates
    if "TransactionDate" in booking_df.columns:
        booking_df["TransactionDate"] = pd.to_datetime(
            booking_df["TransactionDate"], errors="coerce", utc=True
        )

    if "EventDate" in booking_df.columns:
        booking_df["EventDate"] = pd.to_datetime(
            booking_df["EventDate"], errors="coerce", utc=True
        )

    return booking_df


def main():
    """Main entry point."""
    args = parse_args()

    print("=" * 70)
    print("KEYWORD ANALYSIS REPORT")
    print("=" * 70)
    print(f"Generated: {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # Load data
    booking_df = load_booking_data_combined()

    # Check for required columns
    if "EventName" not in booking_df.columns or "EventId" not in booking_df.columns:
        print("ERROR: Missing required columns (EventName, EventId)")
        return

    # Get unique event count
    unique_events = booking_df["EventId"].nunique()
    print(f"  Unique events: {unique_events:,}")

    # Check for industry data
    has_industry = "Industry" in booking_df.columns
    if has_industry:
        industry_count = booking_df["Industry"].nunique()
        print(f"  Industries represented: {industry_count}")
    else:
        print("  Warning: No Industry column found - industry analysis will be skipped")

    print()

    # Generate output filename with date
    today = datetime.now(UK_TZ).strftime("%Y%m%d")
    base_output = args.output.rsplit(".", 1)[0]
    output_file = f"{base_output}_{today}.csv"

    # Create output directory if needed
    output_dir = os.path.dirname(output_file)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # If specific keywords requested, generate focused report
    if args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",")]
        print(f"Generating focused report for keywords: {', '.join(keywords)}")
        print("-" * 70)

        industry_filters = KEYWORD_INDUSTRY_FILTERS if args.industry_filter else None

        results = generate_focused_keyword_report(
            booking_df,
            keywords=keywords,
            output_file=output_file,
            industry_filters=industry_filters,
        )

        if results:
            print(f"\nFocused report generated with {len(results)} output files")

            # Print summary
            if "summary" in results:
                print("\nSummary:")
                print(results["summary"].to_string(index=False))
        else:
            print("No results generated - check keyword spelling")

    else:
        # Generate full keyword analysis reports
        print("Generating full keyword analysis reports...")
        print("-" * 70)

        output_files = generate_keyword_analysis_csvs(
            booking_df,
            output_file=output_file,
            output_folder=None,  # Keep files in same directory
        )

        print(f"\nGenerated {len(output_files)} report files:")
        for name, path in output_files.items():
            print(f"  - {name}: {path}")

    print()
    print("=" * 70)
    print("KEYWORD ANALYSIS COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
