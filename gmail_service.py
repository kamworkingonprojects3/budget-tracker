import os
import json
import re
import base64
from email.utils import parsedate_to_datetime
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_redirect_uri() -> str:
    """
    Use an explicit env var in production.
    """
    uri = os.getenv("OAUTH_REDIRECT_URI") or os.getenv("REDIRECT_URI")
    if uri:
        return uri.strip()

    return "http://127.0.0.1:8000/gmail/auth"


def make_flow() -> Flow:
    """
    On Render we store the entire OAuth client JSON in GOOGLE_OAUTH_CLIENT.
    It must be the full JSON (with a top-level key like "web" or "installed").
    """
    redirect_uri = get_redirect_uri()

    raw = os.getenv("GOOGLE_OAUTH_CLIENT")
    if raw:
        info = json.loads(raw)
        return Flow.from_client_config(
            info,
            scopes=SCOPES,
            redirect_uri=redirect_uri,
        )

    client_file = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "credentials.json")
    return Flow.from_client_secrets_file(
        client_file,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def build_gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds)


def search_receipt_message_ids(service, max_results=25):
    """
    Better search for receipt/order/payment emails while avoiding a lot of promo noise.
    """
    query = (
        '('
        'subject:(receipt OR invoice OR "order confirmation" OR "payment received" OR '
        '"thanks for your purchase" OR charged OR renewal OR "order total") '
        'OR '
        '"amount paid" OR "grand total" OR "payment confirmation"'
        ') '
        '-category:promotions '
        '-label:spam '
        '-label:trash'
    )

    res = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    return [m["id"] for m in res.get("messages", [])]


def _decode_base64url(data: str) -> str:
    if not data:
        return ""

    try:
        padded = data + "=" * (-len(data) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
        return decoded.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _extract_headers(payload: dict) -> dict:
    headers = {}
    for h in payload.get("headers", []):
        name = h.get("name")
        value = h.get("value")
        if name:
            headers[name.lower()] = value or ""
    return headers


def _walk_parts_for_text(part: dict) -> str:
    mime_type = part.get("mimeType", "")
    body = part.get("body", {}) or {}
    data = body.get("data")

    text_chunks = []

    if mime_type == "text/plain" and data:
        text_chunks.append(_decode_base64url(data))
    elif mime_type == "text/html" and data:
        html = _decode_base64url(data)
        html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
        html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
        html = re.sub(r"(?i)<br\s*/?>", "\n", html)
        html = re.sub(r"(?i)</p>", "\n", html)
        html = re.sub(r"(?s)<[^>]+>", " ", html)
        text_chunks.append(html)

    for child in part.get("parts", []) or []:
        text_chunks.append(_walk_parts_for_text(child))

    return "\n".join(x for x in text_chunks if x)


def _clean_text(text: str) -> str:
    if not text:
        return ""

    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+\n", "\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_message_snippet(service, msg_id: str) -> str:
    """
    Kept for compatibility.
    """
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    return msg.get("snippet", "") or ""


def get_full_message_data(service, msg_id: str) -> dict:
    """
    Returns richer email data for better receipt parsing.
    """
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {}) or {}
    headers = _extract_headers(payload)

    subject = headers.get("subject", "") or ""
    sender = headers.get("from", "") or ""
    date_raw = headers.get("date", "") or ""

    body_text = _walk_parts_for_text(payload)
    snippet = msg.get("snippet", "") or ""

    combined_text = _clean_text("\n".join([subject, snippet, body_text]))

    parsed_date = None
    if date_raw:
        try:
            parsed_date = parsedate_to_datetime(date_raw)
        except Exception:
            parsed_date = None

    return {
        "id": msg_id,
        "subject": subject,
        "from": sender,
        "date": parsed_date.isoformat() if parsed_date else None,
        "snippet": snippet,
        "body": _clean_text(body_text),
        "text": combined_text,
    }


def _parse_amount_string(raw: str):
    try:
        return float(raw.replace("$", "").replace(",", "").strip())
    except Exception:
        return None


