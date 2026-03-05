from fastapi import FastAPI, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from google.oauth2.credentials import Credentials
import json
from datetime import datetime, timedelta
from starlette.middleware.sessions import SessionMiddleware
from database import engine, SessionLocal
from models import Base, Budget, Transaction, GmailToken, ProcessedEmail
from gmail_service import (
    make_flow,
    build_gmail_service,
    search_receipt_message_ids,
    get_message_snippet,
    extract_amount_from_text,
)
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import os

app = FastAPI()
Base.metadata.create_all(bind=engine)
app.add_middleware(
    SessionMiddleware,
    secret_key="65x23er",
    same_site="lax",
    https_only=True,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "https://budget-tracker-ke3y.onrender.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

# Serve /frontend/* files (optional but useful if you add css/js later)
app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")

@app.get("/")
def serve_ui():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
# ---------- DB Dependency ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- Helpers ----------
def start_of_week_utc(dt: datetime) -> datetime:
    # Monday = 0 ... Sunday = 6
    monday = dt - timedelta(days=dt.weekday())
    return datetime(monday.year, monday.month, monday.day)


# ---------- Basic ----------
@app.get("/")
def serve_ui():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# ---------- Budget ----------
@app.get("/budget")
def get_budget(db: Session = Depends(get_db)):
    budget = db.query(Budget).first()
    if not budget:
        return {"error": "No budget set yet. Use /set_budget first."}
    return {"weekly_limit": budget.weekly_limit, "remaining": budget.remaining}


@app.post("/set_budget/")
def set_budget(amount: float, db: Session = Depends(get_db)):
    budget = db.query(Budget).first()

    if budget is None:
        budget = Budget(weekly_limit=amount, remaining=amount)
        db.add(budget)
    else:
        budget.weekly_limit = amount
        budget.remaining = amount

    db.commit()
    return {"weekly_limit": budget.weekly_limit, "remaining": budget.remaining}


@app.get("/budget_status")
def budget_status(db: Session = Depends(get_db)):
    budget = db.query(Budget).first()
    if not budget:
        return {"error": "No budget set yet. Use /set_budget first."}

    percent_left = (budget.remaining / budget.weekly_limit) * 100 if budget.weekly_limit else 0.0

    return {
        "weekly_limit": budget.weekly_limit,
        "remaining": budget.remaining,
        "percent_left": round(percent_left, 2),
        "low_budget": percent_left <= 20,
    }


@app.post("/reset_week")
def reset_week(db: Session = Depends(get_db)):
    budget = db.query(Budget).first()
    if not budget:
        return {"error": "No budget set yet. Use /set_budget first."}

    budget.remaining = budget.weekly_limit
    db.commit()

    return {
        "message": "Weekly budget reset",
        "weekly_limit": budget.weekly_limit,
        "remaining": budget.remaining,
    }


# ---------- Transactions ----------
@app.post("/add_transaction/")
def add_transaction(amount: float, store: str, db: Session = Depends(get_db)):
    budget = db.query(Budget).first()
    if not budget:
        return {"error": "Set a budget first using /set_budget."}

    tx = Transaction(amount=amount, store=store)
    db.add(tx)

    budget.remaining -= amount
    db.commit()

    return {"message": "Transaction added", "remaining": budget.remaining}


@app.get("/transactions")
def list_transactions(db: Session = Depends(get_db)):
    txs = db.query(Transaction).order_by(Transaction.id.desc()).all()
    return [
        {"id": t.id, "amount": t.amount, "store": t.store, "created_at": t.created_at}
        for t in txs
    ]


@app.get("/weekly_spending")
def weekly_spending(db: Session = Depends(get_db)):
    week_ago = datetime.utcnow() - timedelta(days=7)
    txs = db.query(Transaction).filter(Transaction.created_at >= week_ago).all()
    total = sum(t.amount for t in txs)
    return {"weekly_spent": round(total, 2)}


@app.get("/weekly_summary")
def weekly_summary(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    week_start = start_of_week_utc(now)

    txs = db.query(Transaction).filter(Transaction.created_at >= week_start).all()
    total_spent = sum(t.amount for t in txs)

    store_totals = {}
    for t in txs:
        store_totals[t.store] = store_totals.get(t.store, 0) + t.amount

    store_breakdown = sorted(
        [{"store": k, "spent": round(v, 2)} for k, v in store_totals.items()],
        key=lambda x: x["spent"],
        reverse=True,
    )

    budget = db.query(Budget).first()

    return {
        "week_start_utc": week_start.isoformat(),
        "weekly_spent": round(total_spent, 2),
        "store_breakdown": store_breakdown,
        "weekly_limit": budget.weekly_limit if budget else None,
        "remaining": budget.remaining if budget else None,
    }


# ---------- Gmail OAuth ----------
@app.get("/gmail/login")
def gmail_login(request: Request, db: Session = Depends(get_db)):
    flow = make_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    # Save state + PKCE verifier into the session cookie
    request.session["oauth_state"] = state
    request.session["code_verifier"] = getattr(flow, "code_verifier", None)

    return RedirectResponse(auth_url)


@app.get("/gmail/auth")
def gmail_auth(request: Request, code: str, state: str, db: Session = Depends(get_db)):
    saved_state = request.session.get("oauth_state")
    code_verifier = request.session.get("code_verifier")

    if not saved_state or state != saved_state:
        return {"error": "OAuth state mismatch. Restart login at /gmail/login"}

    flow = make_flow()
    # restore state
    flow.fetch_token(code=code, code_verifier=code_verifier)

    creds = flow.credentials
    token_data = creds.to_json()

    existing = db.query(GmailToken).first()
    if existing is None:
        db.add(GmailToken(token_json=token_data))
    else:
        existing.token_json = token_data

    db.commit()

    # clear session keys
    request.session.pop("oauth_state", None)
    request.session.pop("code_verifier", None)

    return {"message": "Gmail connected. You can now POST /gmail/sync"}


@app.post("/gmail/sync")
def gmail_sync(db: Session = Depends(get_db)):
    token_row = db.query(GmailToken).first()
    if not token_row:
        return {"error": "Gmail not connected. Go to /gmail/login first."}

    budget = db.query(Budget).first()
    if not budget:
        return {"error": "Set a budget first using /set_budget."}

    creds = Credentials.from_authorized_user_info(json.loads(token_row.token_json))
    service = build_gmail_service(creds)

    msg_ids = search_receipt_message_ids(service, max_results=25)

    imported = 0
    skipped = 0
    no_amount = 0

    for msg_id in msg_ids:
        already = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.gmail_message_id == msg_id)
            .first()
        )
        if already:
            skipped += 1
            continue

        snippet = get_message_snippet(service, msg_id)
        amount = extract_amount_from_text(snippet)

        # Mark processed so we don't keep retrying the same email forever
        db.add(ProcessedEmail(gmail_message_id=msg_id))

        if amount is None:
            no_amount += 1
            continue

        tx = Transaction(amount=amount, store="Email receipt")
        db.add(tx)
        budget.remaining -= amount
        imported += 1

    db.commit()

    return {
        "imported": imported,
        "skipped_already_processed": skipped,
        "emails_with_no_amount_found": no_amount,
        "remaining": budget.remaining,
    }
@app.get("/spending_by_day")
def spending_by_day(db: Session = Depends(get_db)):
    today = datetime.utcnow().date()
    start = today - timedelta(days=6)
    start_dt = datetime(start.year, start.month, start.day)

    txs = db.query(Transaction).filter(Transaction.created_at >= start_dt).all()

    buckets = {(start + timedelta(days=i)).isoformat(): 0.0 for i in range(7)}
    for t in txs:
        d = t.created_at.date().isoformat()
        if d in buckets:
            buckets[d] += float(t.amount)

    labels = list(buckets.keys())
    values = [round(buckets[k], 2) for k in labels]
    total = round(sum(values), 2)
    short_labels = [l[5:] for l in labels]  # MM-DD

    return {"labels": short_labels, "values": values, "total": total}