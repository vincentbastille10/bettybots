# app.py
from __future__ import annotations
import os
import json
import smtplib
import sqlite3
import logging
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

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["TEMPLATES_AUTO_RELOAD"] = True
# (Optionnel en prod HTTPS)
# app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True)

# Base/public URL
BASE_URL = os.getenv("BASE_URL") or os.getenv("PUBLIC_BASE_URL") or ""
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or BASE_URL

# SMTP (optionnel)
SMTP_USER = os.getenv("SMTP_USER") or ""
SMTP_PASS = os.getenv("SMTP_PASS") or ""
SMTP_FROM = SMTP_USER or "noreply@bettybots.local"

# Stripe (CB abonnement)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY") or ""
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID") or ""  # price mensuel
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET") or ""
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# -----------------------------------------------------------------------------
# DB helpers (SQLite) — compatibles Vercel (/tmp en fallback)
# -----------------------------------------------------------------------------
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

def init_db() -> None:
    db_exec("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      first_name TEXT, last_name TEXT,
      email TEXT UNIQUE NOT NULL,
      is_active_subscription INTEGER DEFAULT 0,
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

# -----------------------------------------------------------------------------
# Login
# -----------------------------------------------------------------------------
login_manager = LoginManager(app)
# ⬇️ IMPORTANT : ne redirige plus vers /signup, mais vers la landing
login_manager.login_view = "landing"

class User(UserMixin):
    def __init__(self, row: sqlite3.Row):
        self.id = row["id"]
        self.first_name = row["first_name"]
        self.last_name = row["last_name"]
        self.email = row["email"]
        self.is_active_subscription = bool(row["is_active_subscription"])

@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    row = db_one("SELECT * FROM users WHERE id=?", (user_id,))
    return User(row) if row else None

# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

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

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def landing() -> Response:
    return render_template("landing.html")

@app.route("/index", methods=["GET"])
def index() -> Response:
    return redirect(url_for("landing"))

# ⬇️ Signup : GET redirige vers landing ; POST crée/connexion → dashboard
@app.route("/signup", methods=["GET", "POST"])
def signup() -> Response:
    if request.method == "POST":
        first_name = (request.form.get("first_name") or "").strip()
        last_name  = (request.form.get("last_name") or "").strip()
        email      = (request.form.get("email") or "").strip().lower()

        if not email:
            flash("Email requis.", "error")
            return redirect(url_for("landing"))

        existing = db_one("SELECT * FROM users WHERE email=?", (email,))
        if existing:
            user = User(existing)
            login_user(user, remember=True)
            flash("Bienvenue 👋", "success")
            return redirect(url_for("dashboard"))

        db_exec("INSERT INTO users(first_name,last_name,email,created_at) VALUES(?,?,?,?)",
                (first_name, last_name, email, now_iso()))
        row = db_one("SELECT * FROM users WHERE email=?", (email,))
        login_user(User(row), remember=True)
        flash("Inscription réussie.", "success")
        return redirect(url_for("dashboard"))

    # GET -> on ne montre plus de page d'inscription
    return redirect(url_for("landing"))

@app.route("/logout")
def logout() -> Response:
    logout_user()
    return redirect(url_for("landing"))

@app.route("/dashboard", methods=["GET"])
@login_required
def dashboard() -> Response:
    bot = get_bot(int(current_user.id))
    packs_dir = BASE_DIR / "templates" / "packs"
    metiers: List[str] = []
    if packs_dir.exists():
        metiers = [p.stem for p in sorted(packs_dir.glob("*.yaml"))]
    return render_template("dashboard.html", bot=bot, metiers=metiers)

@app.route("/dashboard/save", methods=["POST"])
@login_required
def dashboard_save() -> Response:
    form = {
        "name": request.form.get("name"),
        "metier": request.form.get("metier"),
        "avatar_url": request.form.get("avatar_url"),
        "color_hex": request.form.get("color_hex"),
        "persona": request.form.get("persona"),
        "welcome_text": request.form.get("welcome_text"),
        "shape": request.form.get("shape"),  # square / rounded / circle
    }
    create_or_update_bot(int(current_user.id), form)
    flash("Configuration enregistrée.", "success")
    return redirect(url_for("test_page"))

# ---- Test (chat sandbox)
@app.route("/test")
@login_required
def test_page() -> Response:
    bot = get_bot(int(current_user.id))
    warning = None if current_user.is_active_subscription else \
        "Votre abonnement est inactif. Souscrivez pour débloquer les conversations réelles."
    template_name = "test.html" if (BASE_DIR / "templates" / "test.html").exists() else "chat.html"

    def getv(obj, key): return (obj[key] if obj and key in obj.keys() else None)

    cfg = {
        "name":       getv(bot, "name") or "Mon Betty Bot",
        "metier":     getv(bot, "metier") or "",
        "avatar_url": getv(bot, "avatar_url") or "",
        "color_hex":  getv(bot, "color_hex") or "#4F46E5",
        "persona":    getv(bot, "persona") or "Assistant",
        "welcome":    getv(bot, "welcome_text") or "Bonjour 👋",
        "shape":      getv(bot, "shape") or "square",
    }
    return render_template(template_name, bot=bot, user=current_user, warning=warning, cfg=cfg)

@app.route("/test/reset", methods=["POST"])
@login_required
def test_reset() -> Response:
    bot = get_bot(int(current_user.id))
    if bot:
        bot_state_reset(int(current_user.id), bot["id"])
    flash("Conversation réinitialisée.", "success")
    return redirect(url_for("test_page"))

# ---- API chat
@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat() -> Response:
    bot = get_bot(int(current_user.id))
    if not bot:
        return jsonify({"error": "bot_not_found"}), 404
    data = request.get_json(silent=True) or {}
    user_msg = (data.get("message") or "").strip()
    if not user_msg:
        return jsonify({"error": "empty_message"}), 400

    state = bot_state_get(int(current_user.id), bot["id"])
    history = state.get("history", [])
    pack = load_pack(bot["metier"] or "") or {}
    cfg = {
        "name": bot["name"] or "Mon Betty Bot",
        "welcome": bot["welcome_text"] or "Bonjour 👋",
    }
    reply = rule_reply(pack, user_msg, history, cfg)

    now = datetime.now(timezone.utc).isoformat()
    history.append({"role": "user", "content": user_msg, "ts": now})
    history.append({"role": "bot", "content": reply, "ts": now})
    if len(history) > 60:
        history = history[-60:]
    state["history"] = history
    bot_state_set(int(current_user.id), bot["id"], state)
    return jsonify({"reply": reply, "history": history[-10:]})

# ---- API lead (widget embarqué)
@app.route("/api/lead", methods=["POST"])
def api_lead() -> Response:
    user_id = request.form.get("user_id") or (request.json or {}).get("user_id")  # type: ignore
    bot_id  = request.form.get("bot_id")  or (request.json or {}).get("bot_id")   # type: ignore
    name    = request.form.get("name")    or (request.json or {}).get("name")     # type: ignore
    email   = request.form.get("email")   or (request.json or {}).get("email")    # type: ignore
    message = request.form.get("message") or (request.json or {}).get("message")  # type: ignore
    extra   = request.form.get("extra_json") or (request.json or {}).get("extra_json")  # type: ignore

    if not (user_id and bot_id and email):
        return jsonify({"error": "missing_params"}), 400

    db_exec("""INSERT INTO leads(user_id,bot_id,name,email,message,extra_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (int(user_id), int(bot_id), name or "", email, message or "", extra or "{}", now_iso()))

    owner = db_one("SELECT email FROM users WHERE id=?", (int(user_id),))
    if owner:
        html = f"""
        <h3>Nouveau lead</h3>
        <p><strong>Nom:</strong> {name or ''}<br>
        <strong>Email:</strong> {email}<br>
        <strong>Message:</strong> {message or ''}</p>
        """
        send_mail(owner["email"], "Nouveau lead via votre bot", html)

    return jsonify({"ok": True})

# ---- Paiement (Stripe abonnement mensuel)
@app.route("/pay", methods=["GET"])
@login_required
def pay() -> Response:
    stripe_enabled = bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID)
    return render_template("pay.html", stripe_enabled=stripe_enabled)

@app.route("/pay/stripe", methods=["POST"])
@login_required
def pay_stripe() -> Response:
    if not (STRIPE_SECRET_KEY and STRIPE_PRICE_ID):
        flash("Paiement indisponible pour le moment.", "error")
        return redirect(url_for("dashboard"))
    success_url = f"{PUBLIC_BASE_URL or BASE_URL}/confirm?provider=stripe&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{PUBLIC_BASE_URL or BASE_URL}/dashboard"
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return redirect(session.url, code=303)

@app.route("/webhook/stripe", methods=["POST"])
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
        user_row = db_one("SELECT * FROM users WHERE email=?", (email.lower(),))
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
            html = "<p>Votre abonnement est actif. Merci !</p>"
            send_mail(user_row["email"], "Confirmation d’abonnement", html)

    return Response(status=200)

@app.route("/confirm", methods=["GET"])
@login_required
def confirm() -> Response:
    provider = request.args.get("provider") or "stripe"
    session_id = request.args.get("session_id") or ""
    amount = None
    currency = "EUR"
    if provider == "stripe" and session_id and STRIPE_SECRET_KEY:
        try:
            s = stripe.checkout.Session.retrieve(session_id)
            amount = (s.get("amount_total") or 0) / 100.0
            currency = (s.get("currency") or "eur").upper()
        except Exception:
            pass
    bot = get_bot(int(current_user.id))
    return render_template(
        "confirm.html",
        provider=provider,
        amount=amount,
        currency=currency,
        bot=bot,
        user=current_user,
        base_url=(PUBLIC_BASE_URL or BASE_URL),
    )

# -----------------------------------------------------------------------------
# Erreurs
# -----------------------------------------------------------------------------
@app.errorhandler(404)
def _404(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def _500(e):
    app.logger.exception("Unhandled error on %s", request.path)
    return render_template("500.html"), 500

# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
