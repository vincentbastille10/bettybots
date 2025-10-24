from __future__ import annotations
import os, sqlite3, secrets, json, logging, re
from pathlib import Path
from typing import Optional
from contextlib import contextmanager
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    Response, send_from_directory, jsonify, g, make_response
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    current_user, login_required
)

# Dépendances optionnelles
try:
    import requests
except Exception:
    requests = None
import urllib.request

import stripe
import yaml

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

def pick_db_path() -> Path:
    """En prod serverless (Vercel), écrire dans /tmp. Sinon fichier local."""
    if os.getenv("DB_PATH"):
        return Path(os.getenv("DB_PATH"))
    if any(os.getenv(k) for k in ("VERCEL", "VERCEL_URL", "AWS_LAMBDA_FUNCTION_NAME", "VERCEL_ENV")):
        return Path("/tmp/payments.db")
    return BASE_DIR / "payments.db"

DB_PATH = pick_db_path()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))

# Stripe
def _env_int(key: str, default: int) -> int:
    try: return int(os.getenv(key, str(default)))
    except: return default

STRIPE_SECRET_KEY   = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_CENTS  = _env_int("STRIPE_PRICE_CENTS", 999)  # 9,99 €
STRIPE_CURRENCY     = os.getenv("STRIPE_CURRENCY", "eur")
PUBLIC_BASE_URL_ENV = (os.getenv("PUBLIC_BASE_URL") or os.getenv("BASE_URL") or "").strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    logger.warning("⚠️ STRIPE_SECRET_KEY manquante — paiements désactivés en prod.")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "root"

# ---------------------------------------------------------------------
# DB helpers (compatibles serverless)
# ---------------------------------------------------------------------
@contextmanager
def get_db():
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    try:
        yield g.db
    finally:
        pass

@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT,
            metier TEXT,
            avatar_url TEXT,
            color_hex TEXT,
            shape TEXT,
            persona TEXT,
            welcome_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)
        conn.commit()

def db_one(sql, params=()):
    with get_db() as c:
        cur = c.execute(sql, params)
        return cur.fetchone()

def db_exec(sql, params=()):
    with get_db() as c:
        c.execute(sql, params)
        c.commit()
        return True

with app.app_context():
    init_db()

# ---------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, id_: int, email: str):
        self.id = id_
        self.email = email

    @property
    def is_guest(self) -> bool:
        return str(self.email or "").endswith("@guest.local")

@login_manager.user_loader
def load_user(user_id: str):
    row = db_one("SELECT * FROM users WHERE id=?", (user_id,))
    return User(row["id"], row["email"]) if row else None

def sanitize_color(val: str) -> str:
    c = (val or "").strip()
    if not c.startswith("#"): c = "#" + c
    return c if len(c) in (4, 7) else "#4F46E5"

def is_guest_user() -> bool:
    return (not current_user.is_authenticated) or str(current_user.email).endswith("@guest.local")

def get_bot(user_id: int) -> Optional[sqlite3.Row]:
    return db_one("SELECT * FROM bots WHERE user_id=? LIMIT 1", (user_id,))

def base_url_for_checkout() -> str:
    """
    Base URL pour success/cancel Stripe.
    - si PUBLIC_BASE_URL est défini → on l'utilise
    - sinon → dérivé de la requête courante (http://host)
    - force http pour localhost/127.0.0.1 (sinon ERR_CONNECTION_REFUSED)
    """
    if PUBLIC_BASE_URL_ENV:
        base = PUBLIC_BASE_URL_ENV
    else:
        base = request.url_root.rstrip("/")
    low = base.lower()
    if low.startswith("https://localhost") or low.startswith("https://127.0.0.1"):
        return "http://" + base.split("://", 1)[1]
    return base

# ---------------------------------------------------------------------
# ✅ AVATARS — seule section modifiée
#    Local d'abord (tes fichiers), proxy externe en secours, puis placeholder
# ---------------------------------------------------------------------
EXTERNAL_AVATARS = {
    "agent_immo": "https://i.postimg.cc/zBWtZ8MH/Betty-Agent-immo-copie.jpg",
    "avocat":     "https://i.postimg.cc/bv4CBs6h/Betty-Avocate-copie.jpg",
    "medecin":    "https://i.postimg.cc/PxZ3sTcL/Betty-Medecine-copie.jpg",
}
DEFAULT_SLUG = "agent_immo"

_PLACEHOLDER_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg width="256" height="256" viewBox="0 0 256 256" xmlns="http://www.w3.org/2000/svg">
 <rect width="256" height="256" fill="#e5e7eb"/>
 <circle cx="128" cy="96" r="40" fill="#9ca3af"/>
 <rect x="56" y="148" width="144" height="60" rx="14" fill="#9ca3af"/>
