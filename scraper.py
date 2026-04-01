import sqlite3
import hashlib
import smtplib
import warnings
import os
import re
import traceback
from email.message import EmailMessage
from playwright.sync_api import sync_playwright
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# Silence environment noise
warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"

import urllib3
urllib3.disable_warnings()

# --- LOAD CREDENTIALS FROM .env ---
load_dotenv()
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")
SMS_TO = f"{PHONE_NUMBER}@vtext.com"
DB_NAME = "webscout_cache.db"
KEYWORDS = ["Advanced Morning Play ( 4.0 recommended)"]

class WebscoutDB:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_NAME)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                hash TEXT PRIMARY KEY,
                title TEXT,
                date TEXT,
                found_at DATETIME
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS registrants (
                event_hash TEXT,
                name TEXT,
                found_at DATETIME,
                PRIMARY KEY (event_hash, name)
            )
        """)
        self.conn.commit()

    def event_hash(self, title, date_str):
        return hashlib.sha256(f"{title}-{date_str}".encode()).hexdigest()

    def ensure_event(self, title, date_str):
        h = self.event_hash(title, date_str)
        self.conn.execute(
            "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)",
            (h, title, date_str, datetime.now())
        )
        self.conn.commit()
        return h

    def get_new_registrants(self, event_hash, current_names):
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM registrants WHERE event_hash=?", (event_hash,))
        known = {row[0] for row in cursor.fetchall()}
        new_names = [n for n in current_names if n not in known]
        if new_names:
            self.conn.executemany(
                "INSERT OR IGNORE INTO registrants VALUES (?, ?, ?)",
                [(event_hash, name, datetime.now()) for name in new_names]
            )
            self.conn.commit()
        return new_names

def parse_ms_date(date_str):
    """Convert /Date(1771765200000)/ to YYYY-MM-DD"""
    match = re.search(r'/Date\((\d+)\)/', date_str or "")
    if match:
        ms = int(match.group(1))
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return "Unknown"

def get_registrants(page, number):
    details_url = f"https://events.courtreserve.com/Online/EventsApi/ApiDetails?id=16396&uiCulture=en-US&number={number}&ajaxCall=false"
    page.goto(details_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(8000)
    try:
        page.locator("text=REGISTRANTS").click(timeout=10000)
    except:
        return []
    page.wait_for_timeout(3000)
    content = page.inner_text("body")
    match = re.search(r"Name\n(.+?)\n© 2026", content, re.DOTALL)
    if match:
        names = [n.strip() for n in match.group(1).strip().split("\n") if n.strip() and n.strip() != "Name"]
        return names
    return []

def send_sms(matches):
    lines = []
    for m in matches:
        slots_info = m['slots_info'].replace('\xa0', ' ').strip()
        names = ", ".join(m['new_registrants'])
        lines.append(f"{m['date']} - {slots_info} | {names}")
    body = "\n".join(lines)

    msg = EmailMessage()
    msg.set_content(body)
    msg["From"] = GMAIL_USER
    msg["To"] = SMS_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)

def run_scraper():
    db = WebscoutDB()
    url = "https://app.courtreserve.com/Online/Calendar/Events/16396/Month"
    captured_data = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        def handle_response(response):
            if "ReadCalendarEvents" in response.url and response.status == 200:
                try:
                    data = response.json()
                    events = data.get("Data", [])
                    captured_data.extend(events)
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            page.wait_for_timeout(15000)

            if not captured_data:
                print("❌ Fatal: ReadCalendarEvents never appeared in network traffic.")
                return

            today = datetime.now(tz=timezone.utc).date()
            cutoff = today + timedelta(days=7)

            matches = []
            for item in captured_data:
                title = item.get("Title", "Untitled")
                date_str = parse_ms_date(item.get("Start", ""))
                if date_str == "Unknown":
                    continue
                event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if event_date < today or event_date > cutoff:
                    continue
                if not any(kw.lower() in title.lower() for kw in KEYWORDS):
                    continue

                signed = item.get("SignedMembers", 0)
                if signed == 0:
                    continue

                slots_info = item.get("SlotsInfo", "")
                number = item.get("Number")

                print(f"📅 Checking {date_str} ({signed} signed)...")
                event_hash = db.ensure_event(title, date_str)
                registrants = get_registrants(page, number)
                new_registrants = db.get_new_registrants(event_hash, registrants)

                if new_registrants:
                    print(f"✨ New registrant(s) on {date_str}: {', '.join(new_registrants)}")
                    matches.append({
                        "date": date_str,
                        "title": title,
                        "slots_info": slots_info,
                        "new_registrants": new_registrants
                    })
                else:
                    print(f"   No new registrants on {date_str}")

            if matches:
                send_sms(matches)
                print(f"🏆 Success: {len(matches)} event(s) with new registrants sent via SMS.")
            else:
                print(f"⏱ Check complete: no new registrants found.")

        except Exception as e:
            print(f"❌ Scrape Error: {e}")
            traceback.print_exc()
        finally:
            browser.close()

if __name__ == "__main__":
    run_scraper()
