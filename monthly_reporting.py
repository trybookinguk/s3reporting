#!/usr/bin/env python3
"""
Monthly reporting script for TryBooking UK.
Runs on the first of each month to report on the previous month's performance.
"""
import os
import sys
import time
import json
import pandas as pd
from datetime import datetime

# Import shared modules
from modules.utils.config import TEST_MODE, UK_TZ
from modules.utils.data_loader import get_s3_client, download_s3_file_cached
from modules.utils.date_utils import get_last_month_dates, get_ytd_dates
from modules.utils.data_loader import load_accounts_data, load_booking_data, filter_successful_transactions
from modules.utils.email_utils import send_html_email
from modules.utils.metrics_calculator import (
    calculate_yoy_change, calculate_percentage, calculate_transaction_metrics,
    calculate_fee_metrics, filter_date_range
)
from modules.utils.industry_utils import (
    calculate_industry_breakdown, prepare_booking_data_with_industry,
    format_industry_metrics_table
)
from modules.utils.validation import validate_environment_variables
from modules.utils.performance import timer_decorator


def load_account_targets():
    """Load account targets from JSON file."""
    targets_file = os.path.join(os.path.dirname(__file__), 'account_targets.json')
    try:
        with open(targets_file, 'r') as f:
            data = json.load(f)
            print(f"Loaded targets from {targets_file}")
            return data.get('targets', {})
    except FileNotFoundError:
        print(f"Warning: account_targets.json not found at {targets_file}")
        return {}
    except json.JSONDecodeError:
        print(f"Warning: account_targets.json is invalid at {targets_file}")
        return {}


def get_target_for_month(targets, year, month_name):
    """Get target for a specific month."""
    year_str = str(year)
    if year_str in targets and 'monthly' in targets[year_str]:
        target = targets[year_str]['monthly'].get(month_name)
        if target is not None:
            try:
                return int(target)
            except (ValueError, TypeError):
                return None
    return None


def calculate_ytd_cumulative(targets, year, current_month_name):
    """Calculate cumulative YTD target up to and including the specified month."""
    year_str = str(year)
    if year_str not in targets or 'monthly' not in targets[year_str]:
        return None
    
    months = ['January', 'February', 'March', 'April', 'May', 'June', 
              'July', 'August', 'September', 'October', 'November', 'December']
    
    cumulative = 0
    for month in months:
        month_target = targets[year_str]['monthly'].get(month)
        if month_target:
            try:
                cumulative += int(month_target)
            except (ValueError, TypeError):
                pass
        
        if month == current_month_name:
            break
    
    return cumulative if cumulative > 0 else None


