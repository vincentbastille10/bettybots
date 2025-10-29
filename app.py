# app.py — BettyBots (full, Vercel-ready) — PART 1/2
from __future__ import annotations

import os, re, json, logging, secrets, sqlite3, smtplib, urllib.request, base64
from email.message import EmailMessage
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    Response, send_from_directory, jsonify, g
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    current_user, login_required
)
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    import requests
except Exception:
    requests = None

import stripe

# Jinja loader patch (pour error.html manquant)
from jinja2 import ChoiceLoader, FileSystemLoader, DictLoader

# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bettybots")

# -----------------------------------------------------------------------------
# APP
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Jinja: fournir error.html (et _base.html) au cas où un include est présent
_builtin_templates = {
    # très minimal pour ne pas casser les includes {% include 'error.html' %}
    "error.html": (
        "<!-- injected by app.py -->"
        "<div style='display:none'></div>"
    ),
    # si ton preview inclut une base inexistante
    "_base.html": "<!-- empty base injected by app.py -->"
}
if isinstance(app.jinja_loader, FileSystemLoader):
    app.jinja_loader = ChoiceLoader([app.jinja_loader, DictLoader(_builtin_templates)])
else:
    app.jinja_loader = ChoiceLoader([FileSystemLoader(str(BASE_DIR / "templates")), DictLoader(_builtin_templates)])

# -----------------------------------------------------------------------------
# ENV / STRIPE
# -----------------------------------------------------------------------------
def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default

STRIPE_SECRET_KEY   = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_CENTS  = _env_int("STRIPE_PRICE_CENTS", 1990)  # 19,90€
STRIPE_CURRENCY     = os.getenv("STRIPE_CURRENCY", "eur")
PUBLIC_BASE_URL_ENV = (os.getenv("PUBLIC_BASE_URL") or os.getenv("BASE_URL") or "").strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    logger.warning("⚠️ STRIPE_SECRET_KEY manquante — paiements désactivés.")

# -----------------------------------------------------------------------------
# SMTP
# -----------------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "no-reply@bettybots.local")
SMTP_TLS  = os.getenv("SMTP_TLS", "0") in ("1", "true", "True", "yes", "on")

def send_mail(to_email: str, subject: str, body: str) -> bool:
    try:
        if not to_email:
            logger.warning("send_mail: destinataire vide")
            return False
        msg = EmailMessage()
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            if SMTP_TLS:
                s.starttls()
            if SMTP_USER and SMTP_PASS:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True
    except Exception as e:
        logger.error(f"Email send error: {e}", exc_info=True)
        return False

# -----------------------------------------------------------------------------
# DB (SQLite compatible Vercel)
# -----------------------------------------------------------------------------
def pick_db_path() -> Path:
    env_db = os.getenv("DB_PATH", "").strip()
    if env_db:
        return Path(env_db)
    if any(os.getenv(k) for k in ("VERCEL", "VERCEL_URL", "AWS_LAMBDA_FUNCTION_NAME", "VERCEL_ENV")):
        return Path("/tmp/bettybots.sqlite3")
    return BASE_DIR / "bettybots.sqlite3"

DB_PATH = pick_db_path()

def _ensure_parent_dir(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        _ensure_parent_dir(DB_PATH)
        g.db = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exception: Optional[BaseException]):
    conn = g.pop("db", None)
    if conn:
        conn.close()

def db_one(sql: str, params: tuple = ()):
    conn = get_db()
    cur = conn.execute(sql, params)
    return cur.fetchone()

def db_exec(sql: str, params: tuple = ()):
    conn = get_db()
    conn.execute(sql, params)
    conn.commit()
    return True

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
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
        widget_size TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        pro_phone TEXT,
        pro_address_label TEXT,
        pro_address_url TEXT,
        pro_description TEXT,
        auth_key TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT,
        message TEXT,
        metier TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()

def ensure_bot_extra_columns():
    try:
        cur = get_db().execute("PRAGMA table_info(bots)")
        existing = {row["name"] for row in cur.fetchall()}
        alters = []
        for col in ("pro_phone","pro_address_label","pro_address_url","pro_description","auth_key","avatar_url"):
            if col not in existing:
                alters.append(f"ALTER TABLE bots ADD COLUMN {col} TEXT")
        if alters:
            conn = get_db()
            for sql in alters:
                conn.execute(sql)
            conn.commit()
    except Exception as e:
        logger.error(f"ensure_bot_extra_columns error: {e}", exc_info=True)

def make_bot_key() -> str:
    return secrets.token_urlsafe(16)

