#!/usr/bin/env python3
"""
PPC Reporting Script for TryBooking UK.

This script integrates Google Analytics 4 data with S3 booking data to track
campaign conversions and revenue attribution for PPC campaigns.

Always runs from June 1, 2024 to today's date.

Usage:
    python ppc_reporting.py
    python ppc_reporting.py --output-file report.csv
    python ppc_reporting.py --test-mode
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
from modules.utils.s3_data_loader import get_s3_client
from modules.utils.date_utils import get_latest_data_date
from modules.utils.data_loaders import load_accounts_data, load_booking_data
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
        try:
            # First try using GA4_SERVICE_ACCOUNT_KEY environment variable directly
            service_account_key = os.environ.get('GA4_SERVICE_ACCOUNT_KEY')
            if service_account_key:
                import json
                try:
                    # Parse the JSON key
                    key_data = json.loads(service_account_key)
                    credentials = service_account.Credentials.from_service_account_info(
                        key_data,
                        scopes=['https://www.googleapis.com/auth/analytics.readonly']
                    )
                    return BetaAnalyticsDataClient(credentials=credentials)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse GA4_SERVICE_ACCOUNT_KEY: {e}")
                    logger.error("Make sure the key is valid JSON format")
                    raise
            
            # Fall back to GOOGLE_APPLICATION_CREDENTIALS file path
            credentials_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
            if not credentials_path:
                raise ValueError("Neither GA4_SERVICE_ACCOUNT_KEY nor GOOGLE_APPLICATION_CREDENTIALS environment variable is set")
            
            if not os.path.exists(credentials_path):
                raise ValueError(f"Service account file not found: {credentials_path}")
            
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
                start_date='2024-06-01',
                end_date='today'
            )],
            dimension_filter=dimension_filter,
            limit=50000  # Increased limit to get more data
        )
        
        # Execute the request
        try:
            response = self.ga_client.run_report(request)
        except Exception as e:
            logger.error(f"GA4 API request failed: {e}")
            raise
        
        # Parse response into DataFrame
        data = []
        logger.info(f"GA4 API returned {len(response.rows)} rows")
        
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
            df['date'] = pd.to_datetime(df['date'], format='%Y%m%d', utc=True)
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
        
        # Get the latest available data date
        latest_date = get_latest_data_date()
        
        # Load accounts data using shared utility
        logger.info("Loading accounts data from S3")
        self.accounts_data = load_accounts_data(self.s3_client, latest_date)
        
        logger.info(f"Loaded {len(self.accounts_data)} accounts")
        
        # Import the shared load_booking_data function with fallback logic
        from modules.utils.data_loaders import load_booking_data
        
        # Load BookingDataAll (historical data)
        logger.info("Loading BookingDataAll...")
        booking_all = load_booking_data(self.s3_client, latest_date, data_type='BookingDataAll')
        logger.info(f"  Loaded {len(booking_all)} historical records")
        
        # Check date range of BookingDataAll
        if 'TransactionDate' in booking_all.columns and not booking_all.empty:
            min_date_all = booking_all['TransactionDate'].min()
            max_date_all = booking_all['TransactionDate'].max()
            logger.info(f"  BookingDataAll date range: {min_date_all} to {max_date_all}")
            logger.info(f"  Latest transaction in BookingDataAll: {max_date_all}")
        
        # Debug: Check event 90060 in BookingDataAll
        if 'EventId' in booking_all.columns:
            event_90060_all = booking_all[booking_all['EventId'] == 90060]
            if not event_90060_all.empty:
                logger.info(f"DEBUG: Event 90060 in BookingDataAll: {len(event_90060_all)} rows, TotalFees: £{event_90060_all['TotalFees'].sum():.2f}")
                if 'BookingTransactionId' in event_90060_all.columns:
                    logger.info(f"  Transaction IDs: {sorted(event_90060_all['BookingTransactionId'].unique())[:5]}...")
        
        # Load current month BookingData
        logger.info("Loading current month BookingData...")
        booking_current = load_booking_data(self.s3_client, latest_date, data_type='BookingData')
        logger.info(f"  Loaded {len(booking_current)} current month records")
        
        # Check date range of BookingData
        if 'TransactionDate' in booking_current.columns and not booking_current.empty:
            min_date_current = booking_current['TransactionDate'].min()
            max_date_current = booking_current['TransactionDate'].max()
            logger.info(f"  BookingData date range: {min_date_current} to {max_date_current}")
            logger.info(f"  Latest transaction in BookingData: {max_date_current}")
            
            # Check for date gap between BookingDataAll and BookingData
            if 'TransactionDate' in booking_all.columns and not booking_all.empty:
                gap_days = (min_date_current - max_date_all).days
                if gap_days > 1:
                    logger.warning(f"  WARNING: {gap_days} day gap between BookingDataAll and BookingData!")
                elif gap_days < 0:
                    logger.warning(f"  WARNING: {abs(gap_days)} days of overlap between BookingDataAll and BookingData!")
                else:
                    logger.info(f"  Date coverage is continuous (gap: {gap_days} days)")
        
        # Debug: Check event 90060 in current BookingData
        if 'EventId' in booking_current.columns:
            event_90060_current = booking_current[booking_current['EventId'] == 90060]
            if not event_90060_current.empty:
                logger.info(f"DEBUG: Event 90060 in BookingData: {len(event_90060_current)} rows, TotalFees: £{event_90060_current['TotalFees'].sum():.2f}")
                if 'BookingTransactionId' in event_90060_current.columns:
                    logger.info(f"  Transaction IDs: {sorted(event_90060_current['BookingTransactionId'].unique())[:5]}...")
        
        all_bookings = [booking_all, booking_current]
        
        if all_bookings:
            self.booking_data = pd.concat(all_bookings, ignore_index=True)
            
            # TransactionDate is already parsed as UTC by load_booking_data
            
            # Remove duplicates if any
            if 'BookingTransactionId' in self.booking_data.columns:
                initial_count = len(self.booking_data)
                
                # Debug: Check event 90060 before deduplication
                event_90060_before = self.booking_data[self.booking_data['EventId'] == 90060]
                if not event_90060_before.empty:
                    logger.info(f"DEBUG: Event 90060 BEFORE dedup: {len(event_90060_before)} rows")
                    
                    # Check for duplicate transaction IDs
                    transaction_counts = event_90060_before['BookingTransactionId'].value_counts()
                    duplicates = transaction_counts[transaction_counts > 1]
                    if not duplicates.empty:
                        logger.info(f"  Found duplicate transactions for event 90060: {dict(duplicates)}")
                
                self.booking_data = self.booking_data.drop_duplicates(subset=['BookingTransactionId'])
                duplicates_removed = initial_count - len(self.booking_data)
                if duplicates_removed > 0:
                    logger.info(f"  Removed {duplicates_removed} duplicate transactions")
                    
                # Debug: Check event 90060 after deduplication
                event_90060_after = self.booking_data[self.booking_data['EventId'] == 90060]
                if not event_90060_after.empty:
                    logger.info(f"DEBUG: Event 90060 AFTER dedup: {len(event_90060_after)} rows")
                    if len(event_90060_before) != len(event_90060_after):
                        logger.info(f"  WARNING: Lost {len(event_90060_before) - len(event_90060_after)} rows for event 90060 during dedup!")
            
            logger.info(f"Loaded {len(self.booking_data)} total booking records")
            
            # Log date range of loaded data for verification
            if 'TransactionDate' in self.booking_data.columns:
                min_date = self.booking_data['TransactionDate'].min()
                max_date = self.booking_data['TransactionDate'].max()
                logger.info(f"Booking data date range: {min_date} to {max_date}")
                
                # Debug: Check event 90060
                event_90060 = self.booking_data[self.booking_data['EventId'] == 90060]
                if not event_90060.empty:
                    logger.info(f"DEBUG: Event 90060 found in combined booking data:")
                    logger.info(f"  - Total rows: {len(event_90060)}")
                    logger.info(f"  - Date range: {event_90060['TransactionDate'].min()} to {event_90060['TransactionDate'].max()}")
                    logger.info(f"  - Total fees: £{event_90060['TotalFees'].sum():.2f}")
                    logger.info(f"  - Unique account IDs: {event_90060['AccountId'].nunique()}")
                    
                    # Show transaction dates
                    transaction_dates = event_90060['TransactionDate'].dt.date.value_counts().sort_index()
                    logger.info(f"  - Transactions by date:")
                    for date, count in transaction_dates.items():
                        logger.info(f"    {date}: {count} transactions")
                    
                    # Check for specific booking IDs we expect
                    expected_bookings = {
                        'cadf28e3-cbb4-4941-bbf1-c69c0b0f1531': {'date': '2025-07-25', 'processing': 3.0, 'ticket': 0.3},
                        'e9ec37aa-f595-448c-b999-3aa6a615babb': {'date': '2025-07-26', 'processing': 3.0, 'ticket': 0.3},
                        'a71d5717-d780-4756-92eb-fa2f47d0c27f': {'date': '2025-07-27', 'processing': 9.0, 'ticket': 0.9},
                        '02993b66-b4c1-42e4-9e3e-6c40866d7113': {'date': '2025-07-28', 'processing': 3.0, 'ticket': 0.3},
                        '696a8460-1d5c-4148-a88e-2a138398ecc7': {'date': '2025-07-30', 'processing': 3.0, 'ticket': 0.3},
                        'c206f484-41ec-4d1a-82da-c7cbf2136a8a': {'date': '2025-07-30', 'processing': 3.0, 'ticket': 0.3},
                        'ceaa73c5-c762-42e4-a284-745cdc6125f1': {'date': '2025-07-30', 'processing': 3.0, 'ticket': 0.3},
                        'b454c0cf-0ee1-4b4c-a8f4-ae274ef9df1e': {'date': '2025-07-30', 'processing': 1.5, 'ticket': 0.15},
                        '9cd08ad0-07cb-4dfe-8572-e4b2ce6d33ca': {'date': '2025-08-01', 'processing': 1.5, 'ticket': 0.15},
                        '8af53582-54f4-4d39-9e5e-4cf74f4b3127': {'date': '2025-08-06', 'processing': 3.0, 'ticket': 0.3}
                    }
                    
                    logger.info(f"  - Checking for {len(expected_bookings)} expected bookings:")
                    found_count = 0
                    total_expected_fees = sum(b['processing'] + b['ticket'] for b in expected_bookings.values())
                    
                    if 'BookingUrlId' in event_90060.columns:
                        for booking_id, expected in expected_bookings.items():
                            booking_row = event_90060[event_90060['BookingUrlId'] == booking_id]
                            if not booking_row.empty:
                                found_count += 1
                                actual_fees = booking_row['TotalFees'].iloc[0]
                                expected_fees = expected['processing'] + expected['ticket']
                                logger.info(f"    ✓ {booking_id[:8]}... on {expected['date']}: Found (fees: £{actual_fees:.2f})")
                            else:
                                logger.warning(f"    ✗ {booking_id[:8]}... on {expected['date']}: MISSING (expected fees: £{expected['processing'] + expected['ticket']:.2f})")
                    else:
                        logger.warning("    BookingUrlId column not found - cannot check specific bookings")
                    
                    logger.info(f"  - Found {found_count}/{len(expected_bookings)} expected bookings")
                    logger.info(f"  - Expected total fees: £{total_expected_fees:.2f}")
                else:
                    logger.info("DEBUG: Event 90060 NOT found in combined booking data")
        else:
            logger.warning("No booking data loaded")
            self.booking_data = pd.DataFrame()
    
    
    @timer_decorator
    def match_conversions(self) -> pd.DataFrame:
        """
        Match GA4 conversions with booking data and calculate revenue.
        Aggregates by account to show one line per account.
        
        IMPORTANT: Uses separate aggregation approach to prevent revenue double counting
        when multiple GA4 sessions exist for the same event. Revenue is aggregated by
        unique events first, then summed by account, whilst GA4 metrics (sessions/users) 
        are summed across all sessions to maintain accurate tracking metrics.
        
        Returns:
            DataFrame with matched conversions and revenue data aggregated by account
        """
        logger.info("Matching GA4 conversions with booking data")
        
        if self.ga_data.empty:
            logger.warning("No GA4 data to match")
            return pd.DataFrame()
        
        # Get unique event IDs from GA4 data
        event_ids = self.ga_data['event_id'].unique()
        logger.info(f"Found {len(event_ids)} unique events with conversions")
        
        # Debug: Check if event 90060 is in GA4 conversions
        if '90060' in event_ids:
            logger.info("DEBUG: Event 90060 found in GA4 conversions")
            ga_90060 = self.ga_data[self.ga_data['event_id'] == '90060']
            logger.info(f"  - GA4 sessions for 90060: {len(ga_90060)}")
            logger.info(f"  - Campaigns: {ga_90060['campaign'].unique()}")
        else:
            logger.info("DEBUG: Event 90060 NOT in GA4 conversions")
        
        # Filter booking data for these events
        # Debug: Check data types
        logger.info(f"DEBUG: EventId dtype in booking_data: {self.booking_data['EventId'].dtype}")
        logger.info(f"DEBUG: event_ids sample (first 5): {list(event_ids)[:5]}")
        
        event_bookings = self.booking_data[
            self.booking_data['EventId'].astype(str).isin(event_ids)
        ].copy()
        
        if event_bookings.empty:
            logger.warning("No booking data found for converted events")
            return pd.DataFrame()
        
        # Debug: Check if event 90060 made it to event_bookings
        event_90060_bookings = event_bookings[event_bookings['EventId'] == 90060]
        if not event_90060_bookings.empty:
            logger.info(f"DEBUG: Event 90060 in event_bookings: {len(event_90060_bookings)} rows")
            logger.info(f"  - BookingFee sum: £{event_90060_bookings['BookingFee'].fillna(0).sum():.2f}")
            logger.info(f"  - CardFee sum: £{event_90060_bookings['CardFee'].fillna(0).sum():.2f}")
            logger.info(f"  - ProcessingFee sum: £{event_90060_bookings['ProcessingFee'].fillna(0).sum():.2f}")
            logger.info(f"  - TicketFee sum: £{event_90060_bookings['TicketFee'].fillna(0).sum():.2f}")
            if 'TotalFees' in event_90060_bookings.columns:
                logger.info(f"  - TotalFees sum: £{event_90060_bookings['TotalFees'].sum():.2f}")
        else:
            logger.info("DEBUG: Event 90060 NOT in event_bookings after filtering")
        
        # Check if we have AccountId column
        if 'AccountId' not in event_bookings.columns:
            logger.error(f"AccountId column not found. Available columns: {list(event_bookings.columns)[:10]}...")
            return pd.DataFrame()
        
        # Use the pre-calculated TotalFees column from data loader
        # This ensures consistency across all reports
        if 'TotalFees' in event_bookings.columns:
            event_bookings['TotalRevenue'] = event_bookings['TotalFees']
            logger.info("Using pre-calculated TotalFees column for revenue")
        else:
            # Fallback: Calculate total revenue per event (fees only, not ticket price)
            logger.warning("TotalFees column not found, calculating manually")
            event_bookings['TotalRevenue'] = (
                event_bookings['BookingFee'].fillna(0) +
                event_bookings['CardFee'].fillna(0) +
                event_bookings['ProcessingFee'].fillna(0) +
                event_bookings['TicketFee'].fillna(0)
            )
        
        # First, aggregate by event to get event-level data
        event_revenue = event_bookings.groupby('EventId').agg({
            'AccountId': 'first',
            'AccountName': 'first',
            'EventName': 'first',
            'TotalRevenue': 'sum',
            'TicketQuantity': 'sum'
        }).reset_index()
        
        # Convert EventId to string for merging
        event_revenue['EventId'] = event_revenue['EventId'].astype(str)
        
        # Debug: Check event 90060 after aggregation
        event_90060_rev = event_revenue[event_revenue['EventId'] == '90060']
        if not event_90060_rev.empty:
            logger.info(f"DEBUG: Event 90060 after aggregation:")
            logger.info(f"  - TotalRevenue: £{event_90060_rev['TotalRevenue'].iloc[0]:.2f}")
            logger.info(f"  - TicketQuantity: {event_90060_rev['TicketQuantity'].iloc[0]}")
        
        # Merge with GA4 data to get conversion dates and campaign info
        event_data = self.ga_data.merge(
            event_revenue,
            left_on='event_id',
            right_on='EventId',
            how='left'  # Keep all GA4 events, including unmatched
        )
        
        # Separate matched and unmatched events
        matched_events = event_data[event_data['AccountId'].notna()].copy()
        unmatched_events = event_data[event_data['AccountId'].isna()].copy()
        
        # Process matched events - aggregate by account
        if not matched_events.empty:
            # CRITICAL FIX: Prevent revenue double counting when multiple GA4 sessions exist for same event
            # 
            # The issue occurs because when we aggregate directly by AccountId, events with multiple
            # GA4 sessions get their revenue counted multiple times (once per session).
            # 
            # Solution: Separate aggregation approach
            # 1. First aggregate unique events to get correct revenue totals (no double counting)
            # 2. Separately aggregate GA4 metrics (sessions/users) which should be summed across all sessions
            # 3. Merge these together for accurate account-level data
            
            # Step 1: Aggregate by unique events to prevent revenue double counting
            # This ensures each event's revenue is counted exactly once per account
            unique_events = matched_events.groupby(['AccountId', 'event_id']).agg({
                'AccountName': 'first',
                'EventName': 'first',
                'campaign': 'first',
                'source': 'first',
                'medium': 'first',
                'conversion_date': 'min',  # Use earliest conversion date for this event
                'TotalRevenue': 'first',  # Each event has a single revenue total (from event_revenue)
                'TicketQuantity': 'first'  # Each event has a single ticket quantity
            }).reset_index()
            
            # Step 2: Aggregate GA4 metrics by account (these should be summed across all sessions)
            ga_metrics = matched_events.groupby('AccountId').agg({
                'sessions': 'sum',  # Sum all sessions across all events and sessions
                'users': 'sum'      # Sum all users across all events and sessions
            }).reset_index()
            
            # Step 3: Now aggregate the unique events by account for revenue totals
            account_revenue = unique_events.groupby('AccountId').agg({
                'AccountName': 'first',
                'event_id': 'first',     # First event ID (from earliest conversion)
                'EventName': 'first',    # Name of first event
                'campaign': 'first',     # Campaign from first conversion  
                'source': 'first',
                'medium': 'first',
                'conversion_date': 'min', # Earliest conversion date across all events
                'TotalRevenue': 'sum',    # Now safely sum - each event counted once
                'TicketQuantity': 'sum'   # Sum tickets across unique events
            }).reset_index()
            
            # Step 4: Merge GA4 metrics with revenue data
            account_agg = account_revenue.merge(ga_metrics, on='AccountId', how='left')
            
            # Count unique events per account
            events_per_account = unique_events.groupby('AccountId')['event_id'].nunique().reset_index()
            events_per_account.columns = ['AccountId', 'events_with_tickets']
            account_agg = account_agg.merge(events_per_account, on='AccountId', how='left')
        else:
            account_agg = pd.DataFrame()
        
        # Process unmatched events - keep as individual events
        if not unmatched_events.empty:
            # For unmatched events, create dummy account records
            unmatched_agg = unmatched_events[['event_id', 'campaign', 'source', 'medium', 
                                              'conversion_date', 'sessions', 'users']].copy()
            unmatched_agg['AccountId'] = 'MANUAL_MATCH_REQUIRED'
            unmatched_agg['AccountName'] = 'Manual Match Required'
            unmatched_agg['EventName'] = 'Event ' + unmatched_agg['event_id'].astype(str)
            unmatched_agg['TotalRevenue'] = 0
            unmatched_agg['TicketQuantity'] = 0
            unmatched_agg['events_with_tickets'] = 0
            
            # Combine matched and unmatched
            if not account_agg.empty:
                account_agg = pd.concat([account_agg, unmatched_agg], ignore_index=True)
            else:
                account_agg = unmatched_agg
        
        # Add account information
        if not self.accounts_data.empty and not account_agg.empty:
            accounts_for_merge = self.accounts_data[['Id', 'Industry', 'SubIndustry', 'DateTimeCreated']].copy()
            accounts_for_merge.rename(columns={'Id': 'AccountId'}, inplace=True)
            
            # Only merge for non-manual match accounts
            non_manual_mask = account_agg['AccountId'] != 'MANUAL_MATCH_REQUIRED'
            
            if non_manual_mask.any():
                # Merge account info for matched accounts
                matched_with_info = account_agg[non_manual_mask].merge(
                    accounts_for_merge,
                    on='AccountId',
                    how='left',
                    suffixes=('', '_account')
                )
                matched_with_info.rename(columns={'DateTimeCreated': 'AccountCreatedDate'}, inplace=True)
                # Ensure AccountCreatedDate is in UTC
                matched_with_info['AccountCreatedDate'] = pd.to_datetime(matched_with_info['AccountCreatedDate'], utc=True)
                
                # Set manual match accounts with null values for account fields
                manual_match = account_agg[~non_manual_mask].copy()
                manual_match['Industry'] = None
                manual_match['SubIndustry'] = None
                manual_match['AccountCreatedDate'] = None
                
                # Combine back together
                account_agg = pd.concat([matched_with_info, manual_match], ignore_index=True)
            else:
                # All are manual match
                account_agg['Industry'] = None
                account_agg['SubIndustry'] = None
                account_agg['AccountCreatedDate'] = None
        
        # Mark matched status
        account_agg['matched_status'] = account_agg['AccountId'] != 'MANUAL_MATCH_REQUIRED'
        
        logger.info(f"Aggregated to {len(account_agg)} unique accounts from {len(event_data)} event conversions")
        
        return account_agg
    
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
                        (self.booking_data['TransactionDate'] >= cutoff_date) &
                        (self.booking_data['TransactionDate'] <= row['conversion_date'])
                    ]
                    
                    capped_revenue = (
                        recent_bookings['BookingFee'].fillna(0).sum() +
                        recent_bookings['CardFee'].fillna(0).sum() +
                        recent_bookings['ProcessingFee'].fillna(0).sum() +
                        recent_bookings['TicketFee'].fillna(0).sum()
                    )
                    
                    df.at[idx, 'revenue_12m'] = capped_revenue
        
        # For unmatched conversions (manual match required)
        unmatched_mask = df['matched_status'] == False
        df.loc[unmatched_mask, 'eligibility_reason'] = 'Manual match required - no booking data found'
        df.loc[unmatched_mask, 'revenue_12m'] = 0  # No revenue if no bookings found
        df.loc[unmatched_mask, 'is_eligible'] = True  # Manual matches are eligible (benefit of doubt)
        df.loc[unmatched_mask, 'events_with_tickets'] = 0
        
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
        
        # Note: We now include all records, including manual match required
        logger.info(f"Report includes {len(eligible_data)} total accounts ({eligible_data['is_eligible'].sum()} eligible)")
        
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
        print("PPC REPORTING SUMMARY (BY ACCOUNT)")
        print("="*60)
        print(f"Report Period: {self.start_date.date()} to {self.end_date.date()}")
        print(f"Total Accounts with Conversions: {len(report)}")
        
        if not report.empty:
            matched_count = report['matched_status'].sum() if 'matched_status' in report.columns else 0
            manual_count = len(report) - matched_count
            print(f"Matched Accounts: {matched_count} ({matched_count/len(report)*100:.1f}%)")
            if manual_count > 0:
                print(f"Manual Match Required: {manual_count} ({manual_count/len(report)*100:.1f}%)")
            
            if 'is_eligible' in report.columns:
                eligible_count = report['is_eligible'].sum()
                print(f"Eligible Accounts: {eligible_count} ({eligible_count/len(report)*100:.1f}%)")
            
            if 'events_with_tickets' in report.columns:
                total_events = report['events_with_tickets'].sum()
                print(f"Total Events with Conversions: {total_events}")
            
            if 'total_revenue' in report.columns:
                total_revenue = report['total_revenue'].sum()
                eligible_revenue = report[report['is_eligible'] == True]['revenue_12m'].sum() if 'is_eligible' in report.columns else 0
                print(f"\nTotal Revenue: £{total_revenue:,.2f}")
                print(f"Eligible Revenue: £{eligible_revenue:,.2f}")
            
            # Campaign breakdown
            if 'campaign' in report.columns:
                print("\nTop Campaigns:")
                campaign_stats = report.groupby('campaign').agg({
                    'account_id': 'count',
                    'total_revenue': 'sum' if 'total_revenue' in report.columns else 'count',
                    'events_with_tickets': 'sum' if 'events_with_tickets' in report.columns else 'count'
                }).sort_values('account_id', ascending=False).head(10)
                
                for campaign, stats in campaign_stats.iterrows():
                    if 'total_revenue' in stats and 'events_with_tickets' in stats:
                        print(f"  {campaign}: {stats['account_id']} accounts, {stats['events_with_tickets']} events, £{stats['total_revenue']:,.2f}")
                    else:
                        print(f"  {campaign}: {stats['account_id']} accounts")
        
        print("="*60 + "\n")


def main():
    """Main entry point."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Generate PPC conversion report')
    parser.add_argument('--property-id', help='GA4 property ID (can also use GA4_PROPERTY_ID env var)')
    parser.add_argument('--output-file', help='Output CSV file path')
    parser.add_argument('--test-mode', action='store_true', help='Run in test mode')
    
    args = parser.parse_args()
    
    # Fixed date range: June 1, 2024 to today
    start_date = pd.to_datetime('2024-06-01').tz_localize('Europe/London')
    end_date = pd.to_datetime('today').tz_localize('Europe/London').replace(hour=23, minute=59, second=59)
    
    logger.info(f"Running PPC report from {start_date.date()} to {end_date.date()}")
    
    # Get property ID from args or environment
    property_id = args.property_id or os.environ.get('GA4_PROPERTY_ID')
    if not property_id:
        logger.error("GA4 property ID must be provided via --property-id or GA4_PROPERTY_ID environment variable")
        sys.exit(1)
    
    # Validate required environment variables
    required_vars = ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY']
    
    # Check for GA4 credentials (either direct key or file path)
    if not (os.environ.get('GA4_SERVICE_ACCOUNT_KEY') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')):
        logger.error("Either GA4_SERVICE_ACCOUNT_KEY or GOOGLE_APPLICATION_CREDENTIALS must be set")
        sys.exit(1)
    
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