# app.py
from __future__ import annotations
import os
import json
import smtplib
import sqlite3
import logging
import secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List
from email.mime.text import MIMEText

from flask import (
    Flask, request, redirect, url_for, render_template,
    flash, jsonify, Response
)
from flask_login import (
    LoginManager, UserMixin, login_user, current_user,
    login_required, logout_user
)
from dotenv import load_dotenv
import yaml
import stripe

# =============================================================================
# Config
# =============================================================================
BASE_DIR = Path(__file__).parent.resolve()
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["TEMPLATES_AUTO_RELOAD"] = True
# En prod HTTPS :
# app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True)

# URLs
BASE_URL = os.getenv("BASE_URL") or os.getenv("PUBLIC_BASE_URL") or ""
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or BASE_URL

# SMTP (optionnel)
SMTP_USER = os.getenv("SMTP_USER") or ""
SMTP_PASS = os.getenv("SMTP_PASS") or ""
SMTP_FROM = SMTP_USER or "noreply@bettybots.local"

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY") or ""
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID") or ""      # price mensuel (9,99€)
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET") or ""
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# =============================================================================
# DB (SQLite)
# =============================================================================
def _pick_default_db_path() -> Path:
    p = os.getenv("DB_PATH")
    if p:
        return Path(p)
    if any(os.getenv(k) for k in ("VERCEL", "VERCEL_URL", "VERCEL_ENV", "AWS_LAMBDA_FUNCTION_NAME")):
        return Path("/tmp/app.db")
    return BASE_DIR / "app.db"

DB_PATH: Path = _pick_default_db_path()

def _safe_connect(path: Path) -> sqlite3.Connection:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        fallback = Path("/tmp/app.db")
        if path != fallback:
            try:
                fallback.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            conn = sqlite3.connect(fallback)
            conn.row_factory = sqlite3.Row
            globals()["DB_PATH"] = fallback
            return conn
        raise

def _db() -> sqlite3.Connection:
    return _safe_connect(DB_PATH)

def db_exec(sql: str, params: Tuple[Any, ...] = ()) -> None:
    with _db() as c:
        c.execute(sql, params)
        c.commit()