def ensure_bot_keys():
    try:
        conn = get_db()
        rows = conn.execute("SELECT id FROM bots WHERE auth_key IS NULL OR auth_key=''").fetchall()
        for r in rows:
            conn.execute("UPDATE bots SET auth_key=? WHERE id=?", (make_bot_key(), r["id"]))
        if rows:
            conn.commit()
    except Exception as e:
        logger.error(f"ensure_bot_keys error: {e}", exc_info=True)

with app.app_context():
    init_db()
    ensure_bot_extra_columns()
    ensure_bot_keys()

# -----------------------------------------------------------------------------
# LOGIN
# -----------------------------------------------------------------------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "root"

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

def is_guest_user() -> bool:
    return (not current_user.is_authenticated) or str(getattr(current_user, "email", "") or "").endswith("@guest.local")

# -----------------------------------------------------------------------------
# Packs & avatar
# -----------------------------------------------------------------------------
DEFAULT_SLUG = "agent_immo"

PACK_TO_INTERNAL = {
    "agent_immobilier": "agent_immo",
    "avocat": "avocat",
    "medecin": "medecin",
}
INTERNAL_TO_PACK = {v: k for k, v in PACK_TO_INTERNAL.items()}
LABEL_TO_INTERNAL = {
    "agent immo": "agent_immo",
    "agent immobilier": "agent_immo",
    "avocate": "avocat",
    "avocat": "avocat",
    "médecin": "medecin",
    "medecin": "medecin",
    "médecine": "medecin",
    "medecine": "medecin",
}

def normalize_metier(raw: str) -> tuple[str, str]:
    v = (raw or "").strip().lower()
    if v in PACK_TO_INTERNAL:
        internal_slug, pack_slug = PACK_TO_INTERNAL[v], v
    elif v in INTERNAL_TO_PACK:
        internal_slug, pack_slug = v, INTERNAL_TO_PACK[v]
    else:
        internal_slug = LABEL_TO_INTERNAL.get(v, DEFAULT_SLUG)
        pack_slug = INTERNAL_TO_PACK.get(internal_slug, "agent_immobilier")
    return internal_slug, pack_slug

EXTERNAL_AVATARS = {
    "agent_immo": "https://i.postimg.cc/zBWtZ8MH/Betty-Agent-immo-copie.jpg",
    "avocat":     "https://i.postimg.cc/bv4CBs6h/Betty-Avocate-copie.jpg",
    "medecin":    "https://i.postimg.cc/PxZ3sTcL/Betty-Medecine-copie.jpg",
}
_LOCAL_FILES = {
    "agent_immo": "Betty Agent immo copie.jpg",
    "avocat":     "Betty Avocate copie.jpg",
    "medecin":    "Betty Medecine copie.jpg",
}
_PLACEHOLDER_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg width="256" height="256" viewBox="0 0 256 256" xmlns="http://www.w3.org/2000/svg">
 <rect width="256" height="256" fill="#e5e7eb"/>
 <circle cx="128" cy="96" r="40" fill="#9ca3af"/>
 <rect x="56" y="148" width="144" height="60" rx="14" fill="#9ca3af"/>
</svg>"""

@app.get("/avatar/<slug>")
def avatar_proxy(slug: str):
    slug = (slug or "").strip().lower()
    if slug not in _LOCAL_FILES:
        slug = DEFAULT_SLUG
    local_name = _LOCAL_FILES[slug]
    local_path = BASE_DIR / "static" / local_name
    if local_path.exists():
        return send_from_directory(local_path.parent, local_path.name, max_age=86400)
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
        req = urllib.request.Request(url, headers={"User-Agent": "BettyBots/1.0", "Referer": "https://postimg.cc/"})
        with urllib.request.urlopen(req, timeout=6) as f:
            data = f.read()
            resp = Response(data, mimetype="image/jpeg")
            resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
            return resp
    except Exception:
        pass
    return Response(_PLACEHOLDER_SVG, mimetype="image/svg+xml", headers={"Cache-Control":"public, max-age=86400"})

# -----------------------------------------------------------------------------
# Utils: base URL, user/bot helpers
# -----------------------------------------------------------------------------
def base_url_for_checkout() -> str:
    if PUBLIC_BASE_URL_ENV:
        return PUBLIC_BASE_URL_ENV.rstrip("/")
    base = (request.url_root or "").rstrip("/")
    if not base:
        return "https://example.com"
    low = base.lower()
    if "localhost" in low or "127.0.0.1" in low:
        return base.replace("https://", "http://")
    return base

def get_bot(user_id: int) -> Optional[sqlite3.Row]:
    return db_one("SELECT * FROM bots WHERE user_id=? LIMIT 1", (user_id,))

def ensure_user_bot(user_id: int) -> sqlite3.Row:
    row = get_bot(user_id)
    if not row:
        name = "Mon Betty Bot"
        pack_slug = "agent_immobilier"
        color_hex = "#4F46E5"
        welcome = "Bonjour 👋"
        persona = "neutre"
        widget_sz = "m"
        shape = "rounded"
        auth_key = make_bot_key()
        db_exec("""INSERT INTO bots(user_id, name, metier, color_hex, welcome_text, persona, widget_size, shape,
                                    pro_phone, pro_address_label, pro_address_url, pro_description, auth_key, avatar_url)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, name, pack_slug, color_hex, welcome, persona, widget_sz, shape,
                 "", "", "", "", auth_key, ""))
        row = get_bot(user_id)
    else:
        row_dict = dict(row)
        if not (row_dict.get("auth_key") or "").strip():
            db_exec("UPDATE bots SET auth_key=? WHERE id=?", (make_bot_key(), row_dict["id"]))
            row = get_bot(user_id)
    return row

