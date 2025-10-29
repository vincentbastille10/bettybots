# app.py — BettyBots (full, Vercel-ready)
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

@app.get("/__ping")
def __ping():
    try:
        _ = db_one("SELECT 1 as ok", ())
        db_ok = True
    except Exception:
        db_ok = False
    return jsonify({
        "ok": True,
        "db_ok": db_ok,
        "db_path": str(DB_PATH),
        "vercel": bool(os.getenv("VERCEL")),
        "python": os.getenv("PYTHON_VERSION", "3.x"),
    })


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
    try:
        # Flask-Login peut passer une str; on tolère int/str
        uid = int(str(user_id).strip())
    except Exception:
        return None
    row = db_one("SELECT id, email FROM users WHERE id=?", (uid,))
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
    "agent-immobilier": "agent_immo",
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
# PNG 1x1 transparent (base64 valide)
_FAVICON_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+Xad8AAAAASUVORK5CYII="
)

@app.get("/favicon.ico")
def favicon_ico():
    # Servez le PNG même pour .ico : suffisant pour éviter les 404/500
    return Response(
        _FAVICON_PNG,
        mimetype="image/png",
        headers={"Cache-Control": "public, max-age=86400, immutable"}
    )

@app.get("/favicon.png")
def favicon_png():
    return Response(
        _FAVICON_PNG,
        mimetype="image/png",
        headers={"Cache-Control": "public, max-age=86400, immutable"}
    )

# -----------------------------------------------------------------------------
# ROOT / ALIAS INDEX / SESSION INVITÉ  (robuste sur Vercel)
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    try:
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        # Génère un email invité stable et très peu collisionnable
        guest_email = f"guest-{secrets.token_urlsafe(9)}@guest.local"

        # Insert avec gestion race-condition (IGNORE) + reselect fiable
        db_exec("INSERT OR IGNORE INTO users(email) VALUES(?)", (guest_email,))
        row = db_one("SELECT id, email FROM users WHERE email=?", (guest_email,))

        # Si, contre toute attente, l'INSERT + SELECT n'a rien renvoyé, on retente une fois
        if not row:
            db_exec("INSERT OR IGNORE INTO users(email) VALUES(?)", (guest_email,))
            row = db_one("SELECT id, email FROM users WHERE email=?", (guest_email,))

        # Si toujours rien: on affiche une page lisible au lieu d'un 500
        if not row:
            app.logger.error("Impossible de créer un utilisateur invité.")
            return Response(
                "<h1>Initialisation en cours…</h1><p>Réessayez dans quelques secondes.</p>",
                mimetype="text/html",
                status=503
            )

        # Login Flask-Login
        login_user(User(row["id"], row["email"]), remember=True)
        return redirect(url_for("dashboard"))

    except Exception as e:
        app.logger.error(f"Exception in / (root): {e}", exc_info=True)
        # Pas de 500 brut : page de secours + lien ping/health
        html = """<!doctype html><meta charset="utf-8">
<title>Initialisation</title>
<body style="background:#0d1117;color:#fff;font-family:Inter,system-ui;padding:40px">
  <h1>Oups…</h1>
  <p>Une étape d’initialisation a échoué. Réessayez, ou vérifiez <a href="/healthz" style="color:#a5b4fc">/healthz</a>.</p>
</body>"""
        return Response(html, mimetype="text/html", status=503)

# ============================== app.py — PART 2/2 ==============================
# Compléments : /install, /code, /logout, /leads, /healthz, robots.txt,
# en-têtes de sécurité, erreurs 403/404. Zéro conflit avec la PART 1/2.

from markupsafe import Markup

# -----------------------------------------------------------------------------
# Helpers : fabrication du snippet d’embed à partir d’un bot
# -----------------------------------------------------------------------------
def build_embed_urls(bot: dict) -> dict:
    """
    Construit:
      - embed_url_simple: /chat?bot=<id>&key=<auth_key> (publique, protégée par clé)
      - embed_url_page  : /embed/<id> (page wrapper)
      - iframe          : <iframe ...> prêt à coller
      - code_block      : snippet HTML complet (div + iframe)
    """
    base = base_url_for_checkout().rstrip("/")
    bot_id = bot.get("id")
    auth_key = (bot.get("auth_key") or "").strip()
    if not bot_id:
        return {}

    if not auth_key:
        try:
            new_key = make_bot_key()
            db_exec("UPDATE bots SET auth_key=? WHERE id=?", (new_key, bot_id))
            auth_key = new_key
            # refresh
            row = db_one("SELECT * FROM bots WHERE id=?", (bot_id,))
            if row:
                bot.update(dict(row))
        except Exception as _e:
            logger.error(f"build_embed_urls: impossible de créer auth_key: {_e}", exc_info=True)

    embed_url_simple = f"{base}/chat?bot={bot_id}&key={auth_key}" if auth_key else f"{base}/chat?bot={bot_id}"
    embed_url_page   = f"{base}/embed/{bot_id}"

    iframe = (
        f'<iframe src="{embed_url_simple}" '
        'width="420" height="580" style="border:0;border-radius:18px;'
        'box-shadow:0 0 25px rgba(0,0,0,.4);" title="Mon Betty Bot"></iframe>'
    )

    code_block = (
        '<!-- Betty Bot — copiez/collez ce bloc dans votre page -->\n'
        '<div id="bettybot-container" style="max-width:520px">\n'
        f'  {iframe}\n'
        '</div>\n'
    )

    return {
        "embed_url_simple": embed_url_simple,
        "embed_url_page": embed_url_page,
        "iframe": iframe,
        "code_block": code_block,
    }

