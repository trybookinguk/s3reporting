"""
SalesIQ Monthly Report Generator

Generates a CSV report of monthly SalesIQ chat statistics including:
- Total number of chats
- Event Organisers count
- Ticket Purchasers count
- New Business count

Usage:
    python3 salesiq_monthly_report.py --year 2025
    python3 salesiq_monthly_report.py --year 2024 --output custom_report.csv
"""
import requests
import argparse
import csv
from datetime import datetime, timezone
from collections import Counter, defaultdict
import os
import sys

# Import shared modules
from modules.utils.config import (
    ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN,
    ZOHO_PORTAL_NAME
)
from modules.utils.zoho_api import get_access_token
from modules.utils.validation import validate_environment_variables
from modules.utils.performance import timer_decorator

# Tags we want to track in the report
TRACKED_TAGS = ["Event Organiser", "Ticket Purchaser", "New Business"]


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate monthly SalesIQ chat statistics report"
    )
    parser.add_argument(
        "--year",
        type=int,
        required=True,
        help="Year to generate report for (e.g., 2025)"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output CSV filename (default: salesiq_monthly_YYYY.csv)"
    )
    return parser.parse_args()


@timer_decorator
def fetch_all_conversations(token, year):
    """
    Fetch all conversations for a given year from SalesIQ API.

    Args:
        token: Zoho OAuth access token
        year: Year to fetch conversations for

    Returns:
        List of conversation objects
    """
    url = f"https://salesiq.zoho.com/api/v2/{ZOHO_PORTAL_NAME}/conversations"
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}"
    }

    # Define year boundaries
    year_start = datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    year_end = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    all_conversations = []
    page = 1
    conversations_before_year = 0

    print(f"Fetching conversations for {year}...")

    while True:
        params = {"page": page}
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        batch = response.json().get("data", [])

        if not batch:
            break

        # Filter conversations for the target year
        stop_pagination = False
        for convo in batch:
            ts = convo.get("start_time")
            if not ts:
                continue

            dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)

            # If we've gone past the year we're looking for, stop
            if dt < year_start:
                conversations_before_year += 1
                # If we've seen 100 conversations before our year, stop pagination
                if conversations_before_year >= 100:
                    stop_pagination = True
                    break
                continue

            # Only include conversations within our target year
            if year_start <= dt <= year_end:
                all_conversations.append(convo)

        if stop_pagination:
            print(f"  Reached conversations from before {year}, stopping pagination")
            break

        if len(batch) < 20:
            break

        page += 1
        if page % 10 == 0:
            print(f"  Fetched page {page}...")

    print(f"  Total conversations fetched for {year}: {len(all_conversations)}")
    return all_conversations


def aggregate_by_month(conversations, year):
    """
    Aggregate conversation statistics by month.

    Args:
        conversations: List of conversation objects
        year: Year for the report

    Returns:
        Dictionary with month as key and statistics as value
    """
    # Initialise monthly counters
    monthly_stats = defaultdict(lambda: {
        "total": 0,
        "Event Organiser": 0,
        "Ticket Purchaser": 0,
        "New Business": 0
    })

    for convo in conversations:
        ts = convo.get("start_time")
        if not ts:
            continue

        dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
        month_key = dt.strftime("%Y-%m")

        # Increment total count
        monthly_stats[month_key]["total"] += 1

        # Count tags
        tags = convo.get("tags", [])
        tag_names = [t["name"].strip() for t in tags if isinstance(t, dict) and "name" in t]

        for tag in TRACKED_TAGS:
            if tag in tag_names:
                monthly_stats[month_key][tag] += 1

    return monthly_stats


def generate_csv_report(monthly_stats, year, output_filename):
    """
    Generate CSV report from monthly statistics.

    Args:
        monthly_stats: Dictionary of monthly statistics
        year: Year for the report
        output_filename: Path to output CSV file
    """
    # Generate all months for the year (even if no data)
    all_months = [f"{year}-{month:02d}" for month in range(1, 13)]

    # Write CSV
    with open(output_filename, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)

        # Header row
        writer.writerow(["Month", "No of Chats", "Event Organisers", "Ticket Purchasers", "New Business"])

        # Data rows
        for month_key in all_months:
            stats = monthly_stats.get(month_key, {
                "total": 0,
                "Event Organiser": 0,
                "Ticket Purchaser": 0,
                "New Business": 0
            })

            # Format month as "January 2025" etc
            month_date = datetime.strptime(month_key, "%Y-%m")
            month_display = month_date.strftime("%B %Y")

            writer.writerow([
                month_display,
                stats["total"],
                stats["Event Organiser"],
                stats["Ticket Purchaser"],
                stats["New Business"]
            ])

    print(f"\nReport saved to: {output_filename}")


def print_summary(monthly_stats, year):
    """Print a summary of the report to console."""
    print(f"\n{'='*60}")
    print(f"SalesIQ Monthly Report - {year}")
    print(f"{'='*60}")
    print(f"{'Month':<15} {'Chats':>10} {'Event Org':>12} {'Ticket Purch':>14} {'New Biz':>10}")
    print(f"{'-'*60}")

    all_months = [f"{year}-{month:02d}" for month in range(1, 13)]
    totals = {"total": 0, "Event Organiser": 0, "Ticket Purchaser": 0, "New Business": 0}

    for month_key in all_months:
        stats = monthly_stats.get(month_key, {
            "total": 0,
            "Event Organiser": 0,
            "Ticket Purchaser": 0,
            "New Business": 0
        })

        month_date = datetime.strptime(month_key, "%Y-%m")
        month_display = month_date.strftime("%b %Y")

        print(f"{month_display:<15} {stats['total']:>10} {stats['Event Organiser']:>12} {stats['Ticket Purchaser']:>14} {stats['New Business']:>10}")

        # Accumulate totals
        for key in totals:
            totals[key] += stats.get(key, 0)

    print(f"{'-'*60}")
    print(f"{'TOTAL':<15} {totals['total']:>10} {totals['Event Organiser']:>12} {totals['Ticket Purchaser']:>14} {totals['New Business']:>10}")
    print(f"{'='*60}")


def main():
    """Main execution function."""
    args = parse_arguments()

    # Validate environment variables
    validate_environment_variables([
        'ZOHO_CLIENT_ID', 'ZOHO_CLIENT_SECRET', 'ZOHO_REFRESH_TOKEN',
        'ZOHO_PORTAL_NAME'
    ])

    # Set default output filename
    output_filename = args.output or f"salesiq_monthly_{args.year}.csv"

    print(f"\n=== SalesIQ Monthly Report Generator ===")
    print(f"Year: {args.year}")
    print(f"Output: {output_filename}")

    try:
        # Get access token
        access_token = get_access_token()

        # Fetch all conversations for the year
        conversations = fetch_all_conversations(access_token, args.year)

        if not conversations:
            print(f"\nNo conversations found for {args.year}")
            sys.exit(0)

        # Aggregate by month
        monthly_stats = aggregate_by_month(conversations, args.year)

        # Print summary to console
        print_summary(monthly_stats, args.year)

        # Generate CSV report
        generate_csv_report(monthly_stats, args.year, output_filename)

        print("\nReport generation complete!")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
