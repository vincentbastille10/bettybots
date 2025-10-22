from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, Response, send_from_directory
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    current_user, login_required
)
import stripe

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

def pick_db_path() -> Path:
    """Sur Vercel (serverless), on doit écrire dans /tmp. Local: fichier dans le projet."""
    # Si l’utilisateur a forcé une variable
    if os.getenv("DB_PATH"):
        return Path(os.getenv("DB_PATH"))
    # Détection Vercel/AWS Lambda -> /tmp
    if any(os.getenv(k) for k in ("VERCEL", "VERCEL_URL", "VERCEL_ENV", "AWS_LAMBDA_FUNCTION_NAME")):
        return Path("/tmp/payments.db")
    # Local
    return BASE_DIR / "payments.db"

DB_PATH = pick_db_path()

def connect_db() -> sqlite3.Connection:
    # Assure que le dossier existe (utile si on a défini DB_PATH ailleurs)
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        # Fallback ultime -> /tmp
        tmp = Path("/tmp/payments.db")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(tmp, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET", "dev_key")

# Stripe (prix dynamique)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_CENTS = int(os.getenv("STRIPE_PRICE_CENTS", "999"))  # 9,99 €
STRIPE_CURRENCY = os.getenv("STRIPE_CURRENCY", "eur")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or os.getenv("BASE_URL") or ""

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "index"

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

def db_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    c = connect_db()
    cur = c.execute(sql, params)
    rows = cur.fetchall()
    c.close()
    return rows

def db_exec(sql: str, params: tuple = ()) -> None:
    c = connect_db()
    c.execute(sql, params)
    c.commit()
    c.close()

def get_bot(user_id: int) -> sqlite3.Row | None:
    return db_one("SELECT * FROM bots WHERE user_id=?", (user_id,))

init_db()

# ---------------------------------------------------------------------
# Auth simplifiée (email-only)
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
# Favicon pour éviter du bruit d’erreurs
# ---------------------------------------------------------------------
@app.get("/favicon.ico")
def favicon() -> Response:
    # Sert un favicon s'il existe; sinon renvoie 204 pour ne pas planter
    fav_dir = BASE_DIR / "static"
    if (fav_dir / "favicon.ico").exists():
        return send_from_directory(fav_dir, "favicon.ico")
    return Response(status=204)

# ---------------------------------------------------------------------
# Page d’accueil → formulaire d’email simple
# ---------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if not email:
            flash("Entrez un email.", "error")
            return redirect(url_for("index"))
        row = db_one("SELECT * FROM users WHERE email=?", (email,))
        if not row:
            db_exec("INSERT INTO users(email) VALUES(?)", (email,))
            row = db_one("SELECT * FROM users WHERE email=?", (email,))
        login_user(User(row["id"], row["email"]))
        return redirect(url_for("dashboard"))
    return render_template("index.html")

# ---------------------------------------------------------------------
# Page 1 — Dashboard (config du bot)
# ---------------------------------------------------------------------
@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    metiers = ["Avocate", "Agent Immo", "Médecine"]
    row = get_bot(int(current_user.id))
    bot = dict(row) if row else None

    if request.method == "POST":
        name = request.form.get("name") or "Mon Betty Bot"
        metier = request.form.get("metier") or ""
        avatar_url = request.form.get("avatar_url") or ""
        color_hex = request.form.get("color_hex") or "#4F46E5"
        shape = request.form.get("shape") or "square"
        persona = request.form.get("persona") or "Assistant"
        welcome_text = request.form.get("welcome_text") or "Bonjour 👋"

        if bot:
            db_exec("""
                UPDATE bots SET name=?, metier=?, avatar_url=?, color_hex=?, shape=?, persona=?, welcome_text=?
                WHERE user_id=?
            """, (name, metier, avatar_url, color_hex, shape, persona, welcome_text, current_user.id))
        else:
            db_exec("""
                INSERT INTO bots(user_id,name,metier,avatar_url,color_hex,shape,persona,welcome_text)
                VALUES(?,?,?,?,?,?,?,?)
            """, (current_user.id, name, metier, avatar_url, color_hex, shape, persona, welcome_text))

        return redirect(url_for("preview"))

    return render_template("dashboard.html", metiers=metiers, bot=bot)

# alias pour ton bouton "Enregistrer / Générer"
@app.post("/dashboard/generate")
@login_required
def dashboard_generate():
    return dashboard()

# ---------------------------------------------------------------------
# Page 2 — Preview
# ---------------------------------------------------------------------
@app.get("/preview")
@login_required
def preview():
    row = get_bot(int(current_user.id))
    if not row:
        flash("Configure d’abord ton bot.", "warning")
        return redirect(url_for("dashboard"))
    return render_template("preview.html", bot=dict(row))

# ---------------------------------------------------------------------
# Page 3 — Paiement Stripe (Checkout dynamique)
# ---------------------------------------------------------------------
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
    currency = STRIPE_CURRENCY

    success_url = f"{PUBLIC_BASE_URL}/confirm?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{PUBLIC_BASE_URL}/pay"

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

# ---------------------------------------------------------------------
# Page 4 — Confirmation
# ---------------------------------------------------------------------
@app.get("/confirm")
@login_required
def confirm():
    return render_template("confirm.html")

# ---------------------------------------------------------------------
# Run local
# ---------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
