from __future__ import annotations
import os
import sqlite3
import secrets
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, Response, send_from_directory, jsonify
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    current_user, login_required
)
import stripe
import yaml
import smtplib
from email.mime.text import MIMEText
import re

"""
This is a complete replacement for your original `app.py` tailored for
BettyBots. It preserves all of the existing functionality around user
management, dashboard configuration, and Stripe payment while adding a
pack‑driven preview experience. The preview now loads a YAML pack
definition from `templates/packs/<slug>.yaml` based off of the
configured métier, displays the proper avatar, colour and opening
phrase, and uses simple rule‑based logic (defined in the YAML) to
handle conversation flows. Leads can be captured and, once the user
provides an email (in the final paid flow), delivered via SMTP.  If
SMTP isn’t configured the lead will simply be logged to the console.

The key additions are:
  • `load_pack()` – load a YAML file from the packs folder and apply
    sensible defaults (slug, name, colour, avatar, etc.).
  • `apply_rules()` – a simple rule engine that inspects the
    conversation and determines the next reply based on the pack’s
    intents and qualification prompts.
  • `send_lead_email()` – sends an email via SMTP using environment
    variables (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`,
    `FROM_EMAIL`).
  • `/api/chat` and `/api/lead` endpoints – handle chat interaction
    using the YAML rules and capture leads; leads are mailed to the
    client if `user_email` is supplied.
  • Updated `/preview` route – loads the correct pack based on the
    user’s configured métier and passes a `cfg` dictionary to the
    template with everything the preview needs (avatar, colour,
    opening, etc.).
"""

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

def pick_db_path() -> Path:
    """Sur Vercel (serverless), écrire dans /tmp. Local: fichier dans le projet."""
    if os.getenv("DB_PATH"):
        return Path(os.getenv("DB_PATH"))
    if any(os.getenv(k) for k in ("VERCEL", "VERCEL_URL", "VERCEL_ENV", "AWS_LAMBDA_FUNCTION_NAME")):
        return Path("/tmp/payments.db")
    return BASE_DIR / "payments.db"

DB_PATH = pick_db_path()

