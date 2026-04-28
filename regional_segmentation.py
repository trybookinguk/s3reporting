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
    UK_TZ,
    get_s3_client,
    get_latest_data_date,
    validate_environment_variables
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
        ['Total Accounts', f"{summary['accounts']['total_all']:,}"],
        ['Accounts With Events', f"{summary['accounts']['with_events']:,} ({summary['accounts']['with_events_pct']:.1f}%)"],
        ['Accounts Analysed', f"{summary['accounts']['analyzed']:,} ({summary['accounts']['analyzed_pct']:.1f}%)"],
        ['  (Have events or postcodes)', ''],
        ['', ''],  # Blank row for separation
        ['Of Accounts Analysed:', ''],
        ['- Have Account Postcode', f"{summary['accounts']['with_postcode']:,} ({summary['accounts']['with_postcode_pct']:.1f}%)"],
        ['- Have Region Assigned', f"{summary['accounts']['with_region']:,} ({summary['accounts']['with_region_pct']:.1f}%)"],
        ['  • From Account Postcode', f"{summary['accounts']['from_account_postcode']:,}"],
        ['  • From Event Postcodes', f"{summary['accounts']['from_event_postcodes']:,}"],
        ['- Without Region', f"{summary['accounts']['without_region']:,}"]
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
    
    # Calculate percentages based on total analyzed
    total_analyzed = summary['accounts']['analyzed']
    if total_analyzed > 0:
        region_df['Percentage'] = (region_df['Account Count'] / total_analyzed * 100).round(1).astype(str) + '%'
    else:
        region_df['Percentage'] = '0%'
    
    # Create postcode area breakdown table (top 20)
    postcode_data = [[area, count] for area, count in summary.get('postcode_area_distribution', {}).items()]
    if postcode_data:
        postcode_df = pd.DataFrame(postcode_data, columns=['Postcode Area', 'Account Count'])
        postcode_df = postcode_df.sort_values('Account Count', ascending=False).head(20)
        postcode_html = f"<h3>Top 20 Postcode Areas (Accounts with Postcodes)</h3>\n{create_html_table(postcode_df)}"
    else:
        postcode_html = "<p><em>No postcode area data available</em></p>"
    
    html = f"""<div style="font-family: Arial, sans-serif; font-size: 11pt;">
<p>Hi Alex,</p>

<p>Please find attached the regional segmentation reports generated on {timestamp}.</p>

<h3>Account Data Quality Summary</h3>
{create_html_table(account_df)}

<h3>Event Data Quality Summary</h3>
{create_html_table(event_df)}

<h3>Regional Distribution (All Analysed Accounts)</h3>
{create_html_table(region_df)}

{postcode_html}

<p>The attached CSV files contain:</p>
<ul>
<li><strong>account_regional_report_*.csv</strong> - Accounts with events or postcodes and their assigned regions</li>
<li><strong>event_regional_report_*.csv</strong> - All events with their postcode areas and regions</li>
<li><strong>accounts_without_region_*.csv</strong> - Analysed accounts with no determinable region (if any)</li>
<li><strong>postcode_area_summary_*.csv</strong> - Valid UK postcode areas with account and event counts</li>
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
Total Accounts: {summary['accounts']['total_all']:,}
Accounts With Events: {summary['accounts']['with_events']:,} ({summary['accounts']['with_events_pct']:.1f}%)
Accounts Analysed: {summary['accounts']['analyzed']:,} ({summary['accounts']['analyzed_pct']:.1f}%)
  (Have events or postcodes)

Of Accounts Analysed:
- Have Account Postcode: {summary['accounts']['with_postcode']:,} ({summary['accounts']['with_postcode_pct']:.1f}%)
- Have Region Assigned: {summary['accounts']['with_region']:,} ({summary['accounts']['with_region_pct']:.1f}%)
  • From Account Postcode: {summary['accounts']['from_account_postcode']:,}
  • From Event Postcodes: {summary['accounts']['from_event_postcodes']:,}
- Without Region: {summary['accounts']['without_region']:,}

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
        if summary['accounts']['analyzed'] > 0:
            pct = count / summary['accounts']['analyzed'] * 100
            text += f"{region}: {count:,} ({pct:.1f}%)\n"
        else:
            text += f"{region}: {count:,} (0%)\n"
    
    text += "\nTOP 20 POSTCODE AREAS\n"
    text += "====================\n"
    
    # Add top postcode areas
    postcode_areas = sorted(summary.get('postcode_area_distribution', {}).items(), 
                          key=lambda x: x[1], reverse=True)[:20]
    for area, count in postcode_areas:
        text += f"{area}: {count:,}\n"
    
    text += """
