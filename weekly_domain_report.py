#!/usr/bin/env python3
"""
Weekly domain report script for TryBooking UK.
Extracts email domains from user data and sends CSV report to marketing/data team.
"""
import os
import pandas as pd

# Import shared modules
from modules.utils.config import TEST_MODE, get_recipients

# Check if email sending is enabled (GitHub Actions boolean)
SEND_EMAILS = os.environ.get("SEND_EMAILS", "true").lower() == "true"
from modules.utils.data_loader import load_users
from modules.utils.date_utils import get_week_dates, get_latest_data_date
from modules.utils.email_utils import send_html_email_with_attachments
from modules.utils.validation import validate_environment_variables
from modules.utils.performance import timer_decorator


@timer_decorator
def extract_email_domains(df):
    """Extract unique email domains from Username column."""
    # Ensure Username column exists (column 4 as per requirement)
    if 'Username' not in df.columns:
        # Try to use the 4th column (0-indexed would be column 3)
        if len(df.columns) > 3:
            username_col = df.columns[3]
            df['Username'] = df[username_col]
        else:
            raise ValueError("Username column not found in data")
    
    # Extract domain from email addresses (everything after @)
    df['EmailDomain'] = df['Username'].str.split('@').str[-1]
    
    # Filter out rows without valid domains (no @ symbol would result in the full username)
    df = df[df['Username'].str.contains('@', na=False)]
    
    # Get unique domains only (no counts)
    unique_domains = df['EmailDomain'].unique()
    
    # Sort alphabetically
    unique_domains = sorted(unique_domains)
    
    # Return as DataFrame with single column
    return pd.DataFrame({'Domain': unique_domains})


@timer_decorator
def generate_domain_report():
    """Generate weekly domain report from user data."""
    print("Starting weekly domain report generation...")
    
    try:
        # Load the latest user data
        target_date = get_latest_data_date()
        print(f"Loading user data for {target_date}...")
        
        users_df = load_users(target_date)
        print(f"Loaded {len(users_df)} user records")
        
        # Extract email domains
        domain_report = extract_email_domains(users_df)
        print(f"Extracted {len(domain_report)} unique domains")
        
        # Generate report dates for email subject
        week_start, week_end = get_week_dates()
        
        
        # Prepare email
        subject = f"Weekly Email Domain Report - {week_start.strftime('%d %b')} to {week_end.strftime('%d %b %Y')}"
        
        # Simple HTML body
        html_body = f"""
        <html>
        <body>
            <p>Please find attached the weekly email domain report.</p>
            <p>Data date: {target_date.strftime('%d %B %Y')}</p>
        </body>
        </html>
        """
        
        # Recipients are managed in modules/utils/config.py → DISTRIBUTION_LISTS["weekly_domain"]
        recipients, _ = get_recipients("weekly_domain")
        if TEST_MODE:
            print("TEST MODE: Sending to test recipients only")

        # Save CSV to temporary file for attachment
        temp_csv_path = f'/tmp/email_domains_{target_date.strftime("%Y%m%d")}.csv'
        domain_report.to_csv(temp_csv_path, index=False)

        # Send email if enabled
        if SEND_EMAILS:
            send_html_email_with_attachments(
                to=recipients,
                subject=subject,
                html_content=html_body,
                attachments=[temp_csv_path]
            )
            print(f"Report sent successfully to {recipients}")
        else:
            print("Email sending disabled - report generated but not sent")
        
        # Also save locally for debugging if needed
        if TEST_MODE:
            local_filename = f'email_domains_{target_date.strftime("%Y%m%d")}.csv'
            domain_report.to_csv(local_filename, index=False)
            print(f"Report also saved locally as {local_filename}")
        
        return domain_report
        
    except Exception as e:
        print(f"Error generating domain report: {e}")
        raise


def main():
    """Main entry point."""
    # Always require AWS credentials
    required_vars = [
        'AWS_ACCESS_KEY_ID',
        'AWS_SECRET_ACCESS_KEY'
    ]
    
    # Only require email credentials if sending emails
    if SEND_EMAILS:
        required_vars.extend([
            'AZURE_TENANT_ID',
            'AZURE_CLIENT_ID',
            'AZURE_CLIENT_SECRET',
            'AZURE_SENDER_MAILBOX',
        ])
    
    validate_environment_variables(required_vars)
    
    # Generate and send the report
    generate_domain_report()
    
    print("Weekly domain report completed successfully")


if __name__ == "__main__":
    main()