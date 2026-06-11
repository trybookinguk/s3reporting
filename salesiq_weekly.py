import requests
from datetime import datetime, timedelta, timezone
from collections import Counter
import os

# Import shared modules
from modules.utils.config import (
    ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN,
    ZOHO_PORTAL_NAME, TEST_MODE, get_recipients
)
from modules.utils.zoho_api import get_access_token
from modules.utils.email_utils import send_html_email
from modules.utils.metrics_calculator import calculate_percentage
from modules.utils.validation import validate_environment_variables
from modules.utils.performance import timer_decorator

# === Tags to track ===
TRACKED_TAGS = ["Unknown User", "Event Organiser", "Ticket Purchaser", "New Business"]

# === Time window: Last week (Monday–Sunday) in UTC ===
today = datetime.now(timezone.utc)
last_monday = today - timedelta(days=today.weekday() + 7)
last_monday = last_monday.replace(hour=0, minute=0, second=0, microsecond=0)
last_sunday = last_monday + timedelta(days=6)
last_sunday = last_sunday.replace(hour=23, minute=59, second=59, microsecond=999999)

# === Step 1: Get access token === 
# Now using shared get_access_token from modules.utils.zoho_api

# === Step 2: Fetch all conversations (stop early when past range) ===
@timer_decorator
def fetch_conversations(token):
    url = f"https://salesiq.zoho.com/api/v2/{ZOHO_PORTAL_NAME}/conversations"
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}"
    }

    all_conversations = []
    page = 1

    while True:
        params = {"page": page}
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        batch = response.json().get("data", [])

        if not batch:
            break

        stop = False
        for convo in batch:
            ts = convo.get("start_time")
            if not ts:
                continue
            dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
            if dt < last_monday:
                stop = True
                break

        all_conversations.extend(batch)
        if stop or len(batch) < 20:
            break
        page += 1

    return all_conversations

# === Step 3: Filter to last week ===
def is_within_last_week(convo):
    ts = convo.get("start_time")
    if not ts:
        return False
    dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
    return last_monday <= dt <= last_sunday

# === Step 4: Summary functions ===
def summarize_tags(conversations):
    tag_counter = Counter()
    for convo in conversations:
        tags = convo.get("tags", [])
        tag_names = [t["name"].strip() for t in tags if isinstance(t, dict) and "name" in t]
        tag_counter.update(tag_names)
    return tag_counter

def get_busiest_day(conversations):
    counter = Counter()
    for convo in conversations:
        ts = convo.get("start_time")
        if not ts:
            continue
        dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
        counter[dt.strftime("%A")] += 1
    if not counter:
        return "N/A"
    day, count = counter.most_common(1)[0]
    return f"{day} ({count} chats)"

def get_day_counts(conversations):
    counts = Counter()
    for convo in conversations:
        ts = convo.get("start_time")
        if not ts:
            continue
        dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
        counts[dt.strftime("%A")] += 1
    return counts

# === Step 5: Send Mailgun email via SMTP ===
def send_email(total, tag_counts, busiest_day, day_counts):
    ordered_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    # Create HTML content
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h2>SalesIQ Weekly Chat Summary</h2>
        <p>Week commencing: {last_monday.strftime('%d %B %Y')}</p>
        
        <p><strong>Total SalesIQ conversations:</strong> {total}<br>
        <strong>Busiest day:</strong> {busiest_day}</p>
        
        <h3>Tagged breakdown:</h3>
        <ul>
        {''.join(f'<li>{tag}: {tag_counts.get(tag, 0)} ({calculate_percentage(tag_counts.get(tag, 0), total):.0f}%)</li>' for tag in TRACKED_TAGS)}
        </ul>
        
        <h3>Chats by day:</h3>
        <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse;">
            <tr><th>Day</th><th>Count</th></tr>
            {''.join(f'<tr><td>{day}</td><td>{day_counts.get(day, 0)}</td></tr>' for day in ordered_days)}
        </table>
    </body>
    </html>
    """
    
    # Plain text version for fallback
    tracked_lines = '\n'.join(
        f"- {tag}: {tag_counts.get(tag, 0)} ({calculate_percentage(tag_counts.get(tag, 0), total):.0f}%)"
        for tag in TRACKED_TAGS
    )
    day_lines = '\n'.join(
        f"{day}: {day_counts.get(day, 0)}" for day in ordered_days
    )
    
    plain_text = f"""Total SalesIQ conversations: {total}
Busiest day: {busiest_day}

Tagged breakdown:
{tracked_lines}

Chats by day:
{day_lines}
"""
    
    # Recipients are managed in modules/utils/config.py → DISTRIBUTION_LISTS["weekly_salesiq"]
    to_recipients, cc_recipients = get_recipients("weekly_salesiq")
    send_html_email(
        to=to_recipients,
        cc=cc_recipients,
        subject=f"SalesIQ Weekly Chat Summary w/c {last_monday.strftime('%d %B %Y')}",
        html_content=html_content,
        plain_text=plain_text
    )

# === Run ===
def main():
    """Main execution function."""
    # Validate environment variables
    validate_environment_variables([
        'ZOHO_CLIENT_ID', 'ZOHO_CLIENT_SECRET', 'ZOHO_REFRESH_TOKEN',
        'ZOHO_PORTAL_NAME',
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', 'AZURE_SENDER_MAILBOX'
    ])
    
    send_email_report = os.environ.get('SEND_EMAIL', '1') != '0'
    
    print(f"\n=== SalesIQ Weekly Report ===")
    print(f"Email sending: {'ENABLED' if send_email_report else 'DISABLED'}")
    if send_email_report and TEST_MODE:
        print("TEST MODE: Email will be sent to henry@trybooking.co.uk only")
    
    try:
        access_token = get_access_token()
        conversations = fetch_conversations(access_token)
        filtered = [c for c in conversations if is_within_last_week(c)]
        print(f"Filtered conversations: {len(filtered)}")

        if not filtered:
            raise Exception("No conversations found in the last week.")

        tag_counts = summarize_tags(filtered)
        busiest_day = get_busiest_day(filtered)
        day_counts = get_day_counts(filtered)
        
        if send_email_report:
            send_email(len(filtered), tag_counts, busiest_day, day_counts)
            print("Email sent successfully!")
        else:
            print("\nEmail sending disabled - report summary:")
            print(f"Total conversations: {len(filtered)}")
            print(f"Tag summary: {tag_counts}")
            print(f"Busiest day: {busiest_day}")
            
    except Exception as e:
        print("Error:", e)
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()