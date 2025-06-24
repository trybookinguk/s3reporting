import requests
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from collections import Counter
import os

# Import shared modules
from modules.config import (
    ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN,
    MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD, MAILGUN_DOMAIN,
    SMTP_HOST, SMTP_PORT, TEST_MODE
)
from modules.zoho_api import get_access_token

# Additional environment variable specific to this script
PORTAL_NAME = os.environ["ZOHO_PORTAL_NAME"]

# === Tags to track ===
TRACKED_TAGS = ["Unknown User", "Event Organiser", "Ticket Purchaser", "New Business"]

# === Time window: Last week (Monday–Sunday) in UTC ===
today = datetime.now(timezone.utc)
last_monday = today - timedelta(days=today.weekday() + 7)
last_monday = last_monday.replace(hour=0, minute=0, second=0, microsecond=0)
last_sunday = last_monday + timedelta(days=6)
last_sunday = last_sunday.replace(hour=23, minute=59, second=59, microsecond=999999)

# === Step 1: Get access token === 
# Now using shared get_access_token from modules.zoho_api

# === Step 2: Fetch all conversations (stop early when past range) ===
def fetch_conversations(token):
    url = f"https://salesiq.zoho.com/api/v2/{PORTAL_NAME}/conversations"
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

    tracked_lines = '\n'.join(
        f"- {tag}: {tag_counts.get(tag, 0)} ({(tag_counts.get(tag, 0) / total * 100):.0f}%)"
        for tag in TRACKED_TAGS
    )
    day_lines = '\n'.join(
        f"{day}: {day_counts.get(day, 0)}" for day in ordered_days
    )

    subject = f"{'[TEST] ' if TEST_MODE else ''}SalesIQ Weekly Chat Summary w/c {last_monday.strftime('%d %B %Y')}"
    body = f"""Total SalesIQ conversations: {total}
Busiest day: {busiest_day}

Tagged breakdown:
{tracked_lines}

Chats by day:
{day_lines}
"""

    msg = EmailMessage()
    msg['From'] = f"TryBooking Reporting <reports@{MAILGUN_DOMAIN}>"
    msg['To'] = "alex@trybooking.co.uk" if TEST_MODE else "jules@trybooking.co.uk"
    msg['Cc'] = "alex@trybooking.co.uk" if TEST_MODE else "alex@trybooking.co.uk"
    msg['Subject'] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(MAILGUN_SMTP_LOGIN, MAILGUN_SMTP_PASSWORD)
        smtp.send_message(msg)

# === Run ===
def main():
    """Main execution function."""
    send_email_report = os.environ.get('SEND_EMAIL', '1') != '0'
    
    print(f"\n=== SalesIQ Weekly Report ===")
    print(f"Email sending: {'ENABLED' if send_email_report else 'DISABLED'}")
    if send_email_report and TEST_MODE:
        print("TEST MODE: Email will be sent to alex@trybooking.co.uk only")
    
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