def connect_db() -> sqlite3.Connection:
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        tmp = Path("/tmp/payments.db")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(tmp, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET", "dev_key")

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_CENTS = int(os.getenv("STRIPE_PRICE_CENTS", "999"))  # 9,99 €
STRIPE_CURRENCY   = os.getenv("STRIPE_CURRENCY", "eur")
PUBLIC_BASE_URL   = os.getenv("PUBLIC_BASE_URL") or os.getenv("BASE_URL") or ""

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "root"

# ---------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------

def init_db() -> None:
    c = connect_db()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            metier TEXT,
            avatar_url TEXT,
            color_hex TEXT,
            shape TEXT,
            persona TEXT,
            welcome_text TEXT
        )
    """)
    c.commit()
    c.close()


def db_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    c = connect_db()
    cur = c.execute(sql, params)
    row = cur.fetchone()
    c.close()
    return row


def db_exec(sql: str, params: tuple = ()) -> None:
    c = connect_db()
    c.execute(sql, params)
    c.commit()
    c.close()


def get_bot(user_id: int) -> sqlite3.Row | None:
    return db_one("SELECT * FROM bots WHERE user_id=?", (user_id,))


init_db()

# ---------------------------------------------------------------------
# Modèle utilisateur
# ---------------------------------------------------------------------

class User(UserMixin):
    def __init__(self, id_: int, email: str):
        self.id = id_
        self.email = email


@login_manager.user_loader
def load_user(user_id: str):
    row = db_one("SELECT * FROM users WHERE id=?", (user_id,))
    if row:
        return User(row["id"], row["email"])
    return None

# ---------------------------------------------------------------------
# Favicon (évite les 500 si pas d'icône)
# ---------------------------------------------------------------------

@app.get("/favicon.ico")
def favicon() -> Response:
    fav_dir = BASE_DIR / "static"
    if (fav_dir / "favicon.ico").exists():
        return send_from_directory(fav_dir, "favicon.ico")
    return Response(status=204)

# ---------------------------------------------------------------------
# Packs & Rules
# ---------------------------------------------------------------------

# Path to the packs folder
PACK_DIR = BASE_DIR / "templates" / "packs"

# Mapping from a user‑friendly métier label to a YAML slug. If you add
# new métiers, extend this dictionary accordingly. The slug corresponds
# to the filename without `.yaml`.
METIER_SLUGS = {
    "Avocate": "avocat_pack",
    "Agent Immo": "agent_immobilierbilier",
    "Médecine": "medecine_pack",
    "Comptable": "comptable_pack",
    "Psychologue": "psychologue_pack",
}


def load_pack(slug: str) -> dict | None:
    """
    Load the YAML pack definition located at `templates/packs/<slug>.yaml`.
    Returns a dictionary with sensible defaults filled in (name, metier,
    colour, avatar shape, avatar URL, opening text). If the file doesn’t
    exist or fails to parse, returns None.
    """
    path = PACK_DIR / f"{slug}.yaml"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # Provide defaults
        data.setdefault("slug", slug)
        data.setdefault("name", data.get("title") or slug.capitalize())
        data.setdefault("metier", data.get("metier") or slug)
        data.setdefault("opening", data.get("opening") or f"Bonjour, je suis Betty {data['name']}.")
        data.setdefault("color", data.get("color") or "#4F46E5")
        data.setdefault("avatar_shape", data.get("avatar_shape") or "rounded")
        # Look for an avatar in static/avatars/<slug>.(png|jpg|jpeg|webp)
        static_dir = BASE_DIR / "static" / "avatars"
        for ext in ("png", "jpg", "jpeg", "webp"):
            fp = static_dir / f"{slug}.{ext}"
            if fp.exists():
                data["avatar_url"] = f"/static/avatars/{slug}.{ext}"
                break
        return data
    except Exception:
        return None


# Regular expressions for simple pattern matching
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(\+?\d[\d\s\.\-]{6,})")


def apply_rules(message: str, pack: dict, history: list[str]) -> dict:
    """
    Apply a very simple rule engine to produce a reply and decide whether
    to prompt for lead capture. The `pack` dictionary may contain:
      • opening: str – initial greeting if no history
      • intents: list of objects with keys {match, reply, ask_lead}
      • qualify: prompts with keys {ask_email, ask_phone, ask_need}
      • fallback: str – default response
    Returns a dict {reply: str, ask_lead: bool}.
    """
    msg = (message or "").strip()
    # If no history, deliver the opening from the pack
    if not history:
        return {
            "reply": pack.get("opening", "Bonjour 👋 On fait un essai ?"),
            "ask_lead": False,
        }
    lower = msg.lower()
    email_match = EMAIL_RE.search(msg or "")
    phone_match = PHONE_RE.search(msg or "")
    # Check explicit intents
    intents = pack.get("intents") or []
    for intent in intents:
        pat = intent.get("match")
        if not pat:
            continue
        try:
            if re.search(pat, lower):
                return {
                    "reply": intent.get("reply") or "Bien noté.",
                    "ask_lead": bool(intent.get("ask_lead")),
                }
        except re.error:
            # Invalid regex falls back to substring match
            if pat in lower:
                return {
                    "reply": intent.get("reply") or "Bien noté.",
                    "ask_lead": bool(intent.get("ask_lead")),
                }
    # Qualification prompts
    qualify = pack.get("qualify") or {}
    # Ask email if none found in history or current message
    if not any(EMAIL_RE.search(h) for h in history) and not email_match:
        return {
            "reply": qualify.get("ask_email", "Quel est votre email pour le suivi ?"),
            "ask_lead": True,
        }
    # Ask phone if none found
    if not any(PHONE_RE.search(h) for h in history) and not phone_match:
        return {
            "reply": qualify.get("ask_phone", "Souhaitez‑vous laisser un numéro pour un rappel ?"),
            "ask_lead": True,
        }
    # Ask need if not clearly specified
    if not any(any(k in h.lower() for k in ["achat", "vente", "estimation", "rdv", "devis", "consult", "urgence"]) for h in history):
        return {
            "reply": qualify.get("ask_need", "Pouvez‑vous préciser votre besoin (ex: achat, vente, devis, RDV) ?"),
            "ask_lead": True,
        }
    # Fallback
    return {
        "reply": pack.get("fallback", "Merci. Je transmets votre demande."),
        "ask_lead": False,
    }


def send_lead_email(to_email: str, subject: str, html_body: str) -> bool:
    """
    Send an HTML email via SMTP. Configuration must be supplied via
    environment variables: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS.
    Optionally FROM_EMAIL. Returns True on success, False otherwise.
    """
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pwd  = os.getenv("SMTP_PASS")
    sender = os.getenv("FROM_EMAIL", user or "no-reply@example.com")
    if not host or not user or not pwd:
        app.logger.warning("SMTP non configuré; envoi d’email désactivé.")
        return False
    try:
        msg = MIMEText(html_body, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to_email
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, pwd)
            s.sendmail(sender, [to_email], msg.as_string())
        return True
    except Exception as e:
        app.logger.error(f"Erreur d’envoi de mail: {e}")
        return False

# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------

# Accueil → création d’un invité + redirection Dashboard
@app.get("/")
def root():
    # si déjà connecté, direct dashboard
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    # crée/charge un utilisateur invité
    email = f"guest-{secrets.token_hex(4)}@guest.local"
    try:
        db_exec("INSERT INTO users(email) VALUES(?)", (email,))
    except Exception:
        pass
    row = db_one("SELECT * FROM users WHERE email=?", (email,))
    if row:
        login_user(User(row["id"], row["email"]))
        app.logger.info(f"Guest user created: {email}")
    return redirect(url_for("dashboard"))


# Page 1 — Dashboard (configuration du bot)
@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    metiers = ["Avocate", "Agent Immo", "Médecine", "Comptable", "Psychologue"]
    row = get_bot(int(current_user.id))
    bot = dict(row) if row else None
    if request.method == "POST":
        name        = request.form.get("name") or "Mon Betty Bot"
        metier      = request.form.get("metier") or ""
        avatar_url  = request.form.get("avatar_url") or ""
        color_hex   = request.form.get("color_hex") or "#4F46E5"
        shape       = request.form.get("shape") or "square"
        persona     = request.form.get("persona") or "Assistant"
        welcome_txt = request.form.get("welcome_text") or "Bonjour 👋"
        if bot:
            db_exec(
                """
                UPDATE bots SET name=?, metier=?, avatar_url=?, color_hex=?, shape=?, persona=?, welcome_text=?
                WHERE user_id=?
                """,
                (name, metier, avatar_url, color_hex, shape, persona, welcome_txt, current_user.id),
            )
        else:
            db_exec(
                """
                INSERT INTO bots(user_id,name,metier,avatar_url,color_hex,shape,persona,welcome_text)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (current_user.id, name, metier, avatar_url, color_hex, shape, persona, welcome_txt),
            )
        return redirect(url_for("preview"))
    return render_template("dashboard.html", metiers=metiers, bot=bot)


