from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session as flask_session, Response
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    current_user, login_required
)
import stripe

# ---------------------------------------------------------------------
# Configuration générale
# ---------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "payments.db"

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev_key")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_CENTS = int(os.getenv("STRIPE_PRICE_CENTS", "999"))
STRIPE_CURRENCY = os.getenv("STRIPE_CURRENCY", "eur")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://bettybots.vercel.app")
BASE_URL = "http://127.0.0.1:5000"

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "index"

# ---------------------------------------------------------------------
# Base de données SQLite
# ---------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_bot(user_id: int):
    conn = get_db()
    cur = conn.execute("SELECT * FROM bots WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def init_db():
    conn = get_db()
    conn.execute("""
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------------------
# Authentification simplifiée
# ---------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, id_: int, email: str):
        self.id = id_
        self.email = email

@login_manager.user_loader
def load_user(user_id: str):
    conn = get_db()
    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return User(row["id"], row["email"])
    return None

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        email = request.form.get("email")
        conn = get_db()
        cur = conn.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cur.fetchone()
        if not user:
            conn.execute("INSERT INTO users (email) VALUES (?)", (email,))
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()
        login_user(User(user["id"], user["email"]))
        return redirect(url_for("dashboard"))
    return render_template("index.html")

# ---------------------------------------------------------------------
# Tableau de bord principal (page 1)
# ---------------------------------------------------------------------
@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    conn = get_db()
    bot = get_bot(current_user.id)
    metiers = ["Avocate", "Agent Immo", "Médecine"]

    if request.method == "POST":
        data = {
            "name": request.form.get("name"),
            "metier": request.form.get("metier"),
            "avatar_url": request.form.get("avatar_url"),
            "color_hex": request.form.get("color_hex"),
            "shape": request.form.get("shape"),
            "persona": request.form.get("persona"),
            "welcome_text": request.form.get("welcome_text"),
        }

        if bot:
            conn.execute("""
                UPDATE bots SET name=?, metier=?, avatar_url=?, color_hex=?, shape=?, persona=?, welcome_text=?
                WHERE user_id=?
            """, (
                data["name"], data["metier"], data["avatar_url"], data["color_hex"],
                data["shape"], data["persona"], data["welcome_text"], current_user.id
            ))
        else:
            conn.execute("""
                INSERT INTO bots (user_id, name, metier, avatar_url, color_hex, shape, persona, welcome_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                current_user.id, data["name"], data["metier"], data["avatar_url"],
                data["color_hex"], data["shape"], data["persona"], data["welcome_text"]
            ))
        conn.commit()
        conn.close()
        return redirect(url_for("preview"))

    conn.close()
    return render_template("dashboard.html", metiers=metiers, bot=bot)

# ---------------------------------------------------------------------
# Page 2 – Preview du bot
# ---------------------------------------------------------------------
@app.route("/preview")
@login_required
def preview():
    bot = get_bot(current_user.id)
    if not bot:
        flash("Configure ton bot avant de continuer.")
        return redirect(url_for("dashboard"))
    return render_template("preview.html", bot=bot)

# ---------------------------------------------------------------------
# Page 3 – Paiement Stripe
# ---------------------------------------------------------------------
@app.route("/pay")
@login_required
def pay():
    bot = get_bot(current_user.id)
    return render_template("pay.html", bot=bot)

@app.post("/pay/stripe")
@login_required
def pay_stripe() -> Response:
    if not STRIPE_SECRET_KEY:
        flash("Paiement indisponible pour le moment.", "error")
        return redirect(url_for("pay"))

    # Récupération du bot configuré
    row = get_bot(current_user.id)
    bot = dict(row) if row else {}
    metier = (bot.get("metier") or "Générique").capitalize()
    avatar = bot.get("avatar_url") or ""
    product_name = f"Abonnement mensuel Betty {metier}"
    amount_cents = STRIPE_PRICE_CENTS
    currency = STRIPE_CURRENCY

    success_url = f"{PUBLIC_BASE_URL}/confirm?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{PUBLIC_BASE_URL}/pay"

    # ✅ Création de session Stripe dynamique (le nom & avatar changent selon le bot)
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
# Page 4 – Confirmation après paiement
# ---------------------------------------------------------------------
@app.route("/confirm")
@login_required
def confirm():
    return render_template("confirm.html")

# ---------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
