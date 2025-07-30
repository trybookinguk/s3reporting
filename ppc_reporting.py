#!/usr/bin/env python3
"""
PPC Reporting Script for TryBooking UK.

This script integrates Google Analytics 4 data with S3 booking data to track
campaign conversions and revenue attribution for PPC campaigns.

Usage:
    python ppc_reporting.py --start-date 2024-01-01 --end-date 2024-01-31
    python ppc_reporting.py --start-date 2024-01-01 --end-date 2024-01-31 --output-file report.csv
    python ppc_reporting.py --start-date 2024-01-01 --end-date 2024-01-31 --test-mode
"""

import os
import sys
import argparse
import json
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Set, Tuple
import re

# Google Analytics imports
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest,
    DateRange,
    Dimension,
    Metric,
    FilterExpression,
    Filter,
    FilterExpressionList
)
from google.oauth2 import service_account

# Import shared modules
from modules.utils.config import UK_TZ, TEST_MODE
from modules.utils.s3_data_loader import get_s3_client, download_s3_file_cached
from modules.utils.date_utils import get_file_date_info
from modules.utils.performance import timer_decorator
from modules.utils.validation import validate_environment_variables

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PPCReporter:
    """Main class for PPC reporting functionality."""
    
    def __init__(self, start_date: datetime, end_date: datetime, test_mode: bool = False):
        """
        Initialize PPC Reporter.
        
        Args:
            start_date: Start date for the report
            end_date: End date for the report
            test_mode: Whether to run in test mode
        """
        self.start_date = start_date
        self.end_date = end_date
        self.test_mode = test_mode or TEST_MODE
        
        # Load campaign configuration
        self.campaigns = self._load_campaign_config()
        
        # Initialize GA4 client
        self.ga_client = self._init_ga4_client()
        
        # S3 client will be initialized when needed
        self.s3_client = None
        
        # Data storage
        self.ga_data = pd.DataFrame()
        self.accounts_data = pd.DataFrame()
        self.booking_data = pd.DataFrame()
        
    def _load_campaign_config(self) -> List[Dict]:
        """Load PPC campaign configuration."""
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'ppc_campaigns.json')
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                active_campaigns = [c for c in config['campaigns'] if c.get('active', True)]
                logger.info(f"Loaded {len(active_campaigns)} active campaigns for exact matching")
                return active_campaigns
        except Exception as e:
            logger.error(f"Failed to load campaign config: {e}")
            raise
            
    def _init_ga4_client(self) -> BetaAnalyticsDataClient:
        """Initialize Google Analytics 4 client."""
        # Check for service account credentials
        credentials_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
        if not credentials_path:
            raise ValueError("GOOGLE_APPLICATION_CREDENTIALS environment variable not set")
        
        if not os.path.exists(credentials_path):
            raise ValueError(f"Service account file not found: {credentials_path}")
        
        try:
            credentials = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=['https://www.googleapis.com/auth/analytics.readonly']
            )
            return BetaAnalyticsDataClient(credentials=credentials)
        except Exception as e:
            logger.error(f"Failed to initialize GA4 client: {e}")
            raise
    
    def _is_tracked_campaign(self, campaign_name: str, source: str = None, medium: str = None) -> bool:
        """Check if a campaign should be tracked based on exact matching.
        
        Args:
            campaign_name: The campaign name from GA4
            source: The traffic source (optional, for disambiguation)
            medium: The traffic medium (optional, for disambiguation)
        """
        if not campaign_name or campaign_name == '(not set)':
            return False  # Exclude non-campaign traffic
        
        # Check for exact match against configured campaigns
        for campaign in self.campaigns:
            # First check campaign name
            if campaign['campaign_name'] == campaign_name:
                # If source/medium are specified in config, they must also match
                if 'source' in campaign and source and campaign['source'] != source:
                    continue
                if 'medium' in campaign and medium and campaign['medium'] != medium:
                    continue
                return True
        
        return False
    
    @timer_decorator
    def fetch_ga4_data(self, property_id: str) -> pd.DataFrame:
        """
        Fetch conversion data from Google Analytics 4.
        
        Args:
            property_id: GA4 property ID (e.g., '123456789')
            
        Returns:
            DataFrame with GA4 conversion data
        """
        logger.info(f"Fetching GA4 data for property {property_id}")
        
        # Build dimension and metric requests
        dimensions = [
            Dimension(name="pagePath"),
            Dimension(name="unifiedScreenClass"),
            Dimension(name="firstUserCampaignName"),
            Dimension(name="firstUserSource"),
            Dimension(name="firstUserMedium"),
            Dimension(name="date")
        ]
        
        metrics = [
            Metric(name="sessions"),
            Metric(name="totalUsers")
        ]
        
        # Build filter for success pages
        page_filter = FilterExpression(
            filter=Filter(
                field_name="pagePath",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.CONTAINS,
                    value="/uk/event/",
                    case_sensitive=False
                )
            )
        )
        
        success_filter = FilterExpression(
            filter=Filter(
                field_name="pagePath",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.CONTAINS,
                    value="/success",
                    case_sensitive=False
                )
            )
        )
        
        # Combine filters
        dimension_filter = FilterExpression(
            and_group=FilterExpressionList(
                expressions=[page_filter, success_filter]
            )
        )
        
        # Build the request
        request = RunReportRequest(
            property=f"properties/{property_id}",
            dimensions=dimensions,
            metrics=metrics,
            date_ranges=[DateRange(
                start_date=self.start_date.strftime('%Y-%m-%d'),
                end_date=self.end_date.strftime('%Y-%m-%d')
            )],
            dimension_filter=dimension_filter,
            limit=10000  # GA4 API limit
        )
        
        # Execute the request
        try:
            response = self.ga_client.run_report(request)
        except Exception as e:
            logger.error(f"GA4 API request failed: {e}")
            raise
        
        # Parse response into DataFrame
        data = []
        for row in response.rows:
            # Extract event ID from page path
            page_path = row.dimension_values[0].value
            event_id = self._extract_event_id(page_path)
            
            if event_id:
                data.append({
                    'page_path': page_path,
                    'unified_screen_class': row.dimension_values[1].value,
                    'campaign': row.dimension_values[2].value or '(not set)',
                    'source': row.dimension_values[3].value or '(not set)',
                    'medium': row.dimension_values[4].value or '(not set)',
                    'date': row.dimension_values[5].value,
                    'sessions': int(row.metric_values[0].value),
                    'users': int(row.metric_values[1].value),
                    'event_id': event_id
                })
        
        df = pd.DataFrame(data)
        
        # Convert date column
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'], format='%Y%m%d')
            df['conversion_date'] = df['date']  # Alias for clarity
            
            # Filter for tracked campaigns (exact match only)
            initial_count = len(df)
            df['is_tracked_campaign'] = df.apply(
                lambda row: self._is_tracked_campaign(
                    row['campaign'], 
                    row.get('source', None), 
                    row.get('medium', None)
                ), 
                axis=1
            )
            df = df[df['is_tracked_campaign']].drop(columns=['is_tracked_campaign'])
            
            if initial_count != len(df):
                logger.info(f"Filtered to {len(df)} conversions from tracked campaigns (from {initial_count} total)")
                logger.info(f"Active campaigns: {[(c['campaign_name'], c.get('source', 'any'), c.get('medium', 'any')) for c in self.campaigns]}")
        
        logger.info(f"Retrieved {len(df)} conversion records from GA4")
        return df
    
    def _extract_event_id(self, url: str) -> str:
        """
        Extract event ID from success page URL.
        
        Args:
            url: URL path (e.g., '/uk/event/123456/success')
            
        Returns:
            Event ID or empty string if not found
        """
        # Pattern to match /uk/event/{ID}/success
        pattern = r'/uk/event/(\d+)/success'
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return match.group(1)
        return ''
    
    @timer_decorator
    def load_s3_data(self):
        """Load accounts and booking data from S3."""
        if not self.s3_client:
            self.s3_client = get_s3_client()
        
        # Load accounts data
        logger.info("Loading accounts data from S3")
        accounts_key = f"{datetime.now().year}/Accounts-TBUK.csv"
        self.accounts_data = download_s3_file_cached(self.s3_client, accounts_key)
        
        # Convert date columns
        self.accounts_data['DateTimeCreated'] = pd.to_datetime(
            self.accounts_data['DateTimeCreated'], 
            errors='coerce'
        ).dt.tz_localize(None).dt.tz_localize('Europe/London')
        
        logger.info(f"Loaded {len(self.accounts_data)} accounts")
        
        # Load booking data for the reporting period
        booking_keys = self._get_booking_keys()
        all_bookings = []
        
        for key in booking_keys:
            logger.info(f"Loading booking data from {key}")
            try:
                df = download_s3_file_cached(self.s3_client, key)
                all_bookings.append(df)
            except Exception as e:
                logger.warning(f"Failed to load {key}: {e}")
        
        if all_bookings:
            self.booking_data = pd.concat(all_bookings, ignore_index=True)
            
            # Parse TransactionDate
            if 'TransactionDate' in self.booking_data.columns:
                self.booking_data['TransactionDate'] = pd.to_datetime(
                    self.booking_data['TransactionDate'], 
                    errors='coerce'
                ).dt.tz_localize(None).dt.tz_localize('Europe/London')
            
            logger.info(f"Loaded {len(self.booking_data)} total booking records")
        else:
            logger.warning("No booking data loaded")
            self.booking_data = pd.DataFrame()
    
    def _get_booking_keys(self) -> List[str]:
        """Generate S3 keys for booking data files."""
        keys = []
        
        # Current month file
        current_info = get_file_date_info(self.end_date)
        keys.append(f"{current_info['folder_year']}/{current_info['folder_month']}/"
                   f"{current_info['file_prefix']}-BookingData-TBUK.csv")
        
        # If date range spans multiple months, add historical files
        if self.start_date.month != self.end_date.month or self.start_date.year != self.end_date.year:
            # Add BookingDataAll for previous months
            keys.append(f"{self.end_date.year}/BookingDataAll-TBUK.csv")
        
        return keys
    
    @timer_decorator
    def match_conversions(self) -> pd.DataFrame:
        """
        Match GA4 conversions with booking data and calculate revenue.
        
        Returns:
            DataFrame with matched conversions and revenue data
        """
        logger.info("Matching GA4 conversions with booking data")
        
        if self.ga_data.empty:
            logger.warning("No GA4 data to match")
            return pd.DataFrame()
        
        # Get unique event IDs from GA4 data
        event_ids = self.ga_data['event_id'].unique()
        logger.info(f"Found {len(event_ids)} unique events with conversions")
        
        # Filter booking data for these events
        event_bookings = self.booking_data[
            self.booking_data['EventId'].astype(str).isin(event_ids)
        ].copy()
        
        if event_bookings.empty:
            logger.warning("No booking data found for converted events")
            return pd.DataFrame()
        
        # Calculate total revenue per event (all fees)
        event_bookings['TotalRevenue'] = (
            event_bookings['PaymentReceived'].fillna(0) +
            event_bookings['BookingFee'].fillna(0) +
            event_bookings['CardFee'].fillna(0) +
            event_bookings['ProcessingFee'].fillna(0) +
            event_bookings['TicketFee'].fillna(0)
        )
        
        # Aggregate by event
        event_revenue = event_bookings.groupby('EventId').agg({
            'AccountId': 'first',
            'AccountName': 'first',
            'EventName': 'first',
            'TotalRevenue': 'sum',
            'TicketQuantity': 'sum'
        }).reset_index()
        
        # Convert EventId to string for merging
        event_revenue['EventId'] = event_revenue['EventId'].astype(str)
        
        # Merge with GA4 data
        matched = self.ga_data.merge(
            event_revenue,
            left_on='event_id',
            right_on='EventId',
            how='left'
        )
        
        # Add account information
        if not self.accounts_data.empty:
            matched = matched.merge(
                self.accounts_data[['AccountId', 'Industry', 'SubIndustry', 'DateTimeCreated']],
                on='AccountId',
                how='left',
                suffixes=('', '_account')
            )
            matched.rename(columns={'DateTimeCreated': 'AccountCreatedDate'}, inplace=True)
        
        # Mark matched status
        matched['matched_status'] = matched['AccountId'].notna()
        
        logger.info(f"Matched {matched['matched_status'].sum()} out of {len(matched)} conversions")
        
        return matched
    
    @timer_decorator
    def apply_eligibility_rules(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Apply account eligibility rules and 12-month revenue cap.
        
        Args:
            df: DataFrame with matched conversions
            
        Returns:
            DataFrame with eligibility flags and capped revenue
        """
        logger.info("Applying eligibility rules")
        
        if df.empty:
            return df
        
        df = df.copy()
        
        # Initialize eligibility columns
        df['is_eligible'] = False
        df['eligibility_reason'] = 'Not evaluated'
        df['revenue_12m'] = df['TotalRevenue']  # Start with actual revenue
        
        # For matched conversions, check eligibility
        matched_mask = df['matched_status'] == True
        
        if matched_mask.any():
            # Calculate account age in days
            df.loc[matched_mask, 'account_age_days'] = (
                df.loc[matched_mask, 'conversion_date'] - 
                df.loc[matched_mask, 'AccountCreatedDate']
            ).dt.days
            
            # Check for previous events (beyond the converted event)
            for idx, row in df[matched_mask].iterrows():
                account_id = row['AccountId']
                
                # Count unique events for this account
                account_events = self.booking_data[
                    self.booking_data['AccountId'] == account_id
                ]['EventId'].nunique()
                
                df.at[idx, 'events_with_tickets'] = account_events
                
                # Eligibility: <90 days old OR no previous events (only 1 event)
                if row['account_age_days'] < 90:
                    df.at[idx, 'is_eligible'] = True
                    df.at[idx, 'eligibility_reason'] = f"New account ({row['account_age_days']} days old)"
                elif account_events <= 1:
                    df.at[idx, 'is_eligible'] = True
                    df.at[idx, 'eligibility_reason'] = "First event for account"
                else:
                    df.at[idx, 'is_eligible'] = False
                    df.at[idx, 'eligibility_reason'] = f"Established account with {account_events} events"
                
                # Apply 12-month revenue cap
                if row['account_age_days'] > 365:
                    # Get bookings from last 12 months only
                    cutoff_date = row['conversion_date'] - timedelta(days=365)
                    recent_bookings = self.booking_data[
                        (self.booking_data['AccountId'] == account_id) &
                        (self.booking_data['TransactionDate'] >= cutoff_date)
                    ]
                    
                    capped_revenue = (
                        recent_bookings['PaymentReceived'].fillna(0).sum() +
                        recent_bookings['BookingFee'].fillna(0).sum() +
                        recent_bookings['CardFee'].fillna(0).sum() +
                        recent_bookings['ProcessingFee'].fillna(0).sum() +
                        recent_bookings['TicketFee'].fillna(0).sum()
                    )
                    
                    df.at[idx, 'revenue_12m'] = capped_revenue
        
        # For unmatched conversions
        unmatched_mask = df['matched_status'] == False
        df.loc[unmatched_mask, 'eligibility_reason'] = 'Event not found in booking data'
        
        eligible_count = df['is_eligible'].sum()
        logger.info(f"{eligible_count} out of {len(df)} conversions are eligible")
        
        return df
    
    @timer_decorator
    def generate_report(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate final report with all required columns.
        
        Args:
            df: DataFrame with eligibility data
            
        Returns:
            Final report DataFrame
        """
        logger.info("Generating final report")
        
        # Select and rename columns for output
        report_columns = {
            'AccountId': 'account_id',
            'AccountName': 'account_name',
            'Industry': 'industry',
            'SubIndustry': 'subindustry',
            'AccountCreatedDate': 'created_date',
            'event_id': 'event_id',
            'EventName': 'event_name',
            'campaign': 'campaign',
            'source': 'source',
            'medium': 'medium',
            'conversion_date': 'conversion_date',
            'events_with_tickets': 'events_with_tickets',
            'TotalRevenue': 'total_revenue',
            'revenue_12m': 'revenue_12m',
            'matched_status': 'matched_status',
            'is_eligible': 'is_eligible',
            'eligibility_reason': 'eligibility_reason',
            'TicketQuantity': 'tickets_sold',
            'sessions': 'ga_sessions',
            'users': 'ga_users'
        }
        
        # Create report DataFrame with selected columns
        available_cols = [col for col in report_columns.keys() if col in df.columns]
        report = df[available_cols].copy()
        
        # Rename columns
        report.rename(columns={k: v for k, v in report_columns.items() if k in available_cols}, inplace=True)
        
        # Sort by conversion date descending
        if 'conversion_date' in report.columns:
            report.sort_values('conversion_date', ascending=False, inplace=True)
        
        # Format date columns
        date_cols = ['created_date', 'conversion_date']
        for col in date_cols:
            if col in report.columns:
                report[col] = report[col].dt.strftime('%Y-%m-%d %H:%M:%S')
        
        # Round numeric columns
        numeric_cols = ['total_revenue', 'revenue_12m']
        for col in numeric_cols:
            if col in report.columns:
                report[col] = report[col].round(2)
        
        return report
    
    def run(self, property_id: str, output_file: str = None) -> pd.DataFrame:
        """
        Run the complete PPC reporting process.
        
        Args:
            property_id: Google Analytics 4 property ID
            output_file: Optional output file path
            
        Returns:
            Final report DataFrame
        """
        logger.info(f"Starting PPC report for {self.start_date.date()} to {self.end_date.date()}")
        
        if self.test_mode:
            logger.info("Running in TEST MODE")
        
        # Step 1: Fetch GA4 data
        self.ga_data = self.fetch_ga4_data(property_id)
        
        if self.ga_data.empty:
            logger.warning("No conversion data found in GA4")
            return pd.DataFrame()
        
        # Step 2: Load S3 data
        self.load_s3_data()
        
        # Step 3: Match conversions with bookings
        matched_data = self.match_conversions()
        
        # Step 4: Apply eligibility rules
        eligible_data = self.apply_eligibility_rules(matched_data)
        
        # Step 5: Generate final report
        report = self.generate_report(eligible_data)
        
        # Step 6: Save report if output file specified
        if output_file:
            report.to_csv(output_file, index=False)
            logger.info(f"Report saved to {output_file}")
        
        # Print summary statistics
        self._print_summary(report)
        
        return report
    
    def _print_summary(self, report: pd.DataFrame):
        """Print summary statistics."""
        print("\n" + "="*60)
        print("PPC REPORTING SUMMARY")
        print("="*60)
        print(f"Report Period: {self.start_date.date()} to {self.end_date.date()}")
        print(f"Total Conversions: {len(report)}")
        
        if not report.empty:
            matched_count = report['matched_status'].sum() if 'matched_status' in report.columns else 0
            print(f"Matched Conversions: {matched_count} ({matched_count/len(report)*100:.1f}%)")
            
            if 'is_eligible' in report.columns:
                eligible_count = report['is_eligible'].sum()
                print(f"Eligible Conversions: {eligible_count} ({eligible_count/len(report)*100:.1f}%)")
            
            if 'total_revenue' in report.columns:
                total_revenue = report['total_revenue'].sum()
                eligible_revenue = report[report['is_eligible'] == True]['revenue_12m'].sum() if 'is_eligible' in report.columns else 0
                print(f"\nTotal Revenue: £{total_revenue:,.2f}")
                print(f"Eligible Revenue: £{eligible_revenue:,.2f}")
            
            # Campaign breakdown
            if 'campaign' in report.columns:
                print("\nTop Campaigns:")
                campaign_stats = report.groupby('campaign').agg({
                    'event_id': 'count',
                    'total_revenue': 'sum' if 'total_revenue' in report.columns else 'count'
                }).sort_values('event_id', ascending=False).head(10)
                
                for campaign, stats in campaign_stats.iterrows():
                    if 'total_revenue' in stats:
                        print(f"  {campaign}: {stats['event_id']} conversions, £{stats['total_revenue']:,.2f}")
                    else:
                        print(f"  {campaign}: {stats['event_id']} conversions")
        
        print("="*60 + "\n")


def main():
    """Main entry point."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Generate PPC conversion report')
    parser.add_argument('--start-date', required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', required=True, help='End date (YYYY-MM-DD)')
    parser.add_argument('--property-id', help='GA4 property ID (can also use GA4_PROPERTY_ID env var)')
    parser.add_argument('--output-file', help='Output CSV file path')
    parser.add_argument('--test-mode', action='store_true', help='Run in test mode')
    
    args = parser.parse_args()
    
    # Validate dates
    try:
        start_date = pd.to_datetime(args.start_date).tz_localize('Europe/London')
        end_date = pd.to_datetime(args.end_date).tz_localize('Europe/London').replace(hour=23, minute=59, second=59)
    except Exception as e:
        logger.error(f"Invalid date format: {e}")
        sys.exit(1)
    
    if start_date > end_date:
        logger.error("Start date must be before end date")
        sys.exit(1)
    
    # Get property ID from args or environment
    property_id = args.property_id or os.environ.get('GA4_PROPERTY_ID')
    if not property_id:
        logger.error("GA4 property ID must be provided via --property-id or GA4_PROPERTY_ID environment variable")
        sys.exit(1)
    
    # Validate required environment variables
    required_vars = ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'GOOGLE_APPLICATION_CREDENTIALS']
    try:
        validate_environment_variables(required_vars)
    except ValueError as e:
        logger.error(f"Environment validation failed: {e}")
        sys.exit(1)
    
    # Create reporter and run
    try:
        reporter = PPCReporter(start_date, end_date, args.test_mode)
        report = reporter.run(property_id, args.output_file)
        
        if report.empty:
            logger.warning("No data generated for report")
            sys.exit(0)
        
        logger.info("Report generation complete")
        
    except Exception as e:
        logger.error(f"Report generation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()