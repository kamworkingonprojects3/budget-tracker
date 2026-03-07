from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles

from sqlalchemy.orm import Session
from google.oauth2.credentials import Credentials
from passlib.context import CryptContext

import json
import os
import traceback
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone

import stripe

from database import engine, SessionLocal
from models import Base, User, Budget, Transaction, GmailToken, ProcessedEmail
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

pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

IS_PROD = os.getenv("RENDER", "").lower() == "true" or os.getenv("ENV", "").lower() == "prod"

# ---------------- Stripe setup ----------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
APP_URL = os.getenv("APP_URL", "http://127.0.0.1:8000")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "65x23er"),
    same_site="lax",
    https_only=IS_PROD,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "https://budget-tracker-ke3y.onrender.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")


@app.exception_handler(Exception)
async def debug_exception_handler(request: Request, exc: Exception):
    print("\n🔥 UNHANDLED ERROR:", repr(exc))
    traceback.print_exc()

    return JSONResponse(
        status_code=500,
        content={
            "error": "server_crash",
            "detail": str(exc),
            "path": str(request.url),
        },
    )


# ---------------- DB Dependency ----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------- Helpers ----------------
def start_of_week_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def get_current_user(request: Request, db: Session) -> User:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Not logged in")

    user = db.query(User).filter(User.id == uid).first()
    if not user:
        request.session.pop("user_id", None)
        raise HTTPException(status_code=401, detail="Invalid session")

    return user


def require_premium(user: User):
    if not user.premium:
        raise HTTPException(status_code=403, detail="Premium required")


def detect_subscriptions(transactions: list[Transaction]):
    grouped: dict[str, list[float]] = {}

    for tx in transactions:
        name = (tx.store or "").strip()
        if not name:
            continue
        grouped.setdefault(name, []).append(float(tx.amount))

    results = []
    for merchant, amounts in grouped.items():
        if len(amounts) < 3:
            continue

        avg_amount = sum(amounts) / len(amounts)
        if max(amounts) - min(amounts) <= 2.0:
            results.append({
                "store": merchant,
                "estimated_monthly": round(avg_amount, 2),
                "payments_found": len(amounts),
                "estimated_yearly": round(avg_amount * 12, 2),
            })

    results.sort(key=lambda x: x["estimated_monthly"], reverse=True)
    return results


def build_ai_analysis(user: User, txs: list[Transaction]):
    if not txs:
        return {
            "summary": "No transactions found yet.",
            "weekly_total": 0.0,
            "top_category": None,
            "top_store": None,
            "largest_transaction": None,
            "subscriptions": [],
            "tips": [],
        }

    total = round(sum(float(t.amount) for t in txs), 2)

    category_totals: dict[str, float] = {}
    store_totals: dict[str, float] = {}

    for t in txs:
        category = (t.category or "Uncategorized").strip()
        store = (t.store or "Unknown").strip()

        category_totals[category] = category_totals.get(category, 0.0) + float(t.amount)
        store_totals[store] = store_totals.get(store, 0.0) + float(t.amount)

    top_category = max(category_totals.items(), key=lambda x: x[1]) if category_totals else None
    top_store = max(store_totals.items(), key=lambda x: x[1]) if store_totals else None
    largest = max(txs, key=lambda t: float(t.amount))

    subscriptions = detect_subscriptions(txs)

    tips = []
    if top_category:
        tips.append(
            f"Your highest spending category is {top_category[0]} at ${round(top_category[1], 2)}."
        )
    if top_store:
        tips.append(
            f"You spent the most at {top_store[0]} with ${round(top_store[1], 2)} total."
        )
    if subscriptions:
        yearly_total = round(sum(x["estimated_yearly"] for x in subscriptions), 2)
        tips.append(
            f"Possible recurring subscriptions detected. Estimated yearly cost: ${yearly_total}."
        )
    if total > 0:
        tips.append(
            f"If you cut this week's spending by 10%, you could save about ${round(total * 0.10, 2)}."
        )

    summary = f"You spent ${total} this week."
    if top_category:
        summary += f" Most of your spending was in {top_category[0]}."
    if subscriptions:
        summary += f" I also found {len(subscriptions)} possible recurring subscriptions."

    return {
        "summary": summary,
        "weekly_total": total,
        "top_category": {
            "name": top_category[0],
            "amount": round(top_category[1], 2),
        } if top_category else None,
        "top_store": {
            "name": top_store[0],
            "amount": round(top_store[1], 2),
        } if top_store else None,
        "largest_transaction": {
            "id": largest.id,
            "store": largest.store,
            "category": largest.category,
            "amount": float(largest.amount),
            "created_at": iso(largest.created_at),
        },
        "subscriptions": subscriptions,
        "tips": tips,
    }


