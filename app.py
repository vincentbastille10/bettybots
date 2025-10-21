"""Main Flask application for Betty Bots subscription service."""
from __future__ import annotations

import json
import logging
import os
import secrets
import smtplib
import sqlite3
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import stripe
import yaml
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from authlib.integrations.flask_client import OAuth
except Exception:  # optional
    OAuth = None

# ---------------------------------------------------------------------------
# Configuration & application setup
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["TEMPLATES_AUTO_RELOAD"] = True

DATABASE_PATH = os.getenv("DATABASE_URL", "sqlite:///data.db")
DB_FILE = DATABASE_PATH.replace("sqlite:///", "") if DATABASE_PATH.startswith("sqlite:///") else "data.db"
DB_PATH = BASE_DIR / DB_FILE

login_manager = LoginManager(app)
login_manager.login_view = "login"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY") or None
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID_10_EUR")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# Google OAuth (optionnel)
oauth: Optional[OAuth] = None
if OAuth is not None:
    oauth = OAuth(app)
    google_client_id = os.getenv("OAUTH_GOOGLE_CLIENT_ID")
    google_client_secret = os.getenv("OAUTH_GOOGLE_CLIENT_SECRET")
    google_redirect_uri = os.getenv("OAUTH_GOOGLE_REDIRECT_URI")
    if google_client_id and google_client_secret:
        oauth.register(
            name="google",
            client_id=google_client_id,
            client_secret=google_client_secret,
            access_token_url="https://oauth2.googleapis.com/token",
            access_token_params={"prompt": "consent"},
            authorize_url="https://accounts.google.com/o/oauth2/auth",
            authorize_params={
                "access_type": "offline",
                "prompt": "consent",
                "response_type": "code",
                "scope": "openid email profile",
            },
            api_base_url="https://www.googleapis.com/oauth2/v1/",
            userinfo_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
            client_kwargs={"scope": "openid email profile"},
        )
        app.config["GOOGLE_REDIRECT_URI"] = google_redirect_uri
    else:
        logger.info("Google OAuth credentials missing; Google signup disabled.")
else:
    logger.info("authlib not installed; Google signup disabled.")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db  # type: ignore[return-value]