def db_query(sql: str, params: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
    with _db() as c:
        cur = c.execute(sql, params)
        return cur.fetchall()

def db_one(sql: str, params: Tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
    rows = db_query(sql, params)
    return rows[0] if rows else None

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def init_db() -> None:
    db_exec("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      first_name TEXT, last_name TEXT,
      email TEXT UNIQUE,
      is_active_subscription INTEGER DEFAULT 0,
      is_guest INTEGER DEFAULT 1,
      created_at TEXT NOT NULL
    )""")
    db_exec("""
    CREATE TABLE IF NOT EXISTS bots(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER UNIQUE NOT NULL,
      name TEXT, metier TEXT,
      avatar_url TEXT, color_hex TEXT,
      persona TEXT, welcome_text TEXT,
      shape TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    db_exec("""
    CREATE TABLE IF NOT EXISTS leads(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      bot_id INTEGER NOT NULL,
      name TEXT, email TEXT, message TEXT,
      extra_json TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id),
      FOREIGN KEY(bot_id) REFERENCES bots(id)
    )""")
    db_exec("""
    CREATE TABLE IF NOT EXISTS subscriptions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      provider TEXT CHECK(provider='stripe') NOT NULL,
      external_id TEXT, status TEXT,
      current_period_end TEXT,
      amount_cents INTEGER, currency TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    db_exec("""
    CREATE TABLE IF NOT EXISTS bot_state(
      user_id INTEGER NOT NULL,
      bot_id INTEGER NOT NULL,
      state_json TEXT,
      PRIMARY KEY(user_id, bot_id)
    )""")

init_db()
app.logger.setLevel(logging.INFO)
app.logger.info(f"DB_PATH in use: {DB_PATH}")

# =============================================================================
# Auth (auto-guest)
# =============================================================================
login_manager = LoginManager(app)
login_manager.login_view = "home"  # si non loggé, renvoie vers "/"

class User(UserMixin):
    def __init__(self, row: sqlite3.Row):
        self.id = row["id"]
        self.first_name = row["first_name"]
        self.last_name = row["last_name"]
        self.email = row["email"]
        self.is_active_subscription = bool(row["is_active_subscription"])
        self.is_guest = bool(row["is_guest"])

@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    row = db_one("SELECT * FROM users WHERE id=?", (user_id,))
    return User(row) if row else None

def ensure_guest_user() -> None:
    """Crée automatiquement un user 'guest' + connexion si personne n'est loggé."""
    if current_user.is_authenticated:
        return
    if request.path.startswith("/webhook/stripe"):
        return
    guest_email = f"guest-{secrets.token_hex(4)}@guest.local"
    db_exec(
        "INSERT INTO users(first_name,last_name,email,is_guest,created_at) VALUES(?,?,?,?,?)",
        ("", "", guest_email, 1, now_iso()),
    )
    row = db_one("SELECT * FROM users WHERE email=?", (guest_email,))
    login_user(User(row), remember=True)
    app.logger.info("Guest user created: %s", guest_email)

@app.before_request
def _auto_guest():
    ensure_guest_user()

# =============================================================================
# Helpers bot
# =============================================================================
def send_mail(to_email: str, subject: str, html: str) -> None:
    if not (SMTP_USER and SMTP_PASS):
        app.logger.warning("SMTP not configured; skip email to %s", to_email)
        return
    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, [to_email], msg.as_string())
    except Exception as e:
        app.logger.warning("SMTP send failed: %s", e)

def get_bot(user_id: int) -> Optional[sqlite3.Row]:
    return db_one("SELECT * FROM bots WHERE user_id=?", (user_id,))

def create_or_update_bot(user_id: int, form: Dict[str, Any]) -> sqlite3.Row:
    existing = get_bot(user_id)
    payload = {
        "name": form.get("name") or "Mon Betty Bot",
        "metier": form.get("metier") or "",
        "avatar_url": form.get("avatar_url") or "",
        "color_hex": form.get("color_hex") or "#4F46E5",
        "persona": form.get("persona") or "Assistant",
        "welcome_text": form.get("welcome_text") or "Bonjour 👋",
        "shape": form.get("shape") or "square",
    }
    if existing:
        db_exec("""
        UPDATE bots SET name=?, metier=?, avatar_url=?, color_hex=?, persona=?, welcome_text=?, shape=?
        WHERE id=?""",
        (payload["name"], payload["metier"], payload["avatar_url"], payload["color_hex"],
         payload["persona"], payload["welcome_text"], payload["shape"], existing["id"]))
        return db_one("SELECT * FROM bots WHERE id=?", (existing["id"],))
    else:
        db_exec("""
        INSERT INTO bots(user_id,name,metier,avatar_url,color_hex,persona,welcome_text,shape,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (user_id, payload["name"], payload["metier"], payload["avatar_url"], payload["color_hex"],
         payload["persona"], payload["welcome_text"], payload["shape"], now_iso()))
        return db_one("SELECT * FROM bots WHERE user_id=?", (user_id,))

def bot_state_get(user_id: int, bot_id: int) -> dict:
    row = db_one("SELECT state_json FROM bot_state WHERE user_id=? AND bot_id=?", (user_id, bot_id))
    if not row or not row["state_json"]:
        return {"history": []}
    try:
        return json.loads(row["state_json"])
    except Exception:
        return {"history": []}

def bot_state_set(user_id: int, bot_id: int, state: dict) -> None:
    db_exec("""
    INSERT INTO bot_state(user_id, bot_id, state_json)
    VALUES(?,?,?)
    ON CONFLICT(user_id, bot_id) DO UPDATE SET state_json=excluded.state_json
    """, (user_id, bot_id, json.dumps(state, ensure_ascii=False)))

def bot_state_reset(user_id: int, bot_id: int) -> None:
    db_exec("DELETE FROM bot_state WHERE user_id=? AND bot_id=?", (user_id, bot_id))

def load_pack(metier_key: str) -> dict:
    """Charge un pack YAML métier depuis templates/packs/<metier>.yaml"""
    if not metier_key:
        return {}
    pack_path = BASE_DIR / "templates" / "packs" / f"{metier_key}.yaml"
    if not pack_path.exists():
        return {}
    with open(pack_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def rule_reply(pack: dict, user_msg: str, history: List[dict], cfg: dict) -> str:
    if not history:
        return pack.get("welcome") or cfg.get("welcome") or "Bonjour, je vous écoute 🙂"
    text = (user_msg or "").lower()
    leads = pack.get("lead_fields") or []
    if any(k in text for k in ["budget", "prix"]) and "budget" in leads:
        return "Quel est votre budget approximatif ?"
    if any(k in text for k in ["ville", "localisation"]) and "ville" in leads:
        return "Dans quelle ville cherchez-vous ?"
    return "Merci. Pouvez-vous préciser votre besoin pour que je le qualifie ?"

# =============================================================================
# Routes — Page 1 : DASHBOARD (config du bot)
# =============================================================================
@app.get("/")
def home() -> Response:
    return redirect(url_for("dashboard"))

@app.get("/dashboard")
@login_required
def dashboard() -> Response:
    # convertit Row -> dict pour éviter les erreurs Jinja
    row = get_bot(int(current_user.id))
    bot: Optional[dict] = dict(row) if row else None

    packs_dir = BASE_DIR / "templates" / "packs"
    metiers: List[str] = []
    if packs_dir.exists():
        metiers = [p.stem for p in sorted(packs_dir.glob("*.yaml"))]

    return render_template("dashboard.html", bot=bot, metiers=metiers)

@app.post("/dashboard/generate")
@login_required
def dashboard_generate() -> Response:
    form = {
        "name": request.form.get("name"),
        "metier": request.form.get("metier"),
        "avatar_url": request.form.get("avatar_url"),
        "color_hex": request.form.get("color_hex"),
        "persona": request.form.get("persona"),
        "welcome_text": request.form.get("welcome_text"),
        "shape": request.form.get("shape"),
    }
    bot_row = create_or_update_bot(int(current_user.id), form)
    # init state si besoin
    state = bot_state_get(int(current_user.id), bot_row["id"])
    if not state.get("history"):
        state["history"] = []
        bot_state_set(int(current_user.id), bot_row["id"], state)
    return redirect(url_for("preview"))

# =============================================================================
# Routes — Page 2 : PREVIEW / TEST
# =============================================================================
@app.get("/preview")
@login_required
def preview() -> Response:
    row = get_bot(int(current_user.id))
    if not row:
        flash("Configurez votre bot d’abord.", "warning")
        return redirect(url_for("dashboard"))
    bot = dict(row)

    def getv(obj, key): return (obj.get(key) if obj else None)

    cfg = {
        "name":       getv(bot, "name") or "Mon Betty Bot",
        "metier":     getv(bot, "metier") or "",
        "avatar_url": getv(bot, "avatar_url") or "",
        "color_hex":  getv(bot, "color_hex") or "#4F46E5",
        "persona":    getv(bot, "persona") or "Assistant",
        "welcome":    getv(bot, "welcome_text") or "Bonjour 👋",
        "shape":      getv(bot, "shape") or "square",
    }
    warning = None if current_user.is_active_subscription else \
        "Aperçu de démonstration (l’abonnement activera les conversations réelles)."

    return render_template("test.html", bot=bot, user=current_user, warning=warning, cfg=cfg)

@app.post("/preview/reset")
@login_required
def preview_reset() -> Response:
    row = get_bot(int(current_user.id))
    if row:
        bot_state_reset(int(current_user.id), row["id"])
    return redirect(url_for("preview"))

@app.post("/dashboard/save_and_pay")
@login_required
def save_and_pay() -> Response:
    return redirect(url_for("pay"))

# API bot (utilisée par la page preview/test)
@app.post("/api/chat")
@login_required
def api_chat() -> Response:
    row = get_bot(int(current_user.id))
    if not row:
        return jsonify({"error": "bot_not_found"}), 404
    bot_id = row["id"]

    data = request.get_json(silent=True) or {}
    user_msg = (data.get("message") or "").strip()
    if not user_msg:
        return jsonify({"error": "empty_message"}), 400

    state = bot_state_get(int(current_user.id), bot_id)
    history = state.get("history", [])
    pack = load_pack((row["metier"] or "")) or {}
    cfg = {"name": (row["name"] or "Mon Betty Bot"), "welcome": (row["welcome_text"] or "Bonjour 👋")}
    reply = rule_reply(pack, user_msg, history, cfg)

    now = datetime.now(timezone.utc).isoformat()
    history.append({"role": "user", "content": user_msg, "ts": now})
    history.append({"role": "bot", "content": reply, "ts": now})
    if len(history) > 60:
        history = history[-60:]
    state["history"] = history
    bot_state_set(int(current_user.id), bot_id, state)
    return jsonify({"reply": reply, "history": history[-10:]})

# =============================================================================
# Routes — Page 3 : PAIEMENT
# =============================================================================
@app.get("/pay")
@login_required
def pay() -> Response:
    stripe_enabled = bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID)
    return render_template("pay.html", stripe_enabled=stripe_enabled)

@app.post("/pay/stripe")
@login_required
def pay_stripe() -> Response:
    if not (STRIPE_SECRET_KEY and STRIPE_PRICE_ID):
        flash("Paiement indisponible pour le moment.", "error")
        return redirect(url_for("pay"))

    success_url = f"{PUBLIC_BASE_URL or BASE_URL}/confirm?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{PUBLIC_BASE_URL or BASE_URL}/pay"

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        automatic_tax={"enabled": True}
    )
    return redirect(session.url, code=303)

# Webhook Stripe : active l’abonnement + met à jour l’email si guest
@app.post("/webhook/stripe")
def webhook_stripe() -> Response:
    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        app.logger.warning("Stripe webhook invalid: %s", e)
        return Response(status=400)

    if event["type"] in ("checkout.session.completed", "invoice.paid", "customer.subscription.updated"):
        data = event["data"]["object"]
        email = (data.get("customer_details") or {}).get("email") or ""
        user_row = db_one("SELECT * FROM users WHERE email=?", (email.lower(),)) if email else None
        if not user_row and email:
            guest_row = db_one("SELECT * FROM users WHERE is_guest=1 ORDER BY id DESC LIMIT 1")
            if guest_row:
                db_exec("UPDATE users SET email=?, is_guest=0 WHERE id=?", (email.lower(), guest_row["id"]))
                user_row = db_one("SELECT * FROM users WHERE id=?", (guest_row["id"],))
        if user_row:
            db_exec("UPDATE users SET is_active_subscription=1 WHERE id=?", (user_row["id"],))
            amount_cents = (data.get("amount_total") or 0) or (data.get("amount_paid") or 0)
            currency = (data.get("currency") or "eur").upper()
            db_exec("""
            INSERT INTO subscriptions(user_id, provider, external_id, status, current_period_end,
                                      amount_cents, currency, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """, (user_row["id"], "stripe", str(data.get("id")), (data.get("status") or "active"),
                  datetime.now(timezone.utc).isoformat(), int(amount_cents or 0), currency, now_iso(), now_iso()))
            if email:
                send_mail(email, "Votre abonnement Betty Bot est actif", "<p>Merci pour votre confiance 🙏</p>")

    return Response(status=200)

# =============================================================================
# Routes — Page 4 : CONFIRMATION (récap + code à copier)
# =============================================================================
@app.get("/confirm")
@login_required
def confirm() -> Response:
    session_id = request.args.get("session_id") or ""
    amount = None
    currency = "EUR"
    if session_id and STRIPE_SECRET_KEY:
        try:
            s = stripe.checkout.Session.retrieve(session_id)
            amount = (s.get("amount_total") or 0) / 100.0
            currency = (s.get("currency") or "eur").upper()
        except Exception:
            pass

    row = get_bot(int(current_user.id))
    bot = dict(row) if row else None

    embed_code = ""
    if bot:
        embed_code = (
            f'<iframe src="{PUBLIC_BASE_URL or BASE_URL}/bot?user_id={current_user.id}&bot_id={bot["id"]}" '
            f'width="420" height="620" style="border:0;border-radius:12px;"></iframe>'
        )

    return render_template(
        "confirm.html",
        price_label="9,99 € / mois",
        amount=amount, currency=currency,
        bot=bot, user=current_user,
        embed_code=embed_code,
        base_url=(PUBLIC_BASE_URL or BASE_URL),
    )

# Widget embarqué simple
@app.get("/bot")
def public_bot_iframe() -> Response:
    user_id = int(request.args.get("user_id", "0"))
    bot_id = int(request.args.get("bot_id", "0"))
    row = db_one("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, user_id))
    if not row:
        return "<p>Bot introuvable</p>"
    bot = dict(row)
    return render_template("bot.html", bot=bot)

# =============================================================================
# Erreurs
# =============================================================================
@app.errorhandler(404)
def _404(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def _500(e):
    app.logger.exception("Unhandled error on %s", request.path)
    return render_template("500.html"), 500

# =============================================================================
# Run
# =============================================================================
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
