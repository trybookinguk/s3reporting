"""
Industry Revenue Report Generator.

Generates revenue reports by industry and sub-industry, organized into a ZIP file structure.
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
    events_with_tickets = booking_df['EventId'].nunique()
    
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
    
    # Determine current period (the report month)
    period_start = report_date.replace(day=1)
    period_end = (period_start + pd.DateOffset(months=1)) - pd.DateOffset(days=1)
    
    logger.info(f"Current period: {period_start.strftime('%Y-%m-%d')} to {period_end.strftime('%Y-%m-%d')}")
    
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
    account_df['DateTimeCreated'] = pd.to_datetime(account_df['DateTimeCreated'], errors='coerce')
    account_df['IsNewAccount'] = (
        (account_df['DateTimeCreated'] >= period_start) & 
        (account_df['DateTimeCreated'] <= period_end)
    )
    
    # Filter booking data to current period
    booking_df['TransactionDate'] = pd.to_datetime(booking_df['TransactionDate'], errors='coerce')
    current_bookings = booking_df[
        (booking_df['TransactionDate'] >= period_start) & 
        (booking_df['TransactionDate'] <= period_end)
    ].copy()
    
    logger.info(f"Found {len(current_bookings):,} bookings in current period")
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
                        
                        # Only include accounts with activity or that are new
                        if metrics['TotalFees'] > 0 or metrics['IsNewAccount']:
                            sub_data.append(metrics)
                    
                    if sub_data:  # Only create sub-industry folder if there's data
                        # Create DataFrame
                        sub_df = pd.DataFrame(sub_data)
                        
                        # Create current report
                        sub_current = sub_df[['AccountId', 'EventsWithTickets', 'PaidTicketsIssued', 
                                            'Tier', 'TotalFees', 'GatewayGroup']].copy()
                        sub_current = sub_current.sort_values('TotalFees', ascending=False)
                        
                        # Create new accounts report
                        sub_new = sub_df[sub_df['IsNewAccount']][
                            ['AccountId', 'EventsWithTickets', 'PaidTicketsIssued', 
                             'Tier', 'TotalFees', 'GatewayGroup']
                        ].copy()
                        sub_new = sub_new.sort_values('TotalFees', ascending=False)
                        
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