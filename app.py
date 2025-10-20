 (cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF' 
diff --git a/app.py b/app.py
index c735331f7ffda25c669cecedc851e0032ba2c167..6ae871357afe6d285f702eec9d97075fc8fa1038 100644
--- a/app.py
+++ b/app.py
@@ -1,639 +1,1064 @@
-# app.py — Betty Bots (UI propre, métiers fiables, FSM serveur)
+"""Main Flask application for Betty Bots subscription service."""
+
+from __future__ import annotations
+
+import json
+import logging
 import os
-import time
-import sqlite3
+import secrets
 import smtplib
-import json
-import uuid
-from email.message import EmailMessage
-from urllib.parse import quote
+import sqlite3
+from datetime import datetime, timedelta
+from email.mime.multipart import MIMEMultipart
+from email.mime.text import MIMEText
+from pathlib import Path
+from typing import Any, Dict, Iterable, List, Optional, Tuple
 
-import requests
 import stripe
-from flask import Flask, render_template, request, redirect, jsonify, url_for
+import yaml
 from dotenv import load_dotenv
+from flask import (
+    Flask,
+    Response,
+    flash,
+    g,
+    jsonify,
+    redirect,
+    render_template,
+    request,
+    url_for,
+)
+from flask_login import (
+    LoginManager,
+    UserMixin,
+    current_user,
+    login_required,
+    login_user,
+    logout_user,
+)
+from werkzeug.security import check_password_hash, generate_password_hash
 
-# Moteur de règles (packs YAML)
-from betty_rules.dialog_manager import reply as rule_reply
-
-# Préchargement optionnel des packs si dispo (no-op sinon)
 try:
-    from betty_rules import loader as _packs_loader
-except Exception:
-    _packs_loader = None
+    from authlib.integrations.flask_client import OAuth
+except Exception:  # pragma: no cover - optional dependency
+    OAuth = None
 
-load_dotenv()
 
-BASE_DIR = os.path.abspath(os.path.dirname(__file__))
-app = Flask(
-    __name__,
-    static_folder=os.path.join(BASE_DIR, "static"),
-    template_folder=os.path.join(BASE_DIR, "templates"),
-)
+# ---------------------------------------------------------------------------
+# Configuration & application setup
+# ---------------------------------------------------------------------------
 
-# -----------------------------------------------------------------------------
-# Config
-# -----------------------------------------------------------------------------
-BASE_URL = os.environ.get("BASE_URL") or os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000")
-
-# Stripe
-stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
-STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
-STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
-
-# PayPal
-PAYPAL_ENV = (os.environ.get("PAYPAL_ENV", "sandbox") or "sandbox").lower()
-PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
-PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
-PAYPAL_PLAN_ID = os.environ.get("PAYPAL_PLAN_ID", "")
-if PAYPAL_ENV == "sandbox":
-    PAYPAL_OAUTH = "https://api-m.sandbox.paypal.com/v1/oauth2/token"
-    PAYPAL_SUBS  = "https://api-m.sandbox.paypal.com/v1/billing/subscriptions/"
+BASE_DIR = Path(__file__).resolve().parent
+load_dotenv(BASE_DIR / ".env")
+
+app = Flask(__name__)
+app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
+app.config["TEMPLATES_AUTO_RELOAD"] = True
+
+DATABASE_PATH = os.getenv("DATABASE_URL", "sqlite:///data.db")
+if DATABASE_PATH.startswith("sqlite:///"):
+    DB_FILE = DATABASE_PATH.replace("sqlite:///", "")
 else:
-    PAYPAL_OAUTH = "https://api-m.paypal.com/v1/oauth2/token"
-    PAYPAL_SUBS  = "https://api-m.paypal.com/v1/billing/subscriptions/"
-
-# SMTP
-SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
-SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
-SMTP_USER = os.environ.get("SMTP_USER", "")
-SMTP_PASS = os.environ.get("SMTP_PASS", "")
-BRAND_NAME = os.environ.get("BRAND_NAME", "Betty Bots")
-
-# SQLite
-DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "payments.sqlite3"))
-
-# -----------------------------------------------------------------------------
-# Métiers / Packs — aucun tech visible en UI
-# -----------------------------------------------------------------------------
-ROLE_ALIAS = {
-    # Santé
-    "medecin": "medecine_pack",
-    "médecin": "medecine_pack",
-    "psychologue": "psychologue_pack",
-    "psy": "psychologue_pack",
-    # Droit/Chiffre
-    "avocat": "avocat_pack",
-    "comptable": "comptable_pack",
-    # Immobilier
-    "immobilier": "agent_immobilierbier",
-    "agent immobilier": "agent_immobilierbier",
-    "agent_immobilier": "agent_immobilierbier",
-    # → complète ici la liste des 20 métiers si besoin
-}
-DEFAULT_ROLE = "psychologue_pack"
-
-DISPLAY_LABELS = {
-    "psychologue_pack": "Psychologue",
-    "medecine_pack": "Médecin",
-    "avocat_pack": "Avocat",
-    "comptable_pack": "Comptable",
-    "agent_immobilierbier": "Agent immobilier",
-}
+    DB_FILE = "data.db"
+DB_PATH = BASE_DIR / DB_FILE
+
+login_manager = LoginManager(app)
+login_manager.login_view = "login"
+
+# Configure logging for SMTP fallbacks, etc.
+logging.basicConfig(level=logging.INFO)
+logger = logging.getLogger(__name__)
+
+# Stripe configuration (safe defaults when missing)
+stripe.api_key = os.getenv("STRIPE_SECRET_KEY") or None
+STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID_10_EUR")
+STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
+
+# OAuth configuration (optional)
+oauth: Optional[OAuth] = None
+if OAuth is not None:
+    oauth = OAuth(app)
+    google_client_id = os.getenv("OAUTH_GOOGLE_CLIENT_ID")
+    google_client_secret = os.getenv("OAUTH_GOOGLE_CLIENT_SECRET")
+    google_redirect_uri = os.getenv("OAUTH_GOOGLE_REDIRECT_URI")
+    if google_client_id and google_client_secret:
+        oauth.register(
+            name="google",
+            client_id=google_client_id,
+            client_secret=google_client_secret,
+            access_token_url="https://oauth2.googleapis.com/token",
+            access_token_params={"prompt": "consent"},
+            authorize_url="https://accounts.google.com/o/oauth2/auth",
+            authorize_params={
+                "access_type": "offline",
+                "prompt": "consent",
+                "response_type": "code",
+                "scope": "openid email profile",
+            },
+            api_base_url="https://www.googleapis.com/oauth2/v1/",
+            userinfo_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
+            client_kwargs={"scope": "openid email profile"},
+        )
+        app.config["GOOGLE_REDIRECT_URI"] = google_redirect_uri
+    else:
+        logger.info("Google OAuth credentials missing; Google signup disabled.")
+else:
+    logger.info("authlib not installed; Google signup disabled.")
+
+
+# ---------------------------------------------------------------------------
+# Database helpers
+# ---------------------------------------------------------------------------
+
+def get_db() -> sqlite3.Connection:
+    if "db" not in g:
+        conn = sqlite3.connect(DB_PATH)
+        conn.row_factory = sqlite3.Row
+        g.db = conn
+    return g.db  # type: ignore[return-value]
+
+
+@app.teardown_appcontext
+def close_db(_: Optional[BaseException]) -> None:
+    db = g.pop("db", None)
+    if db is not None:
+        db.close()
+
+
+def init_db() -> None:
+    conn = sqlite3.connect(DB_PATH)
+    conn.execute("PRAGMA foreign_keys = ON")
+    cur = conn.cursor()
+    cur.executescript(
+        """
+        CREATE TABLE IF NOT EXISTS users (
+            id INTEGER PRIMARY KEY AUTOINCREMENT,
+            email TEXT UNIQUE NOT NULL,
+            password_hash TEXT,
+            name TEXT,
+            auth_provider TEXT,
+            google_sub TEXT,
+            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
+            subscription_status TEXT DEFAULT 'inactive',
+            embed_token TEXT UNIQUE
+        );
+
+        CREATE TABLE IF NOT EXISTS bots (
+            id INTEGER PRIMARY KEY AUTOINCREMENT,
+            user_id INTEGER NOT NULL,
+            name TEXT,
+            metier TEXT,
+            yaml_file TEXT,
+            persona TEXT,
+            color_hex TEXT,
+            shape TEXT DEFAULT 'bubble',
+            welcome_text TEXT,
+            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
+            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
+            is_default INTEGER DEFAULT 0,
+            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
+        );
+
+        CREATE TABLE IF NOT EXISTS chats (
+            id INTEGER PRIMARY KEY AUTOINCREMENT,
+            bot_id INTEGER NOT NULL,
+            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
+            FOREIGN KEY (bot_id) REFERENCES bots(id) ON DELETE CASCADE
+        );
+
+        CREATE TABLE IF NOT EXISTS messages (
+            id INTEGER PRIMARY KEY AUTOINCREMENT,
+            chat_id INTEGER NOT NULL,
+            role TEXT NOT NULL,
+            content TEXT NOT NULL,
+            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
+            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
+        );
+
+        CREATE TABLE IF NOT EXISTS payments (
+            id INTEGER PRIMARY KEY AUTOINCREMENT,
+            user_id INTEGER NOT NULL,
+            stripe_customer_id TEXT,
+            stripe_subscription_id TEXT,
+            status TEXT,
+            current_period_end TEXT,
+            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
+        );
+        """
+    )
+    conn.commit()
+    conn.close()
 
