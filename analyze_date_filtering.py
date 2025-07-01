#!/usr/bin/env python3
"""
Analyze the date filtering logic to understand why 397 instead of 400.
"""
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import pytz

def get_last_month_dates():
    """Replicate the date range calculation from date_utils.py"""
    # Assume we're running on December 1st, 2024
    today = datetime(2024, 12, 1, 9, 0, 0, tzinfo=pytz.timezone('Europe/London'))
    
    # Last month
    last_month_end = today.replace(day=1) - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    
    # Convert to pandas-style timestamps (for comparison)
    # The actual code does: pd.Timestamp(last_month_start).tz_localize(None).tz_localize('Europe/London')
    # This removes timezone then re-adds it, which could cause issues
    
    return {
        'last_month_start': last_month_start,
        'last_month_end': last_month_end.replace(hour=23, minute=59, second=59),
        'month_name': last_month_start.strftime('%B %Y')
    }

def main():
    dates = get_last_month_dates()
    
    print("=== Date Range Analysis ===")
    print(f"Report month: {dates['month_name']}")
    print(f"Start: {dates['last_month_start']}")
    print(f"End: {dates['last_month_end']}")
    
    # The issue might be in the double timezone localization
    print("\n=== Timezone Handling Issue ===")
    print("The code does: pd.Timestamp(datetime).tz_localize(None).tz_localize('Europe/London')")
    print("This removes any existing timezone info and then adds Europe/London")
    print("This could cause issues if the original datetime already has timezone info")
    
    # Show the exact boundaries
    print("\n=== Exact Boundaries ===")
    print(f"Filtering includes accounts created from:")
    print(f"  {dates['last_month_start'].strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"To:")
    print(f"  {dates['last_month_end'].strftime('%Y-%m-%d %H:%M:%S %Z')}")
    
    # The accounts data is loaded with UTC then converted to Europe/London
    print("\n=== Data Loading Timezone Conversion ===")
    print("1. Accounts data DateTimeCreated is parsed as UTC")
    print("2. Then converted to Europe/London timezone")
    print("3. The filter dates use .tz_localize(None).tz_localize('Europe/London')")
    print("   which might not match properly with the converted data")
    
    # Show potential missing accounts
    print("\n=== Potential Issues ===")
    print("1. Timezone mismatch: If accounts were created in UTC at 00:00-00:59 on Nov 1,")
    print("   they would appear as Oct 31 23:00-23:59 in Europe/London (BST -> GMT transition)")
    print("2. End boundary: The filter uses <= for end date, which should include the full last day")
    print("3. The .tz_localize(None).tz_localize() pattern could create inconsistent timestamps")
    
    # November 2024 had a DST change
    print("\n=== Daylight Saving Time ===")
    print("November 2024: BST ended on October 27, 2024")
    print("So November is entirely in GMT (UTC+0)")
    print("This shouldn't affect November filtering, but the date range calculation might be affected")

if __name__ == "__main__":
    main()