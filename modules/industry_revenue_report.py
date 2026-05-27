"""
Industry Revenue Report Generator.

Generates revenue reports by industry and sub-industry, organized into a ZIP file structure or individual CSV files.
"""
import pandas as pd
import numpy as np
import zipfile
import os
import re
from io import BytesIO
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def sanitize_filename(name):
    """
    Sanitize industry/sub-industry names for use as filenames and folder names.
    
    Args:
        name: The name to sanitize
        
    Returns:
        Sanitized name safe for filesystem use
    """
    if pd.isna(name) or name == '':
        return 'unspecified'
    
    # Convert to lowercase and replace spaces with underscores
    name = str(name).lower().replace(' ', '_')
    
    # Remove or replace problematic characters
    # Keep only alphanumeric, underscore, and hyphen
    name = re.sub(r'[^a-z0-9_-]', '', name)
    
    # Remove multiple underscores
    name = re.sub(r'_+', '_', name)
    
    # Strip leading/trailing underscores
    name = name.strip('_')
    
    return name if name else 'unspecified'


def calculate_account_metrics(booking_df, account_id):
    """
    Calculate metrics for a single account.
    
    Args:
        booking_df: DataFrame containing booking data for the account
        account_id: The account ID
        
    Returns:
        Dictionary with calculated metrics
    """
    if booking_df.empty:
        return {
            'AccountId': account_id,
            'EventsWithTickets': 0,
            'PaidTicketsIssued': 0,
            'TotalFees': 0.0
        }
    
    # Number of unique events with tickets
    events_with_tickets = booking_df['EventId'].nunique() if 'EventId' in booking_df.columns else 0
    
    # Total paid tickets (sum of TicketQuantity)
    paid_tickets = booking_df['TicketQuantity'].sum() if 'TicketQuantity' in booking_df.columns else 0
    
    # Calculate total fees
    fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
    total_fees = 0.0
    for col in fee_columns:
        if col in booking_df.columns:
            total_fees += booking_df[col].fillna(0).sum()
    
    return {
        'AccountId': account_id,
        'EventsWithTickets': events_with_tickets,
        'PaidTicketsIssued': int(paid_tickets),
        'TotalFees': round(total_fees, 2)
    }