-def canonical_role(role_label: str) -> str:
-    if not role_label:
-        return DEFAULT_ROLE
-    key = role_label.strip().lower()
-    return ROLE_ALIAS.get(key, DEFAULT_ROLE)
 
-def role_to_label(role: str) -> str:
-    return DISPLAY_LABELS.get(role, "Assistant")
+init_db()
 
-# Précharge les packs si l’API existe (pas bloquant)
-try:
-    if _packs_loader and hasattr(_packs_loader, "preload"):
-        _packs_loader.preload(set(DISPLAY_LABELS.keys()) | {DEFAULT_ROLE})
-except Exception as _e:
-    print("[packs] preload skipped:", _e)
-
-# -----------------------------------------------------------------------------
-# DB helpers
-# -----------------------------------------------------------------------------
-def _db_conn():
-    c = sqlite3.connect(DB_PATH)
-    c.execute("""
-    CREATE TABLE IF NOT EXISTS users (
-        tenant TEXT PRIMARY KEY,
-        name   TEXT,
-        email  TEXT,
-        role   TEXT,
-        color  TEXT,
-        avatar TEXT,
-        updated_at INTEGER
-    )""")
-    c.execute("""
-    CREATE TABLE IF NOT EXISTS subs (
-        tenant   TEXT PRIMARY KEY,
-        provider TEXT,
-        status   TEXT,
-        email    TEXT,
-        plan_id  TEXT,
-        created_at INTEGER
-    )""")
-    # état de conversation par tenant+session
-    c.execute("""
-    CREATE TABLE IF NOT EXISTS conversations (
-        tenant TEXT,
-        session_id TEXT,
-        stage TEXT,
-        payload TEXT,
-        updated_at INTEGER,
-        PRIMARY KEY (tenant, session_id)
-    )""")
-    # leads capturés (tous métiers)
-    c.execute("""
-    CREATE TABLE IF NOT EXISTS leads (
-        id INTEGER PRIMARY KEY AUTOINCREMENT,
-        tenant TEXT,
-        name TEXT,
-        email TEXT,
-        phone TEXT,
-        intent TEXT,
-        created_at INTEGER
-    )""")
-    return c
-
-def slug_email(email: str) -> str:
-    return (email or "").lower().replace("@", "-").replace(".", "-").replace("+", "-").strip("-")
-
-def upsert_user(tenant, name, email, role=None, color=None, avatar=None):
-    c = _db_conn()
-    now = int(time.time())
-    row = c.execute("SELECT tenant FROM users WHERE tenant=?", (tenant,)).fetchone()
-    if row:
-        c.execute("""
-            UPDATE users SET name=?, email=?, role=COALESCE(?, role),
-                             color=COALESCE(?, color), avatar=COALESCE(?, avatar),
-                             updated_at=? WHERE tenant=?
-        """, (name, email, role, color, avatar, now, tenant))
+
+# ---------------------------------------------------------------------------
+# User model for Flask-Login
+# ---------------------------------------------------------------------------
+
+
+class User(UserMixin):
+    def __init__(self, row: sqlite3.Row):
+        self.id = row["id"]
+        self.email = row["email"]
+        self.password_hash = row["password_hash"]
+        self.name = row["name"] or self.email.split("@")[0]
+        self.auth_provider = row["auth_provider"] or "password"
+        self.google_sub = row["google_sub"]
+        self.subscription_status = row["subscription_status"] or "inactive"
+        self.embed_token = row["embed_token"]
+
+    def get_id(self) -> str:  # type: ignore[override]
+        return str(self.id)
+
+    @property
+    def is_active_subscription(self) -> bool:
+        return self.subscription_status == "active"
+
+
+@login_manager.user_loader
+def load_user(user_id: str) -> Optional[User]:
+    row = get_user_by_id(int(user_id))
+    return User(row) if row else None
+
+
+# ---------------------------------------------------------------------------
+# Database utility functions
+# ---------------------------------------------------------------------------
+
+
+def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
+    cur = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,))
+    return cur.fetchone()
+
+
+def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
+    cur = get_db().execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,))
+    return cur.fetchone()
+
+
+def get_user_by_embed(embed_token: str) -> Optional[sqlite3.Row]:
+    cur = get_db().execute("SELECT * FROM users WHERE embed_token = ?", (embed_token,))
+    return cur.fetchone()
+
+
+def get_user_by_stripe_customer(customer_id: str) -> Optional[sqlite3.Row]:
+    cur = get_db().execute(
+        "SELECT u.* FROM users u JOIN payments p ON u.id = p.user_id WHERE p.stripe_customer_id = ?",
+        (customer_id,),
+    )
+    return cur.fetchone()
+
+
+def get_user_by_stripe_subscription(subscription_id: str) -> Optional[sqlite3.Row]:
+    cur = get_db().execute(
+        "SELECT u.* FROM users u JOIN payments p ON u.id = p.user_id WHERE p.stripe_subscription_id = ?",
+        (subscription_id,),
+    )
+    return cur.fetchone()
+
+
+def ensure_embed_token() -> str:
+    conn = get_db()
+    token = secrets.token_urlsafe(32)
+    cur = conn.execute("SELECT 1 FROM users WHERE embed_token = ?", (token,))
+    while cur.fetchone() is not None:
+        token = secrets.token_urlsafe(32)
+        cur = conn.execute("SELECT 1 FROM users WHERE embed_token = ?", (token,))
+    return token
+
+
+def create_user(email: str, password: Optional[str], name: Optional[str], provider: str, google_sub: Optional[str] = None) -> User:
+    db = get_db()
+    embed_token = ensure_embed_token()
+    password_hash = generate_password_hash(password) if password else None
+    db.execute(
+        "INSERT INTO users (email, password_hash, name, auth_provider, google_sub, embed_token) VALUES (?, ?, ?, ?, ?, ?)",
+        (email, password_hash, name, provider, google_sub, embed_token),
+    )
+    db.commit()
+    row = get_user_by_email(email)
+    if not row:
+        raise RuntimeError("User creation failed")
+    user = User(row)
+    create_default_bot_if_needed(user.id)
+    return user
+
+
+def create_default_bot_if_needed(user_id: int) -> None:
+    db = get_db()
+    cur = db.execute("SELECT id FROM bots WHERE user_id = ? AND is_default = 1", (user_id,))
+    if cur.fetchone() is not None:
+        return
+    db.execute(
+        """
+        INSERT INTO bots (user_id, name, metier, yaml_file, persona, color_hex, shape, welcome_text, is_default)
+        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
+        """,
+        (
+            user_id,
+            "Mon Betty Bot",
+            "generaliste",
+            "default_pack.yaml",
+            "Assistant professionnel",
+            "#4F46E5",
+            "bubble",
+            "Bonjour ! Comment puis-je vous aider aujourd'hui ?",
+        ),
+    )
+    db.commit()
+
+
+def get_default_bot(user_id: int) -> Optional[sqlite3.Row]:
+    cur = get_db().execute(
+        "SELECT * FROM bots WHERE user_id = ? AND is_default = 1 ORDER BY id ASC LIMIT 1",
+        (user_id,),
+    )
+    return cur.fetchone()
+
+
+def update_bot_configuration(
+    bot_id: int,
+    name: str,
+    metier: str,
+    yaml_file: str,
+    persona: str,
+    color_hex: str,
+    shape: str,
+    welcome_text: str,
+) -> None:
+    db = get_db()
+    db.execute(
+        """
+        UPDATE bots
+           SET name = ?,
+               metier = ?,
+               yaml_file = ?,
+               persona = ?,
+               color_hex = ?,
+               shape = ?,
+               welcome_text = ?,
+               updated_at = CURRENT_TIMESTAMP
+         WHERE id = ?
+        """,
+        (name, metier, yaml_file, persona, color_hex, shape, welcome_text, bot_id),
+    )
+    db.commit()
+
+
+def update_user_subscription(user_id: int, status: str) -> None:
+    db = get_db()
+    db.execute("UPDATE users SET subscription_status = ? WHERE id = ?", (status, user_id))
+    db.commit()
+
+
+def upsert_payment_record(
+    user_id: int,
+    customer_id: Optional[str],
+    subscription_id: Optional[str],
+    status: Optional[str],
+    current_period_end: Optional[datetime],
+) -> None:
+    db = get_db()
+    cur = db.execute("SELECT id FROM payments WHERE user_id = ?", (user_id,))
+    existing = cur.fetchone()
+    end_value = current_period_end.isoformat() if current_period_end else None
+    if existing:
+        db.execute(
+            """
+            UPDATE payments
+               SET stripe_customer_id = COALESCE(?, stripe_customer_id),
+                   stripe_subscription_id = COALESCE(?, stripe_subscription_id),
+                   status = COALESCE(?, status),
+                   current_period_end = COALESCE(?, current_period_end)
+             WHERE user_id = ?
+            """,
+            (customer_id, subscription_id, status, end_value, user_id),
+        )
     else:
-        c.execute("""
-            INSERT INTO users(tenant,name,email,role,color,avatar,updated_at)
-            VALUES(?,?,?,?,?,?,?)
-        """, (tenant, name, email, role or DEFAULT_ROLE, color or "#2563eb", avatar or "", now))
-    c.commit(); c.close()
-
-def get_user(tenant):
-    c = _db_conn()
-    row = c.execute("SELECT tenant,name,email,role,color,avatar,updated_at FROM users WHERE tenant=?", (tenant,)).fetchone()
-    c.close()
-    return row
-
-def upsert_sub(tenant: str, provider: str, status: str, email: str, plan_id: str):
-    if not tenant:
+        db.execute(
+            """
+            INSERT INTO payments (user_id, stripe_customer_id, stripe_subscription_id, status, current_period_end)
+            VALUES (?, ?, ?, ?, ?)
+            """,
+            (user_id, customer_id, subscription_id, status, end_value),
+        )
+    db.commit()
+
+
+# ---------------------------------------------------------------------------
+# Email utilities
+# ---------------------------------------------------------------------------
+
+
+SMTP_HOST = os.getenv("SMTP_HOST")
+SMTP_PORT = os.getenv("SMTP_PORT")
+SMTP_USER = os.getenv("SMTP_USER")
+SMTP_PASS = os.getenv("SMTP_PASS")
+FROM_EMAIL = os.getenv("FROM_EMAIL", "Betty Bots <no-reply@bettybots.ai>")
+
+
+def build_snippet_html(embed_token: str, shape: Optional[str], color_hex: Optional[str]) -> str:
+    domain = os.getenv("PUBLIC_DOMAIN", "https://your-domain.com")
+    safe_shape = shape or "bubble"
+    safe_color = color_hex or "#4F46E5"
+    snippet = (
+        "<!-- Betty Bot – intégration -->\n"
+        f"<div id=\"betty-bot\" data-embed-token=\"{embed_token}\" "
+        f"data-shape=\"{safe_shape}\" data-color=\"{safe_color}\"></div>\n"
+        f"<script src=\"{domain}/embed.js\" defer></script>"
+    )
+    return snippet
+
+
+def send_snippet_email(recipient: str, snippet_html: str) -> None:
+    if not (SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASS):
+        logger.warning("SMTP configuration incomplete; skipping snippet email to %s", recipient)
         return