The attached CSV files contain:
- account_regional_report_*.csv - Accounts with events or postcodes and their assigned regions
- event_regional_report_*.csv - All events with their postcode areas and regions
- accounts_without_region_*.csv - Analysed accounts with no determinable region
- postcode_area_summary_*.csv - Valid UK postcode areas with account and event counts

Best regards,
TryBooking Reporting System
"""
    
    return text


def main():
    """Main function to generate regional segmentation reports."""
    # Validate environment variables
    validate_environment_variables([
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', 'AZURE_SENDER_MAILBOX'
    ])
    
    logger.info("="*60)
    logger.info("Starting Regional Segmentation Analysis")
    logger.info("="*60)
    
    start_time = time.time()
    
    try:
        # Initialize S3 client
        s3_client = get_s3_client()
        latest_date = get_latest_data_date()
        
        # Load data
        logger.info("Loading accounts data...")
        accounts_df = load_accounts_data(s3_client, latest_date)
        logger.info(f"Loaded {len(accounts_df):,} accounts")
        
        logger.info("Loading booking data...")
        # Use BookingDataAll to get all historical events for comprehensive regional analysis
        booking_df = load_booking_data(s3_client, latest_date, data_type='BookingDataAll')
        logger.info(f"Loaded {len(booking_df):,} booking records")
        
        # Get unique events with their details
        logger.info("Extracting unique events...")
        events_df = booking_df[[
            'EventId', 'EventName', 'AccountId', 'AccountName', 
            'Industry', 'SubIndustry', 'EventPostcode'
        ]].drop_duplicates(subset=['EventId'])
        logger.info(f"Found {len(events_df):,} unique events")
        
        # Get accounts that have issued tickets (have events in booking data)
        accounts_with_events_ids = booking_df['AccountId'].unique()
        logger.info(f"Found {len(accounts_with_events_ids):,} accounts with events")
        
        # Also include accounts that have postcodes but may not have events yet
        accounts_with_postcodes = accounts_df[accounts_df['Postcode'].notna() & (accounts_df['Postcode'] != '')]
        logger.info(f"Found {len(accounts_with_postcodes):,} accounts with postcodes")
        
        # Combine: accounts with events OR accounts with postcodes
        accounts_to_analyze = accounts_df[
            (accounts_df['Id'].isin(accounts_with_events_ids)) | 
            (accounts_df['Postcode'].notna() & (accounts_df['Postcode'] != ''))
        ]
        logger.info(f"Total accounts to analyze: {len(accounts_to_analyze):,} (with events or postcodes)")
        
        # Keep track of counts for reporting
        total_accounts_count = len(accounts_df)
        accounts_with_events_count = len(accounts_df[accounts_df['Id'].isin(accounts_with_events_ids)])
        
        # Process regions for accounts with events or postcodes
        logger.info("\nAssigning regions to accounts...")
        accounts_with_regions = assign_account_regions(accounts_to_analyze, events_df)
        
        logger.info("\nProcessing event regions...")
        events_with_regions = process_event_regions(events_df)
        
        # Generate timestamp for filenames
        timestamp = datetime.now(UK_TZ).strftime('%Y%m%d_%H%M%S')
        
        # Create account report
        logger.info("\nCreating account regional report...")
        account_report = accounts_with_regions[[
            'Id', 'AccountName', 'Industry', 'SubIndustry', 
            'Region', 'Has_Postcode'
        ]].copy()
        
        # Rename Id to AccountId for clarity in the report
        account_report = account_report.rename(columns={'Id': 'AccountId'})
        
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
        
        # Generate summary first to get postcode distribution
        logger.info("\nGenerating data quality summary...")
        summary = generate_data_quality_summary(accounts_with_regions, events_with_regions, 
                                              total_accounts_count, accounts_with_events_count)
        
        # Create postcode area summary report
        logger.info("\nCreating postcode area summary report...")
        if 'postcode_area_distribution' in summary:
            # Get all unique postcode areas from both accounts and events
            all_areas = set(summary['postcode_area_distribution'].keys()) | set(summary.get('event_postcode_distribution', {}).keys())
            
            postcode_area_data = []
            for area in sorted(all_areas):
                account_count = summary['postcode_area_distribution'].get(area, 0)
                event_count = summary.get('event_postcode_distribution', {}).get(area, 0)
                postcode_area_data.append({
                    'Postcode Area': area,
                    'Account Count': account_count,
                    'Event Count': event_count
                })
            
            # Sort by account count (primary), then event count (secondary)
            postcode_area_data.sort(key=lambda x: (x['Account Count'], x['Event Count']), reverse=True)
            
            postcode_area_df = pd.DataFrame(postcode_area_data)
            postcode_area_filename = f'postcode_area_summary_{timestamp}.csv'
            postcode_area_df.to_csv(postcode_area_filename, index=False)
            logger.info(f"Saved postcode area summary: {postcode_area_filename} ({len(postcode_area_df):,} valid UK postcode areas)")
        else:
            postcode_area_filename = None
            logger.info("No postcode area data - skipping postcode area summary")
        
        # Create report for accounts without regions
        logger.info("\nCreating accounts without region report...")
        accounts_without_region = accounts_with_regions[accounts_with_regions['Region'] == 'Unknown'].copy()
        
        if len(accounts_without_region) > 0:
            # Select relevant columns and rename for clarity
            unknown_report = accounts_without_region[[
                'Id', 'AccountName', 'Industry', 'SubIndustry', 'Has_Postcode'
            ]].copy()
            unknown_report = unknown_report.rename(columns={'Id': 'AccountId'})
            # Check if accounts have events to provide better reason
            unknown_report['Has_Events'] = unknown_report['AccountId'].isin(accounts_with_events_ids)
            unknown_report['Has_Postcode'] = unknown_report['Has_Postcode'].map({True: 'Yes', False: 'No'})
            
            # Determine reason based on both flags
            def get_reason(row):
                if row['Has_Postcode'] == 'Yes':
                    return 'Invalid/Unrecognised Postcode'
                elif row['Has_Events']:
                    return 'No Postcode in Account or Events'
                else:
                    return 'No Events Created Yet'
            
            unknown_report['Reason'] = unknown_report.apply(get_reason, axis=1)
            unknown_report = unknown_report.drop('Has_Events', axis=1)  # Remove temporary column
            
            # Sort by account name
            unknown_report = unknown_report.sort_values('AccountName')
            
            unknown_filename = f'accounts_without_region_{timestamp}.csv'
            unknown_report.to_csv(unknown_filename, index=False)
            logger.info(f"Saved unknown regions report: {unknown_filename} ({len(unknown_report):,} rows)")
        else:
            unknown_filename = None
            logger.info("No accounts without regions - skipping unknown regions report")
        
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
        
        # Add unknown regions report if it exists
        if unknown_filename:
            with open(unknown_filename, 'rb') as f:
                unknown_data = f.read()
                attachments.append((unknown_filename, unknown_data, 'text', 'csv'))
        
        # Add postcode area summary if it exists
        if postcode_area_filename:
            with open(postcode_area_filename, 'rb') as f:
                postcode_data = f.read()
                attachments.append((postcode_area_filename, postcode_data, 'text', 'csv'))
        
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