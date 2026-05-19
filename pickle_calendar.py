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
    impersonate_profiles = ['chrome124', 'chrome120', 'chrome116', 'chrome110']

    print("🔐 Logging into Pickle Juice...")
    session = None
    token = None
    for profile in impersonate_profiles:
        print(f"  Trying impersonate={profile}...")
        s = cf_requests.Session(impersonate=profile)
        resp = s.get('https://book.picklejuiceusa.com/users/sign_in')
        match = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', resp.text)
        if match:
            token = match.group(1)
            session = s
            print(f"  ✅ Got login page with {profile}")
            break
        else:
            print(f"  ❌ Cloudflare blocked with {profile} (status {resp.status_code})")

    if not session or not token:
        raise RuntimeError("All impersonation profiles blocked by Cloudflare. Cannot log in.")

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

    def extract_booking(raw, m, sid, waitlist=False):
        start = max(0, m.start() - 2000)
        chunk_end = raw.rfind('<div', start, m.start())
        if chunk_end == -1:
            chunk_end = m.start()
        chunk = raw[start:chunk_end]
        text = re.sub(r'<[^>]+>', ' ', chunk)
        text = html_module.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()

        date_match = re.search(r'((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \w+ \d+)', text)
        time_match = re.search(r'(\d+:\d+ [AP]M - \d+:\d+ [AP]M)', text)

        # Extract program name after last "Paid" or "Unpaid" before "Ventnor"
        keyword = 'Unpaid ' if waitlist else 'Paid '
        if keyword in text and ' Ventnor' in text:
            raw_name = text.rsplit(keyword, 1)[-1].split(' Ventnor')[0].strip()
        else:
            raw_name = 'Open Play'
        name = re.sub(r'\s+', ' ', raw_name).strip()
        if waitlist:
            name = f"{name} (Waitlist)"

        if not date_match or not time_match:
            print(f"  ⚠️ Could not parse session {sid}, skipping")
            return None

        print(f"  Found{'(waitlist)' if waitlist else ''}: {name} | {date_match.group(1)} | {time_match.group(1)}")
        return {
            'session_id': sid,
            'name': name,
            'date': date_match.group(1),
            'time': time_match.group(1),
        }

    bookings = []
    seen_ids = set()

    # Confirmed bookings — sessionId in React component
    for m in re.finditer(r'sessionId&quot;:(\d+)', raw):
        sid = m.group(1)
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        b = extract_booking(raw, m, sid, waitlist=False)
        if b:
            bookings.append(b)

    # Waitlist bookings — session_id in delete URL
    for m in re.finditer(r'delete_clinic_lesson_waitlist\?session_id=(\d+)', raw):
        sid = m.group(1)
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        b = extract_booking(raw, m, sid, waitlist=True)
        if b:
            bookings.append(b)

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

    # Build map of session_id -> existing calendar event
    existing_by_sid = {}
    for e in existing.values():
        desc = e.get("description", "")
        sid_match = re.search(r'Session ID: (\d+)', desc)
        if sid_match:
            existing_by_sid[sid_match.group(1)] = e

    added = 0
    updated = 0
    for booking in bookings:
        sid = booking['session_id']
        expected_summary = f"Pickle Juice - {booking['name']}"
        if sid in existing_by_sid:
            existing_event = existing_by_sid[sid]
            current_summary = existing_event.get("summary", "")
            if current_summary != expected_summary:
                # Status changed (e.g. waitlist -> confirmed) — update the event
                existing_event["summary"] = expected_summary
                service.events().update(
                    calendarId=CALENDAR_ID,
                    eventId=existing_event["id"],
                    body=existing_event
                ).execute()
                print(f"  ✏️ Updated: {current_summary} → {expected_summary}")
                updated += 1
            else:
                print(f"  ⏭ Already in calendar: {booking['name']} on {booking['date']}")
        else:
            add_event(service, booking)
            added += 1

    print(f"\n✅ Done. {added} new event(s) added, {updated} updated.")

if __name__ == "__main__":
    run()