@app.post("/dashboard/generate")
@login_required
def dashboard_generate():
    return dashboard()


# Page 2 — Preview
@app.get("/preview")
@login_required
def preview():
    """
    Show the preview page with the configured bot and pack. The
    `cfg` object passed to the template merges the user’s bot
    configuration with the YAML pack (colour, avatar, opening, etc.).
    The preview is un‑paid (cfg.paid = False).
    """
    row = get_bot(int(current_user.id))
    if not row:
        flash("Configure d’abord ton bot.", "warning")
        return redirect(url_for("dashboard"))
    bot = dict(row)
    # Determine slug from the bot’s metier
    metier = bot.get("metier") or ""
    slug = METIER_SLUGS.get(metier, "agent_immobilierbilier")
    pack = load_pack(slug) or {}
    # Build cfg: prioritise user settings but fall back to pack defaults
    cfg = {
        "name": bot.get("name") or pack.get("name"),
        "metier": bot.get("metier") or pack.get("metier"),
        "avatar_url": bot.get("avatar_url") or pack.get("avatar_url"),
        "color_hex": bot.get("color_hex") or pack.get("color"),
        "shape": bot.get("shape") or pack.get("avatar_shape") or "rounded",
        "paid": False,
        "slug": slug,
        "opening": pack.get("opening"),
    }
    return render_template("preview.html", bot=bot, cfg=cfg)


# Page 3 — Paiement Stripe (Checkout dynamique)
@app.get("/pay")
@login_required
def pay():
    row = get_bot(int(current_user.id))
    return render_template("pay.html", bot=(dict(row) if row else None))