-    c = _db_conn()
-    c.execute("""
-        INSERT INTO subs(tenant,provider,status,email,plan_id,created_at)
-        VALUES(?,?,?,?,?,?)
-        ON CONFLICT(tenant) DO UPDATE SET
-          provider=excluded.provider,
-          status=excluded.status,
-          email=excluded.email,
-          plan_id=excluded.plan_id
-    """, (tenant, provider, status, email or "", plan_id or "", int(time.time())))
-    c.commit(); c.close()
-
-def get_sub(tenant: str):
-    c = _db_conn()
-    row = c.execute("SELECT tenant,provider,status,email,plan_id,created_at FROM subs WHERE tenant=?", (tenant,)).fetchone()
-    c.close()
-    return row
-
-# --- Conversation state -------------------------------------------------------
-def get_session_id(req):
-    sid = (req.args.get("sid") or req.headers.get("X-Chat-Session") or "").strip()
-    return sid or str(uuid.uuid4())
-
-def load_conv(tenant, session_id):
-    c = _db_conn()
-    row = c.execute("SELECT stage,payload FROM conversations WHERE tenant=? AND session_id=?", (tenant, session_id)).fetchone()
-    c.close()
-    if not row:
-        return {"stage": "start", "payload": {}}
-    stage, payload = row
-    return {"stage": stage or "start", "payload": json.loads(payload or "{}")}
-
-def save_conv(tenant, session_id, stage, payload):
-    c = _db_conn()
-    now = int(time.time())
-    c.execute("""
-    INSERT INTO conversations(tenant,session_id,stage,payload,updated_at)
-    VALUES(?,?,?,?,?)
-    ON CONFLICT(tenant,session_id) DO UPDATE SET
-      stage=excluded.stage, payload=excluded.payload, updated_at=excluded.updated_at
-    """, (tenant, session_id, stage, json.dumps(payload, ensure_ascii=False), now))
-    c.commit(); c.close()
-
-def reset_conversations(tenant):
-    c = _db_conn()
-    c.execute("DELETE FROM conversations WHERE tenant=?", (tenant,))
-    c.commit(); c.close()
-
-def store_lead(tenant, name, email, phone, intent):
-    c = _db_conn()
-    c.execute("INSERT INTO leads(tenant,name,email,phone,intent,created_at) VALUES(?,?,?,?,?,?)",
-              (tenant, name, email, phone, intent, int(time.time())))
-    c.commit(); c.close()
-
-# -----------------------------------------------------------------------------
-# Utils
-# -----------------------------------------------------------------------------
-def build_snippet(tenant, role, color, avatar):
-    # UI clean: seul le tenant est exposé
-    embed_src = f"{BASE_URL.rstrip('/')}/static/embed.js"
-    return f'<script src="{embed_src}" data-tenant="{tenant}"></script>'
-
-def send_email(to_email: str, subject: str, html_body: str):
-    if not (SMTP_USER and SMTP_PASS and to_email):
-        return False
-    msg = EmailMessage()
-    msg["From"] = f"{BRAND_NAME} <{SMTP_USER}>"
-    msg["To"] = to_email
-    msg["Subject"] = subject
-    msg.set_content("Votre client e-mail n'affiche pas le HTML. Ouvrez ce message dans un client compatible.")
-    msg.add_alternative(html_body, subtype="html")
-    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
-        s.starttls()
-        s.login(SMTP_USER, SMTP_PASS)
-        s.send_message(msg)
-    return True
-
-def qstr(d: dict) -> str:
-    return "&".join([f"{k}={quote(str(v))}" for k, v in d.items()])
-
-# -----------------------------------------------------------------------------
-# Pages
-# -----------------------------------------------------------------------------
-@app.route("/", methods=["GET", "POST"])
-def welcome():
+
+    msg = MIMEMultipart("alternative")
+    msg["Subject"] = "Votre code d'intégration Betty Bot"
+    msg["From"] = FROM_EMAIL
+    msg["To"] = recipient
+
+    text_body = "Bonjour,\n\nRetrouvez ci-dessous le code d'intégration de votre Betty Bot.\n\n" + snippet_html
+    html_body = f"""
+    <html>
+      <body>
+        <p>Bonjour,<br><br>
+           Voici votre snippet d'intégration :
+        </p>
+        <pre style="background:#f4f4f5;padding:16px;border-radius:8px;">{snippet_html}</pre>
+        <p>Copiez-collez ce bloc sur votre site pour activer le chat.</p>
+      </body>
+    </html>
+    """
+
+    msg.attach(MIMEText(text_body, "plain"))
+    msg.attach(MIMEText(html_body, "html"))
+
+    try:
+        with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT)) as server:
+            server.starttls()
+            server.login(SMTP_USER, SMTP_PASS)
+            server.sendmail(FROM_EMAIL, [recipient], msg.as_string())
+        logger.info("Snippet email sent to %s", recipient)
+    except Exception as exc:  # pragma: no cover - network interaction
+        logger.warning("Failed to send snippet email to %s: %s", recipient, exc)
+
+
+# ---------------------------------------------------------------------------
+# YAML and prompt utilities
+# ---------------------------------------------------------------------------
+
+
+PACKS_DIR = BASE_DIR / "templates" / "packs"
+PACK_MAPPINGS = {
+    "agent_immobilier": "agent_immobilier",
+    "avocat": "avocat",
+    "comptable": "comptable",
+    "medecin": "medecin",
+    "psychologue": "psychologue",
+}
+
+
+def load_yaml_for_metier(metier: str) -> Tuple[str, Dict[str, Any]]:
+    """Return (filename, data) for the best matching pack."""
+    if not PACKS_DIR.exists():
+        return "default_pack.yaml", {
+            "description": "Assistant générique pour TPE/PME.",
+            "guidelines": ["Répondre avec empathie", "Qualifier les leads"],
+        }
+
+    normalized = metier.lower().strip().replace(" ", "_")
+    candidates: List[Path] = []
+
+    # Direct match
+    direct = PACKS_DIR / f"{normalized}.yaml"
+    if direct.exists():
+        candidates.append(direct)
+
+    # Mapping hints
+    mapped = PACK_MAPPINGS.get(normalized)
+    if mapped:
+        for path in PACKS_DIR.glob(f"{mapped}*.yaml"):
+            candidates.append(path)
+
+    # Fuzzy startswith search
+    for path in PACKS_DIR.glob("*.yaml"):
+        if normalized in path.stem:
+            candidates.append(path)
+
+    # Deduplicate while preserving order
+    unique_candidates: List[Path] = []
+    seen = set()
+    for path in candidates:
+        if path not in seen:
+            unique_candidates.append(path)
+            seen.add(path)
+
+    pack_path = unique_candidates[0] if unique_candidates else PACKS_DIR / "default_pack.yaml"
+
+    if not pack_path.exists():
+        return pack_path.name, {
+            "description": "Assistant générique pour TPE/PME.",
+            "guidelines": ["Répondre avec empathie", "Qualifier les leads"],
+        }
+
+    try:
+        with pack_path.open("r", encoding="utf-8") as fh:
+            data = yaml.safe_load(fh) or {}
+    except Exception as exc:
+        logger.warning("Unable to load YAML pack %s: %s", pack_path, exc)
+        data = {}
+    return pack_path.name, data
+
+
+def build_system_prompt(bot_row: sqlite3.Row) -> Tuple[str, Dict[str, Any]]:
+    yaml_file, yaml_data = load_yaml_for_metier(bot_row["metier"] or "generaliste")
+
+    guidelines: List[str] = []
+    if isinstance(yaml_data, dict):
+        if yaml_data.get("system_prompt"):
+            guidelines.append(str(yaml_data["system_prompt"]))
+        if yaml_data.get("description"):
+            guidelines.append(str(yaml_data["description"]))
+        rules = yaml_data.get("guidelines") or yaml_data.get("rules")
+        if isinstance(rules, Iterable):
+            guidelines.extend(str(item) for item in rules)
+        intents = yaml_data.get("intents")
+        if isinstance(intents, Iterable):
+            guidelines.append("Intents cibles : " + ", ".join(map(str, intents)))
+    else:
+        guidelines.append("Assistant conversationnel Betty Bots.")
+
+    persona = bot_row["persona"] or "Assistant professionnel"
+    color = bot_row["color_hex"] or "#4F46E5"
+    shape = bot_row["shape"] or "bubble"
+    welcome = bot_row["welcome_text"] or "Bienvenue !"
+
+    guidelines.extend(
+        [
+            f"Persona choisie : {persona}.",
+            "Collecte les coordonnées (nom, email, téléphone) si l'utilisateur semble qualifié.",
+            "Mentionne de proposer un rendez-vous si pertinent.",
+            f"Identité visuelle : couleur {color}, bulle {shape}.",
+            f"Message d'accueil : {welcome}",
+        ]
+    )
+
+    prompt = "\n".join(guidelines)
+    metadata = {
+        "yaml_file": yaml_file,
+        "persona": persona,
+        "color": color,
+        "shape": shape,
+        "welcome_text": welcome,
+    }
+    return prompt, metadata
+
+
+# ---------------------------------------------------------------------------
+# LLM stub and lead detection
+# ---------------------------------------------------------------------------
+
+
+LLM_PROVIDER = os.getenv("LLM_PROVIDER", "none").lower()
+LLM_API_KEY = os.getenv("LLM_API_KEY")
+
+
+def generate_llm_reply(system_prompt: str, history: List[Dict[str, str]], user_message: str) -> str:
+    """Return a mock or placeholder assistant reply."""
+    if not LLM_API_KEY or LLM_PROVIDER in {"none", "mock"}:
+        return "[Réponse simulée] Merci pour votre message. Un conseiller vous recontactera rapidement."
+
+    # Skeleton for real provider integrations
+    if LLM_PROVIDER == "openai":
+        # TODO: intégrer un appel à l'API OpenAI ChatCompletions
+        return "[TODO OpenAI] Réponse générée en conditions réelles."
+    if LLM_PROVIDER == "together":
+        # TODO: intégrer un appel à l'API Together AI
+        return "[TODO Together] Réponse générée en conditions réelles."
+
+    return "[Réponse simulée générique] Merci pour votre intérêt !"
+
+
+LEAD_KEYWORDS = {
+    "achat": {"achat", "acheter", "acquérir"},
+    "vente": {"vente", "vendre", "cession"},
+    "estimation": {"estimation", "devis", "tarif"},
+    "rdv": {"rdv", "rendez-vous", "appointment"},
+}
+
+
+def detect_lead_info(message: str) -> Dict[str, Optional[str]]:
+    import re
+
+    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", message)
+    phone_match = re.search(r"(?:(?:\+|00)\d{1,3}[\s.-]?)?(?:\d[\s.-]?){6,14}\d", message)
+
+    intent_detected: Optional[str] = None
+    lowered = message.lower()
+    for intent, keywords in LEAD_KEYWORDS.items():
+        if any(keyword in lowered for keyword in keywords):
+            intent_detected = intent
+            break
+
+    return {
+        "email": email_match.group(0) if email_match else None,
+        "phone": phone_match.group(0) if phone_match else None,
+        "intent": intent_detected,
+    }
+
+
+# ---------------------------------------------------------------------------
+# Utility helpers
+# ---------------------------------------------------------------------------
+
+
+def render_with_fallback(template_name: str, **context: Any) -> Response:
+    template_path = BASE_DIR / "templates" / template_name
+    if template_path.exists():
+        return render_template(template_name, **context)
+    return Response(
+        f"<h1>{template_name}</h1><pre>{json.dumps(context, indent=2, ensure_ascii=False)}</pre>",
+        mimetype="text/html",
+    )
+
+
+# ---------------------------------------------------------------------------
+# Routes: Public pages
+# ---------------------------------------------------------------------------
+
+
+@app.route("/")
+def landing() -> Response:
+    return render_with_fallback("landing.html")
+
+
+@app.route("/signup", methods=["GET", "POST"])
+def signup() -> Response:
+    if request.method == "POST":
+        email = (request.form.get("email") or "").strip().lower()
+        password = request.form.get("password") or ""
+        name = request.form.get("name") or None
+        if not email or not password:
+            flash("E-mail et mot de passe sont requis.", "error")
+            return redirect(url_for("signup"))
+        if get_user_by_email(email):
+            flash("Un compte existe déjà avec cet e-mail.", "error")
+            return redirect(url_for("login"))
+        user = create_user(email, password, name, provider="password")
+        login_user(user)
+        default_bot = get_default_bot(user.id)
+        if default_bot:
+            snippet_html = build_snippet_html(user.embed_token, default_bot["shape"], default_bot["color_hex"])
+            send_snippet_email(user.email, snippet_html)
+        flash("Compte créé avec succès !", "success")
+        return redirect(url_for("dashboard"))
+    return render_with_fallback("signup.html")
+
+
+@app.route("/login", methods=["GET", "POST"])
+def login() -> Response:
     if request.method == "POST":
