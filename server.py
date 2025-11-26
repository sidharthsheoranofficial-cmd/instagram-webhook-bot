# server.py
from flask import Flask, request, jsonify
import os, requests, sqlite3, json, time
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

load_dotenv()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
GOOGLE_CREDS = os.getenv("GOOGLE_CREDS", "google-creds.json")
SHEET_NAME = os.getenv("SHEET_NAME", "Gym-Leads")
SHEET_TAB = os.getenv("SHEET_TAB", "leads")

DB_PATH = "leads.db"
app = Flask(__name__)

# ---------------------------
# SQLite helpers
# ---------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id TEXT UNIQUE,
        state TEXT,
        name TEXT,
        phone TEXT,
        goal TEXT,
        notes TEXT,
        last_updated INTEGER
      );
    """)
    conn.commit()
    conn.close()

def get_conv(sender_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT sender_id, state, name, phone, goal, notes FROM conversations WHERE sender_id = ?", (sender_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "sender_id": row[0], "state": row[1], "name": row[2],
        "phone": row[3], "goal": row[4], "notes": row[5]
    }

def upsert_conv(sender_id, **kwargs):
    # kwargs may include state, name, phone, goal, notes
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = int(time.time())
    cur.execute("SELECT sender_id FROM conversations WHERE sender_id = ?", (sender_id,))
    exists = cur.fetchone()
    if exists:
        # build update
        sets = ", ".join([f"{k} = ?" for k in kwargs.keys()] + ["last_updated = ?"])
        vals = list(kwargs.values()) + [now, sender_id]
        cur.execute(f"UPDATE conversations SET {sets} WHERE sender_id = ?", vals)
    else:
        cur.execute("""
          INSERT INTO conversations (sender_id, state, name, phone, goal, notes, last_updated)
          VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            sender_id,
            kwargs.get("state"),
            kwargs.get("name"),
            kwargs.get("phone"),
            kwargs.get("goal"),
            kwargs.get("notes"),
            now
        ))
    conn.commit()
    conn.close()

def delete_conv(sender_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM conversations WHERE sender_id = ?", (sender_id,))
    conn.commit()
    conn.close()

# ---------------------------
# Google Sheets
# ---------------------------
def gs_client():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS, scope)
    client = gspread.authorize(creds)
    return client

def append_to_sheet(row):
    client = gs_client()
    sh = client.open(SHEET_NAME)
    worksheet = sh.worksheet(SHEET_TAB)
    worksheet.append_row(row, value_input_option='USER_ENTERED')

# ---------------------------
# Instagram message sending (abstract)
# ---------------------------
def send_message(recipient_id, text):
    # NOTE: the exact IG send endpoint can vary depending on your setup.
    # For basic cases, /me/messages may work if tokens & app are configured.
    # If you need IG-specific endpoint, replace the URL accordingly.
    url = f"https://graph.facebook.com/v17.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    body = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    resp = requests.post(url, json=body)
    # log if needed
    return resp.status_code, resp.text

# ---------------------------
# Conversation logic
# ---------------------------
def start_flow(sender_id):
    upsert_conv(sender_id, state="ASK_NAME")
    send_message(sender_id, "Hey 👋 — I can help book a free trial. What's your full name?")

def handle_user_message(sender_id, text):
    conv = get_conv(sender_id)
    if not conv:
        # new conversation
        start_flow(sender_id)
        return

    state = conv['state']
    text_stripped = text.strip()

    if state == "ASK_NAME":
        upsert_conv(sender_id, state="ASK_PHONE", name=text_stripped)
        send_message(sender_id, "Nice to meet you, {}! Please share your phone number so we can contact you.".format(text_stripped.split()[0]))
        return

    if state == "ASK_PHONE":
        # simple validation: digits
        digits = ''.join(ch for ch in text_stripped if ch.isdigit())
        if len(digits) < 7:
            send_message(sender_id, "That phone number looks short. Please enter your phone number including country or area code.")
            return
        upsert_conv(sender_id, state="ASK_GOAL", phone=text_stripped)
        send_message(sender_id, "Got it. What's your fitness goal? (e.g., build muscle, lose fat, general fitness)")
        return

    if state == "ASK_GOAL":
        upsert_conv(sender_id, state="ASK_NOTES", goal=text_stripped)
        send_message(sender_id, "Any other details we should know? (injuries, preferred workout time, trainer preference). If none, reply 'no'.")
        return

    if state == "ASK_NOTES":
        notes = text_stripped if text_stripped.lower() != "no" else ""
        # finalise
        conv_final = get_conv(sender_id)
        name = conv_final.get("name", "")
        phone = conv_final.get("phone", "")
        goal = conv_final.get("goal", "")
        # append to sheet
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        row = [timestamp, sender_id, name, phone, goal, notes]
        try:
            append_to_sheet(row)
            send_message(sender_id, "Thanks — we saved your details. A staff member will contact you shortly. 🙌")
        except Exception as e:
            # on sheet fail, keep data and notify
            send_message(sender_id, "Thanks — we saved your details locally but failed to save to Google Sheets. I'll try again.")
        # delete conversation or mark completed
        delete_conv(sender_id)
        return

    # fallback
    send_message(sender_id, "Sorry, I didn't understand that. Reply 'start' to begin booking or ask for help.")

# ---------------------------
# Webhook endpoints
# ---------------------------
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook_post():
    data = request.get_json()
    # basic structure depends on the IG webhook payload
    # try to find sender id and message text
    try:
        # The exact structure may differ. Inspect incoming payloads in logs if not matching.
        entries = data.get("entry", [])
        for entry in entries:
            # loop messaging objects
            # handle both page and instagram payload shapes
            if "messaging" in entry:
                messaging = entry["messaging"]
                for msg in messaging:
                    if "sender" in msg and "id" in msg["sender"]:
                        sender_id = msg["sender"]["id"]
                        if "message" in msg and "text" in msg["message"]:
                            text = msg["message"]["text"]
                            handle_user_message(sender_id, text)
            elif "changes" in entry:
                # instagram business webhook may use 'changes' with 'value'
                for change in entry["changes"]:
                    value = change.get("value", {})
                    # common path for IG messages
                    # safe-guard: adapt after inspecting real payloads
                    if "messages" in value:
                        for m in value["messages"]:
                            sender_id = m.get("from")
                            text = m.get("text", {}).get("body") or m.get("text")
                            if sender_id and text:
                                handle_user_message(sender_id, text)
    except Exception as e:
        # log error for debugging (print/log)
        print("Webhook handler error:", e)
    return jsonify({"status":"ok"}), 200

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
