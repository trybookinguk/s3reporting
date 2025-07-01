"""
Utility modules for TryBooking reporting system.

This package contains infrastructure and helper modules:
- config: Configuration constants and settings
- s3_data_loader: S3 file loading and caching utilities
- zoho_api: Zoho CRM API integration utilities  
- report_generator: Email report generation utilities
- date_utils: Date calculation utilities for reporting periods
- data_loaders: Standardized data loading functions
- email_utils: Generic email sending utilities
"""

# Re-export commonly used utilities for convenience
from .config import *
from .s3_data_loader import get_s3_client, load_booking_data_chunks, load_multiple_booking_files, download_s3_file_cached
from .zoho_api import get_access_token, upsert_to_zoho, upsert_to_zoho_with_details
from .report_generator import (
    generate_upcoming_annual_events_report,
    email_upcoming_events_report,
    email_tier_updates_report
)
from .date_utils import get_last_month_dates, get_ytd_dates, get_week_dates, get_file_date_info, get_latest_data_date
from .data_loaders import load_accounts_data, load_booking_data, filter_successful_transactions
from .email_utils import send_html_email, create_html_table
from .metrics_calculator import (
    calculate_yoy_change, calculate_percentage, calculate_transaction_metrics,
    calculate_fee_metrics, filter_date_range, aggregate_by_day_of_week
)
from .validation import validate_environment_variables, validate_dataframe_columns
from .performance import optimize_dtypes, chunk_dataframe, timer_decorator