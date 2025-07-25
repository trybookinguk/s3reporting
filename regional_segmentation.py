"""
Regional Segmentation Report Generator

This script generates CSV reports showing UK regional distribution of accounts and events
for client segmentation and targeted marketing campaigns.
"""

import os
import pandas as pd
from datetime import datetime
import logging
import time
from modules.uk_regional_segmentation import (
    assign_account_regions, 
    process_event_regions, 
    generate_data_quality_summary
)
from modules.utils import (
    load_accounts_data, 
    load_booking_data,
    send_html_email,
    create_html_table,
    UK_TZ
)

# Setup logging
logging.basicConfig(
    level=logging.DEBUG if os.environ.get('DEBUG_MODE') == 'true' else logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def create_summary_html(summary: dict, timestamp: str) -> str:
    """Create HTML summary for email body."""
    # Account summary table
    account_data = [
        ['Total Accounts', f"{summary['accounts']['total']:,}"],
        ['Accounts with Postcode', f"{summary['accounts']['with_postcode']:,} ({summary['accounts']['with_postcode_pct']:.1f}%)"],
        ['Accounts with Region Assigned', f"{summary['accounts']['with_region']:,} ({summary['accounts']['with_region_pct']:.1f}%)"],
        ['- From Account Postcode', f"{summary['accounts']['from_account_postcode']:,}"],
        ['- From Event Postcodes', f"{summary['accounts']['from_event_postcodes']:,}"],
        ['Accounts without Region', f"{summary['accounts']['without_region']:,}"]
    ]
    account_df = pd.DataFrame(account_data, columns=['Metric', 'Value'])
    
    # Event summary table
    event_data = [
        ['Total Events', f"{summary['events']['total']:,}"],
        ['Events with Postcode', f"{summary['events']['with_postcode']:,} ({summary['events']['with_postcode_pct']:.1f}%)"],
        ['Events with Valid Region', f"{summary['events']['with_region']:,} ({summary['events']['with_region_pct']:.1f}%)"]
    ]
    event_df = pd.DataFrame(event_data, columns=['Metric', 'Value'])
    
    # Regional distribution table (sorted by count)
    region_data = [[region, count] for region, count in summary['regional_distribution'].items()]
    region_df = pd.DataFrame(region_data, columns=['Region', 'Account Count'])
    region_df = region_df.sort_values('Account Count', ascending=False)
    
    # Calculate percentages
    total_with_region = summary['accounts']['with_region']
    if total_with_region > 0:
        region_df['Percentage'] = (region_df['Account Count'] / total_with_region * 100).round(1).astype(str) + '%'
    else:
        region_df['Percentage'] = '0%'
    
    html = f"""<div style="font-family: Arial, sans-serif; font-size: 11pt;">
<p>Hi Alex,</p>

<p>Please find attached the regional segmentation reports generated on {timestamp}.</p>

<h3>Account Data Quality Summary</h3>
{create_html_table(account_df)}

<h3>Event Data Quality Summary</h3>
{create_html_table(event_df)}

<h3>Regional Distribution (Accounts with Assigned Regions)</h3>
{create_html_table(region_df)}

<p>The attached CSV files contain:</p>
<ul>
<li><strong>account_regional_report_{timestamp}.csv</strong> - All accounts with their assigned regions</li>
<li><strong>event_regional_report_{timestamp}.csv</strong> - All events with their postcode areas and regions</li>
</ul>

<p>Best regards,<br>
TryBooking Reporting System</p>
</div>"""
    
    return html


def create_summary_text(summary: dict, timestamp: str) -> str:
    """Create plain text summary for email."""
    text = f"""Hi Alex,

Please find attached the regional segmentation reports generated on {timestamp}.

ACCOUNT DATA QUALITY SUMMARY
============================
Total Accounts: {summary['accounts']['total']:,}
Accounts with Postcode: {summary['accounts']['with_postcode']:,} ({summary['accounts']['with_postcode_pct']:.1f}%)
Accounts with Region Assigned: {summary['accounts']['with_region']:,} ({summary['accounts']['with_region_pct']:.1f}%)
- From Account Postcode: {summary['accounts']['from_account_postcode']:,}
- From Event Postcodes: {summary['accounts']['from_event_postcodes']:,}
Accounts without Region: {summary['accounts']['without_region']:,}

EVENT DATA QUALITY SUMMARY
=========================
Total Events: {summary['events']['total']:,}
Events with Postcode: {summary['events']['with_postcode']:,} ({summary['events']['with_postcode_pct']:.1f}%)
Events with Valid Region: {summary['events']['with_region']:,} ({summary['events']['with_region_pct']:.1f}%)

REGIONAL DISTRIBUTION
====================
"""
    
    # Add regional distribution
    for region, count in sorted(summary['regional_distribution'].items(), 
                               key=lambda x: x[1], reverse=True):
        if summary['accounts']['with_region'] > 0:
            pct = count / summary['accounts']['with_region'] * 100
            text += f"{region}: {count:,} ({pct:.1f}%)\n"
        else:
            text += f"{region}: {count:,} (0%)\n"
    
    text += """
The attached CSV files contain:
- account_regional_report_*.csv - All accounts with their assigned regions
- event_regional_report_*.csv - All events with their postcode areas and regions

Best regards,
TryBooking Reporting System
"""
    
    return text


def main():
    """Main function to generate regional segmentation reports."""
    logger.info("="*60)
    logger.info("Starting Regional Segmentation Analysis")
    logger.info("="*60)
    
    start_time = time.time()
    
    try:
        # Load data
        logger.info("Loading accounts data...")
        accounts_df = load_accounts_data()
        logger.info(f"Loaded {len(accounts_df):,} accounts")
        
        logger.info("Loading booking data...")
        booking_df = load_booking_data()
        logger.info(f"Loaded {len(booking_df):,} booking records")
        
        # Get unique events with their details
        logger.info("Extracting unique events...")
        events_df = booking_df[[
            'EventId', 'EventName', 'AccountId', 'AccountName', 
            'Industry', 'SubIndustry', 'EventPostcode'
        ]].drop_duplicates(subset=['EventId'])
        logger.info(f"Found {len(events_df):,} unique events")
        
        # Process regions
        logger.info("\nAssigning regions to accounts...")
        accounts_with_regions = assign_account_regions(accounts_df, events_df)
        
        logger.info("\nProcessing event regions...")
        events_with_regions = process_event_regions(events_df)
        
        # Generate timestamp for filenames
        timestamp = datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')
        
        # Create account report
        logger.info("\nCreating account regional report...")
        account_report = accounts_with_regions[[
            'AccountId', 'AccountName', 'Industry', 'SubIndustry', 
            'Region', 'Has_Postcode'
        ]].copy()
        
        # Convert boolean to Yes/No for clarity
        account_report['Has_Postcode'] = account_report['Has_Postcode'].map({True: 'Yes', False: 'No'})
        
        # Sort by region then account name
        account_report = account_report.sort_values(['Region', 'AccountName'])
        
        account_filename = f'account_regional_report_{timestamp}.csv'
        account_report.to_csv(account_filename, index=False)
        logger.info(f"Saved account report: {account_filename} ({len(account_report):,} rows)")
        
        # Create event report
        logger.info("\nCreating event regional report...")
        event_report = events_with_regions[[
            'EventId', 'EventName', 'AccountId', 'AccountName', 
            'Industry', 'SubIndustry', 'PostcodeArea', 'Region'
        ]].copy()
        
        # Sort by region then event name
        event_report = event_report.sort_values(['Region', 'EventName'])
        
        event_filename = f'event_regional_report_{timestamp}.csv'
        event_report.to_csv(event_filename, index=False)
        logger.info(f"Saved event report: {event_filename} ({len(event_report):,} rows)")
        
        # Generate summary
        logger.info("\nGenerating data quality summary...")
        summary = generate_data_quality_summary(accounts_with_regions, events_with_regions)
        
        # Create email content
        html_content = create_summary_html(summary, datetime.now(UK_TZ).strftime('%d %B %Y at %H:%M'))
        plain_text = create_summary_text(summary, datetime.now(UK_TZ).strftime('%d %B %Y at %H:%M'))
        
        # Read CSV files for attachments
        attachments = []
        
        with open(account_filename, 'rb') as f:
            account_data = f.read()
            attachments.append((account_filename, account_data, 'text', 'csv'))
        
        with open(event_filename, 'rb') as f:
            event_data = f.read()
            attachments.append((event_filename, event_data, 'text', 'csv'))
        
        # Send email
        logger.info("\nSending email report...")
        send_html_email(
            to='alex@trybooking.co.uk',
            subject=f'Regional Segmentation Reports - {datetime.now(UK_TZ).strftime("%B %Y")}',
            html_content=html_content,
            plain_text=plain_text,
            attachments=attachments
        )
        
        elapsed_time = time.time() - start_time
        logger.info(f"\nRegional segmentation analysis complete in {elapsed_time:.1f} seconds!")
        logger.info("="*60)
        
    except Exception as e:
        logger.error(f"Error during regional segmentation: {str(e)}", exc_info=True)
        raise


if __name__ == '__main__':
    main()