@timer_decorator
def calculate_metrics(accounts_df, booking_df, booking_all_df, dates):
    """Calculate all required metrics."""
    metrics = {}
    
    # The gateway column is 'GatewayGroup' (PascalCase, no space)
    gateway_column = 'GatewayGroup' if 'GatewayGroup' in booking_df.columns else None
    if gateway_column:
        print(f"Using gateway column: '{gateway_column}'")
    else:
        print("Warning: 'GatewayGroup' column not found in booking data")
    
    # 1. Total new accounts for last month
    # Ensure DateTimeCreated is datetime
    accounts_df['DateTimeCreated'] = pd.to_datetime(accounts_df['DateTimeCreated'], errors='coerce')
    
    # Debug timezone info
    if os.environ.get('DEBUG_MODE'):
        print(f"\nDEBUG: DateTimeCreated column analysis:")
        print(f"  Original dtype: {accounts_df['DateTimeCreated'].dtype}")
        sample_dates = accounts_df['DateTimeCreated'].head(3)
        print(f"  Sample values (first 3):")
        for i, dt in enumerate(sample_dates):
            print(f"    {i}: {dt} (type: {type(dt)})")
    
    # Handle timezone - the S3 data is in UTC
    if accounts_df['DateTimeCreated'].dt.tz is None:
        # Data is timezone-naive, assume UTC and convert to Europe/London
        if os.environ.get('DEBUG_MODE'):
            print(f"  Timezone is None, localizing as UTC then converting to London")
        accounts_df['DateTimeCreated'] = accounts_df['DateTimeCreated'].dt.tz_localize('UTC').dt.tz_convert('Europe/London')
    else:
        # Data has timezone, convert to Europe/London
        if os.environ.get('DEBUG_MODE'):
            print(f"  Timezone exists: {accounts_df['DateTimeCreated'].dt.tz}, converting to London")
        accounts_df['DateTimeCreated'] = accounts_df['DateTimeCreated'].dt.tz_convert('Europe/London')
    
    if os.environ.get('DEBUG_MODE'):
        print(f"  After conversion - timezone: {accounts_df['DateTimeCreated'].dt.tz}")
        print(f"  Sample after conversion: {accounts_df['DateTimeCreated'].iloc[0]}")
    
    # Filter for accounts created in the last month
    last_month_accounts = filter_date_range(
        accounts_df, 'DateTimeCreated', 
        dates['last_month_start'], dates['last_month_end']
    )
    
    # Debug: Print detailed account information
    if os.environ.get('DEBUG_MODE'):
        print(f"\nDEBUG: Date filtering details:")
        print(f"  Filter start: {dates['last_month_start']}")
        print(f"  Filter end: {dates['last_month_end']}")
        print(f"  Total accounts in file: {len(accounts_df)}")
        print(f"  Accounts in date range: {len(last_month_accounts)}")
        
        # Show accounts at the boundaries
        print(f"\nDEBUG: Boundary analysis:")
        
        # Get accounts just before and after the month
        before_month = accounts_df[accounts_df['DateTimeCreated'] < dates['last_month_start']]
        after_month = accounts_df[accounts_df['DateTimeCreated'] > dates['last_month_end']]
        
        # Show last 5 accounts before the month
        if len(before_month) > 0:
            print(f"\n  Last 5 accounts BEFORE {dates['month_name']}:")
            for idx, acc in before_month.tail(5).iterrows():
                print(f"    ID: {acc.get('Id', 'N/A'):8} Created: {acc['DateTimeCreated']}")
        
        # Show first and last 5 accounts in the month
        if len(last_month_accounts) > 0:
            print(f"\n  First 5 accounts IN {dates['month_name']}:")
            for idx, acc in last_month_accounts.head(5).iterrows():
                print(f"    ID: {acc.get('Id', 'N/A'):8} Created: {acc['DateTimeCreated']}")
            
            if len(last_month_accounts) > 10:
                print(f"\n  ... ({len(last_month_accounts) - 10} accounts omitted) ...")
            
            print(f"\n  Last 5 accounts IN {dates['month_name']}:")
            for idx, acc in last_month_accounts.tail(5).iterrows():
                print(f"    ID: {acc.get('Id', 'N/A'):8} Created: {acc['DateTimeCreated']}")
        
        # Show first 5 accounts after the month
        if len(after_month) > 0:
            print(f"\n  First 5 accounts AFTER {dates['month_name']}:")
            for idx, acc in after_month.head(5).iterrows():
                print(f"    ID: {acc.get('Id', 'N/A'):8} Created: {acc['DateTimeCreated']}")
        
        # Check for timezone edge cases
        print(f"\nDEBUG: Timezone check:")
        print(f"  First account date: {last_month_accounts['DateTimeCreated'].min() if len(last_month_accounts) > 0 else 'None'}")
        print(f"  Last account date: {last_month_accounts['DateTimeCreated'].max() if len(last_month_accounts) > 0 else 'None'}")
        print(f"  Date range spans: {(last_month_accounts['DateTimeCreated'].max() - last_month_accounts['DateTimeCreated'].min()).days if len(last_month_accounts) > 0 else 0} days")
        
        # If requested, show ALL accounts in the month
        if os.environ.get('DEBUG_SHOW_ALL_ACCOUNTS'):
            print(f"\nDEBUG: ALL {len(last_month_accounts)} accounts in {dates['month_name']}:")
            sorted_accounts = last_month_accounts.sort_values('DateTimeCreated')
            for i, (idx, acc) in enumerate(sorted_accounts.iterrows(), 1):
                print(f"  {i:3d}. ID: {acc.get('Id', 'N/A'):8} Created: {acc['DateTimeCreated']}")
    
    metrics['total_new_accounts'] = len(last_month_accounts)
    
    # Store debug info in metrics for optional reporting
    metrics['_debug_total_accounts'] = len(accounts_df)
    metrics['_debug_date_range'] = f"{dates['last_month_start'].date()} to {dates['last_month_end'].date()}"
    
    # Load and compare against targets
    targets = load_account_targets()
    if targets:
        # Get target for the reporting month
        report_year = dates['last_month_start'].year
        report_month_name = dates['month_only']  # Use month name without year
        
        print(f"Looking for target: Year={report_year}, Month={report_month_name}")
        month_target = get_target_for_month(targets, report_year, report_month_name)
        if month_target:
            metrics['monthly_target'] = month_target
            metrics['monthly_target_achieved'] = metrics['total_new_accounts']
            metrics['monthly_target_variance'] = metrics['total_new_accounts'] - month_target
            metrics['monthly_target_percentage'] = calculate_percentage(
                metrics['total_new_accounts'], month_target
            )
        
        # Calculate YTD actual accounts up to the end of the reporting month
        ytd_start = dates['last_month_start'].replace(month=1, day=1)
        ytd_end = dates['last_month_end']
        
        ytd_accounts = filter_date_range(
            accounts_df, 'DateTimeCreated',
            ytd_start,
            ytd_end
        )
        metrics['ytd_new_accounts'] = len(ytd_accounts)
        
        # Get YTD target based on reporting month (using month name without year)
        ytd_target = calculate_ytd_cumulative(targets, report_year, report_month_name)
        if ytd_target:
            metrics['ytd_target'] = ytd_target
            metrics['ytd_target_achieved'] = metrics['ytd_new_accounts']
            metrics['ytd_target_variance'] = metrics['ytd_new_accounts'] - ytd_target
            metrics['ytd_target_percentage'] = calculate_percentage(
                metrics['ytd_new_accounts'], ytd_target
            )
    
    # 2. Accounts that created events last month
    # Ensure FirstEventCreation is datetime
    if 'FirstEventCreation' in last_month_accounts.columns:
        last_month_accounts['FirstEventCreation'] = pd.to_datetime(
            last_month_accounts['FirstEventCreation'], errors='coerce'
        )
    
    accounts_with_events = last_month_accounts[last_month_accounts['FirstEventCreation'].notna()]
    metrics['accounts_with_events'] = len(accounts_with_events)
    metrics['accounts_with_events_pct'] = calculate_percentage(
        metrics['accounts_with_events'], metrics['total_new_accounts']
    )
    
    # 3. Accounts that have sold tickets (present in BookingData)
    # Get unique account IDs from last month's bookings
    last_month_bookings = filter_successful_transactions(
        filter_date_range(booking_df, 'TransactionDate', 
                         dates['last_month_start'], dates['last_month_end'])
    )
    
    # Find which new accounts sold tickets
    # Ensure Id columns are the same type for matching
    last_month_accounts['Id'] = last_month_accounts['Id'].astype(str)
    booking_account_ids = last_month_bookings['AccountId'].astype(str).unique()
    
    accounts_with_sales = last_month_accounts[
        last_month_accounts['Id'].isin(booking_account_ids)
    ]
    metrics['accounts_with_sales'] = len(accounts_with_sales)
    metrics['accounts_with_sales_pct'] = calculate_percentage(
        metrics['accounts_with_sales'], metrics['total_new_accounts']
    )
    
    # 4 & 5. Transaction metrics
    transaction_metrics = calculate_transaction_metrics(last_month_bookings)
    metrics['avg_transaction_value'] = transaction_metrics['avg_amount']
    metrics['total_transactions'] = transaction_metrics['count']
    metrics['avg_tickets_per_transaction'] = transaction_metrics['avg_quantity']
    metrics['total_revenue'] = transaction_metrics['total_amount']  # Total Ticket Revenue
    
    # 6. Total fees for last month
    metrics['total_fees_last_month'] = last_month_bookings['TotalFees'].sum()
    
    # Last year same month fees (from BookingDataAll)
    last_year_month_bookings = filter_successful_transactions(
        filter_date_range(booking_all_df, 'TransactionDate',
                         dates['last_year_month_start'], dates['last_year_month_end'])
    )
    metrics['total_fees_last_year_month'] = last_year_month_bookings['TotalFees'].sum()
    
    # Calculate YoY change
    metrics['fees_yoy_change'] = calculate_yoy_change(
        metrics['total_fees_last_month'], 
        metrics['total_fees_last_year_month']
    )
    
    # 6a. Gateway Group breakdown for last month
    if gateway_column:
        # Normalize gateway groups as requested
        gateway_df = last_month_bookings.copy()
        # Handle categorical column type
        if gateway_df[gateway_column].dtype.name == 'category':
            gateway_df[gateway_column] = gateway_df[gateway_column].astype(str)
        gateway_df['Gateway_Normalized'] = gateway_df[gateway_column].fillna('Unknown')
        
        # Combine all Default gateways
        gateway_df.loc[gateway_df['Gateway_Normalized'].str.contains('Default', case=False, na=False), 'Gateway_Normalized'] = 'TryBooking Gateway'
        
        # Combine all Stripe Connect gateways
        gateway_df.loc[gateway_df['Gateway_Normalized'].str.contains('Stripe Connect', case=False, na=False), 'Gateway_Normalized'] = 'Stripe'
        
        # Calculate fees by gateway group
        gateway_breakdown = gateway_df.groupby('Gateway_Normalized')['TotalFees'].agg(['sum', 'count']).round(2)
        gateway_breakdown['percentage'] = (gateway_breakdown['sum'] / gateway_breakdown['sum'].sum() * 100).round(1)
        gateway_breakdown = gateway_breakdown.sort_values('sum', ascending=False)
        
        metrics['gateway_breakdown'] = gateway_breakdown.to_dict('index')
        
        # Also calculate for last year same month for comparison
        if gateway_column and gateway_column in last_year_month_bookings.columns:
            gateway_df_ly = last_year_month_bookings.copy()
            # Handle categorical column type
            if gateway_df_ly[gateway_column].dtype.name == 'category':
                gateway_df_ly[gateway_column] = gateway_df_ly[gateway_column].astype(str)
            gateway_df_ly['Gateway_Normalized'] = gateway_df_ly[gateway_column].fillna('Unknown')
            gateway_df_ly.loc[gateway_df_ly['Gateway_Normalized'].str.contains('Default', case=False, na=False), 'Gateway_Normalized'] = 'TryBooking Gateway'
            gateway_df_ly.loc[gateway_df_ly['Gateway_Normalized'].str.contains('Stripe Connect', case=False, na=False), 'Gateway_Normalized'] = 'Stripe'
            
            gateway_breakdown_ly = gateway_df_ly.groupby('Gateway_Normalized')['TotalFees'].sum().round(2)
            metrics['gateway_breakdown_ly'] = gateway_breakdown_ly.to_dict()
    else:
        print(f"Warning: 'GatewayGroup' column not found. Available columns: {sorted(last_month_bookings.columns)[:10]}...")
        metrics['gateway_breakdown'] = None
        metrics['gateway_breakdown_ly'] = None
    
    # 7. YTD fees
    ytd_dates = get_ytd_dates()
    
    # For YTD, we need to combine BookingDataAll + last month's BookingData
    # BookingDataAll contains data up to the 1st of the report month
    # BookingData contains the full report month's data
    # Together they give us complete YTD through the report month
    
    # Combine the datasets for YTD calculation
    combined_ytd_df = pd.concat([booking_all_df, booking_df], ignore_index=True)
    if 'BookingTransactionId' in combined_ytd_df.columns:
        combined_ytd_df = combined_ytd_df.drop_duplicates(subset=['BookingTransactionId'])
    
    # Filter for current year YTD
    ytd_bookings = filter_successful_transactions(
        filter_date_range(combined_ytd_df, 'TransactionDate',
                         ytd_dates['ytd_start'], ytd_dates['ytd_end'])
    )
    metrics['total_fees_ytd'] = ytd_bookings['TotalFees'].sum()
    
    # Last year YTD
    ytd_bookings_ly = filter_successful_transactions(
        filter_date_range(booking_all_df, 'TransactionDate',
                         ytd_dates['ytd_start_ly'], ytd_dates['ytd_end_ly'])
    )
    metrics['total_fees_ytd_ly'] = ytd_bookings_ly['TotalFees'].sum()
    
    # YTD YoY change
    metrics['fees_ytd_yoy_change'] = calculate_yoy_change(
        metrics['total_fees_ytd'],
        metrics['total_fees_ytd_ly']
    )
    
    # 7a. Industry breakdown for last month
    # Merge industry from accounts into last month's bookings
    last_month_bookings_with_industry = prepare_booking_data_with_industry(
        last_month_bookings, accounts_df
    )
    
    # Calculate industry metrics - include TotalFees instead of TicketQuantity
    industry_breakdown = calculate_industry_breakdown(
        last_month_bookings_with_industry,
        ['EventId', 'TotalFees', 'PaymentReceived']
    )
    
    if not industry_breakdown.empty:
        metrics['industry_breakdown'] = industry_breakdown.to_dict('records')
        print(f"\nIndustry breakdown calculated: {len(industry_breakdown)} industries")
    else:
        metrics['industry_breakdown'] = None
        print("\nWarning: No valid industry data found for breakdown")
    
    # 7b. YTD Gateway Group breakdown
    # Use the same gateway column name we found earlier
    if gateway_column and gateway_column in ytd_bookings.columns:
        # Normalize gateway groups for YTD
        gateway_ytd_df = ytd_bookings.copy()
        # Handle categorical column type
        if gateway_ytd_df[gateway_column].dtype.name == 'category':
            gateway_ytd_df[gateway_column] = gateway_ytd_df[gateway_column].astype(str)
        gateway_ytd_df['Gateway_Normalized'] = gateway_ytd_df[gateway_column].fillna('Unknown')
        gateway_ytd_df.loc[gateway_ytd_df['Gateway_Normalized'].str.contains('Default', case=False, na=False), 'Gateway_Normalized'] = 'TryBooking Gateway'
        gateway_ytd_df.loc[gateway_ytd_df['Gateway_Normalized'].str.contains('Stripe Connect', case=False, na=False), 'Gateway_Normalized'] = 'Stripe'
        
        gateway_ytd_breakdown = gateway_ytd_df.groupby('Gateway_Normalized')['TotalFees'].agg(['sum', 'count']).round(2)
        gateway_ytd_breakdown['percentage'] = (gateway_ytd_breakdown['sum'] / gateway_ytd_breakdown['sum'].sum() * 100).round(1)
        gateway_ytd_breakdown = gateway_ytd_breakdown.sort_values('sum', ascending=False)
        
        metrics['gateway_ytd_breakdown'] = gateway_ytd_breakdown.to_dict('index')
        
        # YTD last year for comparison
        if gateway_column and gateway_column in ytd_bookings_ly.columns:
            gateway_ytd_df_ly = ytd_bookings_ly.copy()
            # Handle categorical column type
            if gateway_ytd_df_ly[gateway_column].dtype.name == 'category':
                gateway_ytd_df_ly[gateway_column] = gateway_ytd_df_ly[gateway_column].astype(str)
            gateway_ytd_df_ly['Gateway_Normalized'] = gateway_ytd_df_ly[gateway_column].fillna('Unknown')
            gateway_ytd_df_ly.loc[gateway_ytd_df_ly['Gateway_Normalized'].str.contains('Default', case=False, na=False), 'Gateway_Normalized'] = 'TryBooking Gateway'
            gateway_ytd_df_ly.loc[gateway_ytd_df_ly['Gateway_Normalized'].str.contains('Stripe Connect', case=False, na=False), 'Gateway_Normalized'] = 'Stripe'
            
            gateway_ytd_breakdown_ly = gateway_ytd_df_ly.groupby('Gateway_Normalized')['TotalFees'].sum().round(2)
            metrics['gateway_ytd_breakdown_ly'] = gateway_ytd_breakdown_ly.to_dict()
    else:
        metrics['gateway_ytd_breakdown'] = None
        metrics['gateway_ytd_breakdown_ly'] = None
    
    return metrics


