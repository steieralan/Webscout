"""
Pickle Juice Booking -> Google Calendar Sync
Logs into book.picklejuiceusa.com, scrapes current bookings,
and adds any new ones to Google Calendar.
"""

import os
import re
import json
import html as html_module
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from curl_cffi import requests as cf_requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

PICKLE_EMAIL = os.getenv("PICKLE_EMAIL")
PICKLE_PASSWORD = os.getenv("PICKLE_PASSWORD")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "google_token.json")
ET = ZoneInfo("America/New_York")

def get_calendar_service():
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)
    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data["scopes"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)
    return build("calendar", "v3", credentials=creds)

def get_existing_events(service):
    """Fetch existing Pickle Juice events from Google Calendar."""
    result = service.events().list(
        calendarId=CALENDAR_ID,
        q="Pickle Juice",
        singleEvents=True,
        maxResults=100
    ).execute()
    return {e["id"]: e for e in result.get("items", [])}

def booking_key(booking):
    return f"{booking['name']}|{booking['date']}|{booking['time']}"

def parse_datetime(date_str, time_str):
    """Convert 'Wed, May 20' and '12:30 PM - 02:30 PM' to start/end datetimes."""
    year = datetime.now(ET).year
    # If the month is earlier than current month, assume next year
    start_time_str, end_time_str = [t.strip() for t in time_str.split('-')]
    start_dt = datetime.strptime(f"{date_str} {year} {start_time_str}", "%a, %b %d %Y %I:%M %p")
    end_dt   = datetime.strptime(f"{date_str} {year} {end_time_str}",   "%a, %b %d %Y %I:%M %p")
    # Localize to ET
    start_dt = start_dt.replace(tzinfo=ET)
    end_dt   = end_dt.replace(tzinfo=ET)
    return start_dt.isoformat(), end_dt.isoformat()

def add_event(service, booking):
    start_iso, end_iso = parse_datetime(booking["date"], booking["time"])
    event = {
        "summary": f"Pickle Juice - {booking['name']}",
        "location": "Pickle Juice Pickleball Club, Ventnor Heights, NJ",
        "description": f"Booked via Pickle Juice\nSession ID: {booking['session_id']}",
        "start": {"dateTime": start_iso, "timeZone": "America/New_York"},
        "end":   {"dateTime": end_iso,   "timeZone": "America/New_York"},
    }
    service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    print(f"  📅 Added: {booking['name']} on {booking['date']} {booking['time']}")

def scrape_bookings():
    """Log into Pickle Juice and parse all upcoming bookings."""
    session = cf_requests.Session(impersonate='chrome124')

    print("🔐 Logging into Pickle Juice...")
    resp = session.get('https://book.picklejuiceusa.com/users/sign_in')
    token = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', resp.text).group(1)
    session.post('https://book.picklejuiceusa.com/users/sign_in', data={
        'authenticity_token': token,
        'user[email]': PICKLE_EMAIL,
        'user[password]': PICKLE_PASSWORD,
        'user[remember_me]': '0',
    })
    print("  ✅ Logged in")

    print("📋 Fetching reservations...")
    r = session.get('https://book.picklejuiceusa.com/account/reservations')
    raw = r.text  # keep original HTML for tag-stripping

    bookings = []
    # Session IDs are HTML-encoded inside attributes: &quot;sessionId&quot;:12345
    for m in re.finditer(r'sessionId&quot;:(\d+)', raw):
        sid = m.group(1)
        start = max(0, m.start() - 2000)
        # End chunk at the last <div before the session ID to avoid cutting mid-tag
        chunk_end = raw.rfind('<div', start, m.start())
        if chunk_end == -1:
            chunk_end = m.start()
        chunk = raw[start:chunk_end]
        # Strip tags FIRST, then unescape entities
        text = re.sub(r'<[^>]+>', ' ', chunk)
        text = html_module.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()

        date_match = re.search(r'((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \w+ \d+)', text)
        time_match = re.search(r'(\d+:\d+ [AP]M - \d+:\d+ [AP]M)', text)
        # Extract program name: take everything after the last "Paid " and before " Ventnor"
        if 'Paid ' in text and ' Ventnor' in text:
            after_last_paid = text.rsplit('Paid ', 1)[-1]
            raw_name = after_last_paid.split(' Ventnor')[0].strip()
        else:
            raw_name = None
        prog_match = type('m', (), {'group': lambda self, n: raw_name})() if raw_name else None

        if not date_match or not time_match:
            print(f"  ⚠️ Could not parse session {sid}, skipping")
            continue

        name = prog_match.group(1).strip() if prog_match else "Open Play"
        name = re.sub(r'\s+', ' ', name).strip()

        booking = {
            'session_id': sid,
            'name': name,
            'date': date_match.group(1),
            'time': time_match.group(1),
        }
        bookings.append(booking)
        print(f"  Found: {name} | {booking['date']} | {booking['time']}")

    return bookings

def run():
    print("=== Pickle Juice → Google Calendar Sync ===\n")
    try:
        bookings = scrape_bookings()
    except Exception as e:
        print(f"❌ Scrape error: {e}")
        traceback.print_exc()
        return

    if not bookings:
        print("No bookings found.")
        return

    print(f"\n📆 Syncing {len(bookings)} booking(s) to Google Calendar...")
    service = get_calendar_service()
    existing = get_existing_events(service)

    # Build set of existing event keys for dedup
    existing_keys = set()
    for e in existing.values():
        desc = e.get("description", "")
        sid_match = re.search(r'Session ID: (\d+)', desc)
        if sid_match:
            existing_keys.add(sid_match.group(1))

    added = 0
    for booking in bookings:
        if booking['session_id'] in existing_keys:
            print(f"  ⏭ Already in calendar: {booking['name']} on {booking['date']}")
        else:
            add_event(service, booking)
            added += 1

    print(f"\n✅ Done. {added} new event(s) added to Google Calendar.")

if __name__ == "__main__":
    run()