def generate_industry_revenue_reports(booking_df, account_df, tier_updates, report_date):
    """
    Generate industry revenue reports and package them into a ZIP file.
    
    Args:
        booking_df: DataFrame with booking data including fees
        account_df: DataFrame with account information including Industry, SubIndustry, DateTimeCreated
        tier_updates: DataFrame with tier calculations including Current_Tier
        report_date: The report date (pd.Timestamp) to determine current period
        
    Returns:
        BytesIO object containing the ZIP file
    """
    logger.info("Starting industry revenue report generation")
    
    # Determine current period (last 365 days for tier calculations)
    # This aligns with CUTOFF_365 used in tier calculations
    today = pd.Timestamp.now('UTC').tz_localize(None)
    period_end = today
    period_start = today - pd.Timedelta(days=365)
    
    # Use the same period for both accounts and bookings (last 365 days)
    
    logger.info(f"Current period (365 days): {period_start.strftime('%Y-%m-%d')} to {period_end.strftime('%Y-%m-%d')}")
    
    # Ensure we have necessary columns
    if 'Industry' not in account_df.columns:
        logger.error("Industry column not found in account data")
        raise ValueError("Industry column not found in account data")
    
    # Convert AccountId to string for consistent merging
    account_df['AccountId'] = account_df['Id'].astype(str) if 'Id' in account_df.columns else account_df.index.astype(str)
    booking_df['AccountId'] = booking_df['AccountId'].astype(str)
    
    # Add gateway group from account data
    gateway_columns = ['Gateway Group', 'GatewayGroup', 'Gateway_Group']
    gateway_col = None
    for col in gateway_columns:
        if col in account_df.columns:
            gateway_col = col
            break
    
    if not gateway_col:
        logger.warning("Gateway Group column not found, will use 'Unknown'")
        account_df['GatewayGroup'] = 'Unknown'
        gateway_col = 'GatewayGroup'
    
    # Prepare tier lookup
    tier_lookup = {}
    if tier_updates is not None and not tier_updates.empty and 'Account_Id' in tier_updates.columns:
        # tier_updates uses Account_Id column
        tier_lookup = tier_updates.set_index('Account_Id')['Current_Tier'].to_dict()
    else:
        logger.warning("Tier updates not available, will use 'NIL' for all accounts")
    
    # Parse DateTimeCreated for new account identification
    # Ensure DateTimeCreated is timezone-naive for comparison
    account_df['DateTimeCreated'] = pd.to_datetime(account_df['DateTimeCreated'], errors='coerce', utc=True).dt.tz_localize(None)
    account_df['IsNewAccount'] = (
        (account_df['DateTimeCreated'] >= period_start) & 
        (account_df['DateTimeCreated'] <= period_end)
    )
    
    # Filter booking data to current period (last 365 days)
    # Ensure TransactionDate is timezone-naive for comparison
    booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'], errors='coerce', utc=True).dt.tz_localize(None)
    current_bookings = booking_df[
        (booking_df['TransactionDate'] >= period_start) & 
        (booking_df['TransactionDate'] <= period_end)
    ].copy()
    
    logger.info(f"Found {len(current_bookings):,} bookings in current period (last 365 days)")
    logger.info(f"Found {account_df['IsNewAccount'].sum():,} new accounts (created in last 365 days)")
    logger.info(f"Processing {account_df['Industry'].nunique()} industries")
    
    # Create ZIP file in memory
    zip_buffer = BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Group accounts by industry
        for industry in account_df['Industry'].unique():
            if pd.isna(industry):
                industry = 'Unspecified'
            
            industry_folder = sanitize_filename(industry)
            logger.info(f"Processing industry: {industry} -> {industry_folder}")
            
            # Get accounts for this industry
            industry_accounts = account_df[account_df['Industry'] == industry]
            industry_account_ids = industry_accounts['AccountId'].unique()
            
            # Get bookings for these accounts
            industry_bookings = current_bookings[current_bookings['AccountId'].isin(industry_account_ids)]
            
            # Calculate metrics for each account in this industry
            industry_data = []
            for account_id in industry_account_ids:
                account_bookings = industry_bookings[industry_bookings['AccountId'] == account_id]
                metrics = calculate_account_metrics(account_bookings, account_id)
                
                # Add tier and gateway group
                metrics['Tier'] = tier_lookup.get(account_id, 'NIL')
                
                account_info = industry_accounts[industry_accounts['AccountId'] == account_id].iloc[0]
                metrics['GatewayGroup'] = account_info[gateway_col] if gateway_col in account_info else 'Unknown'
                metrics['IsNewAccount'] = account_info['IsNewAccount'] if 'IsNewAccount' in account_info else False
                
                # Only include accounts with activity or that are new
                if metrics['TotalFees'] > 0 or metrics['IsNewAccount']:
                    industry_data.append(metrics)
            
            # Create DataFrame
            if industry_data:
                industry_df = pd.DataFrame(industry_data)
                
                # Create current report (all accounts)
                current_report = industry_df[['AccountId', 'EventsWithTickets', 'PaidTicketsIssued', 
                                             'Tier', 'TotalFees', 'GatewayGroup']].copy()
                current_report = current_report.sort_values('TotalFees', ascending=False)
                
                # Create new accounts report
                new_accounts_report = industry_df[industry_df['IsNewAccount']][
                    ['AccountId', 'EventsWithTickets', 'PaidTicketsIssued', 
                     'Tier', 'TotalFees', 'GatewayGroup']
                ].copy()
                new_accounts_report = new_accounts_report.sort_values('TotalFees', ascending=False)
            else:
                # Create empty DataFrames with correct structure
                empty_df = pd.DataFrame(columns=['AccountId', 'EventsWithTickets', 'PaidTicketsIssued', 
                                                'Tier', 'TotalFees', 'GatewayGroup'])
                current_report = empty_df.copy()
                new_accounts_report = empty_df.copy()
            
            # Save industry-level reports
            current_csv = current_report.to_csv(index=False)
            new_csv = new_accounts_report.to_csv(index=False)
            
            zip_file.writestr(f"{industry_folder}/{industry_folder}_current.csv", current_csv)
            zip_file.writestr(f"{industry_folder}/{industry_folder}_current_new.csv", new_csv)
            
            logger.info(f"  - Generated {industry_folder}_current.csv with {len(current_report)} accounts")
            logger.info(f"  - Generated {industry_folder}_current_new.csv with {len(new_accounts_report)} accounts")
            
            # Process sub-industries
            if 'SubIndustry' in account_df.columns:
                sub_industries = industry_accounts['SubIndustry'].unique()
                
                for sub_industry in sub_industries:
                    if pd.isna(sub_industry):
                        continue
                    
                    sub_industry_folder = sanitize_filename(sub_industry)
                    
                    # Get accounts for this sub-industry
                    sub_accounts = industry_accounts[industry_accounts['SubIndustry'] == sub_industry]
                    sub_account_ids = sub_accounts['AccountId'].unique()
                    
                    # Filter industry bookings to this sub-industry
                    sub_bookings = industry_bookings[industry_bookings['AccountId'].isin(sub_account_ids)]
                    
                    # Calculate metrics for each account in this sub-industry
                    sub_data = []
                    for account_id in sub_account_ids:
                        account_bookings = sub_bookings[sub_bookings['AccountId'] == account_id]
                        metrics = calculate_account_metrics(account_bookings, account_id)
                        
                        # Add tier and gateway group
                        metrics['Tier'] = tier_lookup.get(account_id, 'NIL')
                        
                        account_info = sub_accounts[sub_accounts['AccountId'] == account_id].iloc[0]
                        metrics['GatewayGroup'] = account_info[gateway_col] if gateway_col in account_info else 'Unknown'
                        metrics['IsNewAccount'] = account_info['IsNewAccount'] if 'IsNewAccount' in account_info else False
                        
                        # Include all accounts with any activity in the current period
                        if metrics['TotalFees'] > 0:
                            sub_data.append(metrics)
                    
                    if sub_data:  # Only create sub-industry folder if there's data
                        # Create DataFrame
                        sub_df = pd.DataFrame(sub_data)
                        
                        # Create current report
                        sub_current = sub_df[['AccountId', 'EventsWithTickets', 'PaidTicketsIssued', 
                                            'Tier', 'TotalFees', 'GatewayGroup']].copy()
                        sub_current = sub_current.sort_values('TotalFees', ascending=False)
                        
                        # Create new accounts report
                        new_accounts = sub_df[sub_df['IsNewAccount']]
                        if not new_accounts.empty:
                            sub_new = new_accounts[
                                ['AccountId', 'EventsWithTickets', 'PaidTicketsIssued', 
                                 'Tier', 'TotalFees', 'GatewayGroup']
                            ].copy()
                            sub_new = sub_new.sort_values('TotalFees', ascending=False)
                        else:
                            # Create empty DataFrame with correct structure
                            sub_new = pd.DataFrame(columns=['AccountId', 'EventsWithTickets', 'PaidTicketsIssued', 
                                                           'Tier', 'TotalFees', 'GatewayGroup'])
                        
                        # Save sub-industry reports
                        sub_current_csv = sub_current.to_csv(index=False)
                        sub_new_csv = sub_new.to_csv(index=False)
                        
                        zip_file.writestr(
                            f"{industry_folder}/{sub_industry_folder}/{industry_folder}_{sub_industry_folder}_current.csv", 
                            sub_current_csv
                        )
                        zip_file.writestr(
                            f"{industry_folder}/{sub_industry_folder}/{industry_folder}_{sub_industry_folder}_current_new.csv", 
                            sub_new_csv
                        )
                        
                        logger.info(f"    - Sub-industry {sub_industry_folder}: {len(sub_current)} accounts ({len(sub_new)} new)")
        
        # Add a summary report at the root
        summary_data = []
        for industry in account_df['Industry'].unique():
            if pd.isna(industry):
                industry = 'Unspecified'
            
            industry_accounts = account_df[account_df['Industry'] == industry]
            industry_account_ids = industry_accounts['AccountId'].unique()
            industry_bookings = current_bookings[current_bookings['AccountId'].isin(industry_account_ids)]
            
            # Calculate industry totals
            total_accounts = len(industry_account_ids)
            new_accounts = industry_accounts['IsNewAccount'].sum()
            total_fees = 0.0
            
            fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
            for col in fee_columns:
                if col in industry_bookings.columns:
                    total_fees += industry_bookings[col].fillna(0).sum()
            
            summary_data.append({
                'Industry': industry,
                'TotalAccounts': total_accounts,
                'NewAccounts': new_accounts,
                'TotalFees': round(total_fees, 2)
            })
        
        summary_df = pd.DataFrame(summary_data).sort_values('TotalFees', ascending=False)
        summary_csv = summary_df.to_csv(index=False)
        zip_file.writestr('industry_summary.csv', summary_csv)
        
        logger.info(f"Added industry summary report with {len(summary_df)} industries")
    
    # Reset buffer position
    zip_buffer.seek(0)
    
    return zip_buffer