def sanitize_color(val: str) -> str:
    c = (val or "").strip()
    if not c.startswith("#"): c = "#" + c
    return c if len(c) in (4, 7) else "#4F46E5"

# -----------------------------------------------------------------------------
# FAVICONS (évite 500 sur /favicon.ico et /favicon.png)
# -----------------------------------------------------------------------------
# petit PNG 16x16 transparent (base64)
_FAVICON_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAAHElEQVQoka3NsQkAMAgEwXf/"
    "z1a4w6xIYwV8Fh8jXo8HcQk3e0b9a8gQ9j2z0AAAAASUVORK5CYII="
)

@app.get("/favicon.ico")
def favicon_ico():
    # renvoyer le PNG même pour .ico, suffisant pour éviter 500
    return Response(_FAVICON_PNG, mimetype="image/png", headers={"Cache-Control":"public, max-age=86400"})

@app.get("/favicon.png")
def favicon_png():
    return Response(_FAVICON_PNG, mimetype="image/png", headers={"Cache-Control":"public, max-age=86400"})

# -----------------------------------------------------------------------------
# ROOT / ALIAS INDEX / SESSION INVITÉ
# -----------------------------------------------------------------------------
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

# Alias pour les templates qui appellent url_for('index')
@app.get("/index")
def index():
    return redirect(url_for("dashboard"))
# app.py — PART 2/2 (suite)

