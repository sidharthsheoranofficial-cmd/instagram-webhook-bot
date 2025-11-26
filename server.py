from flask import Flask, request
import requests
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    else:
        return "Forbidden", 403

@app.post("/webhook")
def handle_messages():
    data = request.json

    try:
        messaging = data["entry"][0]["messaging"][0]
        sender_id = messaging["sender"]["id"]

        if "message" in messaging and "text" in messaging["message"]:
            user_msg = messaging["message"]["text"]

            send_message(sender_id, f"You said: {user_msg}")
    except:
        pass

    return "EVENT_RECEIVED", 200

def send_message(recipient_id, text):
    url = f"https://graph.facebook.com/v19.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    body = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    requests.post(url, json=body)

if __name__ == "__main__":
    app.run(port=3000)
