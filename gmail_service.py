import re
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
REDIRECT_URI = "https://budget-tracker-ke3y.onrender.com/gmail/auth"
CLIENT_SECRETS_FILE = "credentials.json"

def make_flow():
    return Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

def build_gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds)

def search_receipt_message_ids(service, max_results=20):
    query = 'subject:(receipt OR "order confirmation" OR "thanks for your purchase")'
    res = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    return [m["id"] for m in res.get("messages", [])]

def get_message_snippet(service, msg_id: str) -> str:
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    return msg.get("snippet", "") or ""

def extract_amount_from_text(text: str):
    m = re.search(r"\$\s?(\d+(?:\.\d{2})?)", text)
    if not m:
        return None
    return float(m.group(1))