# -----------------------------------------------------------------------------
# DASHBOARD (enregistrement fiable du pack choisi)
# -----------------------------------------------------------------------------
@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    row = get_bot(int(current_user.id))
    bot = dict(row) if row else None

    if request.method == "GET":
        internal_slug, pack_slug = normalize_metier((bot or {}).get("metier") or "")
        avatar_url = (bot or {}).get("avatar_url") or f"/avatar/{internal_slug}"
        cfg = {
            "name": (bot or {}).get("name", "Mon Betty Bot"),
            "color_hex": (bot or {}).get("color_hex", "#4F46E5"),
            "welcome_text": (bot or {}).get("welcome_text", "Bonjour 👋"),
            "persona": (bot or {}).get("persona", "neutre"),
            "shape": (bot or {}).get("shape", "rounded"),
            "widget_size": (bot or {}).get("widget_size", "m"),
            "slug": pack_slug,
            "avatar_key": 0 if internal_slug == "agent_immo" else (1 if internal_slug == "avocat" else 2),
            "avatar_url": avatar_url,
            "metier": (bot or {}).get("metier") or pack_slug,
            "pro_phone": (bot or {}).get("pro_phone"),
            "pro_address_label": (bot or {}).get("pro_address_label"),
            "pro_address_url": (bot or {}).get("pro_address_url"),
            "pro_description": (bot or {}).get("pro_description"),
        }
        try:
            return render_template("dashboard.html", bot=bot, cfg=cfg)
        except Exception as e:
            logger.error(f"dashboard GET template error: {e}", exc_info=True)
            return Response("<h1>Dashboard</h1><p>Template introuvable.</p>", mimetype="text/html")

    # POST
    name       = (request.form.get("bot_name") or "Mon Betty Bot").strip()[:100]
    pack_input = (request.form.get("pack_slug") or "agent_immobilier").strip().lower()
    internal_slug, pack_slug = normalize_metier(pack_input)

    color_hex  = sanitize_color(request.form.get("color_hex") or "#4F46E5")
    welcome    = (request.form.get("greeting") or "Bonjour 👋").strip()[:500]
    persona    = (request.form.get("persona") or "neutre").strip()
    widget_sz  = (request.form.get("widget_size") or "m").strip()
    shape_map  = {"s":"circle","m":"square","l":"rounded"}
    shape      = shape_map.get(widget_sz, "square")

    pro_phone        = (request.form.get("pro_phone") or "").strip()[:100]
    pro_address_lbl  = (request.form.get("pro_address_label") or "").strip()[:200]
    pro_address_url  = (request.form.get("pro_address_url") or "").strip()[:300]
    pro_description  = (request.form.get("pro_description") or "").strip()[:400]

    avatar_url = f"/avatar/{internal_slug}"

    if bot:
        db_exec("""UPDATE bots SET name=?, metier=?, color_hex=?, welcome_text=?, persona=?, widget_size=?, shape=?,
                                   pro_phone=?, pro_address_label=?, pro_address_url=?, pro_description=?, avatar_url=?
                   WHERE user_id=?""",
                (name, pack_slug, color_hex, welcome, persona, widget_sz, shape,
                 pro_phone, pro_address_lbl, pro_address_url, pro_description, avatar_url, current_user.id))
    else:
        auth_key = make_bot_key()
        db_exec("""INSERT INTO bots(user_id, name, metier, color_hex, welcome_text, persona, widget_size, shape,
                                    pro_phone, pro_address_label, pro_address_url, pro_description, auth_key, avatar_url)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (current_user.id, name, pack_slug, color_hex, welcome, persona, widget_sz, shape,
                 pro_phone, pro_address_lbl, pro_address_url, pro_description, auth_key, avatar_url))

    flash("✅ Bot sauvegardé.", "success")
    return redirect(url_for("preview"))

# -----------------------------------------------------------------------------
# PREVIEW
# -----------------------------------------------------------------------------
@app.get("/preview")
@login_required
def preview():
    row = get_bot(int(current_user.id))
    if not row:
        flash("Configure d'abord ton bot.", "warning")
        return redirect(url_for("dashboard"))

    bot = dict(row)
    internal_slug, pack_slug = normalize_metier(bot.get("metier") or "")
    shape = bot.get("shape") or "rounded"
    shape_to_size = {"circle":"s","square":"m","rounded":"l"}

    cfg = {
        "name": bot.get("name", "Mon Betty Bot"),
        "color_hex": bot.get("color_hex", "#4F46E5"),
        "welcome_text": bot.get("welcome_text", "Bonjour 👋"),
        "persona": bot.get("persona", "neutre"),
        "shape": shape,
        "widget_size": shape_to_size.get(shape, "m"),
        "slug": pack_slug,
        "avatar_key": 0 if internal_slug == "agent_immo" else (1 if internal_slug == "avocat" else 2),
        "avatar_url": bot.get("avatar_url") or f"/avatar/{internal_slug}",
        "metier": bot.get("metier") or pack_slug,
        "pro_phone": bot.get("pro_phone"),
        "pro_address_label": bot.get("pro_address_label"),
        "pro_address_url": bot.get("pro_address_url"),
        "pro_description": bot.get("pro_description"),
        "show_controls": True,
        "show_brand": False,
        "inject_hide_css": False
    }
    try:
        return render_template("preview.html", bot=bot, cfg=cfg)
    except Exception as e:
        # Si preview.html appelle un include inexistant, on renvoie un fallback simple
        logger.error(f"preview template error: {e}", exc_info=True)
        html = f"""<!doctype html><meta charset="utf-8">
<title>Aperçu</title>
<body style="background:#0d1117;color:#fff;font-family:Inter,system-ui;padding:40px">
  <h1>Prévisualisation</h1>
  <p>Template introuvable. Pack: {cfg['slug']}</p>
  <p><a href="/dashboard" style="color:#a5b4fc">← Retour au dashboard</a></p>
</body>"""
        return Response(html, mimetype="text/html")

# -----------------------------------------------------------------------------
# CHAT PUBLIC (iframe) — sécurisé par bot_id + auth_key (si visiteur externe)
# -----------------------------------------------------------------------------
@app.get("/chat")
def chat_public():
    bot_id = request.args.get("bot", type=int)
    key    = (request.args.get("key") or "").strip()
    clean  = request.args.get("clean")

    if not bot_id:
        return "Aucun bot à afficher. Fournissez ?bot=<id>.", 400

    row = db_one("SELECT * FROM bots WHERE id=?", (bot_id,))
    if not row:
        return "Bot introuvable.", 404

    is_owner = False
    if current_user.is_authenticated:
        owners_bot = get_bot(int(current_user.id))
        if owners_bot and dict(owners_bot)["id"] == bot_id:
            is_owner = True

    if not is_owner:
        db_key = (row["auth_key"] or "").strip()
        if not db_key or key != db_key:
            return "Accès non autorisé (clé invalide).", 403

    bot = dict(row)
    internal_slug, pack_slug = normalize_metier(bot.get("metier") or "")
    shape = bot.get("shape") or "rounded"
    shape_to_size = {"circle":"s","square":"m","rounded":"l"}

    cfg = {
        "name": bot.get("name", "Mon Betty Bot"),
        "color_hex": bot.get("color_hex", "#4F46E5"),
        "welcome_text": bot.get("welcome_text", "Bonjour 👋"),
        "persona": bot.get("persona", "neutre"),
        "shape": shape,
        "widget_size": shape_to_size.get(shape, "m"),
        "slug": pack_slug,
        "avatar_key": 0 if internal_slug == "agent_immo" else (1 if internal_slug == "avocat" else 2),
        "avatar_url": bot.get("avatar_url") or f"/avatar/{internal_slug}",
        "metier": bot.get("metier") or pack_slug,
        "pro_phone": bot.get("pro_phone"),
        "pro_address_label": bot.get("pro_address_label"),
        "pro_address_url": bot.get("pro_address_url"),
        "pro_description": bot.get("pro_description"),
        # Flags embedding (nettoyage des contrôles)
        "show_controls": False,
        "show_brand": True,
        "inject_hide_css": True,
        "brand_text": "Betty Bot — propulsé par Spectra Media",
        "brand_link": "https://spectramedia.ai"
    }
    try:
        return render_template("preview.html", bot=bot, cfg=cfg)
    except Exception as e:
        logger.error(f"chat_public template error: {e}", exc_info=True)
        return Response("<h1>Chat</h1><p>Template introuvable.</p>", mimetype="text/html")

# -----------------------------------------------------------------------------
# SIGNUP
# -----------------------------------------------------------------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        try:
            return render_template("signup.html")
        except Exception:
            return Response("<h1>Créer un compte</h1><form method='post'>"
                            "<input name='email' placeholder='email'><input name='email_confirm' placeholder='confirmez'>"
                            "<button>OK</button></form>", mimetype="text/html")
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

# -----------------------------------------------------------------------------
# PAY
# -----------------------------------------------------------------------------
@app.get("/pay")
@login_required
def pay():
    row = ensure_user_bot(int(current_user.id))
    bot = dict(row) if row else None
    is_guest = is_guest_user()
    ctx = {
        "bot": bot,
        "stripe_enabled": bool(STRIPE_SECRET_KEY),
        "is_guest": is_guest,
        "STRIPE_CURRENCY": STRIPE_CURRENCY,
        "STRIPE_PRICE_CENTS": STRIPE_PRICE_CENTS,
        "price_eur": round(STRIPE_PRICE_CENTS / 100, 2),
        "user_email": getattr(current_user, "email", None),
        "checkout_path": url_for("pay_stripe"),
        "base_url": base_url_for_checkout(),
        "signup_path": url_for("signup"),
    }
    try:
        return render_template("pay.html", **ctx)
    except Exception as e:
        logger.error("Exception on /pay [GET]: %s", e, exc_info=True)
        fallback_html = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paiement — Betty Bots</title>
<style>
  body{{background:#0d1117;color:#fff;font-family:Inter,system-ui,sans-serif;margin:0;padding:40px}}
  .card{{max-width:520px;margin:0 auto;background:#111827;border-radius:16px;padding:24px;box-shadow:0 0 25px rgba(0,0,0,.4)}}
  h1{{font-size:22px;margin:0 0 12px}}
  p{{opacity:.85;line-height:1.5}}
  .price{{font-size:28px;margin:12px 0 24px}}
  form button{{background:#4F46E5;border:0;color:#fff;padding:12px 18px;border-radius:10px;cursor:pointer}}
  .muted{{opacity:.7;font-size:13px;margin-top:14px}}
  a{{color:#a5b4fc;text-decoration:none}}
</style></head><body>
  <div class="card">
    <h1>Abonnement Betty Bots</h1>
    <p>Compte : {ctx.get('user_email') or '—'}</p>
    <div class="price">{ctx.get('price_eur')} {ctx.get('STRIPE_CURRENCY').upper()} / mois</div>
    {"<p style='color:#fca5a5'>Le paiement est désactivé (clé Stripe manquante).</p>" if not ctx.get("stripe_enabled") else ""}
    <form method="post" action="{ctx.get('checkout_path')}">
      <button type="submit" {"disabled" if not ctx.get('stripe_enabled') else ""}>
        Payer avec Stripe
      </button>
    </form>
    {("<p class='muted'>Pas encore de compte ? <a href='" + url_for('signup') + "'>Créer un compte</a></p>") if ctx.get("is_guest") else ""}
    <p class="muted">← <a href="/preview">Retour au test du bot</a></p>
    <p class="muted">Page fallback: si le template casse, cette version s’affiche.</p>
  </div>
</body></html>"""
        return Response(fallback_html, mimetype="text/html")

@app.post("/pay/stripe")
@login_required
def pay_stripe():
    if is_guest_user():
        flash("Créez votre compte avec un email valide pour procéder au paiement.", "warning")
        return redirect(url_for("signup"))
    if not STRIPE_SECRET_KEY:
        flash("Paiement indisponible (clé Stripe).", "error")
        return redirect(url_for("pay"))

    base = base_url_for_checkout()
    row = ensure_user_bot(int(current_user.id))
    bot = dict(row) if row else {}
    _, pack_slug = normalize_metier(bot.get("metier") or "")
    label_map = {"agent_immobilier":"Agent immobilier","avocat":"Avocat","medecin":"Médecin"}
    metier_label = label_map.get(pack_slug, "Générique")
    product_name = f"Abonnement mensuel Betty {metier_label}"

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            ui_mode="hosted",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": STRIPE_CURRENCY,
                    "recurring": {"interval": "month"},
                    "unit_amount": STRIPE_PRICE_CENTS,
                    "product_data": {"name": product_name}
                },
                "quantity": 1
            }],
            customer_email=(current_user.email or None),
            client_reference_id=str(current_user.id),
            allow_promotion_codes=True,
            success_url=f"{base}/confirm?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/pay",
        )
        return redirect(session.url, code=303)
    except Exception as e:
        logger.error(f"Stripe error: {e}", exc_info=True)
        flash("Erreur Stripe.", "error")
        return redirect(url_for("pay"))