# ---------------- UI pages ----------------
@app.get("/")
def serve_ui():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/insights")
def serve_insights():
    return FileResponse(os.path.join(FRONTEND_DIR, "insights.html"))


@app.get("/ai-analysis")
def serve_ai_analysis(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_premium(user)
    return FileResponse(os.path.join(FRONTEND_DIR, "ai_analysis.html"))


# ---------------- Auth ----------------
@app.post("/auth/signup")
def signup(email: str, password: str, request: Request, db: Session = Depends(get_db)):
    email = (email or "").strip().lower()

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already exists")

    user = User(email=email, password_hash=pwd.hash(password))
    db.add(user)
    db.commit()
    db.refresh(user)

    request.session["user_id"] = user.id
    return {"ok": True, "id": user.id, "email": user.email, "premium": user.premium}


@app.post("/auth/login")
def login(email: str, password: str, request: Request, db: Session = Depends(get_db)):
    email = (email or "").strip().lower()
    user = db.query(User).filter(User.email == email).first()

    if not user or not pwd.verify(password, user.password_hash):
        raise HTTPException(status_code=400, detail="Invalid email or password")

    request.session["user_id"] = user.id
    return {"ok": True, "id": user.id, "email": user.email, "premium": user.premium}


@app.post("/auth/logout")
def logout(request: Request):
    request.session.pop("user_id", None)
    return {"ok": True}


@app.get("/auth/me")
def me(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return {"id": user.id, "email": user.email, "premium": user.premium}


# ---------------- Premium ----------------
@app.get("/premium/status")
def premium_status(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return {"premium": user.premium}


@app.get("/create-checkout-session")
def create_checkout_session(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Missing STRIPE_SECRET_KEY")
    if not STRIPE_PRICE_ID:
        raise HTTPException(status_code=500, detail="Missing STRIPE_PRICE_ID")

    checkout_session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        line_items=[
            {
                "price": STRIPE_PRICE_ID,
                "quantity": 1,
            }
        ],
        success_url=f"{APP_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_URL}/cancel",
        client_reference_id=str(user.id),
        metadata={
            "user_id": str(user.id),
            "email": user.email,
        },
    )

    return RedirectResponse(checkout_session.url, status_code=303)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(
                payload=payload,
                sig_header=sig_header,
                secret=STRIPE_WEBHOOK_SECRET,
            )
        else:
            event = json.loads(payload.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")

    event_type = event["type"]

    if event_type == "checkout.session.completed":
        session_data = event["data"]["object"]
        user_id = session_data.get("metadata", {}).get("user_id") or session_data.get("client_reference_id")

        if user_id:
            user = db.query(User).filter(User.id == int(user_id)).first()
            if user:
                user.premium = True
                db.commit()

    elif event_type in ["customer.subscription.deleted", "customer.subscription.updated"]:
        subscription = event["data"]["object"]
        status = subscription.get("status")
        metadata = subscription.get("metadata", {})
        user_id = metadata.get("user_id")

        if user_id and status in ["canceled", "unpaid", "incomplete_expired"]:
            user = db.query(User).filter(User.id == int(user_id)).first()
            if user:
                user.premium = False
                db.commit()

    return {"received": True}


@app.get("/success")
def stripe_success(session_id: str | None = None, request: Request = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    if session_id and stripe.api_key:
        try:
            session_obj = stripe.checkout.Session.retrieve(session_id)
            if session_obj and session_obj.payment_status in ["paid", "no_payment_required"]:
                user.premium = True
                db.commit()
        except Exception as e:
            print("Stripe success verify failed:", e)

    return RedirectResponse(url="/?upgraded=true", status_code=302)


@app.get("/cancel")
def stripe_cancel():
    return RedirectResponse(url="/?checkout=cancelled", status_code=302)


# ---------------- Budget (per-user) ----------------
@app.get("/budget")
def get_budget(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    budget = db.query(Budget).filter(Budget.user_id == user.id).first()
    if not budget:
        return {"error": "No budget set yet. Use /set_budget first."}

    return {"weekly_limit": float(budget.weekly_limit), "remaining": float(budget.remaining)}


@app.post("/set_budget")
def set_budget(amount: float, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    if amount is None or amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be > 0")

    budget = db.query(Budget).filter(Budget.user_id == user.id).first()
    if budget is None:
        budget = Budget(user_id=user.id, weekly_limit=float(amount), remaining=float(amount))
        db.add(budget)
    else:
        budget.weekly_limit = float(amount)
        budget.remaining = float(amount)

    db.commit()
    db.refresh(budget)
    return {"weekly_limit": float(budget.weekly_limit), "remaining": float(budget.remaining)}


@app.get("/budget_status")
def budget_status(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    budget = db.query(Budget).filter(Budget.user_id == user.id).first()
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
def reset_week(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    budget = db.query(Budget).filter(Budget.user_id == user.id).first()
    if not budget:
        return {"error": "No budget set yet. Use /set_budget first."}

    budget.remaining = float(budget.weekly_limit)
    db.commit()
    db.refresh(budget)

    return {
        "message": "Weekly budget reset",
        "weekly_limit": float(budget.weekly_limit),
        "remaining": float(budget.remaining),
    }


# ---------------- Transactions (per-user) ----------------
@app.post("/add_transaction")
def add_transaction(
    request: Request,
    amount: float,
    store: str,
    category: str = "Uncategorized",
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)

    budget = db.query(Budget).filter(Budget.user_id == user.id).first()
    if not budget:
        return {"error": "Set a budget first using /set_budget."}

    if amount is None or amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be > 0")
    if not store:
        store = "Manual"

    tx = Transaction(user_id=user.id, amount=float(amount), store=store, category=category)
    db.add(tx)

    budget.remaining = float(budget.remaining) - float(amount)

    db.commit()
    db.refresh(budget)

    return {"ok": True, "message": "Transaction added", "remaining": float(budget.remaining)}


@app.get("/transactions")
def list_transactions(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    txs = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id)
        .order_by(Transaction.id.desc())
        .all()
    )

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


@app.get("/weekly_summary")
def weekly_summary(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    week_start = start_of_week_utc(datetime.now(timezone.utc))

    txs = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.created_at >= week_start)
        .all()
    )

    total_spent = sum(float(t.amount) for t in txs)

    store_totals: dict[str, float] = {}
    for t in txs:
        store_totals[t.store] = store_totals.get(t.store, 0.0) + float(t.amount)

    store_breakdown = sorted(
        [{"store": k, "spent": round(v, 2)} for k, v in store_totals.items()],
        key=lambda x: x["spent"],
        reverse=True,
    )

    budget = db.query(Budget).filter(Budget.user_id == user.id).first()

    return {
        "week_start_utc": week_start.isoformat(),
        "weekly_spent": round(total_spent, 2),
        "store_breakdown": store_breakdown,
        "weekly_limit": float(budget.weekly_limit) if budget else None,
        "remaining": float(budget.remaining) if budget else None,
    }


@app.get("/spending_by_day")
def spending_by_day(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=6)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)

    txs = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.created_at >= start_dt)
        .all()
    )

    buckets = {(start + timedelta(days=i)).isoformat(): 0.0 for i in range(7)}
    for t in txs:
        d = t.created_at.astimezone(timezone.utc).date().isoformat()
        if d in buckets:
            buckets[d] += float(t.amount)

    labels = list(buckets.keys())
    values = [round(buckets[k], 2) for k in labels]
    total = round(sum(values), 2)

    short_labels = [l[5:] for l in labels]
    return {"labels": short_labels, "values": values, "total": total}


@app.get("/top_stores")
def top_stores(request: Request, db: Session = Depends(get_db), limit: int = 5):
    user = get_current_user(request, db)

    week_start = start_of_week_utc(datetime.now(timezone.utc))

    txs = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.created_at >= week_start)
        .all()
    )

    totals: dict[str, float] = {}
    for t in txs:
        totals[t.store] = totals.get(t.store, 0.0) + float(t.amount)

    ranked = sorted(
        [{"store": k, "spent": round(v, 2)} for k, v in totals.items()],
        key=lambda x: x["spent"],
        reverse=True,
    )

    return ranked[: max(1, int(limit))]


# ---------------- Gmail OAuth (per-user) ----------------
@app.get("/gmail/login")
def gmail_login(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    try:
        flow = make_flow()
        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )

        request.session["oauth_state"] = state
        request.session["code_verifier"] = getattr(flow, "code_verifier", None)

        print("✅ /gmail/login created session", {
            "user_id": user.id,
            "state": state,
            "has_code_verifier": bool(request.session.get("code_verifier")),
        })

        return RedirectResponse(auth_url)

    except Exception as e:
        print("❌ /gmail/login error:", repr(e))
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"gmail_login failed: {e}")


@app.get("/gmail/auth")
def gmail_auth(request: Request, code: str, state: str, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    saved_state = request.session.get("oauth_state")
    code_verifier = request.session.get("code_verifier")

    print("➡️ /gmail/auth received", {
        "user_id": user.id,
        "state_from_google": state,
        "saved_state": saved_state,
        "has_code_verifier": bool(code_verifier),
        "has_code": bool(code),
    })

    if not saved_state:
        raise HTTPException(
            status_code=400,
            detail="Missing oauth_state in session. Session cookie likely not saved/sent.",
        )

    if state != saved_state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch. Restart login at /gmail/login")

    try:
        flow = make_flow()
        flow.fetch_token(code=code, code_verifier=code_verifier)
        token_data = flow.credentials.to_json()
    except Exception as e:
        print("❌ Token exchange failed:", repr(e))
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")

    existing = db.query(GmailToken).filter(GmailToken.user_id == user.id).first()
    if existing is None:
        db.add(GmailToken(user_id=user.id, token_json=token_data))
    else:
        existing.token_json = token_data

    db.commit()

    request.session.pop("oauth_state", None)
    request.session.pop("code_verifier", None)

    params = urlencode({"gmail": "connected"})
    return RedirectResponse(url=f"/?{params}", status_code=302)


@app.post("/gmail/sync")
def gmail_sync(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    token_row = db.query(GmailToken).filter(GmailToken.user_id == user.id).first()
    if not token_row:
        return {"error": "Gmail not connected. Go to /gmail/login first."}

    budget = db.query(Budget).filter(Budget.user_id == user.id).first()
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
            .filter(ProcessedEmail.user_id == user.id, ProcessedEmail.gmail_message_id == msg_id)
            .first()
        )
        if already:
            skipped += 1
            continue

        snippet = get_message_snippet(service, msg_id)
        amount = extract_amount_from_text(snippet)

        db.add(ProcessedEmail(user_id=user.id, gmail_message_id=msg_id))

        if amount is None:
            no_amount += 1
            continue

        tx = Transaction(user_id=user.id, amount=float(amount), store="Email receipt", category="Email")
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


# ---------------- Insights Data (per-user) ----------------
@app.get("/insights_data")
def insights_data(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)

    week_start = start_of_week_utc(datetime.now(timezone.utc))

    txs = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.created_at >= week_start)
        .all()
    )

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
        {"id": t.id, "store": t.store, "amount": float(t.amount), "created_at": iso(t.created_at)}
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


# ---------------- Premium AI Analysis ----------------
@app.get("/api/ai-analysis")
def ai_analysis_api(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_premium(user)

    week_start = start_of_week_utc(datetime.now(timezone.utc))

    txs = (
        db.query(Transaction)
        .filter(Transaction.user_id == user.id, Transaction.created_at >= week_start)
        .all()
    )

    return build_ai_analysis(user, txs)