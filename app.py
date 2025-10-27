from __future__ import annotations
import os, sqlite3, secrets, json, logging, re, smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    Response, send_from_directory, jsonify, g
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    current_user, login_required
)
from werkzeug.middleware.proxy_fix import ProxyFix  # respecte X-Forwarded-* en prod (Vercel/Render)

# Dépendances optionnelles pour le proxy d’images (secours)
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

# Respecte le schéma/host envoyés par le reverse-proxy (Vercel/Render/Nginx)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

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

# Email SMTP (simple)
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))  # pratique avec MailHog/Mailpit en dev
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "no-reply@bettybots.local")
SMTP_TLS  = os.getenv("SMTP_TLS", "0") in ("1", "true", "True", "yes", "on")

def send_mail(to_email: str, subject: str, body: str) -> bool:
    """Envoie un email texte simple via SMTP_* (env). Retourne True si OK."""
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

# Login
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
            widget_size TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            pro_phone TEXT,
            pro_address_label TEXT,
            pro_address_url TEXT,
            pro_description TEXT,
            auth_key TEXT,                             -- ✅ clé d’accès publique
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
    """Ajoute pro_phone, pro_address_label, pro_address_url, pro_description, auth_key si absents (migration douce)."""
    try:
        with get_db() as conn:
            cur = conn.execute("PRAGMA table_info(bots)")
            existing = {row["name"] for row in cur.fetchall()}
            alters = []
            if "pro_phone" not in existing:
                alters.append("ALTER TABLE bots ADD COLUMN pro_phone TEXT")
            if "pro_address_label" not in existing:
                alters.append("ALTER TABLE bots ADD COLUMN pro_address_label TEXT")
            if "pro_address_url" not in existing:
                alters.append("ALTER TABLE bots ADD COLUMN pro_address_url TEXT")
            if "pro_description" not in existing:
                alters.append("ALTER TABLE bots ADD COLUMN pro_description TEXT")
            if "auth_key" not in existing:
                alters.append("ALTER TABLE bots ADD COLUMN auth_key TEXT")
            for sql in alters:
                conn.execute(sql)
            if alters:
                conn.commit()
    except Exception as e:
        logger.error(f"ensure_bot_extra_columns error: {e}", exc_info=True)

def db_one(sql, params=()):
    with get_db() as c:
        cur = c.execute(sql, params)
        return cur.fetchone()

def db_exec(sql, params=()):
    with get_db() as c:
        c.execute(sql, params)
        c.commit()
        return True

# --- Génération + remplissage des clés d’accès (sécurité d’embed) ----
def make_bot_key() -> str:
    return secrets.token_urlsafe(16)

def ensure_bot_keys():
    """Génère une auth_key pour tous les bots qui n'en ont pas (migration douce)."""
    try:
        with get_db() as conn:
            cur = conn.execute("SELECT id FROM bots WHERE auth_key IS NULL OR auth_key=''")
            rows = cur.fetchall()
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

# ---------------------------------------------------------------------
# Utilitaires / Normalisation
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
    """Base URL absolue pour success/cancel Stripe (jamais localhost en prod)."""
    # 1) Si fournie, on force PUBLIC_BASE_URL / BASE_URL (ex: https://bettybots.vercel.app)
    if PUBLIC_BASE_URL_ENV:
        return PUBLIC_BASE_URL_ENV.rstrip("/")
    # 2) Sinon, on dérive de la requête (ProxyFix respecte X-Forwarded-Proto/Host)
    base = (request.url_root or "").rstrip("/")
    if not base:
        return "https://example.com"  # fallback extrême
    low = base.lower()
    # En dev local, on tolère http://localhost
    if "localhost" in low or "127.0.0.1" in low:
        return base.replace("https://", "http://")
    return base

# --- Normalisation métier/pack/avatar ---
DEFAULT_SLUG = "agent_immo"  # pour l'URL /avatar/...

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
    """
    Retourne (internal_slug, pack_slug)
    internal_slug -> pour /avatar/<slug>   (agent_immo|avocat|medecin)
    pack_slug     -> pour l'API/chat & affichage (agent_immobilier|avocat|medecin)
    """
    v = (raw or "").strip().lower()
    if v in PACK_TO_INTERNAL:
        internal_slug = PACK_TO_INTERNAL[v]
        pack_slug = v
    elif v in INTERNAL_TO_PACK:
        internal_slug = v
        pack_slug = INTERNAL_TO_PACK[v]
    else:
        internal_slug = LABEL_TO_INTERNAL.get(v, DEFAULT_SLUG)
        pack_slug = INTERNAL_TO_PACK.get(internal_slug, "agent_immobilier")
    return internal_slug, pack_slug