# -----------------------------------------------------------------------------
# CONFIRM
# -----------------------------------------------------------------------------
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

        if cust_id:
            db_exec("UPDATE users SET stripe_customer_id=? WHERE id=?", (cust_id, int(current_user.id)))
        if sub_id:
            db_exec("UPDATE users SET stripe_subscription_id=? WHERE id=?", (sub_id, int(current_user.id)))

        row = ensure_user_bot(int(current_user.id))
        bot = dict(row) if row else {}
        base = base_url_for_checkout()
        _, pack_slug = normalize_metier(bot.get("metier") or "")
        welcome = bot.get("welcome_text", "Bonjour 👋")

        bot_id = bot.get("id")
        auth_key = (bot.get("auth_key") or "").strip() if bot_id else None
        if bot_id and not auth_key:
            try:
                new_key = make_bot_key()
                db_exec("UPDATE bots SET auth_key=? WHERE id=?", (new_key, bot_id))
                auth_key = new_key
                row = get_bot(int(current_user.id))
                bot = dict(row) if row else bot
            except Exception as _e:
                logger.error(f"Impossible de créer l'auth_key: {_e}", exc_info=True)

        embed_url_simple = None
        if bot_id and auth_key:
            embed_url_simple = f"{base.rstrip('/')}/chat?bot={bot_id}&key={auth_key}"
        if not embed_url_simple and bot_id:
            embed_url_simple = f"{base.rstrip('/')}/chat?bot={bot_id}"

        embed_iframe = (
            f'<iframe src="{embed_url_simple}" '
            'width="420" height="580" style="border:0;border-radius:18px;box-shadow:0 0 25px rgba(0,0,0,.4);" '
            'title="Mon Betty Bot"></iframe>'
        ) if embed_url_simple else None

        embed_url_page = f"{base.rstrip('/')}/embed/{bot_id}" if bot_id else None

        try:
            owner_to = "spectramediabots@gmail.com"
            buyer_email = getattr(current_user, "email", None) or "inconnu"
            bot_info = f"bot_id={bot_id or '—'} pack={pack_slug} base={base.rstrip('/')}"
            subject = f"[BettyBots] Nouveau paiement — {payment_status} — {buyer_email}"
            body = (
                "Nouveau paiement Betty Bots.\n\n"
                f"Acheteur : {buyer_email}\n"
                f"{bot_info}\n\n"
                f"Session ID : {session_id or '—'}\n"
                f"Subscription ID : {sub_id or '—'}\n"
                f"Customer ID : {cust_id or '—'}\n"
            )
            send_mail(owner_to, subject, body)
        except Exception as _e:
            logger.error(f"Owner email send failed: {_e}", exc_info=True)

        return render_template(
            "confirm.html",
            session_id=session_id,
            payment_status=payment_status,
            sub_id=sub_id,
            sub_status=sub_status,
            cust_id=cust_id,
            base_url=base.rstrip("/"),
            pack=pack_slug,
            welcome_text=welcome,
            embed_code=None,
            bot_id=bot_id,
            embed_url_simple=embed_url_simple,
            embed_url_page=embed_url_page,
            embed_iframe=embed_iframe,
        )
    except Exception as e:
        logger.error(f"Confirm error: {e}", exc_info=True)
        flash("Erreur lors de la confirmation.", "error")
        return redirect(url_for("pay"))