def extract_amount_from_text(text: str):
    """
    Better amount extraction:
    - prefers totals/charged/paid contexts
    - penalizes subtotal/tax/shipping/discount contexts
    - falls back to best scored amount
    """
    if not text:
        return None

    normalized = text.lower()

    pattern = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+(?:\.\d{2}))")
    scored = []

    for match in pattern.finditer(normalized):
        amount_raw = match.group(1)
        value = _parse_amount_string(amount_raw)
        if value is None:
            continue

        if value <= 0:
            continue

        start = max(0, match.start() - 60)
        end = min(len(normalized), match.end() + 60)
        context = normalized[start:end]

        score = 0

        positive_terms = [
            "grand total",
            "order total",
            "total paid",
            "amount paid",
            "you paid",
            "charged",
            "payment received",
            "payment amount",
            "total",
            "renewal",
        ]
        negative_terms = [
            "subtotal",
            "tax",
            "shipping",
            "delivery",
            "discount",
            "saved",
            "coupon",
            "tip",
            "before tax",
        ]

        for term in positive_terms:
            if term in context:
                score += 5 if term != "total" else 3

        for term in negative_terms:
            if term in context:
                score -= 4

        if value < 1:
            score -= 2

        scored.append((score, value))

    if not scored:
        return None

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    best_score, best_value = scored[0]

    if best_score < -2:
        return None

    return best_value


def extract_store_name(subject: str, sender: str, body: str) -> str:
    """
    Best-effort merchant detection from sender/domain/subject/body.
    """
    subject = subject or ""
    sender = sender or ""
    body = body or ""

    subject_l = subject.lower()
    sender_l = sender.lower()
    body_l = body.lower()
    text = f"{subject_l}\n{body_l}"

    merchant_map = {
        "amazon": "Amazon",
        "walmart": "Walmart",
        "target": "Target",
        "best buy": "Best Buy",
        "costco": "Costco",
        "uber": "Uber",
        "uber eats": "Uber Eats",
        "doordash": "DoorDash",
        "grubhub": "Grubhub",
        "instacart": "Instacart",
        "lyft": "Lyft",
        "netflix": "Netflix",
        "spotify": "Spotify",
        "hulu": "Hulu",
        "apple": "Apple",
        "google": "Google",
        "youtube": "YouTube",
        "paypal": "PayPal",
        "ebay": "eBay",
        "etsy": "Etsy",
        "shein": "SHEIN",
        "temu": "Temu",
        "nike": "Nike",
        "adidas": "Adidas",
        "steam": "Steam",
        "playstation": "PlayStation",
        "xbox": "Xbox",
        "discord": "Discord",
        "chatgpt": "ChatGPT",
        "openai": "OpenAI",
    }

    # strongest signal: known merchant name anywhere
    for key, label in merchant_map.items():
        if key in sender_l or key in subject_l or key in text:
            return label

    # next: sender email domain
    email_match = re.search(r"<([^>]+)>", sender)
    sender_email = email_match.group(1).lower() if email_match else sender_l

    domain_match = re.search(r"@([a-z0-9.-]+\.[a-z]{2,})", sender_email)
    if domain_match:
        domain = domain_match.group(1)
        root = domain.split(".")[0]
        if root and root not in ["mail", "email", "notifications", "noreply", "no-reply", "support"]:
            return root.replace("-", " ").replace("_", " ").title()

    # next: parse patterns like "Your Amazon order" or "Receipt from Uber"
    subject_patterns = [
        r"receipt from ([a-z0-9&' .-]+)",
        r"order confirmation from ([a-z0-9&' .-]+)",
        r"your ([a-z0-9&' .-]+) order",
        r"thanks for your purchase from ([a-z0-9&' .-]+)",
    ]

    for pat in subject_patterns:
        m = re.search(pat, subject_l, re.IGNORECASE)
        if m:
            name = m.group(1).strip(" .-")
            if name:
                return name.title()

    return "Email receipt"


def guess_category(store: str) -> str:
    store_l = (store or "").lower()

    subscriptions = {"netflix", "spotify", "hulu", "apple", "google", "youtube", "discord", "openai", "chatgpt"}
    transport = {"uber", "uber eats", "lyft"}
    food = {"doordash", "grubhub", "instacart", "uber eats"}
    shopping = {"amazon", "walmart", "target", "best buy", "costco", "ebay", "etsy", "shein", "temu", "nike", "adidas"}
    gaming = {"steam", "playstation", "xbox"}

    if store_l in subscriptions:
        return "Subscriptions"
    if store_l in transport:
        return "Transport"
    if store_l in food:
        return "Food"
    if store_l in shopping:
        return "Shopping"
    if store_l in gaming:
        return "Entertainment"

    return "Email"


def extract_receipt_data(service, msg_id: str) -> dict:
    """
    Main helper for syncing receipts into transactions.
    """
    message = get_full_message_data(service, msg_id)

    amount = extract_amount_from_text(message["text"])
    store = extract_store_name(
        subject=message["subject"],
        sender=message["from"],
        body=message["body"],
    )
    category = guess_category(store)

    return {
        "message_id": msg_id,
        "amount": amount,
        "store": store,
        "category": category,
        "subject": message["subject"],
        "from": message["from"],
        "date": message["date"],
        "text": message["text"],
    }