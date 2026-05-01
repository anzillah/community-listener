"""
Quick diagnostic — runs outside the main app.
Sends a minimal test conversation to Help Scout and prints the full
request payload + raw response so we can see exactly what's wrong.

Usage:
    python3 debug_helpscout.py
"""
import json
import os
from dotenv import load_dotenv
import requests

load_dotenv()

CLIENT_ID     = os.environ["HELPSCOUT_CLIENT_ID"]
CLIENT_SECRET = os.environ["HELPSCOUT_CLIENT_SECRET"]
MAILBOX_ID    = int(os.environ["HELPSCOUT_MAILBOX_ID"])

# ── 1. Get a token ────────────────────────────────────────────────────────────
print("=== Getting OAuth token ===")
token_resp = requests.post(
    "https://api.helpscout.net/v2/oauth2/token",
    data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    },
    timeout=10,
)
print(f"Status: {token_resp.status_code}")
print(f"Body:   {token_resp.text[:500]}")
token_resp.raise_for_status()
token = token_resp.json()["access_token"]
print(f"Token:  {token[:20]}…\n")

# ── 2. Try a minimal payload ──────────────────────────────────────────────────
print("=== Sending minimal test conversation ===")
payload = {
    "subject": "Test from debug script",
    "mailboxId": MAILBOX_ID,
    "type": "email",
    "status": "active",
    "customer": {
        "email": "debug-test@test.io",
    },
    "threads": [
        {
            "type": "customer",
            "customer": {"email": "debug-test@test.io"},
            "text": "This is a test message from the debug script.",
        }
    ],
}

print("Payload:")
print(json.dumps(payload, indent=2))
print()

resp = requests.post(
    "https://api.helpscout.net/v2/conversations",
    json=payload,
    headers={"Authorization": f"Bearer {token}"},
    timeout=15,
)

print(f"Status:  {resp.status_code}")
print(f"Headers: {dict(resp.headers)}")
print(f"Body:    {resp.text[:1000]}")

if resp.status_code == 201:
    location = resp.headers.get("Location", "")
    conv_id = location.rstrip("/").split("/")[-1]
    print(f"\n✅ SUCCESS — conversation ID: {conv_id}")
    print(f"   View at: https://secure.helpscout.net/conversation/{conv_id}/")
else:
    print(f"\n❌ FAILED with {resp.status_code}")