-        name = (request.form.get("name") or "").strip()
-        email = (request.form.get("email") or "").strip()
-        if not (name and email):
-            return render_template("welcome.html", error="Merci de remplir votre nom et votre email.")
-        tenant = slug_email(email)
-        upsert_user(tenant, name, email)
-        return redirect(url_for("dashboard", tenant=tenant))
-    return render_template("welcome.html")
+        email = (request.form.get("email") or "").strip().lower()
+        password = request.form.get("password") or ""
+        row = get_user_by_email(email)
+        if row and row["password_hash"] and check_password_hash(row["password_hash"], password):
+            user = User(row)
+            login_user(user)
+            flash("Connexion réussie.", "success")
+            return redirect(url_for("dashboard"))
+        flash("Identifiants invalides.", "error")
+        return redirect(url_for("login"))
+    return render_with_fallback("login.html")
+
+
+@app.route("/logout")
+@login_required
+def logout() -> Response:
+    logout_user()
+    flash("Déconnexion effectuée.", "info")
+    return redirect(url_for("landing"))
+
+
+@app.route("/auth/google")
+def auth_google() -> Response:
+    if oauth is None or "google" not in oauth:
+        flash("Google OAuth non disponible.", "warning")
+        return redirect(url_for("signup"))
+    redirect_uri = app.config.get("GOOGLE_REDIRECT_URI") or url_for("auth_google_callback", _external=True)
+    return oauth.google.authorize_redirect(redirect_uri)
+
+
+@app.route("/auth/google/callback")
+def auth_google_callback() -> Response:
+    if oauth is None or "google" not in oauth:
+        flash("Google OAuth non disponible.", "warning")
+        return redirect(url_for("signup"))
+    token = oauth.google.authorize_access_token()
+    userinfo = oauth.google.parse_id_token(token)
+    email = userinfo.get("email")
+    google_sub = userinfo.get("sub")
+    name = userinfo.get("name")
+    if not email:
+        flash("Impossible de récupérer l'e-mail Google.", "error")
+        return redirect(url_for("signup"))
+    existing = get_user_by_email(email)
+    if existing:
+        user = User(existing)
+        login_user(user)
+        flash("Connexion Google réussie.", "success")
+        return redirect(url_for("dashboard"))
+    user = create_user(email, password=None, name=name, provider="google", google_sub=google_sub)
+    login_user(user)
+    default_bot = get_default_bot(user.id)
+    if default_bot:
+        snippet_html = build_snippet_html(user.embed_token, default_bot["shape"], default_bot["color_hex"])
+        send_snippet_email(user.email, snippet_html)
+    flash("Bienvenue chez Betty Bots !", "success")
+    return redirect(url_for("dashboard"))
+
+
+# ---------------------------------------------------------------------------
+# Routes: Dashboard & configuration
+# ---------------------------------------------------------------------------
+
 
 @app.route("/dashboard")
-def dashboard():
-    tenant = (request.args.get("tenant") or "").strip()
-    if not tenant:
-        return redirect(url_for("welcome"))
-    u = get_user(tenant)
-    name = u[1] if u else ""
-    email = u[2] if u else ""
-    return render_template("dashboard.html", tenant=tenant, name=name, email=email)
-
-@app.route("/save", methods=["POST"])
-def save_settings():
-    tenant = (request.form.get("tenant") or "").strip()
-    role_label = request.form.get("role") or "psychologue"   # libellé humain
-    color  = request.form.get("color") or "#2563eb"
-    avatar = request.form.get("avatar") or ""
-
-    u = get_user(tenant)
-    if not u:
-        return redirect(url_for("welcome"))
-
-    old_role = u[3] or DEFAULT_ROLE
-    new_role = canonical_role(role_label)
-
-    # si le métier change → purge de l'état de conversation
-    if new_role != old_role:
-        reset_conversations(tenant)
-
-    upsert_user(tenant, u[1], u[2], role=new_role, color=color, avatar=avatar)
-    return redirect(url_for("preview", tenant=tenant))
-
-@app.route("/preview")
-def preview():
-    tenant = (request.args.get("tenant") or "").strip()
-    u = get_user(tenant)
-    if not u:
-        return redirect(url_for("welcome"))
-    _, name, email, role, color, avatar, _ = u
-    return render_template("preview.html",
-                           tenant=tenant, name=name, email=email,
-                           role_label=role_to_label(role), color=color, avatar=avatar)
-
-@app.route("/chat")
-def chat_page():
-    tenant = (request.args.get("tenant") or "").strip()
-    u = get_user(tenant)
-    if not u:
-        return redirect(url_for("welcome"))
-    _, _, _, role, color, avatar, _ = u
-    return render_template("chat.html",
-                           tenant=tenant, role_label=role_to_label(role), color=color, avatar=avatar)
+@login_required
+def dashboard() -> Response:
+    bot = get_default_bot(int(current_user.id))
+    packs = []
+    if PACKS_DIR.exists():
+        packs = sorted(p.stem for p in PACKS_DIR.glob("*.yaml"))
+    snippet_html = ""
+    if bot:
+        snippet_html = build_snippet_html(current_user.embed_token, bot["shape"], bot["color_hex"])
+    context = {
+        "user": current_user,
+        "bot": bot,
+        "packs": packs,
+        "snippet_html": snippet_html,
+    }
+    return render_with_fallback("dashboard.html", **context)
+
+
+@app.route("/dashboard/save", methods=["POST"])
+@login_required
+def save_dashboard() -> Response:
+    bot = get_default_bot(int(current_user.id))
+    if not bot:
+        flash("Bot introuvable.", "error")
+        return redirect(url_for("dashboard"))
+
+    name = request.form.get("name") or bot["name"] or "Mon Betty Bot"
+    metier = request.form.get("metier") or bot["metier"] or "generaliste"
+    persona = request.form.get("persona") or bot["persona"] or "Assistant professionnel"
+    color_hex = request.form.get("color_hex") or bot["color_hex"] or "#4F46E5"
+    shape = request.form.get("shape") or bot["shape"] or "bubble"
+    welcome_text = request.form.get("welcome_text") or bot["welcome_text"] or "Bienvenue !"
+
+    yaml_file, _ = load_yaml_for_metier(metier)
+
+    update_bot_configuration(
+        bot_id=bot["id"],
+        name=name,
+        metier=metier,
+        yaml_file=yaml_file,
+        persona=persona,
+        color_hex=color_hex,
+        shape=shape,
+        welcome_text=welcome_text,
+    )
+
+    flash("Configuration enregistrée.", "success")
+    return redirect(url_for("dashboard"))
+
+
+@app.route("/test")
+@login_required
+def test_page() -> Response:
+    bot = get_default_bot(int(current_user.id))
+    warning = None
+    if not current_user.is_active_subscription:
+        warning = "Votre abonnement est inactif. Souscrivez pour débloquer les conversations réelles."
+    context = {
+        "bot": bot,
+        "user": current_user,
+        "warning": warning,
+    }
+    template_name = "test.html" if (BASE_DIR / "templates" / "test.html").exists() else "chat.html"
+    return render_with_fallback(template_name, **context)
+
+
+# ---------------------------------------------------------------------------
+# Routes: Stripe payment flow
+# ---------------------------------------------------------------------------
+
 
 @app.route("/pay")
