from fastapi import FastAPI, Depends, Request
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles

from sqlalchemy.orm import Session
from google.oauth2.credentials import Credentials

import json
import os
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone

from database import engine, SessionLocal
from models import Base, Budget, Transaction, GmailToken, ProcessedEmail
from gmail_service import (
    make_flow,
    build_gmail_service,
    search_receipt_message_ids,
    get_message_snippet,
    extract_amount_from_text,
)

# ---------------- App setup ----------------
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

# Serve /frontend/* files (optional)
app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")


# ---------------- DB Dependency ----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------- Helpers ----------------
def start_of_week_utc(dt: datetime) -> datetime:
    """Return Monday 00:00:00 UTC of the week containing dt."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def iso(dt: datetime | None):
    return dt.isoformat() if dt else None


# ---------------- UI صفحات ----------------
@app.get("/")
def serve_ui():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/insights")
def serve_insights():
    return FileResponse(os.path.join(FRONTEND_DIR, "insights.html"))


# ---------------- Budget ----------------
@app.get("/budget")
def get_budget(db: Session = Depends(get_db)):
    budget = db.query(Budget).first()
    if not budget:
        return {"error": "No budget set yet. Use /set_budget first."}
    return {
        "weekly_limit": float(budget.weekly_limit),
        "remaining": float(budget.remaining),
    }


@app.post("/set_budget")
def set_budget(amount: float, db: Session = Depends(get_db)):
    if amount is None or amount <= 0:
        return {"error": "Amount must be > 0"}

    budget = db.query(Budget).first()

    if budget is None:
        budget = Budget(weekly_limit=amount, remaining=amount)
        db.add(budget)
    else:
        budget.weekly_limit = amount
        budget.remaining = amount

    db.commit()
    db.refresh(budget)

    return {
        "weekly_limit": float(budget.weekly_limit),
        "remaining": float(budget.remaining),
    }


@app.get("/budget_status")
def budget_status(db: Session = Depends(get_db)):
    budget = db.query(Budget).first()
    if not budget:
        return {"error": "No budget set yet. Use /set_budget first."}

    weekly = float(budget.weekly_limit or 0)
    remaining = float(budget.remaining or 0)
    percent_left = (remaining / weekly) * 100 if weekly else 0.0

    return {
        "weekly_limit": weekly,
        "remaining": remaining,
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
    db.refresh(budget)

    return {
        "message": "Weekly budget reset",
        "weekly_limit": float(budget.weekly_limit),
        "remaining": float(budget.remaining),
    }


# ---------------- Transactions ----------------
@app.post("/add_transaction")
def add_transaction(
    amount: float,
    store: str,
    category: str = "Uncategorized",
    db: Session = Depends(get_db),
):
    budget = db.query(Budget).first()
    if not budget:
        return {"error": "Set a budget first using /set_budget."}

    if amount is None or amount <= 0:
        return {"error": "Amount must be > 0"}

    tx = Transaction(amount=amount, store=store, category=category)
    db.add(tx)

    budget.remaining = float(budget.remaining) - float(amount)
    db.commit()
    db.refresh(budget)

    return {"message": "Transaction added", "remaining": float(budget.remaining)}


@app.get("/transactions")
def list_transactions(db: Session = Depends(get_db)):
    txs = db.query(Transaction).order_by(Transaction.id.desc()).all()
    return [
        {
            "id": t.id,
            "amount": float(t.amount),
            "store": t.store,
            "category": t.category,
            "created_at": iso(t.created_at),
        }
        for t in txs
    ]


@app.get("/weekly_spending")
def weekly_spending(db: Session = Depends(get_db)):
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    txs = db.query(Transaction).filter(Transaction.created_at >= week_ago).all()
    total = sum(float(t.amount) for t in txs)
    return {"weekly_spent": round(total, 2)}


@app.get("/weekly_summary")
def weekly_summary(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    week_start = start_of_week_utc(now)

    txs = db.query(Transaction).filter(Transaction.created_at >= week_start).all()
    total_spent = sum(float(t.amount) for t in txs)

    store_totals: dict[str, float] = {}
    for t in txs:
        store_totals[t.store] = store_totals.get(t.store, 0.0) + float(t.amount)

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
        "weekly_limit": float(budget.weekly_limit) if budget else None,
        "remaining": float(budget.remaining) if budget else None,
    }


@app.get("/spending_by_day")
def spending_by_day(db: Session = Depends(get_db)):
    # last 7 days in UTC
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=6)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)

    txs = db.query(Transaction).filter(Transaction.created_at >= start_dt).all()

    buckets = {(start + timedelta(days=i)).isoformat(): 0.0 for i in range(7)}
    for t in txs:
        d = t.created_at.astimezone(timezone.utc).date().isoformat()
        if d in buckets:
            buckets[d] += float(t.amount)

    labels = list(buckets.keys())
    values = [round(buckets[k], 2) for k in labels]
    total = round(sum(values), 2)

    # show MM-DD on chart
    short_labels = [l[5:] for l in labels]

    return {"labels": short_labels, "values": values, "total": total}


@app.get("/top_stores")
def top_stores(db: Session = Depends(get_db), limit: int = 5):
    week_start = start_of_week_utc(datetime.now(timezone.utc))

    # ✅ fixed typo: query
    txs = db.query(Transaction).filter(Transaction.created_at >= week_start).all()

    totals: dict[str, float] = {}
    for t in txs:
        totals[t.store] = totals.get(t.store, 0.0) + float(t.amount)

    ranked = sorted(
        [{"store": k, "spent": round(v, 2)} for k, v in totals.items()],
        key=lambda x: x["spent"],
        reverse=True,
    )

    return ranked[: max(1, int(limit))]


# ---------------- Gmail OAuth ----------------
@app.get("/gmail/login")
def gmail_login(request: Request, db: Session = Depends(get_db)):
    flow = make_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

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
    flow.fetch_token(code=code, code_verifier=code_verifier)

    creds = flow.credentials
    token_data = creds.to_json()

    existing = db.query(GmailToken).first()
    if existing is None:
        db.add(GmailToken(token_json=token_data))
    else:
        existing.token_json = token_data

    db.commit()

    request.session.pop("oauth_state", None)
    request.session.pop("code_verifier", None)

    params = urlencode({"gmail": "connected"})
    return RedirectResponse(url=f"/?{params}", status_code=302)


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
        already = db.query(ProcessedEmail).filter(ProcessedEmail.gmail_message_id == msg_id).first()
        if already:
            skipped += 1
            continue

        snippet = get_message_snippet(service, msg_id)
        amount = extract_amount_from_text(snippet)

        # mark processed so we don't re-check forever
        db.add(ProcessedEmail(gmail_message_id=msg_id))

        if amount is None:
            no_amount += 1
            continue

        tx = Transaction(amount=float(amount), store="Email receipt", category="Email")
        db.add(tx)

        budget.remaining = float(budget.remaining) - float(amount)
        imported += 1

    db.commit()
    db.refresh(budget)

    return {
        "imported": imported,
        "skipped_already_processed": skipped,
        "emails_with_no_amount_found": no_amount,
        "remaining": float(budget.remaining),
    }


# ---------------- Insights Data ----------------
@app.get("/insights_data")
def insights_data(db: Session = Depends(get_db)):
    week_start = start_of_week_utc(datetime.now(timezone.utc))
    txs = db.query(Transaction).filter(Transaction.created_at >= week_start).all()

    weekly_spent = round(sum(float(t.amount) for t in txs), 2)
    avg_per_day = round(weekly_spent / 7.0, 2)

    totals: dict[str, float] = {}
    for t in txs:
        totals[t.store] = totals.get(t.store, 0.0) + float(t.amount)

    top_stores_list = sorted(
        [{"store": k, "spent": round(v, 2)} for k, v in totals.items()],
        key=lambda x: x["spent"],
        reverse=True,
    )[:5]

    largest_tx = None
    if txs:
        largest_obj = max(txs, key=lambda t: float(t.amount))
        largest_tx = {
            "id": largest_obj.id,
            "store": largest_obj.store,
            "amount": float(largest_obj.amount),
            "created_at": iso(largest_obj.created_at),
        }

    biggest_sorted = sorted(txs, key=lambda t: float(t.amount), reverse=True)[:10]
    biggest_txs = [
        {
            "id": t.id,
            "store": t.store,
            "amount": float(t.amount),
            "created_at": iso(t.created_at),
        }
        for t in biggest_sorted
    ]

    return {
        "week_start": week_start.isoformat(),
        "weekly_spent": weekly_spent,
        "avg_per_day": avg_per_day,
        "top_stores": top_stores_list,
        "largest_tx": largest_tx,
        "biggest_txs": biggest_txs,
    }