def generate_industry_revenue_csv_files(booking_df, account_df, tier_updates, report_date,
                                        reports_dir=".", account_metrics_365=None):
    """
    Generate industry revenue reports as individual CSV files.

    This creates a simplified output with just the main industry reports (no sub-industries)
    to keep the number of output files manageable.

    Args:
        booking_df: DataFrame with booking data including fees
        account_df: DataFrame with account information including Industry, SubIndustry, DateTimeCreated
        tier_updates: DataFrame with tier calculations including Current_Tier
        report_date: The report date (pd.Timestamp) to determine current period
        reports_dir: Directory to write the CSV files into (created if missing).
        account_metrics_365: Optional pre-computed per-account 365-day aggregate
            {account_id: {EventsWithTickets, PaidTicketsIssued, TotalFees}}. When
            given (warehouse path), the per-account metrics come from this dict
            instead of filtering+grouping booking_df — so booking_df can be None
            and no full frame is held. When None (combined path), metrics are
            computed from booking_df as before.

    Returns:
        List of generated CSV file paths
    """
    logger.info("Starting industry revenue CSV generation")
    os.makedirs(reports_dir, exist_ok=True)
    
    # Determine current period (last 365 days for tier calculations)
    today = pd.Timestamp.now('UTC').tz_localize(None)
    period_end = today
    period_start = today - pd.Timedelta(days=365)
    
    # Use the same period for both accounts and bookings (last 365 days)
    
    logger.info(f"Current period (365 days): {period_start.strftime('%Y-%m-%d')} to {period_end.strftime('%Y-%m-%d')}")
    
    # Ensure we have necessary columns
    if 'Industry' not in account_df.columns:
        logger.error("Industry column not found in account data")
        raise ValueError("Industry column not found in account data")
    
    # Convert AccountId to string for consistent merging
    account_df['AccountId'] = account_df['Id'].astype(str) if 'Id' in account_df.columns else account_df.index.astype(str)
    booking_df['AccountId'] = booking_df['AccountId'].astype(str)
    
    # Add gateway group from account data
    gateway_columns = ['Gateway Group', 'GatewayGroup', 'Gateway_Group']
    gateway_col = None
    for col in gateway_columns:
        if col in account_df.columns:
            gateway_col = col
            break
    
    if not gateway_col:
        logger.warning("Gateway Group column not found, will use 'Unknown'")
        account_df['GatewayGroup'] = 'Unknown'
        gateway_col = 'GatewayGroup'
    
    # Prepare tier lookup
    tier_lookup = {}
    if tier_updates is not None and not tier_updates.empty and 'Account_Id' in tier_updates.columns:
        tier_lookup = tier_updates.set_index('Account_Id')['Current_Tier'].to_dict()
    else:
        logger.warning("Tier updates not available, will use 'NIL' for all accounts")
    
    # Parse DateTimeCreated for new account identification
    account_df['DateTimeCreated'] = pd.to_datetime(account_df['DateTimeCreated'], errors='coerce', utc=True).dt.tz_localize(None)
    account_df['IsNewAccount'] = (
        (account_df['DateTimeCreated'] >= period_start) & 
        (account_df['DateTimeCreated'] <= period_end)
    )
    
    # Filter booking data to current period (last 365 days). Skipped on the
    # warehouse path, where per-account 365-day metrics arrive pre-aggregated
    # in account_metrics_365 (no full booking frame held).
    if account_metrics_365 is None:
        booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'], errors='coerce', utc=True).dt.tz_localize(None)
        current_bookings = booking_df[
            (booking_df['TransactionDate'] >= period_start) &
            (booking_df['TransactionDate'] <= period_end)
        ].copy()
        logger.info(f"Found {len(current_bookings):,} bookings in current period (last 365 days)")
    else:
        current_bookings = None
        logger.info(f"Using pre-aggregated 365-day metrics for {len(account_metrics_365):,} accounts")
    logger.info(f"Found {account_df['IsNewAccount'].sum():,} new accounts (created in last 365 days)")
    logger.info(f"Processing {account_df['Industry'].nunique()} industries")
    
    generated_files = []
    
    # Create summary report
    summary_data = []
    
    # Group accounts by industry
    for industry in account_df['Industry'].unique():
        if pd.isna(industry):
            industry = 'Unspecified'
        
        industry_folder = sanitize_filename(industry)
        logger.info(f"Processing industry: {industry}")
        
        # Get accounts for this industry
        industry_accounts = account_df[account_df['Industry'] == industry]
        industry_account_ids = industry_accounts['AccountId'].unique()
        
        # Get bookings for these accounts (combined path only)
        if account_metrics_365 is None:
            industry_bookings = current_bookings[current_bookings['AccountId'].isin(industry_account_ids)]

        # Calculate metrics for each account in this industry
        industry_data = []
        for account_id in industry_account_ids:
            if account_metrics_365 is None:
                account_bookings = industry_bookings[industry_bookings['AccountId'] == account_id]
                metrics = calculate_account_metrics(account_bookings, account_id)
            else:
                # Pre-aggregated from SQL; fall back to zeros for accounts with
                # no activity in the period (matches calculate_account_metrics).
                agg = account_metrics_365.get(account_id)
                metrics = dict(agg) if agg else {
                    'EventsWithTickets': 0, 'PaidTicketsIssued': 0, 'TotalFees': 0.0
                }
                metrics['AccountId'] = account_id

            # Add tier and gateway group
            metrics['Tier'] = tier_lookup.get(account_id, 'NIL')
            
            account_info = industry_accounts[industry_accounts['AccountId'] == account_id].iloc[0]
            metrics['GatewayGroup'] = account_info[gateway_col] if gateway_col in account_info else 'Unknown'
            metrics['IsNewAccount'] = account_info['IsNewAccount'] if 'IsNewAccount' in account_info else False
            
            # Include all accounts with any activity in the current period
            if metrics['TotalFees'] > 0:
                industry_data.append(metrics)
        
        # Create DataFrame
        if industry_data:
            industry_df = pd.DataFrame(industry_data)
            
            # Create current report (all accounts)
            current_report = industry_df[['AccountId', 'EventsWithTickets', 'PaidTicketsIssued', 
                                         'Tier', 'TotalFees', 'GatewayGroup']].copy()
            current_report = current_report.sort_values('TotalFees', ascending=False)
            
            # Create new accounts report
            new_accounts_report = industry_df[industry_df['IsNewAccount']][
                ['AccountId', 'EventsWithTickets', 'PaidTicketsIssued', 
                 'Tier', 'TotalFees', 'GatewayGroup']
            ].copy()
            new_accounts_report = new_accounts_report.sort_values('TotalFees', ascending=False)
        else:
            # Create empty DataFrames with correct structure
            empty_df = pd.DataFrame(columns=['AccountId', 'EventsWithTickets', 'PaidTicketsIssued', 
                                            'Tier', 'TotalFees', 'GatewayGroup'])
            current_report = empty_df.copy()
            new_accounts_report = empty_df.copy()
        
        # Save industry-level reports as CSV files
        current_filename = os.path.join(reports_dir, f"{industry_folder}_current_{report_date.strftime('%Y%m')}.csv")
        new_filename = os.path.join(reports_dir, f"{industry_folder}_current_new_{report_date.strftime('%Y%m')}.csv")
        
        current_report.to_csv(current_filename, index=False)
        new_accounts_report.to_csv(new_filename, index=False)
        
        generated_files.append(current_filename)
        generated_files.append(new_filename)
        
        logger.info(f"  - Saved {current_filename} with {len(current_report)} accounts")
        logger.info(f"  - Saved {new_filename} with {len(new_accounts_report)} accounts")
        
        # Add to summary
        total_fees = industry_df['TotalFees'].sum() if industry_data else 0
        summary_data.append({
            'Industry': industry,
            'TotalAccounts': len(current_report),
            'NewAccounts': len(new_accounts_report),
            'TotalFees': round(total_fees, 2)
        })
    
    # Create and save summary report
    summary_df = pd.DataFrame(summary_data).sort_values('TotalFees', ascending=False)
    summary_filename = os.path.join(reports_dir, f"industry_summary_{report_date.strftime('%Y%m')}.csv")
    summary_df.to_csv(summary_filename, index=False)
    generated_files.insert(0, summary_filename)  # Add at beginning
    
    logger.info(f"Saved industry summary report: {summary_filename}")
    
    return generated_files