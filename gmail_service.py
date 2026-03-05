import os, json, re
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

REDIRECT_URI = os.getenv("REDIRECT_URI", "http://127.0.0.1:8000/gmail/auth")

def make_flow() -> Flow:
    """
    On Render we won't have credentials.json. We'll store the whole
    client secret JSON in an environment variable called GOOGLE_OAUTH_CLIENT.
    """
    raw = os.getenv("GOOGLE_OAUTH_CLIENT")

    if raw:
        info = json.loads(raw)
        return Flow.from_client_config(info, scopes=SCOPES, redirect_uri=REDIRECT_URI)


    client_file = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "credentials.json")
    return Flow.from_client_secrets_file(client_file, scopes=SCOPES, redirect_uri=REDIRECT_URI)

def build_gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds)

def search_receipt_message_ids(service, max_results=25):
    query = 'subject:(receipt OR "order confirmation" OR "thanks for your purchase" OR invoice)'
    res = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    return [m["id"] for m in res.get("messages", [])]

def get_message_snippet(service, msg_id: str) -> str:
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    return msg.get("snippet", "") or ""

def extract_amount_from_text(text: str):
    m = re.search(r"(total|order total|grand total)[^\$]{0,40}\$\s?(\d+(?:\.\d{2})?)", text, re.IGNORECASE)
    if m:
        return float(m.group(2))
    m2 = re.search(r"\$\s?(\d+(?:\.\d{2})?)", text)
    if not m2:
        return None
    return float(m2.group(1))