@app.post("/pay/stripe")
@login_required
def pay_stripe() -> Response:
    if not STRIPE_SECRET_KEY:
        flash("Paiement indisponible pour le moment.", "error")
        return redirect(url_for("pay"))
    row = get_bot(int(current_user.id))
    bot = dict(row) if row else {}
    metier = (bot.get("metier") or "Générique").capitalize()
    avatar = bot.get("avatar_url") or ""
    product_name = f"Abonnement mensuel Betty {metier}"
    amount_cents = STRIPE_PRICE_CENTS
    currency     = STRIPE_CURRENCY
    success_url = f"{PUBLIC_BASE_URL}/confirm?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url  = f"{PUBLIC_BASE_URL}/pay"
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{
            "price_data": {
                "currency": currency,
                "recurring": {"interval": "month"},
                "unit_amount": amount_cents,
                "product_data": {
                    "name": product_name,
                    "images": [avatar] if avatar else []
                }
            },
            "quantity": 1
        }],
        success_url=success_url,
        cancel_url=cancel_url,
        subscription_data={
            "metadata": {
                "user_id": str(current_user.id),
                "bot_id": str(bot.get("id") or ""),
                "metier": metier
            }
        }
    )
    return redirect(session.url, code=303)


# Page 4 — Confirmation
@app.get("/confirm")
@login_required
def confirm():
    return render_template("confirm.html")


# ---------------------------------------------------------------------
# API Endpoints for Preview (Chat + Lead)
# ---------------------------------------------------------------------

@app.post("/api/chat")
@login_required
def api_chat():
    """
    Receive a message from the preview chat. The body should be
    JSON with keys:
      • message: str – the last user message
      • history: list[str] – list of previous user messages
      • pack (optional): str – slug of the pack to use
    If `pack` isn’t provided, we derive it from the current user’s
    configured métier. Returns JSON with {reply, ask_lead}.
    """
    data = request.get_json(force=True) or {}
    message = data.get("message") or ""
    history = data.get("history") or []
    slug = data.get("pack")
    if not slug:
        row = get_bot(int(current_user.id))
        if row:
            metier = row["metier"]
            slug = METIER_SLUGS.get(metier, "agent_immobilierbilier")
        else:
            slug = "agent_immobilierbilier"
    pack = load_pack(slug) or {}
    out = apply_rules(message, pack, history)
    return jsonify(out)


@app.post("/api/lead")
@login_required
def api_lead():
    """
    Capture a lead from the preview page. Expected JSON body:
      • name: str – visitor’s name
      • email: str – visitor’s email
      • phone: str (optional)
      • message: str (optional)
      • user_email: str (optional) – the email of the client (user) to
        whom leads should be sent. When absent, the lead is logged.
    Returns JSON {ok: True, mailed: bool}.
    """
    d = request.get_json(force=True) or {}
    name  = (d.get("name") or "").strip()
    email = (d.get("email") or "").strip()
    phone = (d.get("phone") or "").strip()
    message = (d.get("message") or "").strip()
    user_email = (d.get("user_email") or "").strip()
    if not name or not email:
        return jsonify({"error": "missing name/email"}), 400
    row = get_bot(int(current_user.id))
    slug = None
    if row:
        metier = row["metier"]
        slug = METIER_SLUGS.get(metier, "agent_immobilierbilier")
    lead_data = {
        "bot_id": row["id"] if row else None,
        "user_id": current_user.id,
        "name": name,
        "email": email,
        "phone": phone,
        "message": message,
        "slug": slug,
    }
    app.logger.info(f"[LEAD] {lead_data}")
    mailed = False
    if user_email:
        html = f"""
        <h2>Nouveau lead — {slug or 'bot'}</h2>
        <p><b>Nom:</b> {name}<br>
        <b>Email:</b> {email}<br>
        <b>Téléphone:</b> {phone or '—'}<br>
        <b>Message:</b> {message or '—'}</p>
        """
        subject = f"[Betty] Nouveau lead ({slug or 'bot'})"
        mailed = send_lead_email(user_email, subject, html)
    return jsonify({"ok": True, "mailed": mailed})


# ---------------------------------------------------------------------
# Run local
# ---------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