# ---------------------------------------------------------------------
# ✅ AVATARS — local d'abord, proxy externe en secours, placeholder sinon
# ---------------------------------------------------------------------
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

    # 1) Local
    local_name = _LOCAL_FILES[slug]
    local_path = BASE_DIR / "static" / local_name
    if local_path.exists():
        return send_from_directory(local_path.parent, local_path.name, max_age=86400)

    # 2) Proxy externe
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

    # 3) Placeholder
    return Response(_PLACEHOLDER_SVG, mimetype="image/svg+xml", headers={"Cache-Control":"public, max-age=86400"})

# ---------------------------------------------------------------------
# Routes
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
        # --- normalisation du pack & avatar, QUELLE QUE SOIT la valeur en base
        internal_slug, pack_slug = normalize_metier((bot or {}).get("metier") or "")
        cfg = {
            "name": (bot or {}).get("name", "Mon Betty Bot"),
            "color_hex": (bot or {}).get("color_hex", "#4F46E5"),
            "welcome_text": (bot or {}).get("welcome_text", "Bonjour 👋"),
            "persona": (bot or {}).get("persona", "neutre"),
            "shape": (bot or {}).get("shape", "rounded"),
            "widget_size": (bot or {}).get("widget_size", "m"),
            "slug": pack_slug,                           # pack normalisé
            "avatar_key": 0 if internal_slug == "agent_immo" else (1 if internal_slug == "avocat" else 2),
            "avatar_url": f"/avatar/{internal_slug}",    # avatar cohérent
            "metier": (bot or {}).get("metier") or pack_slug,
            # Coordonnées publiques
            "pro_phone": (bot or {}).get("pro_phone"),
            "pro_address_label": (bot or {}).get("pro_address_label"),
            "pro_address_url": (bot or {}).get("pro_address_url"),
            "pro_description": (bot or {}).get("pro_description"),
        }
        return render_template("dashboard.html", bot=bot, cfg=cfg)

    # --- POST: sauvegarde (on enregistre le slug pack tel quel)
    name       = (request.form.get("bot_name") or "Mon Betty Bot").strip()[:100]
    pack_slug  = (request.form.get("pack_slug") or "agent_immobilier").strip().lower()
    color_hex  = sanitize_color(request.form.get("color_hex") or "#4F46E5")
    welcome    = (request.form.get("greeting") or "Bonjour 👋").strip()[:500]
    persona    = (request.form.get("persona") or "neutre").strip()
    widget_sz  = (request.form.get("widget_size") or "m").strip()
    shape_map  = {"s":"circle","m":"square","l":"rounded"}
    shape      = shape_map.get(widget_sz, "square")

    # Nouveaux champs (facultatifs)
    pro_phone        = (request.form.get("pro_phone") or "").strip()[:100]
    pro_address_lbl  = (request.form.get("pro_address_label") or "").strip()[:200]
    pro_address_url  = (request.form.get("pro_address_url") or "").strip()[:300]
    pro_description  = (request.form.get("pro_description") or "").strip()[:400]

    if bot:
        db_exec("""UPDATE bots SET name=?, metier=?, color_hex=?, welcome_text=?, persona=?, widget_size=?, shape=?,
                                   pro_phone=?, pro_address_label=?, pro_address_url=?, pro_description=?
                   WHERE user_id=?""",
                (name, pack_slug, color_hex, welcome, persona, widget_sz, shape,
                 pro_phone, pro_address_lbl, pro_address_url, pro_description, current_user.id))
    else:
        # ✅ nouvelle clé d’accès pour le bot
        auth_key = make_bot_key()
        db_exec("""INSERT INTO bots(user_id, name, metier, color_hex, welcome_text, persona, widget_size, shape,
                                    pro_phone, pro_address_label, pro_address_url, pro_description, auth_key)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (current_user.id, name, pack_slug, color_hex, welcome, persona, widget_sz, shape,
                 pro_phone, pro_address_lbl, pro_address_url, pro_description, auth_key))

    flash("✅ Bot sauvegardé.", "success")
    return redirect(url_for("preview"))

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
        "slug": pack_slug,                           # pack pour l’UI & l’API
        "avatar_key": 0 if internal_slug == "agent_immo" else (1 if internal_slug == "avocat" else 2),
        "avatar_url": f"/avatar/{internal_slug}",    # avatar choisi
        "metier": bot.get("metier") or pack_slug,
        # Coordonnées publiques (optionnel)
        "pro_phone": bot.get("pro_phone"),
        "pro_address_label": bot.get("pro_address_label"),
        "pro_address_url": bot.get("pro_address_url"),
        "pro_description": bot.get("pro_description"),
    }

    return render_template("preview.html", bot=bot, cfg=cfg)

# --------------------------- CHAT PUBLIC (iframe) --------------------
@app.get("/chat")
def chat_public():
    """
    Affiche le widget dans une page minimaliste pour l'iframe.
    Sécurité : exige ?bot=<id> et, si visiteur externe, ?key=<auth_key>.
    Si l'utilisateur propriétaire est connecté, la clé n'est pas requise.
    """
    bot_id = request.args.get("bot", type=int)
    key    = (request.args.get("key") or "").strip()

    if not bot_id:
        return "Aucun bot à afficher. Fournissez ?bot=<id>.", 400

    row = db_one("SELECT * FROM bots WHERE id=?", (bot_id,))
    if not row:
        return "Bot introuvable.", 404

    # Propriétaire connecté ?
    is_owner = False
    if current_user.is_authenticated:
        owners_bot = get_bot(int(current_user.id))
        if owners_bot and dict(owners_bot)["id"] == bot_id:
            is_owner = True

    # Si pas le propriétaire, on vérifie la clé
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
        "avatar_url": f"/avatar/{internal_slug}",
        "metier": bot.get("metier") or pack_slug,
        "pro_phone": bot.get("pro_phone"),
        "pro_address_label": bot.get("pro_address_label"),
        "pro_address_url": bot.get("pro_address_url"),
        "pro_description": bot.get("pro_description"),
    }

    # On réutilise le template de test (UI identique à /preview)
    return render_template("preview.html", bot=bot, cfg=cfg)

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
    # libellé produit
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

        if cust_id:
            db_exec("UPDATE users SET stripe_customer_id=? WHERE id=?", (cust_id, int(current_user.id)))
        if sub_id:
            db_exec("UPDATE users SET stripe_subscription_id=? WHERE id=?", (sub_id, int(current_user.id)))

        row = get_bot(int(current_user.id))
        bot = dict(row) if row else {}
        base = base_url_for_checkout()
        _, pack_slug = normalize_metier(bot.get("metier") or "")
        welcome = bot.get("welcome_text", "Bonjour 👋")

        # --- Données bot + clé ---
        bot_id   = bot.get("id")
        auth_key = (bot.get("auth_key") or "").strip() if bot_id else None

        # --- Code d’intégration (historique) — maintenant avec clé pour sécurité ---
        embed_src_legacy = f"{base.rstrip('/')}/chat?bot={bot_id}&key={auth_key}" if bot_id and auth_key else f"{base.rstrip('/')}/chat"
        embed_code = (
            f'<iframe src="{embed_src_legacy}" '
            'style="border:0;width:420px;height:580px;border-radius:18px;box-shadow:0 0 25px rgba(0,0,0,.4);" '
            'title="Mon Betty Bot"></iframe>'
        )

        # --- ⚡️ Liens d’intégration ultra simples (pour Wix/NoCode) — incluent la clé ---
        embed_url_simple = f"{base.rstrip('/')}/chat?bot={bot_id}&key={auth_key}" if bot_id and auth_key else None   # “Adresse du site Web”
        embed_url_page   = f"{base.rstrip('/')}/embed/{bot_id}" if bot_id else None      # page wrapper (inclut la clé automatiquement)
        embed_iframe     = (
            f'<iframe src="{embed_url_simple}" '
            'width="420" height="580" style="border:0;border-radius:18px;box-shadow:0 0 25px rgba(0,0,0,.4);" '
            'title="Mon Betty Bot"></iframe>'
        ) if embed_url_simple else None

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
            embed_code=embed_code,
            bot_id=bot_id,
            embed_url_simple=embed_url_simple,
            embed_url_page=embed_url_page,
            embed_iframe=embed_iframe,
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
        raw_msg = data.get("message") or ""
        message = raw_msg.strip().lower()
        pack    = (data.get("pack") or "agent_immobilier").strip().lower()

        # Réponse très simple + amorce de qualif par pack (proactif)
        lead_prompts = {
            "agent_immobilier": "Souhaitez-vous acheter, vendre ou louer ? Quel budget et sur quelle zone ?",
            "avocat": "Pouvez-vous préciser le type de dossier (famille, travail, pénal...), l'urgence et vos coordonnées ?",
            "medecin": "Quel est votre motif de consultation et vos disponibilités ?",
            "coiffeur": "Quel service souhaitez-vous et quand êtes-vous disponible ?",
            "coach_sportif": "Quel objectif (perte de poids, performance, remise en forme) et quels créneaux ?",
        }

        if not message or "bonjour" in message:
            return jsonify({"reply": "Bonjour 👋", "ask_lead": False})

        if any(k in message for k in ["rdv", "rendez", "dispo", "disponibilit"]):
            return jsonify({"reply": "D’accord. Préférez-vous matin, après-midi ou soir ?", "ask_lead": True})

        return jsonify({"reply": lead_prompts.get(pack, "Pouvez-vous préciser votre besoin, votre budget et votre délai ?"), "ask_lead": True})

    except Exception:
        return jsonify({"reply": "Erreur.", "ask_lead": False}), 500

# ----------- API LEAD : enregistrement + email au propriétaire --------
@app.post("/api/lead")
@login_required
def api_lead():
    """
    Attend un JSON :
    {
      "name": "Prénom NOM",
      "email": "lead@example.com",
      "message": "récap avec téléphone etc.",
      "extra": { "metier": "<slug pack>" }
    }
    → Enregistre en DB et envoie un mail au propriétaire (current_user.email).
    """
    try:
        data = request.get_json(force=True) or {}
        name    = (data.get("name") or "").strip()
        email   = (data.get("email") or "").strip()
        message = (data.get("message") or "").strip()
        metier  = ((data.get("extra") or {}).get("metier") or "inconnu").strip()

        # 1) Stocke en DB
        db_exec(
            "INSERT INTO leads (name, email, message, metier) VALUES (?, ?, ?, ?)",
            (name, email, message, metier)
        )

        # 2) Envoie un mail au propriétaire (email du compte connecté)
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
        else:
            logger.warning("Aucun owner_email (utilisateur non connecté ?)")

        return jsonify({"status": "saved", "emailed": bool(emailed)}), 200

    except Exception as e:
        logger.error(f"/api/lead error: {e}", exc_info=True)
        return jsonify({"status": "error"}), 500

# ----------- API : envoi du code d’intégration à l’email d’inscription ----
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

# --------------------------- EMBED WRAPPER ---------------------------
@app.get("/embed/<int:bot_id>")
def embed(bot_id: int):
    row = db_one("SELECT * FROM bots WHERE id=?", (bot_id,))
    if not row:
        return "Bot introuvable.", 404
    base = base_url_for_checkout().rstrip("/")
    key  = (row["auth_key"] or "").strip()
    src  = f"{base}/chat?bot={bot_id}&key={key}"  # ✅ inclut la clé
    # page ultra-minimale pour Wix/Notion/Webflow...
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<style>html,body{{margin:0;height:100%}}.wrap{{display:grid;place-items:center;height:100%;background:#0d1117}}</style>'
        f'</head><body><div class="wrap">'
        f'<iframe src="{src}" width="420" height="580" style="border:0;border-radius:18px;box-shadow:0 0 25px rgba(0,0,0,.4);" title="Betty Bot"></iframe>'
        f'</div></body></html>'
    )

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
    # En local, on écoute en HTTP pour éviter les mixed-content si tu testes depuis http://
    app.run(debug=True, host="0.0.0.0", port=5000)