# -----------------------------------------------------------------------------
# API CHAT (simple + “qualif lead” par pack)
# -----------------------------------------------------------------------------
@app.post("/api/chat")
def api_chat():
    try:
        data = request.get_json(force=True) or {}
        raw_msg = data.get("message") or ""
        message = raw_msg.strip().lower()
        pack    = (data.get("pack") or "agent_immobilier").strip().lower()

        lead_prompts = {
            "agent_immobilier": "Souhaitez-vous acheter, vendre ou louer ? Quel budget et sur quelle zone ?",
            "avocat": "Pouvez-vous préciser le type de dossier (famille, travail, pénal...), l'urgence et vos coordonnées ?",
            "medecin": "Quel est votre motif de consultation et vos disponibilités ?",
            "coiffeur": "Quel service souhaitez-vous et quand êtes-vous disponible ?",
            "coach_sportif": "Quel objectif (perte de poids, performance, remise en forme) et quels créneaux ?",
        }

        if not message:
            return jsonify({"reply": "Bonjour 👋", "ask_lead": False})

        if pack == "avocat":
            if any(w in message for w in ("divorce","garde","pension","famille")):
                return jsonify({"reply": "Droit de la famille — avez-vous une date d’audience ou une échéance ?", "ask_lead": True})
            if any(w in message for w in ("licenciement","prud'h","prudhom","contrat de travail")):
                return jsonify({"reply": "Contentieux du travail — quel est votre délai et votre ville ?", "ask_lead": True})
            return jsonify({"reply": lead_prompts["avocat"], "ask_lead": True})

        if pack == "medecin":
            if "douleur" in message or "rdv" in message or "dispon" in message:
                return jsonify({"reply": "Très bien. Matin, après-midi ou soir ? Et une date souhaitée ?", "ask_lead": True})
            return jsonify({"reply": lead_prompts["medecin"], "ask_lead": True})

        if pack == "agent_immobilier":
            if any(w in message for w in ("acheter","vente","vendre","louer","location")):
                return jsonify({"reply": "Merci. Quel budget et quelle zone recherchez-vous ?", "ask_lead": True})
            return jsonify({"reply": lead_prompts["agent_immobilier"], "ask_lead": True})

        return jsonify({"reply": "Pouvez-vous préciser votre besoin, votre budget et votre délai ?", "ask_lead": True})

    except Exception as e:
        logger.error(f"/api/chat error: {e}", exc_info=True)
        return jsonify({"reply": "Erreur.", "ask_lead": False}), 500

