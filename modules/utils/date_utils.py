"""
Date utilities for reporting periods.
"""
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import pandas as pd
from .config import UK_TZ


def get_last_month_dates():
    """Calculate date ranges for last month and last year comparison."""
    today = datetime.now(UK_TZ)
    
    # Last month - ensure we get clean dates
    last_month_end = today.replace(day=1, hour=23, minute=59, second=59, microsecond=999999) - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # Same month last year
    last_year_month_start = last_month_start - relativedelta(years=1)
    last_year_month_end = last_month_end - relativedelta(years=1)
    
    return {
        'last_month_start': pd.Timestamp(last_month_start).tz_localize(None).tz_localize('Europe/London').replace(hour=0, minute=0, second=0),
        'last_month_end': pd.Timestamp(last_month_end).tz_localize(None).tz_localize('Europe/London').replace(hour=23, minute=59, second=59),
        'last_year_month_start': pd.Timestamp(last_year_month_start).tz_localize(None).tz_localize('Europe/London').replace(hour=0, minute=0, second=0),
        'last_year_month_end': pd.Timestamp(last_year_month_end).tz_localize(None).tz_localize('Europe/London').replace(hour=23, minute=59, second=59),
        'month_name': last_month_start.strftime('%B %Y'),
        'month_name_ly': last_year_month_start.strftime('%B %Y'),
        'month_only': last_month_start.strftime('%B')  # Just month name without year
    }


def get_ytd_dates():
    """Calculate YTD date ranges for current and previous year."""
    today = datetime.now(UK_TZ)
    current_year_start = today.replace(month=1, day=1, hour=0, minute=0, second=0)
    last_year_start = current_year_start - relativedelta(years=1)
    
    # YTD ends at the last day of the previous month
    ytd_end = today.replace(day=1) - timedelta(days=1)
    ytd_end_ly = ytd_end - relativedelta(years=1)
    
    return {
        'ytd_start': pd.Timestamp(current_year_start).tz_localize(None).tz_localize('Europe/London'),
        'ytd_end': pd.Timestamp(ytd_end).tz_localize(None).tz_localize('Europe/London').replace(hour=23, minute=59, second=59),
        'ytd_start_ly': pd.Timestamp(last_year_start).tz_localize(None).tz_localize('Europe/London'),
        'ytd_end_ly': pd.Timestamp(ytd_end_ly).tz_localize(None).tz_localize('Europe/London').replace(hour=23, minute=59, second=59)
    }


def get_week_dates(weeks_back=1):
    """Calculate date ranges for a specific week in the past."""
    today = datetime.today()
    target_date = today - timedelta(days=today.weekday() + (7 * weeks_back))
    week_start = pd.Timestamp(target_date.date(), tz='Europe/London')
    week_end = week_start + pd.Timedelta(days=6, hours=23, minutes=59, seconds=59)
    
    # Calculate ISO week info for last year comparison
    iso_year, iso_week, _ = target_date.isocalendar()
    last_year_week_start = datetime.strptime(f'{iso_year - 1}-W{iso_week}-1', '%G-W%V-%u')
    last_year_week_end = last_year_week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    last_year_week_start = pd.Timestamp(last_year_week_start, tz='Europe/London')
    last_year_week_end = pd.Timestamp(last_year_week_end, tz='Europe/London')
    
    return {
        'week_start': week_start,
        'week_end': week_end,
        'last_year_week_start': last_year_week_start,
        'last_year_week_end': last_year_week_end
    }


def get_file_date_info(date=None):
    """Get year/month/prefix info for S3 file paths based on a date."""
    if date is None:
        date = datetime.now(UK_TZ)
    
    return {
        'folder_year': date.strftime('%Y'),
        'folder_month': date.strftime('%m'),
        'file_prefix': date.strftime('%Y%m')
    }


def get_latest_data_date():
    """
    Get the date for the latest available S3 data files.
    
    S3 files are generated daily at midnight for the previous day's data.
    This function returns yesterday's date, which corresponds to the latest
    available data files.
    
    Returns:
        pd.Timestamp: Yesterday's date in Europe/London timezone
    """
    return pd.Timestamp.now('Europe/London') - pd.Timedelta(days=1)