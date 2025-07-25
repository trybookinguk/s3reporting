"""
Generic email utilities for TryBooking reports.
"""
import smtplib
from email.message import EmailMessage
from .config import (
    MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD,
    MAILGUN_DOMAIN, SMTP_HOST, SMTP_PORT, TEST_MODE
)


def send_html_email(to, subject, html_content, cc=None, bcc=None, plain_text=None, attachments=None):
    """
    Send an HTML email via Mailgun SMTP.
    
    Args:
        to: Recipient email address(es) - string or list
        subject: Email subject (will be prefixed with [TEST] in test mode)
        html_content: HTML body content
        cc: CC recipients - string or list (optional)
        bcc: BCC recipients - string or list (optional)
        plain_text: Plain text alternative (optional, auto-generated if not provided)
        attachments: List of tuples (filename, content, maintype, subtype) (optional)
    
    Returns:
        None
    """
    msg = EmailMessage()
    msg['From'] = f"TryBooking Reporting <reports@{MAILGUN_DOMAIN}>"
    
    # Handle list of recipients
    if isinstance(to, list):
        msg['To'] = ', '.join(to)
    else:
        msg['To'] = to
    
    # Add CC if provided
    if cc:
        if isinstance(cc, list):
            msg['Cc'] = ', '.join(cc)
        else:
            msg['Cc'] = cc
    
    # Add BCC if provided (BCC is not added to headers but used in send)
    all_recipients = [to] if isinstance(to, str) else list(to)
    if cc:
        cc_list = [cc] if isinstance(cc, str) else list(cc)
        all_recipients.extend(cc_list)
    if bcc:
        bcc_list = [bcc] if isinstance(bcc, str) else list(bcc)
        all_recipients.extend(bcc_list)
    
    # Add test prefix if in test mode
    msg['Subject'] = f"{'[TEST] ' if TEST_MODE else ''}{subject}"
    
    # Set content
    if plain_text is None:
        plain_text = "This is an HTML report. Please view it in an HTML-compatible client."
    msg.set_content(plain_text)
    msg.add_alternative(html_content, subtype='html')
    
    # Add attachments if provided
    if attachments:
        for filename, content, maintype, subtype in attachments:
            msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    
    # Build recipient list for logging
    recipients = msg['To']
    if msg.get('Cc'):
        recipients += f", {msg['Cc']}"
    
    print(f"\nSending email to: {recipients}")
    
    # Send via SMTP
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD)
        # If we have BCC recipients, use sendmail instead of send_message
        if bcc:
            server.sendmail(msg['From'], all_recipients, msg.as_string())
        else:
            server.send_message(msg)
    
    print("Email sent successfully!")


def create_html_table(data, headers=None, style="border-collapse: collapse;"):
    """
    Create an HTML table from a list of dictionaries or pandas DataFrame.
    
    Args:
        data: List of dicts or pandas DataFrame
        headers: Optional list of headers (uses dict keys if not provided)
        style: CSS style for the table
    
    Returns:
        HTML string for the table
    """
    import pandas as pd
    
    if isinstance(data, pd.DataFrame):
        # Convert DataFrame to list of dicts
        data = data.to_dict('records')
    
    if not data:
        return "<p>No data available</p>"
    
    # Get headers from first row if not provided
    if headers is None:
        headers = list(data[0].keys())
    
    html = f'<table border="1" cellpadding="5" cellspacing="0" style="{style}">\n'
    
    # Headers
    html += '<tr style="background-color: #f0f0f0;">\n'
    for header in headers:
        html += f'<th>{header}</th>\n'
    html += '</tr>\n'
    
    # Data rows
    for row in data:
        html += '<tr>\n'
        for header in headers:
            value = row.get(header, '')
            html += f'<td>{value}</td>\n'
        html += '</tr>\n'
    
    html += '</table>'
    
    return html


def send_html_email_with_attachments(to, subject, html_content, attachments=None, cc=None, bcc=None):
    """
    Send an HTML email with CSV file attachments.
    
    Args:
        to: Recipient email address(es) - string or list
        subject: Email subject
        html_content: HTML body content
        attachments: List of file paths to attach as CSV files
        cc: CC recipients (optional)
        bcc: BCC recipients (optional)
    """
    attachment_tuples = []
    
    if attachments:
        for filepath in attachments:
            try:
                with open(filepath, 'rb') as f:
                    content = f.read()
                    filename = filepath.split('/')[-1]  # Get just the filename
                    attachment_tuples.append((filename, content, 'text', 'csv'))
                    print(f"  Attached: {filename}")
            except Exception as e:
                print(f"  Warning: Could not attach {filepath}: {str(e)}")
    
    send_html_email(
        to=to,
        subject=subject,
        html_content=html_content,
        cc=cc,
        bcc=bcc,
        attachments=attachment_tuples if attachment_tuples else None
    )