def create_md_email_content(metrics, dates):
    """Create HTML email content for MD with full financial details."""
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h2>Monthly Report for {dates['month_name']}</h2>
        
        <h2>Account Acquisition</h2>
        <h3>New Account Summary</h3>
        <ul>
            <li>Total new accounts: <strong>{metrics['total_new_accounts']:,}</strong>"""
    
    # Add monthly target comparison if available
    if metrics.get('monthly_target'):
        variance_color = 'green' if metrics['monthly_target_variance'] >= 0 else 'red'
        html_content += f""" (Target: {metrics['monthly_target']:,}, 
            <span style="color: {variance_color};">{'+' if metrics['monthly_target_variance'] >= 0 else ''}{metrics['monthly_target_variance']:,} 
            / {metrics['monthly_target_percentage']:.1f}%</span>)"""
    
    html_content += f"""</li>
            <li>Accounts that created events: <strong>{metrics['accounts_with_events']:,}</strong> ({metrics['accounts_with_events_pct']:.1f}%)</li>
            <li>Accounts that sold tickets: <strong>{metrics['accounts_with_sales']:,}</strong> ({metrics['accounts_with_sales_pct']:.1f}%)</li>"""
    
    # Add YTD progress at the bottom if available
    if metrics.get('ytd_target'):
        variance_color = 'green' if metrics['ytd_target_variance'] >= 0 else 'red'
        html_content += f"""
            <li>YTD new accounts (Jan-{dates['month_only']}): <strong>{metrics['ytd_new_accounts']:,}</strong> 
                (Target: {metrics['ytd_target']:,}, 
                <span style="color: {variance_color};">{'+' if metrics['ytd_target_variance'] >= 0 else ''}{metrics['ytd_target_variance']:,} 
                / {metrics['ytd_target_percentage']:.1f}%</span>)</li>"""
    
    html_content += f"""
        </ul>
        
        <h2>Ticket Revenue Data</h2>
        <h3>Revenue Summary</h3>
        <ul>
            <li>Average transaction value: <strong>£{metrics['avg_transaction_value']:.2f}</strong></li>
            <li>Average tickets per transaction: <strong>{metrics['avg_tickets_per_transaction']:.1f}</strong></li>
        </ul>
        
        <h2>Financial Performance</h2>
        <h3>Fee Revenue Summary</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
            <tr>
                <th>Metric</th>
                <th>This Year</th>
                <th>Last Year</th>
                <th>Change</th>
            </tr>
            <tr>
                <td>{dates['month_name']} Fees</td>
                <td>£{metrics['total_fees_last_month']:,.2f}</td>
                <td>£{metrics['total_fees_last_year_month']:,.2f}</td>
                <td>{'+' if metrics['fees_yoy_change'] > 0 else ''}{metrics['fees_yoy_change']:.1f}%</td>
            </tr>
            <tr>
                <td>YTD Fees</td>
                <td>£{metrics['total_fees_ytd']:,.2f}</td>
                <td>£{metrics['total_fees_ytd_ly']:,.2f}</td>
                <td>{'+' if metrics['fees_ytd_yoy_change'] > 0 else ''}{metrics['fees_ytd_yoy_change']:.1f}%</td>
            </tr>
        </table>
    """
    
    
    # Add Gateway breakdown if available
    if metrics.get('gateway_breakdown'):
        html_content += f"""
        <h2>Payment Gateway Analysis</h2>
        <h3>TryBooking vs Stripe - {dates['month_name']}</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
            <tr>
                <th>Gateway Group</th>
                <th>Revenue</th>
                <th>% of Total</th>
                <th>Transactions</th>"""
        
        # Add last year column if data available
        if metrics.get('gateway_breakdown_ly'):
            html_content += "<th>Last Year Revenue</th><th>YoY Change</th>"
            
        html_content += "</tr>"
        
        # Add rows for each gateway
        for gateway, data in metrics['gateway_breakdown'].items():
            revenue = data['sum']
            percentage = data['percentage']
            count = data['count']
            
            html_content += f"""
            <tr>
                <td>{gateway}</td>
                <td>£{revenue:,.2f}</td>
                <td>{percentage:.1f}%</td>
                <td>{count:,}</td>"""
            
            # Add last year comparison if available
            if metrics.get('gateway_breakdown_ly'):
                ly_revenue = metrics['gateway_breakdown_ly'].get(gateway, 0)
                if ly_revenue > 0:
                    yoy_change = calculate_yoy_change(revenue, ly_revenue)
                    html_content += f"<td>£{ly_revenue:,.2f}</td>"
                    html_content += f"<td>{'+' if yoy_change > 0 else ''}{yoy_change:.1f}%</td>"
                else:
                    html_content += "<td>-</td><td>New</td>"
            
            html_content += "</tr>"
        
        html_content += "</table>"
    
    # Add YTD Gateway breakdown if available
    if metrics.get('gateway_ytd_breakdown'):
        html_content += f"""
        <h3>TryBooking vs Stripe - YTD</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
            <tr>
                <th>Gateway Group</th>
                <th>YTD Revenue</th>
                <th>% of Total</th>
                <th>Transactions</th>"""
        
        # Add last year column if data available
        if metrics.get('gateway_ytd_breakdown_ly'):
            html_content += "<th>Last Year YTD</th><th>YoY Change</th>"
            
        html_content += "</tr>"
        
        # Add rows for each gateway
        for gateway, data in metrics['gateway_ytd_breakdown'].items():
            revenue = data['sum']
            percentage = data['percentage']
            count = data['count']
            
            html_content += f"""
            <tr>
                <td>{gateway}</td>
                <td>£{revenue:,.2f}</td>
                <td>{percentage:.1f}%</td>
                <td>{count:,}</td>"""
            
            # Add last year comparison if available
            if metrics.get('gateway_ytd_breakdown_ly'):
                ly_revenue = metrics['gateway_ytd_breakdown_ly'].get(gateway, 0)
                if ly_revenue > 0:
                    yoy_change = calculate_yoy_change(revenue, ly_revenue)
                    html_content += f"<td>£{ly_revenue:,.2f}</td>"
                    html_content += f"<td>{'+' if yoy_change > 0 else ''}{yoy_change:.1f}%</td>"
                else:
                    html_content += "<td>-</td><td>New</td>"
            
            html_content += "</tr>"
        
        html_content += "</table>"
    
    # Add Industry breakdown if available
    if metrics.get('industry_breakdown'):
        # Custom industry table with fees instead of tickets
        html_content += f"""
        <h2>Industry Analysis</h2>
        <h3>Revenue by Industry - {dates['month_name']}</h3>
        <p><em>Total industries: {len(metrics['industry_breakdown'])}</em></p>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
            <tr>
                <th>Industry</th>
                <th>Events</th>
                <th>% of Events</th>
                <th>Ticket Revenue</th>
                <th>% of Ticket Revenue</th>
                <th>TB Fees</th>
                <th>% of Fees</th>
            </tr>"""
        
        # Sort by fees descending
        industry_data = sorted(metrics['industry_breakdown'], 
                             key=lambda x: x.get('TotalFees', 0), 
                             reverse=True)
        
        # Show all industries
        for industry in industry_data:
            html_content += f"""
            <tr>
                <td>{industry['Industry']}</td>
                <td>{industry['events']:,}</td>
                <td>{industry['events_pct']:.1f}%</td>
                <td>£{industry['revenue']:,.2f}</td>
                <td>{industry['revenue_pct']:.1f}%</td>
                <td>£{industry['TotalFees']:,.2f}</td>
                <td>{industry['TotalFees_pct']:.1f}%</td>
            </tr>"""
        
        html_content += """
        </table>"""
    
    html_content += """
    </body>
    </html>
    """
    
    return html_content


def create_staff_email_content(metrics, dates):
    """Create HTML email content for general staff with limited financial details."""
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h2>Monthly Report for {dates['month_name']}</h2>
        
        <h3>New Account Summary</h3>
        <ul>
            <li>Total new accounts: <strong>{metrics['total_new_accounts']:,}</strong>"""
    
    # Add monthly target comparison if available
    if metrics.get('monthly_target'):
        variance_color = 'green' if metrics['monthly_target_variance'] >= 0 else 'red'
        html_content += f""" (Target: {metrics['monthly_target']:,}, 
            <span style="color: {variance_color};">{'+' if metrics['monthly_target_variance'] >= 0 else ''}{metrics['monthly_target_variance']:,} 
            / {metrics['monthly_target_percentage']:.1f}%</span>)"""
    
    html_content += f"""</li>"""
    
    # Add YTD progress if available
    if metrics.get('ytd_target'):
        variance_color = 'green' if metrics['ytd_target_variance'] >= 0 else 'red'
        html_content += f"""
            <li>YTD new accounts (Jan-{dates['month_only']}): <strong>{metrics['ytd_new_accounts']:,}</strong> 
                (Target: {metrics['ytd_target']:,}, 
                <span style="color: {variance_color};">{'+' if metrics['ytd_target_variance'] >= 0 else ''}{metrics['ytd_target_variance']:,} 
                / {metrics['ytd_target_percentage']:.1f}%</span>)</li>"""
    
    html_content += f"""
            <li>Accounts that created events: <strong>{metrics['accounts_with_events']:,}</strong> ({metrics['accounts_with_events_pct']:.1f}%)</li>
            <li>Accounts that sold tickets: <strong>{metrics['accounts_with_sales']:,}</strong> ({metrics['accounts_with_sales_pct']:.1f}%)</li>
        </ul>"""
    
    # Add YTD progress if target available
    if metrics.get('ytd_target'):
        progress_color = 'green' if metrics['ytd_target_percentage'] >= 100 else 'orange' if metrics['ytd_target_percentage'] >= 90 else 'red'
        html_content += f"""
        <h3>Year-to-Date Progress</h3>
        <p>YTD new accounts (Jan-{dates['month_only']}): <strong>{metrics['ytd_new_accounts']:,}</strong> of {metrics['ytd_target']:,} target 
        (<span style="color: {progress_color};">{metrics['ytd_target_percentage']:.1f}%</span>)</p>"""
    
    # Add industry breakdown table for staff (percentages only)
    if metrics.get('industry_breakdown'):
        html_content += f"""
        <h3>Industry Breakdown - {dates['month_name']}</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
            <tr>
                <th>Industry</th>
                <th>% of Events</th>
                <th>% of Revenue</th>
            </tr>"""
        
        # Sort by events percentage descending
        industry_data = sorted(metrics['industry_breakdown'], 
                             key=lambda x: x.get('events_pct', 0), 
                             reverse=True)
        
        # Show all industries
        for industry in industry_data:
            html_content += f"""
            <tr>
                <td>{industry['Industry']}</td>
                <td>{industry['events_pct']:.1f}%</td>
                <td>{industry['revenue_pct']:.1f}%</td>
            </tr>"""
        
        html_content += """
        </table>"""
    
    html_content += """
    </body>
    </html>
    """
    
    return html_content