# -----------------------------------------------------------------------------
# API LEAD
# -----------------------------------------------------------------------------
@app.post("/api/lead")
@login_required
def api_lead():
    try:
        data = request.get_json(force=True) or {}
        name    = (data.get("name") or "").strip()
        email   = (data.get("email") or "").strip()
        message = (data.get("message") or "").strip()
        metier  = ((data.get("extra") or {}).get("metier") or "inconnu").strip()

        db_exec("INSERT INTO leads (name, email, message, metier) VALUES (?, ?, ?, ?)",
                (name, email, message, metier))

        owner_email = getattr(current_user, "email", None)
        subject = f"Nouveau lead — {metier} — {name or 'inconnu'}"
        body = (
            "Vous avez reçu un nouveau lead depuis votre bot Betty.\n\n"
            f"Métier (pack) : {metier}\n"
            f"Nom complet   : {name}\n"
            f"Email lead    : {email}\n"
            f"Message       :\n{message}\n\n"
            "---\n"
            "Vous pouvez répondre directement à ce message pour recontacter le prospect."
        )
        emailed = False
        if owner_email:
            emailed = send_mail(owner_email, subject, body)

        return jsonify({"status": "saved", "emailed": bool(emailed)}), 200
    except Exception as e:
        logger.error(f"/api/lead error: {e}", exc_info=True)
        return jsonify({"status": "error"}), 500