</svg>"""

# mapping vers TES noms de fichiers exacts (vus sur ta capture GitHub)
_LOCAL_FILES = {
    "agent_immo": "Betty Agent immo copie.jpg",
    "avocat":     "Betty Avocate copie.jpg",
    "medecin":    "Betty Medecine copie.jpg",
}

@app.get("/avatar/<slug>")
def avatar_proxy(slug: str):
    """
    1) Sert le fichier local s'il existe (le plus fiable sur Vercel)
    2) Sinon tente le proxy externe (avec UA + Referer)
    3) Sinon renvoie un placeholder (200)
    """
    slug = (slug or "").strip().lower()
    if slug not in _LOCAL_FILES:
        slug = DEFAULT_SLUG

    # 1) Local d'abord
    local_name = _LOCAL_FILES[slug]
    local_path = BASE_DIR / "static" / local_name
    if local_path.exists():
        # Cache agressif côté CDN
        resp = send_from_directory(local_path.parent, local_path.name, max_age=86400)
        return resp

    # 2) Proxy externe en secours
    url = EXTERNAL_AVATARS.get(slug, EXTERNAL_AVATARS[DEFAULT_SLUG])
    try:
        if requests:
            r = requests.get(
                url,
                headers={
                    "User-Agent": "BettyBots/1.0",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Referer": "https://postimg.cc/",
                },
                timeout=6,
            )
            if r.status_code == 200 and r.content:
                resp = Response(r.content, mimetype=r.headers.get("Content-Type") or "image/jpeg")
                resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
                return resp
        # urllib fallback
        req = urllib.request.Request(url, headers={"User-Agent": "BettyBots/1.0", "Referer": "https://postimg.cc/"})
        with urllib.request.urlopen(req, timeout=6) as f:
            data = f.read()
            resp = Response(data, mimetype="image/jpeg")
            resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
            return resp
    except Exception:
        pass

    # 3) Placeholder propre (évite l'icône d'image cassée)
    return Response(_PLACEHOLDER_SVG, mimetype="image/svg+xml", headers={"Cache-Control":"public, max-age=86400"})

# ---------------------------------------------------------------------
# Routes principales (inchangées)
# ---------------------------------------------------------------------
@app.get("/")
def root():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    email = f"guest-{secrets.token_urlsafe(8)}@guest.local"
    db_exec("INSERT OR IGNORE INTO users(email) VALUES(?)", (email,))
    row = db_one("SELECT * FROM users WHERE email=?", (email,))
    if row:
        login_user(User(row["id"], row["email"]))
    return redirect(url_for("dashboard"))

@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    row = get_bot(int(current_user.id))
    bot = dict(row) if row else None

    if request.method == "GET":
        cfg = {
            "name": (bot or {}).get("name", "Mon Betty Bot"),
            "color_hex": (bot or {}).get("color_hex", "#4F46E5"),
            "welcome_text": (bot or {}).get("welcome_text", "Bonjour 👋"),
            "avatar_url": (bot or {}).get("avatar_url", "/avatar/agent_immo"),
        }
        return render_template("dashboard.html", bot=bot, cfg=cfg)

    # POST: sauvegarde rapide
    name = (request.form.get("bot_name") or "Mon Betty Bot").strip()[:100]
    metier = (request.form.get("pack_slug") or "Agent Immo").strip()
    color_hex = sanitize_color(request.form.get("color_hex") or "#4F46E5")
    welcome = (request.form.get("greeting") or "Bonjour 👋").strip()[:500]

    if bot:
        db_exec("""UPDATE bots SET name=?, metier=?, color_hex=?, welcome_text=? WHERE user_id=?""",
                (name, metier, color_hex, welcome, current_user.id))
    else:
        db_exec("""INSERT INTO bots(user_id, name, metier, color_hex, welcome_text) VALUES(?,?,?,?,?)""",
                (current_user.id, name, metier, color_hex, welcome))

    flash("✅ Bot sauvegardé.", "success")
    return redirect(url_for("preview"))

@app.get("/preview")
@login_required
def preview():
    row = get_bot(int(current_user.id))
    if not row:
        flash("Configure d'abord ton bot.", "warning")
        return redirect(url_for("dashboard"))
    return render_template("preview.html", bot=dict(row))

# ---------------------------- SIGNUP ---------------------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")
    email = (request.form.get("email") or "").strip().lower()
    email2 = (request.form.get("email_confirm") or "").strip().lower()
    if not email or email != email2 or not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        flash("Email invalide.", "warning")
        return redirect(url_for("signup"))

    row = db_one("SELECT * FROM users WHERE email=?", (email,))
    if row:
        login_user(User(row["id"], row["email"]), remember=True)
    else:
        db_exec("INSERT INTO users(email) VALUES(?)", (email,))
        row = db_one("SELECT * FROM users WHERE email=?", (email,))
        login_user(User(row["id"], row["email"]), remember=True)

    return redirect(url_for("pay"))

# ----------------------------- PAY -----------------------------------
@app.get("/pay")
@login_required
def pay():
    row = get_bot(int(current_user.id))
    bot = dict(row) if row else None
    return render_template("pay.html", bot=bot, stripe_enabled=bool(STRIPE_SECRET_KEY))

@app.post("/pay/stripe")
@login_required
def pay_stripe():
    if is_guest_user():
        flash("Créez votre compte avec un email valide avant de payer.", "warning")
        return redirect(url_for("pay"))
    if not STRIPE_SECRET_KEY:
        flash("Paiement indisponible (clé Stripe).", "error")
        return redirect(url_for("pay"))

    base = base_url_for_checkout()
    row = get_bot(int(current_user.id))
    bot = dict(row) if row else {}
    metier = (bot.get("metier") or "Générique").capitalize()
    product_name = f"Abonnement mensuel Betty {metier}"

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{
                "price_data": {
                    "currency": STRIPE_CURRENCY,
                    "recurring": {"interval": "month"},
                    "unit_amount": STRIPE_PRICE_CENTS,
                    "product_data": {"name": product_name}
                },
                "quantity": 1
            }],
            success_url=f"{base}/confirm?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/pay",
        )
        return redirect(session.url, code=303)
    except Exception as e:
        logger.error(f"Stripe error: {e}", exc_info=True)
        flash("Erreur Stripe.", "error")
        return redirect(url_for("pay"))

# --------------------------- CONFIRM ---------------------------------
@app.get("/confirm")
@login_required
def confirm():
    session_id = request.args.get("session_id")
    if not session_id:
        flash("Session Stripe introuvable.", "warning")
        return redirect(url_for("pay"))

    try:
        checkout = stripe.checkout.Session.retrieve(session_id, expand=["subscription", "customer"])
        payment_status = (checkout.get("payment_status") or "").lower()
        subscription = checkout.get("subscription")
        sub_id = getattr(subscription, "id", None) if subscription else None
        sub_status = getattr(subscription, "status", None) if subscription else None
        customer = checkout.get("customer")
        cust_id = getattr(customer, "id", None) if customer else (checkout.get("customer") if isinstance(checkout.get("customer"), str) else None)

        # Mémorise côté user (si dispo)
        if cust_id:
            db_exec("UPDATE users SET stripe_customer_id=? WHERE id=?", (cust_id, int(current_user.id)))
        if sub_id:
            db_exec("UPDATE users SET stripe_subscription_id=? WHERE id=?", (sub_id, int(current_user.id)))

        # Données pour générer le snippet dans le template
        row = get_bot(int(current_user.id))
        bot = dict(row) if row else {}
        base = base_url_for_checkout()
        pack_map = {
            "Avocate": "avocat_pack",
            "Agent Immo": "agent_immobilier_pack",
            "Médecine": "medecine_pack",
            "Comptable": "comptable_pack",
            "Psychologue": "psychologue_pack",
        }
        pack = pack_map.get(bot.get("metier") or "", "agent_immobilier_pack")
        welcome = bot.get("welcome_text", "Bonjour 👋")

        return render_template(
            "confirm.html",
            session_id=session_id,
            payment_status=payment_status,
            sub_id=sub_id,
            sub_status=sub_status,
            cust_id=cust_id,
            base_url=base.rstrip("/"),
            pack=pack,
            welcome_text=welcome
        )
    except Exception as e:
        logger.error(f"Confirm error: {e}", exc_info=True)
        flash("Erreur lors de la confirmation.", "error")
        return redirect(url_for("pay"))

# ------------------------------ API ----------------------------------
@app.post("/api/chat")
def api_chat():
    try:
        data = request.get_json(force=True) or {}
        message = (data.get("message") or "").strip().lower()
        reply = "Bonjour 👋" if ("bonjour" in message or not message) else "Je vous écoute."
        return jsonify({"reply": reply, "ask_lead": False})
    except Exception:
        return jsonify({"reply": "Erreur.", "ask_lead": False}), 500

# ----------------------------- Divers --------------------------------
@app.get("/favicon.ico")
def favicon():
    fav = BASE_DIR / "static" / "favicon.ico"
    return send_from_directory(fav.parent, fav.name) if fav.exists() else Response(status=204)

@app.errorhandler(404)
def not_found(e): return "404", 404

@app.errorhandler(500)
def server_err(e): return "500", 500

# Local
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