def main():
    """Main execution function."""
    start_time = time.time()
    
    # Validate environment variables
    validate_environment_variables([
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', 'AZURE_SENDER_MAILBOX'
    ])
    
    print(f"\n=== Monthly Reporting Started at {datetime.now(UK_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    if TEST_MODE:
        print("TEST MODE: Email will be sent to henry@trybooking.co.uk only")
    
    try:
        # Initialize S3 client
        s3_client = get_s3_client()
        
        # Get date ranges
        dates = get_last_month_dates()
        print(f"\nReporting for: {dates['month_name']}")
        print(f"Comparison with: {dates['month_name_ly']}")
        
        # Load data
        print("\nLoading data from S3...")
        
        # Load accounts data for the last month
        accounts_df = load_accounts_data(s3_client, dates['last_month_end'])
        print(f"Total accounts loaded: {len(accounts_df):,}")
        
        # Load BookingData for last month (contains only last month's bookings)
        booking_df = load_booking_data(s3_client, dates['last_month_end'])
        print(f"Last month booking records loaded: {len(booking_df):,}")
        
        # Show status breakdown if in debug mode
        if os.environ.get('DEBUG_MODE') and 'Status' in booking_df.columns:
            status_counts = booking_df['Status'].value_counts()
            print("\nBooking status breakdown:")
            for status, count in status_counts.items():
                print(f"  {status}: {count:,}")
        
        # Load BookingDataAll (historical data up to 1st of current month)
        # Note: When running on Dec 1 for Nov report, this loads Nov's BookingDataAll
        # which contains data only up to Nov 1st, not the full month
        booking_all_df = load_booking_data(s3_client, dates['last_month_end'], data_type='BookingDataAll')
        print(f"Total historical booking records loaded: {len(booking_all_df):,}")
        
        # Calculate metrics
        print("\nCalculating metrics...")
        print(f"\nDate ranges being used:")
        print(f"- Report month: {dates['month_name']} ({dates['last_month_start'].date()} to {dates['last_month_end'].date()})")
        print(f"- YTD period: Jan 1 to {dates['last_month_end'].date()}")
        
        metrics = calculate_metrics(accounts_df, booking_df, booking_all_df, dates)
        
        # Print summary
        print(f"\nSummary for {dates['month_name']}:")
        print(f"- New accounts: {metrics['total_new_accounts']:,}", end="")
        if metrics.get('monthly_target'):
            variance_symbol = '+' if metrics['monthly_target_variance'] >= 0 else ''
            print(f" (Target: {metrics['monthly_target']:,}, {variance_symbol}{metrics['monthly_target_variance']:,} / {metrics['monthly_target_percentage']:.1f}%)")
        else:
            print()
        
        if os.environ.get('DEBUG_MODE'):
            print(f"  (DEBUG: Total accounts in file: {metrics.get('_debug_total_accounts', 'N/A'):,})")
            print(f"  (DEBUG: Date range checked: {metrics.get('_debug_date_range', 'N/A')})")
        
        print(f"- Accounts with events: {metrics['accounts_with_events']:,} ({metrics['accounts_with_events_pct']:.1f}%)")
        print(f"- Accounts with sales: {metrics['accounts_with_sales']:,} ({metrics['accounts_with_sales_pct']:.1f}%)")
        print(f"- Total ticket revenue: £{metrics['total_revenue']:,.2f}")
        print(f"- Total fees: £{metrics['total_fees_last_month']:,.2f} (YoY: {metrics['fees_yoy_change']:+.1f}%)")
        print(f"- YTD fees: £{metrics['total_fees_ytd']:,.2f} (YoY: {metrics['fees_ytd_yoy_change']:+.1f}%)")
        
        # Print target comparison
        if metrics.get('ytd_target'):
            print(f"\nYTD Target Comparison (Jan-{dates['month_only']}):")
            print(f"- YTD new accounts: {metrics['ytd_new_accounts']:,} of {metrics['ytd_target']:,} ({metrics['ytd_target_percentage']:.1f}%)")
            print(f"- YTD variance: {'+' if metrics['ytd_target_variance'] >= 0 else ''}{metrics['ytd_target_variance']:,}")
        
        # Print gateway breakdown if available
        if metrics.get('gateway_breakdown'):
            print(f"\nRevenue by Gateway Group - {dates['month_name']}:")
            for gateway, data in metrics['gateway_breakdown'].items():
                print(f"- {gateway}: £{data['sum']:,.2f} ({data['percentage']:.1f}%, {data['count']:,} transactions)")
                if metrics.get('gateway_breakdown_ly') and gateway in metrics['gateway_breakdown_ly']:
                    ly_revenue = metrics['gateway_breakdown_ly'][gateway]
                    yoy = calculate_yoy_change(data['sum'], ly_revenue)
                    print(f"  (Last year: £{ly_revenue:,.2f}, YoY: {yoy:+.1f}%)")
        
        if metrics.get('gateway_ytd_breakdown'):
            print(f"\nYTD Revenue by Gateway Group:")
            for gateway, data in metrics['gateway_ytd_breakdown'].items():
                print(f"- {gateway}: £{data['sum']:,.2f} ({data['percentage']:.1f}%, {data['count']:,} transactions)")
                if metrics.get('gateway_ytd_breakdown_ly') and gateway in metrics['gateway_ytd_breakdown_ly']:
                    ly_revenue = metrics['gateway_ytd_breakdown_ly'][gateway]
                    yoy = calculate_yoy_change(data['sum'], ly_revenue)
                    print(f"  (Last year YTD: £{ly_revenue:,.2f}, YoY: {yoy:+.1f}%)")
        
        # Print industry breakdown if available
        if metrics.get('industry_breakdown'):
            print(f"\nRevenue by Industry - {dates['month_name']}:")
            # Sort by fees for console output too
            industry_sorted = sorted(metrics['industry_breakdown'], 
                                   key=lambda x: x.get('TotalFees', 0), 
                                   reverse=True)
            for industry in industry_sorted[:5]:
                print(f"- {industry['Industry']}: {industry['events']:,} events, "
                      f"£{industry['revenue']:,.2f} ticket revenue, £{industry.get('TotalFees', 0):,.2f} fees "
                      f"({industry.get('TotalFees_pct', 0):.1f}% of total fees)")
            if len(metrics['industry_breakdown']) > 5:
                print(f"  ... and {len(metrics['industry_breakdown']) - 5} more industries")
        
        # Create and send MD email (full financial details)
        print("\nPreparing MD email with full financial details...")
        md_html_content = create_md_email_content(metrics, dates)
        send_html_email(
            to='henry@trybooking.co.uk' if TEST_MODE else 'joan@trybooking.co.uk, henry@trybooking.co.uk',
            cc=None,
            bcc=None,
            subject=f"Monthly Report - {dates['month_name']}",
            html_content=md_html_content
        )
        print(f"MD update email sent to: {'henry@trybooking.co.uk (TEST MODE)' if TEST_MODE else 'joan@trybooking.co.uk'}")
        
        # Create and send general staff email (limited financial details)
        print("\nPreparing general staff email (limited financial details)...")
        staff_html_content = create_staff_email_content(metrics, dates)
        send_html_email(
            to='henry@trybooking.co.uk' if TEST_MODE else ['louise@trybooking.co.uk', 'jules@trybooking.co.uk'],
            cc=None,
            bcc=None,
            subject=f"Monthly Report - {dates['month_name']}",
            html_content=staff_html_content
        )
        print(f"General staff email sent to: {'henry@trybooking.co.uk (TEST MODE)' if TEST_MODE else 'louise@trybooking.co.uk, jules@trybooking.co.uk'}")
        
        print(f"\n=== Monthly Reporting Completed in {time.time() - start_time:.1f} seconds ===")
        
    except Exception as e:
        print(f"\nERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()