# -----------------------------------------------------------------------------
# INSTALL (compatibilité avec anciens parcours : preview -> pay -> install)
# -----------------------------------------------------------------------------
@app.get("/install")
@login_required
def install():
    row = ensure_user_bot(int(current_user.id))
    if not row:
        return redirect(url_for("dashboard"))
    bot = dict(row)
    urls = build_embed_urls(bot)
    # Si le template existe, on l’utilise, sinon fallback HTML.
    try:
        return render_template(
            "install.html",
            bot=bot,
            embed_url_simple=urls.get("embed_url_simple"),
            embed_url_page=urls.get("embed_url_page"),
            embed_iframe=Markup(urls.get("iframe", "")),
            embed_code=urls.get("code_block"),
        )
    except Exception as e:
        logger.error(f"install template error: {e}", exc_info=True)
        html = f"""<!doctype html><meta charset="utf-8">
<title>Installation — Betty Bot</title>
<body style="background:#0d1117;color:#fff;font-family:Inter,system-ui;padding:40px">
  <h1>Installation</h1>
  <p>Copiez-collez ce code d’intégration dans votre site&nbsp;:</p>
  <pre style="white-space:pre-wrap;background:#111827;padding:16px;border-radius:12px">{urls.get('code_block') or ''}</pre>
  <p><a style="color:#a5b4fc" href="/preview">← Retour au test du bot</a></p>
</body>"""
        return Response(html, mimetype="text/html")

# -----------------------------------------------------------------------------
# CODE (page minimale qui affiche uniquement le snippet propre à copier)
# -----------------------------------------------------------------------------
@app.get("/code")
@login_required
def code_snippet():
    row = ensure_user_bot(int(current_user.id))
    if not row:
        return "Bot introuvable.", 404
    bot = dict(row)
    urls = build_embed_urls(bot)
    try:
        return render_template(
            "code.html",
            bot=bot,
            embed_code=urls.get("code_block"),
            embed_iframe=Markup(urls.get("iframe", "")),
            embed_url_simple=urls.get("embed_url_simple"),
            embed_url_page=urls.get("embed_url_page"),
        )
    except Exception:
        # Fallback texte brut pour copier facilement (Content-Type HTML pour compat copier)
        return Response(
            f"<pre>{urls.get('code_block') or ''}</pre>",
            mimetype="text/html"
        )

# -----------------------------------------------------------------------------
# LEADS (liste simple, interne)
# -----------------------------------------------------------------------------
@app.get("/leads")
@login_required
def leads_list():
    try:
        cur = get_db().execute(
            "SELECT id, name, email, message, metier, created_at FROM leads ORDER BY id DESC LIMIT 200"
        )
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"/leads query error: {e}", exc_info=True)
        rows = []
    try:
        return render_template("leads.html", leads=rows)
    except Exception:
        items = "\n".join(
            f"<li><strong>{r.get('name') or '—'}</strong> &lt;{r.get('email') or '—'}&gt; "
            f"[{r.get('metier') or '—'}] — {r.get('message') or ''}</li>"
            for r in rows
        )
        return Response(
            f"<!doctype html><meta charset='utf-8'><h1>Leads</h1><ul>{items}</ul>",
            mimetype="text/html"
        )

# -----------------------------------------------------------------------------
# LOGOUT
# -----------------------------------------------------------------------------
@app.get("/logout")
def logout():
    try:
        logout_user()
    except Exception:
        pass
    return redirect(url_for("root"))

# -----------------------------------------------------------------------------
# HEALTHCHECK & ROBOTS
# -----------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    try:
        # ping DB léger
        _ = db_one("SELECT 1 as ok", ())
        ok = True
    except Exception:
        ok = False
    status = 200 if ok else 500
    return jsonify({"ok": ok, "time": datetime.utcnow().isoformat() + "Z"}), status

@app.get("/robots.txt")
def robots_txt():
    txt = "User-agent: *\nDisallow: /leads\nDisallow: /pay\nDisallow: /confirm\n"
    return Response(txt, mimetype="text/plain")

# -----------------------------------------------------------------------------
# HEADERS DE SÉCURITÉ (CSP douce pour éviter de casser l’embed)
# -----------------------------------------------------------------------------
@app.after_request
def add_security_headers(resp: Response):
    try:
        # X-Frame-Options: autorise l'embed sur n'importe quel site (nécessaire pour l’iframe client),
        # si tu veux restreindre : 'SAMEORIGIN' ou des ACL précises via CSP frame-ancestors
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-XSS-Protection", "0")
        # CSP minimale : autorise self + data: pour images + inline styles du snippet,
        # et frame-ancestors * pour permettre l’intégration (ajuste si besoin)
        csp = (
            "default-src 'self'; "
            "img-src 'self' data: https:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; "
            "frame-src 'self'; "
            "frame-ancestors *"
        )
        resp.headers.setdefault("Content-Security-Policy", csp)
    except Exception:
        pass
    return resp

# -----------------------------------------------------------------------------
# ERREURS 403/404 (déjà un 500 dans PART 1/2)
# -----------------------------------------------------------------------------
@app.errorhandler(403)
def on_403(err):
    try:
        return render_template("error.html", code=403, message="Accès interdit"), 403
    except Exception:
        return Response("<h1>403 — Accès interdit</h1>", mimetype="text/html"), 403

@app.errorhandler(404)
def on_404(err):
    try:
        return render_template("error.html", code=404, message="Page introuvable"), 404
    except Exception:
        return Response("<h1>404 — Page introuvable</h1>", mimetype="text/html"), 404

# ============================ fin app.py — PART 2/2 ============================