-def pay():
-    tenant = (request.args.get("tenant") or "").strip()
-    u = get_user(tenant)
-    if not u:
-        return redirect(url_for("welcome"))
-    _, name, email, role, color, avatar, _ = u
-    return render_template(
-        "pay.html",
-        tenant=tenant, role_label=role_to_label(role), color=color, avatar=avatar,
-        paypal_env=PAYPAL_ENV, paypal_client_id=PAYPAL_CLIENT_ID, paypal_plan_id=PAYPAL_PLAN_ID
-    )
+@login_required
+def pay() -> Response:
+    context = {"price": "10 €", "price_id": STRIPE_PRICE_ID, "subscription_status": current_user.subscription_status}
+    return render_with_fallback("pay.html", **context)
 
-@app.route("/bot")
-def bot_page():
-    tenant = (request.args.get("tenant") or "").strip()
-    if not tenant:
-        return redirect(url_for("welcome"))
-
-    sub = get_sub(tenant)
-    if not sub or sub[2] not in ("active", "trialing"):
-        return redirect(url_for("pay", tenant=tenant))
-
-    u = get_user(tenant)
-    if not u:
-        return redirect(url_for("welcome"))
-    _, name, email, role, color, avatar, _ = u
-
-    paid_flag = request.args.get("paid") == "1"
-    return render_template("bot.html",
-                           tenant=tenant, role_label=role_to_label(role),
-                           color=color, avatar=avatar, paid=paid_flag)
-
-# -----------------------------------------------------------------------------
-# API — Widget
-# -----------------------------------------------------------------------------
-@app.route("/api/widget-config", methods=["GET"])
-def widget_config():
-    tenant = (request.args.get("tenant") or "").strip()
-    u = get_user(tenant)
-    if not u:
-        return jsonify({"ok": False, "reason": "unknown-tenant"}), 404
-    _, name, email, role, color, avatar, _ = u
-    return jsonify({
-        "ok": True,
-        "tenant": tenant,
-        "name": name,
-        "role_label": role_to_label(role),
-        "color": color,
-        "avatar": avatar or ""
-    })
 
-# -----------------------------------------------------------------------------
-# API — Chat (FSM anti-répétition + bons métiers)
-# -----------------------------------------------------------------------------
-@app.route("/api/chat", methods=["POST"])
-def api_chat():
-    data = request.get_json(force=True, silent=True) or {}
-    tenant = (data.get("tenant") or "").strip()
-    text   = (data.get("text") or "").strip()
-    sid    = data.get("sid") or get_session_id(request)
-
-    if not tenant:
-        return jsonify({"reply": "Missing tenant."}), 400
-    u = get_user(tenant)
-    if not u:
-        return jsonify({"reply": "Unknown tenant."}), 404
-
-    role = u[3] or DEFAULT_ROLE  # clé de pack interne
-    conv = load_conv(tenant, sid)
-    stage, p = conv["stage"], conv["payload"]
-
-    # --- Déduplication anti-double event ---
-    last_text = p.get("_last_text"); last_ts = p.get("_last_ts", 0.0)
-    now = time.time()
-    if last_text == text and (now - last_ts) < 2.0:
-        return jsonify({"reply": None, "sid": sid})
-    p["_last_text"], p["_last_ts"] = text, now
-
-    def reply(msg, next_stage=None):
-        save_conv(tenant, sid, next_stage or stage, p)
-        return jsonify({"reply": msg, "sid": sid})
-
-    # ------------------ Flows par métier ------------------
-    # IMMOBILIER
-    if role == "agent_immobilierbier":
-        if stage == "start":
-            return reply("Bonjour 👋 Je vous aide pour votre projet immobilier. Pour commencer, quel est votre nom et prénom ?", "ask_name")
-        if stage == "ask_name":
-            if len(text) < 2:
-                return reply("Je n’ai pas bien saisi. Pouvez-vous me donner votre nom et prénom ?")
-            p["name"] = text
-            return reply(f"Merci {p['name']}. Quelle est votre adresse e-mail pour vous recontacter ?", "ask_email")
-        if stage == "ask_email":
-            if "@" not in text or "." not in text:
-                return reply("Pouvez-vous indiquer une adresse e-mail valide, s’il vous plaît ?")
-            p["email"] = text
-            return reply("Souhaitez-vous laisser un numéro de téléphone pour un rappel ? (facultatif)", "ask_phone")
-        if stage == "ask_phone":
-            digits = "".join(ch for ch in text if ch.isdigit())
-            p["phone"] = digits if 8 <= len(digits) <= 15 else ""
-            return reply("Pouvez-vous préciser votre besoin ? (achat, vente, estimation…)", "ask_intent")
-        if stage == "ask_intent":
-            if not text:
-                return reply("Dites-moi simplement : achat, vente, estimation…")
-            p["intent"] = text
-            store_lead(tenant, p.get("name",""), p.get("email",""), p.get("phone",""), p.get("intent",""))
-            save_conv(tenant, sid, "done", p)
-            suivi = rule_reply(tenant, role, f"conseil_{p['intent']}".lower())
-            return jsonify({
-                "reply": f"Parfait 👍 J’ai tout noté.\n• Nom: {p['name']}\n• Email: {p['email']}\n• Téléphone: {p['phone'] or '—'}\n• Besoin: {p['intent']}\n\nUn conseiller vous recontactera très vite.\n\n{suivi or ''}",
-                "sid": sid
-            })
-        # free chat après capture
-        msg = rule_reply(tenant, role, text)
-        return jsonify({"reply": msg, "sid": sid})
-
-    # MEDECIN
-    if role == "medecine_pack":
-        if stage == "start":
-            return reply("Bonjour 👋 Je peux vous orienter. Pour commencer, quel est votre nom et prénom ?", "ask_name")
-        if stage == "ask_name":
-            if len(text) < 2:
-                return reply("Je n’ai pas bien saisi. Pouvez-vous me donner votre nom et prénom ?")
-            p["name"] = text
-            return reply(f"Merci {p['name']}. Quelle est votre adresse e-mail pour vous recontacter si besoin ?", "ask_email")
-        if stage == "ask_email":
-            if "@" not in text or "." not in text:
-                return reply("Pouvez-vous indiquer une adresse e-mail valide, s’il vous plaît ?")
-            p["email"] = text
-            return reply("Souhaitez-vous laisser un numéro de téléphone ? (facultatif)", "ask_phone")
-        if stage == "ask_phone":
-            digits = "".join(ch for ch in text if ch.isdigit())
-            p["phone"] = digits if 8 <= len(digits) <= 15 else ""
-            return reply("Quel est le motif de votre demande ? (ex. prise de RDV, renouvellement ordonnance, symptômes…)", "ask_reason")
-        if stage == "ask_reason":
-            if not text:
-                return reply("Quelques mots suffisent : RDV, symptômes, ordonnance…")
-            p["intent"] = text
-            store_lead(tenant, p.get("name",""), p.get("email",""), p.get("phone",""), p.get("intent",""))
-            save_conv(tenant, sid, "done", p)
-            suivi = rule_reply(tenant, role, f"tri_{p['intent']}".lower())
-            return jsonify({
-                "reply": f"Merci {p['name']}. J’ai noté votre demande : {p['intent']}.\nUn professionnel vous recontacte rapidement.\n\n{suivi or ''}",
-                "sid": sid
-            })
-        msg = rule_reply(tenant, role, text)
-        return jsonify({"reply": msg, "sid": sid})
-
-    # AUTRES MÉTIERS → ouverture + moteur
-    if stage == "start":
-        opening = rule_reply(tenant, role, "opening")
-        save_conv(tenant, sid, "free", p)
-        return jsonify({"reply": opening or "Bonjour, comment puis-je vous aider ?", "sid": sid})
-    msg = rule_reply(tenant, role, text)
-    return jsonify({"reply": msg, "sid": sid})
-
-# -----------------------------------------------------------------------------
-# Stripe API
-# -----------------------------------------------------------------------------
-@app.route("/api/stripe/checkout", methods=["POST"])
-def stripe_checkout():
+@app.route("/create-checkout-session", methods=["POST"])
+@login_required
+def create_checkout_session() -> Response:
     if not stripe.api_key or not STRIPE_PRICE_ID:
-        return jsonify({"error": "Stripe non configuré (clé ou price manquant)."}), 400
-    data = request.get_json(force=True, silent=True) or {}
-    tenant = (data.get("tenant") or "").strip()
-    if not tenant:
-        return jsonify({"error": "tenant manquant"}), 400
-    u = get_user(tenant)
-    if not u:
-        return jsonify({"error": "utilisateur introuvable"}), 400
-
-    success_url = f"{BASE_URL}/bot?" + qstr({"tenant": tenant, "paid": 1})
-    cancel_url  = f"{BASE_URL}/pay?tenant={quote(tenant)}"
+        flash("Stripe n'est pas configuré actuellement.", "error")
+        return redirect(url_for("pay"))
+
+    success_url = request.host_url.rstrip("/") + url_for("dashboard")
+    cancel_url = request.host_url.rstrip("/") + url_for("pay")
+
     try:
         session = stripe.checkout.Session.create(
             mode="subscription",
             line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
             success_url=success_url,
             cancel_url=cancel_url,
-            client_reference_id=tenant,
-            customer_email=u[2] or None,
-            metadata={"tenant": tenant}
+            customer_email=current_user.email,
+            metadata={"user_id": current_user.id, "embed_token": current_user.embed_token},
         )
-        return jsonify({"url": session.url})
-    except Exception as e:
-        return jsonify({"error": str(e)}), 400
-
-@app.route("/webhooks/stripe", methods=["POST"])
-def stripe_webhook():
-    if not STRIPE_WEBHOOK_SECRET:
-        return "webhook secret manquant", 400
-    payload = request.data
-    sig = request.headers.get("Stripe-Signature", "")
-    try:
-        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
-    except Exception as e:
-        return str(e), 400
+        return redirect(session.url)
+    except Exception as exc:  # pragma: no cover - network interaction
+        logger.error("Stripe checkout session error: %s", exc)
+        flash("Impossible de créer la session de paiement.", "error")
+        return redirect(url_for("pay"))
+
+
+@app.route("/stripe/webhook", methods=["POST"])
+def stripe_webhook() -> Response:
+    payload = request.get_data(as_text=True)
+    sig_header = request.headers.get("Stripe-Signature")
+
+    if STRIPE_WEBHOOK_SECRET and stripe.api_key:
+        try:
+            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
+        except Exception as exc:  # pragma: no cover - depends on signature
+            logger.error("Webhook signature verification failed: %s", exc)
+            return ("Invalid signature", 400)
+    else:
+        event = json.loads(payload or "{}")
+
+    event_type = event.get("type")
+    data_object = event.get("data", {}).get("object", {})
+
+    user_row: Optional[sqlite3.Row] = None
+
+    if event_type == "checkout.session.completed":
+        metadata = data_object.get("metadata", {})
+        user_id = metadata.get("user_id")
+        if user_id:
+            user_row = get_user_by_id(int(user_id))
+        if not user_row and data_object.get("customer"):
+            user_row = get_user_by_stripe_customer(data_object.get("customer"))
+        subscription_id = data_object.get("subscription")
+        customer_id = data_object.get("customer")
+    elif event_type == "invoice.payment_succeeded":
+        subscription_id = data_object.get("subscription")
+        customer_id = data_object.get("customer")
+        if subscription_id:
+            user_row = get_user_by_stripe_subscription(subscription_id)
+        if not user_row and customer_id:
+            user_row = get_user_by_stripe_customer(customer_id)
+    else:
+        subscription_id = data_object.get("subscription")
+        customer_id = data_object.get("customer")
+
+    if user_row:
+        current_end_ts = data_object.get("current_period_end")
+        current_end = datetime.utcfromtimestamp(current_end_ts) if isinstance(current_end_ts, int) else None
+        upsert_payment_record(
+            user_id=user_row["id"],
+            customer_id=customer_id,
+            subscription_id=subscription_id,
+            status=data_object.get("status"),
+            current_period_end=current_end,
+        )
+        if event_type in {"checkout.session.completed", "invoice.payment_succeeded"}:
+            previous_status = user_row["subscription_status"]
+            update_user_subscription(user_row["id"], "active")
+            if previous_status != "active":
+                bot = get_default_bot(user_row["id"])
+                if bot:
+                    snippet_html = build_snippet_html(user_row["embed_token"], bot["shape"], bot["color_hex"])
+                    send_snippet_email(user_row["email"], snippet_html)
+
+    return ("ok", 200)
+
+
+# ---------------------------------------------------------------------------
+# Routes: Snippet & embed
+# ---------------------------------------------------------------------------
+
+
+@app.route("/snippet")
+@login_required
+def snippet() -> Response:
+    bot = get_default_bot(int(current_user.id))
+    if not bot:
+        flash("Bot introuvable.", "error")
+        return redirect(url_for("dashboard"))
+    snippet_html = build_snippet_html(current_user.embed_token, bot["shape"], bot["color_hex"])
+    return render_with_fallback("snippet.html", snippet=snippet_html, bot=bot, user=current_user)
+
+
+@app.route("/email-snippet", methods=["POST"])
+@login_required
+def email_snippet() -> Response:
+    bot = get_default_bot(int(current_user.id))
+    if not bot:
+        flash("Bot introuvable.", "error")
+        return redirect(url_for("dashboard"))
+    snippet_html = build_snippet_html(current_user.embed_token, bot["shape"], bot["color_hex"])
+    send_snippet_email(current_user.email, snippet_html)
+    flash("Snippet envoyé par e-mail.", "success")
+    return redirect(url_for("dashboard"))
+
+
+@app.route("/embed.js")
+def embed_js() -> Response:
+    embed_token = request.args.get("bot")
+    js_path = BASE_DIR / "static" / "embed.js"
+    if not js_path.exists():
+        return Response("console.error('embed.js introuvable');", mimetype="application/javascript")
+
+    content = js_path.read_text(encoding="utf-8")
+    theme_config: Optional[Dict[str, Any]] = None
+    if embed_token:
+        user_row = get_user_by_embed(embed_token)
+        if user_row:
+            bot = get_default_bot(user_row["id"])
+            if bot:
+                theme_config = {
+                    "color": bot["color_hex"] or "#4F46E5",
+                    "shape": bot["shape"] or "bubble",
+                    "welcomeText": bot["welcome_text"] or "Bonjour !",
+                }
+    if theme_config:
+        content += "\n;window.__bettyBotTheme = " + json.dumps(theme_config) + ";\n"
+
+    response = Response(content, mimetype="application/javascript")
+    response.headers["Cache-Control"] = "no-store"
+    return response
+
+
+# ---------------------------------------------------------------------------
+# Routes: Chat API
+# ---------------------------------------------------------------------------
+
+
+def get_or_create_chat(bot_id: int) -> sqlite3.Row:
+    db = get_db()
+    cur = db.execute(
+        "SELECT * FROM chats WHERE bot_id = ? ORDER BY datetime(created_at) DESC LIMIT 1",
+        (bot_id,),
+    )
+    chat = cur.fetchone()
+    if chat:
+        created_at = datetime.fromisoformat(chat["created_at"]) if chat["created_at"] else datetime.utcnow()
+        if created_at < datetime.utcnow() - timedelta(hours=1):
+            chat = None
+    if not chat:
+        db.execute("INSERT INTO chats (bot_id) VALUES (?)", (bot_id,))
+        db.commit()
+        cur = db.execute("SELECT * FROM chats WHERE bot_id = ? ORDER BY id DESC LIMIT 1", (bot_id,))
+        chat = cur.fetchone()
+    return chat
+
+
+def fetch_chat_history(chat_id: int, limit: int = 10) -> List[Dict[str, str]]:
+    cur = get_db().execute(
+        "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
+        (chat_id, limit),
+    )
+    rows = list(cur.fetchall())[::-1]
+    return [{"role": row["role"], "content": row["content"]} for row in rows]
 