@app.teardown_appcontext
def close_db(_: Optional[BaseException]) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            name TEXT,
            auth_provider TEXT,
            google_sub TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            subscription_status TEXT DEFAULT 'inactive',
            embed_token TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT,
            metier TEXT,
            yaml_file TEXT,
            persona TEXT,
            color_hex TEXT,
            shape TEXT DEFAULT 'square',
            welcome_text TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_default INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (bot_id) REFERENCES bots(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            status TEXT,
            current_period_end TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------

class User(UserMixin):
    def __init__(self, row: sqlite3.Row):
        self.id = row["id"]
        self.email = row["email"]
        self.password_hash = row["password_hash"]
        self.name = row["name"] or self.email.split("@")[0]
        self.auth_provider = row["auth_provider"] or "password"
        self.google_sub = row["google_sub"]
        self.subscription_status = row["subscription_status"] or "inactive"
        self.embed_token = row["embed_token"]

    def get_id(self) -> str:  # type: ignore[override]
        return str(self.id)

    @property
    def is_active_subscription(self) -> bool:
        return self.subscription_status == "active"

@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    row = get_user_by_id(int(user_id))
    return User(row) if row else None

# ---------------------------------------------------------------------------
# DB utils
# ---------------------------------------------------------------------------

def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    return get_db().execute("SELECT * FROM users WHERE LOWER(email)=LOWER(?)", (email,)).fetchone()

def get_user_by_embed(embed_token: str) -> Optional[sqlite3.Row]:
    return get_db().execute("SELECT * FROM users WHERE embed_token = ?", (embed_token,)).fetchone()

def get_user_by_stripe_customer(customer_id: str) -> Optional[sqlite3.Row]:
    sql = "SELECT u.* FROM users u JOIN payments p ON u.id=p.user_id WHERE p.stripe_customer_id=?"
    return get_db().execute(sql, (customer_id,)).fetchone()

def get_user_by_stripe_subscription(subscription_id: str) -> Optional[sqlite3.Row]:
    sql = "SELECT u.* FROM users u JOIN payments p ON u.id=p.user_id WHERE p.stripe_subscription_id=?"
    return get_db().execute(sql, (subscription_id,)).fetchone()

def ensure_embed_token() -> str:
    conn = get_db()
    token = secrets.token_urlsafe(32)
    cur = conn.execute("SELECT 1 FROM users WHERE embed_token = ?", (token,))
    while cur.fetchone() is not None:
        token = secrets.token_urlsafe(32)
        cur = conn.execute("SELECT 1 FROM users WHERE embed_token = ?", (token,))
    return token

def create_user(email: str, password: Optional[str], name: Optional[str],
                provider: str, google_sub: Optional[str] = None) -> User:
    db = get_db()
    embed_token = ensure_embed_token()
    password_hash = generate_password_hash(password) if password else None
    db.execute(
        "INSERT INTO users (email, password_hash, name, auth_provider, google_sub, embed_token) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (email, password_hash, name, provider, google_sub, embed_token),
    )
    db.commit()
    row = get_user_by_email(email)
    if not row:
        raise RuntimeError("User creation failed")
    user = User(row)
    create_default_bot_if_needed(user.id)
    return user

def create_default_bot_if_needed(user_id: int) -> None:
    db = get_db()
    if db.execute("SELECT 1 FROM bots WHERE user_id=? AND is_default=1", (user_id,)).fetchone():
        return
    db.execute(
        """
        INSERT INTO bots (user_id, name, metier, yaml_file, persona, color_hex, shape, welcome_text, is_default)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            user_id,
            "Mon Betty Bot",
            "generaliste",
            "default_pack.yaml",
            "Assistant professionnel",
            "#4F46E5",
            "square",  # carré par défaut
            "Bonjour, je suis Betty, votre assistante AI. Que puis-je faire pour vous ?",
        ),
    )
    db.commit()

def get_default_bot(user_id: int) -> Optional[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM bots WHERE user_id=? AND is_default=1 ORDER BY id ASC LIMIT 1", (user_id,)
    ).fetchone()

def update_bot_configuration(
    bot_id: int,
    name: str,
    metier: str,
    yaml_file: str,
    persona: str,
    color_hex: str,
    shape: str,
    welcome_text: str,
) -> None:
    db = get_db()
    db.execute(
        """
        UPDATE bots
           SET name=?, metier=?, yaml_file=?, persona=?, color_hex=?, shape=?, welcome_text=?,
               updated_at=CURRENT_TIMESTAMP
         WHERE id=?
        """,
        (name, metier, yaml_file, persona, color_hex, shape, welcome_text, bot_id),
    )
    db.commit()

def update_user_subscription(user_id: int, status: str) -> None:
    get_db().execute("UPDATE users SET subscription_status=? WHERE id=?", (status, user_id))
    get_db().commit()

def upsert_payment_record(
    user_id: int,
    customer_id: Optional[str],
    subscription_id: Optional[str],
    status: Optional[str],
    current_period_end: Optional[datetime],
) -> None:
    db = get_db()
    existing = db.execute("SELECT id FROM payments WHERE user_id=?", (user_id,)).fetchone()
    end_value = current_period_end.isoformat() if current_period_end else None
    if existing:
        db.execute(
            """UPDATE payments
               SET stripe_customer_id=COALESCE(?,stripe_customer_id),
                   stripe_subscription_id=COALESCE(?,stripe_subscription_id),
                   status=COALESCE(?,status),
                   current_period_end=COALESCE(?,current_period_end)
             WHERE user_id=?""",
            (customer_id, subscription_id, status, end_value, user_id),
        )
    else:
        db.execute(
            "INSERT INTO payments (user_id, stripe_customer_id, stripe_subscription_id, status, current_period_end) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, customer_id, subscription_id, status, end_value),
        )
    db.commit()

# ---------------------------------------------------------------------------
# Email utilities
# ---------------------------------------------------------------------------

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = os.getenv("SMTP_PORT")
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
FROM_EMAIL = os.getenv("FROM_EMAIL", "Betty Bots <no-reply@bettybots.ai>")

def build_snippet_html(embed_token: str, shape: Optional[str], color_hex: Optional[str]) -> str:
    domain = os.getenv("PUBLIC_DOMAIN") or (request.host_url.rstrip("/") if request else "https://your-domain.com")
    safe_shape = shape or "square"
    safe_color = color_hex or "#4F46E5"
    return (
        "<!-- Betty Bot – intégration -->\n"
        f'<div id="betty-bot" data-embed-token="{embed_token}" '
        f'data-shape="{safe_shape}" data-color="{safe_color}"></div>\n'
        f'<script src="{domain}/embed.js" defer></script>'
    )

def _smtp_send(to: str, subject: str, html_body: str, text_body: Optional[str] = None) -> None:
    if not (SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASS and to):
        logger.warning("SMTP not fully configured; skip email to %s", to)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to
    msg.attach(MIMEText(text_body or "Voir version HTML.", "plain"))
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT)) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_EMAIL, [to], msg.as_string())
    except Exception as e:
        logger.warning("SMTP send failed: %s", e)

def send_snippet_email(recipient: str, snippet_html: str) -> None:
    html = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial">
      <p>Bonjour,</p>
      <p>Voici votre snippet d’intégration Betty :</p>
      <pre style="background:#f4f4f5;padding:16px;border-radius:8px;white-space:pre-wrap">{snippet_html}</pre>
      <p>Collez ce bloc avant la balise <code>&lt;/body&gt;</code> de votre site.</p>
    </div>
    """
    _smtp_send(recipient, "Votre code d'intégration Betty Bot", html, "Votre snippet est dans la version HTML.")

def send_lead_email(recipient: str, lead: Dict[str, Optional[str]]) -> None:
    if not recipient:
        return
    html = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial">
      <h3>📩 Nouveau lead depuis votre Betty Bot</h3>
      <ul>
        <li><b>Email</b> : {lead.get('email') or '—'}</li>
        <li><b>Téléphone</b> : {lead.get('phone') or '—'}</li>
        <li><b>Intention</b> : {lead.get('intent') or '—'}</li>
      </ul>
    </div>
    """
    _smtp_send(recipient, "Nouveau lead (Betty Bot)", html, "Nouveau lead. Voir détails en HTML.")

# ---------------------------------------------------------------------------
# YAML & prompts
# ---------------------------------------------------------------------------

PACKS_DIR = BASE_DIR / "templates" / "packs"
PACK_MAPPINGS = {
    "agent_immobilier": "agent_immobilier",
    "avocat": "avocat",
    "comptable": "comptable",
    "medecin": "medecin",
    "psychologue": "psychologue",
}

def load_yaml_for_metier(metier: str) -> Tuple[str, Dict[str, Any]]:
    if not PACKS_DIR.exists():
        return "default_pack.yaml", {
            "description": "Assistant générique pour TPE/PME.",
            "guidelines": ["Répondre avec empathie", "Qualifier les leads"],
        }
    normalized = metier.lower().strip().replace(" ", "_")
    candidates: List[Path] = []
    direct = PACKS_DIR / f"{normalized}.yaml"
    if direct.exists():
        candidates.append(direct)
    mapped = PACK_MAPPINGS.get(normalized)
    if mapped:
        candidates += list(PACKS_DIR.glob(f"{mapped}*.yaml"))
    for path in PACKS_DIR.glob("*.yaml"):
        if normalized in path.stem:
            candidates.append(path)
    unique: List[Path] = []
    seen = set()
    for p in candidates:
        if p not in seen:
            unique.append(p)
            seen.add(p)
    pack_path = unique[0] if unique else PACKS_DIR / "default_pack.yaml"
    if not pack_path.exists():
        return pack_path.name, {"description": "Assistant générique pour TPE/PME.", "guidelines": ["Répondre avec empathie", "Qualifier les leads"]}
    try:
        with pack_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.warning("Unable to load YAML pack %s: %s", pack_path, exc)
        data = {}
    return pack_path.name, data

def build_system_prompt(bot_row: sqlite3.Row) -> Tuple[str, Dict[str, Any]]:
    yaml_file, yaml_data = load_yaml_for_metier(bot_row["metier"] or "generaliste")
    guidelines: List[str] = []
    if isinstance(yaml_data, dict):
        if yaml_data.get("system_prompt"):
            guidelines.append(str(yaml_data["system_prompt"]))
        if yaml_data.get("description"):
            guidelines.append(str(yaml_data["description"]))
        rules = yaml_data.get("guidelines") or yaml_data.get("rules")
        if isinstance(rules, Iterable):
            guidelines.extend(str(x) for x in rules)
        intents = yaml_data.get("intents")
        if isinstance(intents, Iterable):
            guidelines.append("Intents cibles : " + ", ".join(map(str, intents)))
    else:
        guidelines.append("Assistant conversationnel Betty Bots.")
    persona = bot_row["persona"] or "Assistant professionnel"
    color = bot_row["color_hex"] or "#4F46E5"
    shape = bot_row["shape"] or "square"
    welcome = bot_row["welcome_text"] or "Bonjour, je suis Betty, votre assistante AI. Que puis-je faire pour vous ?"
    guidelines += [
        f"Persona choisie : {persona}.",
        "Collecte les coordonnées (nom, email, téléphone) si l'utilisateur semble qualifié.",
        "Propose un rendez-vous si pertinent.",
        f"Identité visuelle : couleur {color}, forme {shape}.",
        f"Message d'accueil : {welcome}",
    ]
    metadata = {"yaml_file": yaml_file, "persona": persona, "color": color, "shape": shape, "welcome_text": welcome}
    return "\n".join(guidelines), metadata

# ---------------------------------------------------------------------------
# LLM stub & lead detection
# ---------------------------------------------------------------------------

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "none").lower()
LLM_API_KEY = os.getenv("LLM_API_KEY")

def generate_llm_reply(system_prompt: str, history: List[Dict[str, str]], user_message: str) -> str:
    if not LLM_API_KEY or LLM_PROVIDER in {"none", "mock"}:
        return "[Réponse simulée] Merci pour votre message. Un conseiller vous recontactera rapidement."
    if LLM_PROVIDER == "openai":
        return "[TODO OpenAI] Réponse générée en conditions réelles."
    if LLM_PROVIDER == "together":
        return "[TODO Together] Réponse générée en conditions réelles."
    return "[Réponse simulée générique] Merci pour votre intérêt !"

LEAD_KEYWORDS = {
    "achat": {"achat", "acheter", "acquérir"},
    "vente": {"vente", "vendre", "cession"},
    "estimation": {"estimation", "devis", "tarif"},
    "rdv": {"rdv", "rendez-vous", "appointment"},
}

def detect_lead_info(message: str) -> Dict[str, Optional[str]]:
    import re
    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", message)
    phone_match = re.search(r"(?:(?:\+|00)\d{1,3}[\s.-]?)?(?:\d[\s.-]?){6,14}\d", message)
    intent_detected: Optional[str] = None
    lowered = message.lower()
    for intent, keywords in LEAD_KEYWORDS.items():
        if any(k in lowered for k in keywords):
            intent_detected = intent
            break
    return {"email": email_match.group(0) if email_match else None,
            "phone": phone_match.group(0) if phone_match else None,
            "intent": intent_detected}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def render_with_fallback(template_name: str, **context: Any) -> Response:
    template_path = BASE_DIR / "templates" / template_name
    if template_path.exists():
        return render_template(template_name, **context)
    return Response(f"<h1>{template_name}</h1><pre>{json.dumps(context, indent=2, ensure_ascii=False)}</pre>",
                    mimetype="text/html")

# ---------------------------------------------------------------------------
# Routes: Public pages
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def landing() -> Response:
    # GET -> landing
    if request.method == "GET":
        return render_with_fallback("landing.html")
    # POST -> inscription express (email + prénom optionnel)
    email = (request.form.get("email") or "").strip().lower()
    first_name = (request.form.get("first_name") or "").strip() or None
    if not email:
        flash("Merci d’indiquer votre e-mail.", "error")
        return redirect(url_for("landing"))
    existing = get_user_by_email(email)
    if existing:
        user = User(existing)
    else:
        user = create_user(email=email, password=None, name=first_name, provider="email")
        # envoi du snippet dès l’inscription
        default_bot = get_default_bot(user.id)
        if default_bot:
            snippet_html = build_snippet_html(user.embed_token, default_bot["shape"], default_bot["color_hex"])
            send_snippet_email(user.email, snippet_html)
    login_user(user)
    return redirect(url_for("dashboard"))

@app.route("/signup", methods=["GET", "POST"])
def signup() -> Response:
    # la landing est l’inscription -> on redirige
    return redirect(url_for("landing"))

@app.route("/login", methods=["GET", "POST"])
def login() -> Response:
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        row = get_user_by_email(email)
        if row and row["password_hash"] and check_password_hash(row["password_hash"], password):
            user = User(row)
            login_user(user)
            flash("Connexion réussie.", "success")
            return redirect(url_for("dashboard"))
        flash("Identifiants invalides.", "error")
        return redirect(url_for("login"))
    return render_with_fallback("login.html")

@app.route("/logout")
@login_required
def logout() -> Response:
    logout_user()
    flash("Déconnexion effectuée.", "info")
    return redirect(url_for("landing"))

@app.route("/auth/google")
def auth_google() -> Response:
    if oauth is None or "google" not in oauth:
        flash("Google OAuth non disponible.", "warning")
        return redirect(url_for("landing"))
    redirect_uri = app.config.get("GOOGLE_REDIRECT_URI") or url_for("auth_google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/auth/google/callback")
def auth_google_callback() -> Response:
    if oauth is None or "google" not in oauth:
        flash("Google OAuth non disponible.", "warning")
        return redirect(url_for("landing"))
    token = oauth.google.authorize_access_token()
    userinfo = oauth.google.parse_id_token(token)
    email = userinfo.get("email")
    google_sub = userinfo.get("sub")
    name = userinfo.get("name")
    if not email:
        flash("Impossible de récupérer l’e-mail Google.", "error")
        return redirect(url_for("landing"))
    existing = get_user_by_email(email)
    if existing:
        user = User(existing)
        login_user(user)
        return redirect(url_for("dashboard"))
    user = create_user(email, password=None, name=name, provider="google", google_sub=google_sub)
    login_user(user)
    default_bot = get_default_bot(user.id)
    if default_bot:
        snippet_html = build_snippet_html(user.embed_token, default_bot["shape"], default_bot["color_hex"])
        send_snippet_email(user.email, snippet_html)
    return redirect(url_for("dashboard"))

# ---------------------------------------------------------------------------
# Dashboard & configuration
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard() -> Response:
    bot = get_default_bot(int(current_user.id))
    packs = sorted(p.stem for p in PACKS_DIR.glob("*.yaml")) if PACKS_DIR.exists() else []
    snippet_html = build_snippet_html(current_user.embed_token, bot["shape"], bot["color_hex"]) if bot else ""
    return render_with_fallback("dashboard.html",
                                user=current_user, bot=bot, packs=packs, snippet_html=snippet_html)

@app.route("/dashboard/save", methods=["POST"])
@login_required
def save_dashboard() -> Response:
    bot = get_default_bot(int(current_user.id))
    if not bot:
        flash("Bot introuvable.", "error")
        return redirect(url_for("dashboard"))
    name = request.form.get("name") or bot["name"] or "Mon Betty Bot"
    metier = request.form.get("metier") or bot["metier"] or "generaliste"
    persona = request.form.get("persona") or bot["persona"] or "Assistant professionnel"
    color_hex = request.form.get("color_hex") or bot["color_hex"] or "#4F46E5"
    shape = request.form.get("shape") or bot["shape"] or "square"
    welcome_text = request.form.get("welcome_text") or bot["welcome_text"] or \
                   "Bonjour, je suis Betty, votre assistante AI. Que puis-je faire pour vous ?"
    yaml_file, _ = load_yaml_for_metier(metier)
    update_bot_configuration(bot_id=bot["id"], name=name, metier=metier, yaml_file=yaml_file,
                             persona=persona, color_hex=color_hex, shape=shape, welcome_text=welcome_text)
    flash("Configuration enregistrée.", "success")
    return redirect(url_for("test_page"))

@app.route("/test")
@login_required
def test_page() -> Response:
    bot = get_default_bot(int(current_user.id))
    warning = None if current_user.is_active_subscription else \
        "Votre abonnement est inactif. Souscrivez pour débloquer les conversations réelles."
    template_name = "test.html" if (BASE_DIR / "templates" / "test.html").exists() else "chat.html"
    
    cfg = {
        "avatar_url": (bot.get("avatar_url") if isinstance(bot, dict) else getattr(bot, "avatar_url", None)),
        "name":       (bot.get("name")       if isinstance(bot, dict) else getattr(bot, "name", None)) or "Mon Betty Bot",
        "color_hex":  (bot.get("color_hex")  if isinstance(bot, dict) else getattr(bot, "color_hex", None)) or "#4F46E5",
        "shape":      (bot.get("shape")      if isinstance(bot, dict) else getattr(bot, "shape", None)) or "square",
        "persona":    (bot.get("persona")    if isinstance(bot, dict) else getattr(bot, "persona", None)) or "Assistant",
        "welcome":    (bot.get("welcome_text") if isinstance(bot, dict) else getattr(bot, "welcome_text", None)) or "Bonjour 👋",
    }

return render_with_fallback(template_name, bot=bot, user=current_user, warning=warning)

# ---------------------------------------------------------------------------
# Stripe payment flow
# ---------------------------------------------------------------------------

@app.route("/pay")
@login_required
def pay() -> Response:
    return render_with_fallback("pay.html",
                                price="10 €", price_id=STRIPE_PRICE_ID,
                                subscription_status=current_user.subscription_status)

@app.route("/create-checkout-session", methods=["POST"])
@login_required
def create_checkout_session() -> Response:
    if not stripe.api_key or not STRIPE_PRICE_ID:
        flash("Le paiement n’est pas configuré pour le moment.", "error")
        return redirect(url_for("pay"))
    success_url = request.host_url.rstrip("/") + url_for("snippet")
    cancel_url = request.host_url.rstrip("/") + url_for("pay")
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=current_user.email,
            metadata={"user_id": current_user.id, "embed_token": current_user.embed_token},
        )
        return redirect(session.url)
    except Exception as exc:
        logger.error("Stripe checkout session error: %s", exc)
        flash("Impossible de créer la session de paiement.", "error")
        return redirect(url_for("pay"))

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook() -> Response:
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature")
    if STRIPE_WEBHOOK_SECRET and stripe.api_key:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except Exception as exc:
            logger.error("Webhook signature verification failed: %s", exc)
            return ("Invalid signature", 400)
    else:
        event = json.loads(payload or "{}")

    event_type = event.get("type")
    data_object = event.get("data", {}).get("object", {}) or {}
    user_row: Optional[sqlite3.Row] = None
    if event_type == "checkout.session.completed":
        metadata = data_object.get("metadata", {}) or {}
        user_id = metadata.get("user_id")
        if user_id:
            user_row = get_user_by_id(int(user_id))
        if not user_row and data_object.get("customer"):
            user_row = get_user_by_stripe_customer(data_object.get("customer"))
        subscription_id = data_object.get("subscription")
        customer_id = data_object.get("customer")
    elif event_type == "invoice.payment_succeeded":
        subscription_id = data_object.get("subscription")
        customer_id = data_object.get("customer")
        if subscription_id:
            user_row = get_user_by_stripe_subscription(subscription_id)
        if not user_row and customer_id:
            user_row = get_user_by_stripe_customer(customer_id)
    else:
        subscription_id = data_object.get("subscription")
        customer_id = data_object.get("customer")

    if user_row:
        current_end_ts = data_object.get("current_period_end")
        current_end = datetime.utcfromtimestamp(current_end_ts) if isinstance(current_end_ts, int) else None
        upsert_payment_record(user_id=user_row["id"], customer_id=customer_id,
                              subscription_id=subscription_id, status=data_object.get("status"),
                              current_period_end=current_end)
        if event_type in {"checkout.session.completed", "invoice.payment_succeeded"}:
            prev = user_row["subscription_status"]
            update_user_subscription(user_row["id"], "active")
            if prev != "active":
                bot = get_default_bot(user_row["id"])
                if bot:
                    snippet_html = build_snippet_html(user_row["embed_token"], bot["shape"], bot["color_hex"])
                    send_snippet_email(user_row["email"], snippet_html)
    return ("ok", 200)

# ---------------------------------------------------------------------------
# Snippet & embed
# ---------------------------------------------------------------------------

@app.route("/snippet")
@login_required
def snippet() -> Response:
    bot = get_default_bot(int(current_user.id))
    if not bot:
        flash("Bot introuvable.", "error")
        return redirect(url_for("dashboard"))
    snippet_html = build_snippet_html(current_user.embed_token, bot["shape"], bot["color_hex"])
    return render_with_fallback("snippet.html", snippet=snippet_html, bot=bot, user=current_user)

@app.route("/email-snippet", methods=["POST"])
@login_required
def email_snippet() -> Response:
    bot = get_default_bot(int(current_user.id))
    if not bot:
        flash("Bot introuvable.", "error")
        return redirect(url_for("dashboard"))
    snippet_html = build_snippet_html(current_user.embed_token, bot["shape"], bot["color_hex"])
    send_snippet_email(current_user.email, snippet_html)
    flash("Snippet envoyé par e-mail.", "success")
    return redirect(url_for("dashboard"))

@app.route("/embed.js")
def embed_js() -> Response:
    embed_token = request.args.get("bot")
    js_path = BASE_DIR / "static" / "embed.js"
    if not js_path.exists():
        return Response("console.error('embed.js introuvable');", mimetype="application/javascript")
    content = js_path.read_text(encoding="utf-8")
    theme_config: Optional[Dict[str, Any]] = None
    if embed_token:
        user_row = get_user_by_embed(embed_token)
        if user_row:
            bot = get_default_bot(user_row["id"])
            if bot:
                theme_config = {
                    "color": bot["color_hex"] or "#4F46E5",
                    "shape": bot["shape"] or "square",
                    "welcomeText": bot["welcome_text"] or "Bonjour, je suis Betty, votre assistante AI. Que puis-je faire pour vous ?",
                }
    if theme_config:
        content += "\n;window.__bettyBotTheme = " + json.dumps(theme_config) + ";\n"
    resp = Response(content, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-store"
    return resp

# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------

def get_or_create_chat(bot_id: int) -> sqlite3.Row:
    db = get_db()
    cur = db.execute("SELECT * FROM chats WHERE bot_id=? ORDER BY datetime(created_at) DESC LIMIT 1", (bot_id,))
    chat = cur.fetchone()
    if chat:
        created_at = datetime.fromisoformat(chat["created_at"]) if chat["created_at"] else datetime.utcnow()
        if created_at < datetime.utcnow() - timedelta(hours=1):
            chat = None
    if not chat:
        db.execute("INSERT INTO chats (bot_id) VALUES (?)", (bot_id,))
        db.commit()
        chat = db.execute("SELECT * FROM chats WHERE bot_id=? ORDER BY id DESC LIMIT 1", (bot_id,)).fetchone()
    return chat

def fetch_chat_history(chat_id: int, limit: int = 10) -> List[Dict[str, str]]:
    rows = get_db().execute(
        "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)
    ).fetchall()[::-1]
    return [{"role": r["role"], "content": r["content"]} for r in rows]

def append_message(chat_id: int, role: str, content: str) -> None:
    get_db().execute("INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)", (chat_id, role, content))
    get_db().commit()

@app.route("/api/chat", methods=["POST"])
def api_chat() -> Response:
    payload = request.get_json(force=True, silent=True) or {}
    embed_token = payload.get("embed_token")
    message = (payload.get("message") or "").strip()
    if not embed_token or not message:
        return jsonify({"error": "missing_parameters"}), 400

    user_row = get_user_by_embed(embed_token)
    if not user_row:
        return jsonify({"error": "unknown_bot"}), 404

    user = User(user_row)
    if user.subscription_status != "active":
        return jsonify({"error": "subscription_required"}), 402

    bot = get_default_bot(user.id)
    if not bot:
        return jsonify({"error": "bot_not_configured"}), 500

    chat = get_or_create_chat(bot["id"])
    history = fetch_chat_history(chat["id"], limit=10)

    system_prompt, metadata = build_system_prompt(bot)
    append_message(chat["id"], "user", message)
    reply = generate_llm_reply(system_prompt, history, message)
    append_message(chat["id"], "assistant", reply)

    lead_info = detect_lead_info(message + "\n" + reply)
    if any([lead_info.get("email"), lead_info.get("phone"), lead_info.get("intent")]):
        # e-mail lead à l’adresse d’inscription
        send_lead_email(user.email, lead_info)

    return jsonify({"reply": reply, "lead_suggestion": lead_info, "metadata": metadata})

# ---------------------------------------------------------------------------
# Errors & CLI
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(_: Exception) -> Response:
    return render_with_fallback("404.html"), 404

@app.errorhandler(500)
def internal_error(_: Exception) -> Response:
    return render_with_fallback("500.html"), 500

@app.cli.command("create-db")
def cli_create_db() -> None:
    init_db()
    print("Database initialized.")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# -------- Alias rétro-compat: save_settings -> save_dashboard --------
try:
    from flask_login import login_required  # peut déjà exister
except Exception:
    pass

try:
    @app.post("/_alias/save-settings", endpoint="save_settings")
    @login_required
    def _alias_save_settings():
        return save_dashboard()
except Exception:
    # Si l'endpoint existe déjà, on ignore.
    pass
# --------------------------------------------------------------------