# -----------------------------------------------------------------------------
# ENVOI DU CODE PAR EMAIL
# -----------------------------------------------------------------------------
@app.post("/api/send_code")
@login_required
def api_send_code():
    try:
        data = request.get_json(force=True) or {}
        code = (data.get("code") or "").strip()
        if not code:
            return jsonify({"ok": False, "error": "missing code"}), 400
        to_email = getattr(current_user, "email", None)
        if not to_email:
            return jsonify({"ok": False, "error": "no user email"}), 400

        subject = "Votre code d’intégration Betty Bot"
        body = (
            "Bonjour,\n\n"
            "Voici le code d’intégration de votre bot Betty, prêt à copier-coller dans votre site :\n\n"
            f"{code}\n\n"
            "Besoin d’aide ? Répondez simplement à cet email.\n\n"
            "— L’équipe Betty Bots"
        )
        ok = send_mail(to_email, subject, body)
        return jsonify({"ok": bool(ok)})
    except Exception as e:
        logger.error(f"/api/send_code error: {e}", exc_info=True)
        return jsonify({"ok": False}), 500

# -----------------------------------------------------------------------------
# EMBED WRAPPER (page dédiée)
# -----------------------------------------------------------------------------
@app.get("/embed/<int:bot_id>")
def embed(bot_id: int):
    row = db_one("SELECT * FROM bots WHERE id=?", (bot_id,))
    if not row:
        return "Bot introuvable.", 404
    bot = dict(row)
    internal_slug, pack_slug = normalize_metier(bot.get("metier") or "")
    shape = bot.get("shape") or "rounded"
    shape_to_size = {"circle":"s","square":"m","rounded":"l"}

    cfg = {
        "name": bot.get("name", "Mon Betty Bot"),
        "color_hex": bot.get("color_hex", "#4F46E5"),
        "welcome_text": bot.get("welcome_text", "Bonjour 👋"),
        "persona": bot.get("persona", "neutre"),
        "shape": shape,
        "widget_size": shape_to_size.get(shape, "m"),
        "slug": pack_slug,
        "avatar_key": 0 if internal_slug == "agent_immo" else (1 if internal_slug == "avocat" else 2),
        "avatar_url": bot.get("avatar_url") or f"/avatar/{internal_slug}",
        "metier": bot.get("metier") or pack_slug,
        "pro_phone": bot.get("pro_phone"),
        "pro_address_label": bot.get("pro_address_label"),
        "pro_address_url": bot.get("pro_address_url"),
        "pro_description": bot.get("pro_description"),
        "show_controls": False,
        "show_brand": True,
        "inject_hide_css": True,
        "brand_text": "Betty Bot — propulsé par Spectra Media",
        "brand_link": "https://spectramedia.ai"
    }
    try:
        return render_template("embed.html", bot=bot, cfg=cfg)
    except Exception:
        html = f"""<!doctype html><meta charset="utf-8">
<title>Betty — Embed</title>
<div style="position:sticky;top:0;background:#0b0b0f;color:#cbd5e1;border-bottom:1px solid rgba(255,255,255,.06);padding:8px;text-align:center;font:500 12px/18px Inter,system-ui">
  <a href="https://spectramedia.ai" target="_blank" style="color:#a5b4fc;text-decoration:none">Betty Bot — propulsé par Spectra Media</a>
</div>
<iframe src="/chat?bot={bot['id']}" width="420" height="580"
 style="border:0;border-radius:18px;box-shadow:0 0 25px rgba(0,0,0,.4);"></iframe>"""
        return Response(html, mimetype="text/html")

# -----------------------------------------------------------------------------
# ERREUR 500 (fallback)
# -----------------------------------------------------------------------------
@app.errorhandler(500)
def on_500(err):
    logger.error("HTTP 500: %s", err, exc_info=True)
    html = """<!doctype html><meta charset="utf-8">
<title>Erreur</title>
<body style="background:#0d1117;color:#fff;font-family:Inter,system-ui,sans-serif;padding:40px">
  <h1>Oups…</h1>
  <p>Une erreur s’est produite. Vous pouvez réessayer ou <a href="/pay">aller au paiement</a>.</p>
</body>"""
    return Response(html, mimetype="text/html"), 500

# -----------------------------------------------------------------------------
# MAIN (local)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