-    etype = event.get("type")
-    obj = event.get("data", {}).get("object", {}) or {}
 
-    try:
-        if etype in ("checkout.session.completed", "customer.subscription.created", "customer.subscription.updated"):
-            tenant = (obj.get("client_reference_id") or (obj.get("metadata") or {}).get("tenant") or "").strip()
-            email_from_stripe = ""
-            if obj.get("customer_details"):
-                email_from_stripe = obj["customer_details"].get("email") or ""
-            if tenant:
-                upsert_sub(tenant, provider="stripe", status="active", email=email_from_stripe, plan_id=STRIPE_PRICE_ID)
-                u = get_user(tenant)
-                if u:
-                    _, name, email, role, color, avatar, _ = u
-                    to = email or email_from_stripe
-                    snippet = build_snippet(tenant, role, color, avatar)
-                    html = f"""
-                    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial">
-                      <h2>🎉 Merci de vous être abonné — {BRAND_NAME}</h2>
-                      <p>Bonjour {name},</p>
-                      <p>Votre abonnement est actif. Voici votre code d’intégration :</p>
-                      <pre style="background:#0b1220;color:#e5e7eb;padding:12px;border-radius:8px;white-space:pre-wrap">{snippet}</pre>
-                      <p>Collez-le <b>avant &lt;/body&gt;</b> dans votre site.</p>
-                      <p>Retrouvez-le aussi ici : <a href="{BASE_URL}/bot?tenant={tenant}&paid=1">{BASE_URL}/bot?tenant={tenant}&paid=1</a></p>
-                      <hr/>
-                      <p>Un reçu/facture Stripe vous est envoyé automatiquement.</p>
-                    </div>
-                    """
-                    if to:
-                        try:
-                            send_email(to, f"{BRAND_NAME} — Abonnement confirmé", html)
-                        except Exception as e:
-                            print("Email send error:", e)
-    except Exception as e:
-        print("Stripe webhook processing error:", e)
-
-    return "ok", 200
-
-# -----------------------------------------------------------------------------
-# PayPal API
-# -----------------------------------------------------------------------------
-def paypal_token() -> str:
-    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
-        raise RuntimeError("PayPal non configuré (CLIENT_ID/SECRET manquants).")
-    r = requests.post(PAYPAL_OAUTH, auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
-                      data={"grant_type": "client_credentials"}, timeout=30)
-    r.raise_for_status()
-    return r.json()["access_token"]
-
-@app.route("/api/paypal/verify", methods=["POST"])
-def paypal_verify():
-    data = request.get_json(force=True, silent=True) or {}
-    tenant = (data.get("tenant") or "").strip()
-    subscription_id = (data.get("subscriptionID") or "").strip()
-    if not tenant or not subscription_id:
-        return jsonify({"ok": False, "reason": "missing-tenant-or-subscription"}), 400
+def append_message(chat_id: int, role: str, content: str) -> None:
+    db = get_db()
+    db.execute("INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)", (chat_id, role, content))
+    db.commit()
+
+
+@app.route("/api/chat", methods=["POST"])
+def api_chat() -> Response:
+    payload = request.get_json(force=True, silent=True) or {}
+    embed_token = payload.get("embed_token")
+    message = (payload.get("message") or "").strip()
+    if not embed_token or not message:
+        return jsonify({"error": "missing_parameters"}), 400
+
+    user_row = get_user_by_embed(embed_token)
+    if not user_row:
+        return jsonify({"error": "unknown_bot"}), 404
+
+    user = User(user_row)
+    if user.subscription_status != "active":
+        return jsonify({"error": "subscription_required"}), 402
+
+    bot = get_default_bot(user.id)
+    if not bot:
+        return jsonify({"error": "bot_not_configured"}), 500
+
+    chat = get_or_create_chat(bot["id"])
+    history = fetch_chat_history(chat["id"], limit=10)
+
+    system_prompt, metadata = build_system_prompt(bot)
+
+    append_message(chat["id"], "user", message)
+    reply = generate_llm_reply(system_prompt, history, message)
+    append_message(chat["id"], "assistant", reply)
+
+    lead_info = detect_lead_info(message + "\n" + reply)
+
+    response_body = {
+        "reply": reply,
+        "lead_suggestion": lead_info,
+        "metadata": metadata,
+    }
+    return jsonify(response_body)
+
+
+# ---------------------------------------------------------------------------
+# Error handlers
+# ---------------------------------------------------------------------------
+
+
+@app.errorhandler(404)
+def not_found(_: Exception) -> Response:
+    return render_with_fallback("404.html"), 404
+
+
+@app.errorhandler(500)
+def internal_error(_: Exception) -> Response:
+    return render_with_fallback("500.html"), 500
+
+
+# ---------------------------------------------------------------------------
+# CLI helper
+# ---------------------------------------------------------------------------
+
+
+@app.cli.command("create-db")
+def cli_create_db() -> None:
+    """Initialize the database tables."""
+    init_db()
+    print("Database initialized.")
+
+
+# ---------------------------------------------------------------------------
+# Entry point
+# ---------------------------------------------------------------------------
+
 
-    try:
-        token = paypal_token()
-        r = requests.get(PAYPAL_SUBS + subscription_id, headers={"Authorization": f"Bearer {token}"}, timeout=30)
-        if r.status_code != 200:
-            return jsonify({"ok": False, "reason": "lookup-failed"}), 400
-
-        info = r.json()
-        status = info.get("status")
-        email_pp = (info.get("subscriber", {}) or {}).get("email_address", "")
-
-        if status == "ACTIVE":
-            upsert_sub(tenant, provider="paypal", status="active", email=email_pp, plan_id=PAYPAL_PLAN_ID)
-            u = get_user(tenant)
-            if u:
-                _, name, email, role, color, avatar, _ = u
-                to = email or email_pp
-                snippet = build_snippet(tenant, role, color, avatar)
-                html = f"""
-                <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial">
-                  <h2>🎉 Merci de vous être abonné — {BRAND_NAME}</h2>
-                  <p>Bonjour {name},</p>
-                  <p>Votre abonnement est actif. Voici votre code d’intégration :</p>
-                  <pre style="background:#0b1220;color:#e5e7eb;padding:12px;border-radius:8px;white-space:pre-wrap">{snippet}</pre>
-                  <p>Collez-le <b>avant &lt;/body&gt;</b> dans votre site.</p>
-                  <p>Retrouvez-le aussi ici : <a href="{BASE_URL}/bot?tenant={tenant}&paid=1">{BASE_URL}/bot?tenant={tenant}&paid=1</a></p>
-                </div>
-                """
-                if to:
-                    try:
-                        send_email(to, f"{BRAND_NAME} — Abonnement confirmé", html)
-                    except Exception as e:
-                        print("Email send error:", e)
-            return jsonify({"ok": True})
-
-        return jsonify({"ok": False, "reason": status or "unknown"}), 400
-    except Exception as e:
-        return jsonify({"ok": False, "reason": str(e)}), 400
-
-# -----------------------------------------------------------------------------
-# Health
-# -----------------------------------------------------------------------------
-@app.route("/healthz")
-def healthz():
-    return jsonify({"ok": True, "ts": int(time.time())})
-
-# -----------------------------------------------------------------------------
-# Entrée
-# -----------------------------------------------------------------------------
 if __name__ == "__main__":
-    port = int(os.environ.get("PORT", 5000))
-    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("DEBUG")))
+    port = int(os.getenv("PORT", 10000))
+    app.run(host="0.0.0.0", port=port) 
EOF
)
