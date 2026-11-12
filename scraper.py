cd /Users/alansteier/Webscout
cp scraper.py scraper.py.bak
import sqlite3
import hashlib
import smtplib
import warnings
import os
import re
import traceback
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright
from datetime import datetime, timedelta
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
KEYWORDS = ["advanced morning play"]
ET = ZoneInfo("America/New_York")

class WebscoutDB:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_NAME)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS counts (
                event_hash TEXT PRIMARY KEY,
                signed INTEGER,
                updated_at DATETIME
            )
        """)
        self.conn.commit()

    def update_count(self, title, date_str, signed):
        """Store the current signup count and return the previous one (0 if unseen)."""
        h = hashlib.sha256(f"{title}-{date_str}".encode()).hexdigest()
        row = self.conn.execute(
            "SELECT signed FROM counts WHERE event_hash=?", (h,)
        ).fetchone()
        previous = row[0] if row else 0
        self.conn.execute(
            "INSERT OR REPLACE INTO counts VALUES (?, ?, ?)",
            (h, signed, datetime.now())
        )
        self.conn.commit()
        return previous

def parse_ms_date(date_str):
    """Convert /Date(1771765200000)/ to YYYY-MM-DD in Eastern time."""
    match = re.search(r'/Date\((\d+)\)/', date_str or "")
    if match:
        ms = int(match.group(1))
        return datetime.fromtimestamp(ms / 1000, tz=ET).strftime("%Y-%m-%d")
    return "Unknown"

def send_sms(matches):
    lines = []
    for m in matches:
        slots = m["slots_info"].replace("\xa0", " ").strip()
        gained = m["signed"] - m["previous"]
        lines.append(f"{m['date']}: {m['signed']} signed (+{gained}) - {slots}")
    body = "Advanced Morning Play\n" + "\n".join(lines)

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
                    captured_data.extend(data.get("Data", []))
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            print("🚀 Webscout: Initiating API interception...")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            print("⏳ Hydrating API data (15s)...")
            page.wait_for_timeout(15000)

            if not captured_data:
                print("❌ Fatal: ReadCalendarEvents never appeared in network traffic.")
                return

            today = datetime.now(ET).date()
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
                if not any(kw in title.lower() for kw in KEYWORDS):
                    continue

                signed = item.get("SignedMembers", 0) or 0
                previous = db.update_count(title, date_str, signed)
                print(f"📅 {date_str}: {signed} signed (was {previous})")

                if signed > previous:
                    matches.append({
                        "date": date_str,
                        "signed": signed,
                        "previous": previous,
                        "slots_info": item.get("SlotsInfo", ""),
                    })

            if matches:
                send_sms(matches)
                print(f"🏆 Success: {len(matches)} session(s) with new signups sent via SMS.")
            else:
                print("⏱ Check complete: no new signups.")

        except Exception as e:
            print(f"❌ Scrape Error: {e}")
            traceback.print_exc()
        finally:
            browser.close()

if __name__ == "__main__":
    run_scraper()
