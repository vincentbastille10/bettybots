from __future__ import annotations
import os
import sqlite3
import secrets
from pathlib import Path
from typing import Optional
from contextlib import contextmanager
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, Response, send_from_directory, jsonify, g
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    current_user, login_required
)
import stripe
import yaml
from functools import wraps
import logging

# ---------------------------------------------------------------------
# Configuration et Logging
# ---------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

def pick_db_path() -> Path:
    """Sur Vercel (serverless), écrire dans /tmp. Local: fichier dans le projet."""
    if os.getenv("DB_PATH"):
        return Path(os.getenv("DB_PATH"))
    if any(os.getenv(k) for k in ("VERCEL", "VERCEL_URL", "VERCEL_ENV", "AWS_LAMBDA_FUNCTION_NAME")):
        logger.warning("⚠️ Serverless détecté : DB éphémère dans /tmp")
        return Path("/tmp/payments.db")
    return BASE_DIR / "payments.db"

DB_PATH = pick_db_path()

# Configuration Flask
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))

# Stripe - Validation robuste
def get_env_int(key: str, default: int) -> int:
    """Récupère une variable d'environnement entière avec fallback."""
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        logger.warning(f"Variable {key} invalide, utilisation de {default}")
        return default

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_CENTS = get_env_int("STRIPE_PRICE_CENTS", 999)  # 9,99 €
STRIPE_CURRENCY = os.getenv("STRIPE_CURRENCY", "eur")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or os.getenv("BASE_URL") or ""

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    logger.warning("⚠️ STRIPE_SECRET_KEY non configurée - paiements désactivés")

# Login Manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "root"

# ---------------------------------------------------------------------
# Gestion DB thread-safe avec context manager
# ---------------------------------------------------------------------

@contextmanager
def get_db():
    """Context manager pour connexions DB thread-safe."""
    if 'db' not in g:
        try:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            g.db = sqlite3.connect(str(DB_PATH), timeout=10)
            g.db.row_factory = sqlite3.Row
        except (sqlite3.OperationalError, PermissionError) as e:
            logger.error(f"Erreur connexion DB principale : {e}")
            tmp = Path("/tmp/payments.db")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            g.db = sqlite3.connect(str(tmp), timeout=10)
            g.db.row_factory = sqlite3.Row
    
    try:
        yield g.db
    finally:
        pass  # Connexion fermée dans teardown_appcontext

@app.teardown_appcontext
def close_db(error):
    """Ferme la connexion DB à la fin de chaque requête."""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db() -> None:
    """Initialise les tables de la base de données."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                metier TEXT,
                avatar_url TEXT,
                color_hex TEXT DEFAULT '#4F46E5',
                shape TEXT DEFAULT 'square',
                persona TEXT DEFAULT 'Assistant',
                welcome_text TEXT DEFAULT 'Bonjour 👋',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_bots_user_id ON bots(user_id)
        """)
        conn.commit()
        logger.info("✅ Base de données initialisée")

def db_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    """Exécute une requête et retourne une seule ligne."""
    try:
        with get_db() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchone()
    except sqlite3.Error as e:
        logger.error(f"Erreur DB (db_one): {e}")
        return None

def db_exec(sql: str, params: tuple = ()) -> bool:
    """Exécute une requête avec commit."""
    try:
        with get_db() as conn:
            conn.execute(sql, params)
            conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"Erreur DB (db_exec): {e}")
        return False

def get_bot(user_id: int) -> Optional[sqlite3.Row]:
    """Récupère le bot associé à un utilisateur."""
    return db_one("SELECT * FROM bots WHERE user_id=? LIMIT 1", (user_id,))

# Initialisation DB au démarrage
with app.app_context():
    init_db()

# ---------------------------------------------------------------------
# Modèle utilisateur
# ---------------------------------------------------------------------

class User(UserMixin):
    def __init__(self, id_: int, email: str):
        self.id = id_
        self.email = email
    
    def __repr__(self):
        return f"<User {self.id}: {self.email}>"

@login_manager.user_loader
def load_user(user_id: str):
    """Charge un utilisateur depuis son ID."""
    try:
        row = db_one("SELECT * FROM users WHERE id=?", (int(user_id),))
        if row:
            return User(row["id"], row["email"])
    except (ValueError, TypeError) as e:
        logger.error(f"Erreur load_user: {e}")
    return None

# ---------------------------------------------------------------------
# Utilitaires et validation
# ---------------------------------------------------------------------

def sanitize_color(color: str) -> str:
    """Valide et nettoie un code couleur hexadécimal."""
    color = color.strip()
    if not color.startswith('#'):
        color = '#' + color
    # Validation simple : #XXX ou #XXXXXX
    if len(color) in (4, 7) and all(c in '0123456789ABCDEFabcdef#' for c in color):
        return color
    return '#4F46E5'  # Couleur par défaut

def sanitize_url(url: str) -> str:
    """Valide une URL (basique)."""
    url = url.strip()
    if url and (url.startswith('http://') or url.startswith('https://')):
        return url[:500]  # Limite de longueur
    return ""

def rate_limit(max_requests: int = 100):
    """Décorateur de rate limiting simple (à améliorer en production)."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # TODO: Implémenter avec Redis ou Flask-Limiter
            return f(*args, **kwargs)
        return wrapped
    return decorator

# ---------------------------------------------------------------------
# Routes - Favicon
# ---------------------------------------------------------------------

@app.get("/favicon.ico")
def favicon() -> Response:
    """Sert le favicon ou retourne 204."""
    fav_dir = BASE_DIR / "static"
    if (fav_dir / "favicon.ico").exists():
        return send_from_directory(fav_dir, "favicon.ico")
    return Response(status=204)

# ---------------------------------------------------------------------
# Routes - Accueil
# ---------------------------------------------------------------------

@app.get("/")
def root():
    """Page d'accueil - crée un utilisateur invité si nécessaire."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    # Crée un utilisateur invité unique
    email = f"guest-{secrets.token_urlsafe(8)}@guest.local"
    
    if db_exec("INSERT OR IGNORE INTO users(email) VALUES(?)", (email,)):
        row = db_one("SELECT * FROM users WHERE email=?", (email,))
        if row:
            login_user(User(row["id"], row["email"]))
            logger.info(f"✅ Utilisateur invité créé : {email}")
        else:
            logger.error("❌ Échec création utilisateur invité")
            flash("Erreur lors de la création du compte invité", "error")
    
    return redirect(url_for("dashboard"))

# ---------------------------------------------------------------------
# Routes - Dashboard (Page 1)
# ---------------------------------------------------------------------

@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    """Configuration du bot - Page 1."""
    metiers = ["Avocate", "Agent Immo", "Médecine", "Comptable", "Psychologue"]
    
    try:
        row = get_bot(int(current_user.id))
        bot = dict(row) if row else None
    except (ValueError, TypeError):
        bot = None

    if request.method == "POST":
        # Récupération et validation des données
        name = (request.form.get("name") or "Mon Betty Bot").strip()[:100]
        metier = (request.form.get("metier") or "").strip()
        avatar_url = sanitize_url(request.form.get("avatar_url") or "")
        color_hex = sanitize_color(request.form.get("color_hex") or "#4F46E5")
        shape = (request.form.get("shape") or "square").strip()
        persona = (request.form.get("persona") or "Assistant").strip()[:500]
        welcome_txt = (request.form.get("welcome_text") or "Bonjour 👋").strip()[:500]

        # Validation du métier
        if metier not in metiers:
            metier = ""

        # Validation de la forme
        if shape not in ("square", "circle", "rounded"):
            shape = "square"

        try:
            if bot:
                success = db_exec("""
                    UPDATE bots 
                    SET name=?, metier=?, avatar_url=?, color_hex=?, shape=?, persona=?, welcome_text=?
                    WHERE user_id=?
                """, (name, metier, avatar_url, color_hex, shape, persona, welcome_txt, current_user.id))
            else:
                success = db_exec("""
                    INSERT INTO bots(user_id,name,metier,avatar_url,color_hex,shape,persona,welcome_text)
                    VALUES(?,?,?,?,?,?,?,?)
                """, (current_user.id, name, metier, avatar_url, color_hex, shape, persona, welcome_txt))
            
            if success:
                flash("✅ Configuration sauvegardée !", "success")
                return redirect(url_for("preview"))
            else:
                flash("❌ Erreur lors de la sauvegarde", "error")
        
        except Exception as e:
            logger.error(f"Erreur sauvegarde bot : {e}")
            flash("❌ Erreur inattendue lors de la sauvegarde", "error")

    return render_template("dashboard.html", metiers=metiers, bot=bot)

# ---------------------------------------------------------------------
# Routes - Preview (Page 2)
# ---------------------------------------------------------------------

@app.get("/preview")
@login_required
def preview():
    """Prévisualisation du bot - Page 2."""
    try:
        row = get_bot(int(current_user.id))
        if not row:
            flash("⚠️ Configure d'abord ton bot", "warning")
            return redirect(url_for("dashboard"))
        
        bot_data = dict(row)
        return render_template("preview.html", bot=bot_data, cfg=bot_data)
    
    except Exception as e:
        logger.error(f"Erreur preview : {e}")
        flash("❌ Erreur lors du chargement de la prévisualisation", "error")
        return redirect(url_for("dashboard"))

# ---------------------------------------------------------------------
# Routes - Paiement (Page 3)
# ---------------------------------------------------------------------

@app.get("/pay")
@login_required
def pay():
    """Page de paiement - Page 3."""
    try:
        row = get_bot(int(current_user.id))
        bot = dict(row) if row else None
    except Exception:
        bot = None
    
    return render_template("pay.html", bot=bot, stripe_enabled=bool(STRIPE_SECRET_KEY))

@app.post("/pay/stripe")
@login_required
def pay_stripe() -> Response:
    """Crée une session Stripe Checkout."""
    if not STRIPE_SECRET_KEY:
        flash("❌ Paiement indisponible pour le moment", "error")
        return redirect(url_for("pay"))
    
    if not PUBLIC_BASE_URL:
        flash("❌ Configuration serveur incomplète", "error")
        logger.error("PUBLIC_BASE_URL non configurée")
        return redirect(url_for("pay"))

    try:
        row = get_bot(int(current_user.id))
        bot = dict(row) if row else {}
        
        metier = (bot.get("metier") or "Générique").capitalize()
        avatar = sanitize_url(bot.get("avatar_url") or "")
        product_name = f"Abonnement mensuel Betty {metier}"

        success_url = f"{PUBLIC_BASE_URL.rstrip('/')}/confirm?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = f"{PUBLIC_BASE_URL.rstrip('/')}/pay"

        # Création session Stripe
        session_params = {
            "mode": "subscription",
            "line_items": [{
                "price_data": {
                    "currency": STRIPE_CURRENCY,
                    "recurring": {"interval": "month"},
                    "unit_amount": STRIPE_PRICE_CENTS,
                    "product_data": {
                        "name": product_name,
                    }
                },
                "quantity": 1
            }],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "subscription_data": {
                "metadata": {
                    "user_id": str(current_user.id),
                    "bot_id": str(bot.get("id") or ""),
                    "metier": metier
                }
            }
        }

        # Ajout de l'image si disponible
        if avatar:
            session_params["line_items"][0]["price_data"]["product_data"]["images"] = [avatar]

        session = stripe.checkout.Session.create(**session_params)
        logger.info(f"✅ Session Stripe créée pour user {current_user.id}")
        return redirect(session.url, code=303)
    
    except stripe.error.StripeError as e:
        logger.error(f"Erreur Stripe : {e}")
        flash(f"❌ Erreur de paiement : {str(e)}", "error")
        return redirect(url_for("pay"))
    
    except Exception as e:
        logger.error(f"Erreur inattendue paiement : {e}")
        flash("❌ Erreur inattendue lors du paiement", "error")
        return redirect(url_for("pay"))

# ---------------------------------------------------------------------
# Routes - Confirmation (Page 4)
# ---------------------------------------------------------------------

@app.get("/confirm")
@login_required
def confirm():
    """Page de confirmation après paiement - Page 4."""
    session_id = request.args.get("session_id")
    
    # TODO: Vérifier le paiement avec Stripe
    if session_id:
        logger.info(f"✅ Confirmation paiement - Session : {session_id}")
    
    return render_template("confirm.html", session_id=session_id)

# ---------------------------------------------------------------------
# API - Chat et Health
# ---------------------------------------------------------------------

METIER_SLUGS = {
    "Avocate": "avocat_pack",
    "Agent Immo": "agent_immobilier_pack",  # ✅ Corrigé
    "Médecine": "medecine_pack",
    "Comptable": "comptable_pack",
    "Psychologue": "psychologue_pack",
}

def load_pack(slug: str) -> dict:
    """Charge un pack YAML de règles conversationnelles."""
    path = BASE_DIR / "templates" / "packs" / f"{slug}.yaml"
    
    if not path.exists():
        logger.warning(f"⚠️ Pack inexistant : {slug}")
        return {}
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except (yaml.YAMLError, IOError) as e:
        logger.error(f"Erreur chargement pack {slug} : {e}")
        return {}

def apply_rules(message: str, pack: dict, history: list) -> dict:
    """Applique les règles conversationnelles du pack."""
    message_lower = message.lower().strip()
    
    for rule in pack.get("rules", []):
        trigger = rule.get("if", "").lower()
        if trigger and trigger in message_lower:
            return {
                "reply": rule.get("then", "Je vous écoute 👂"),
                "ask_lead": rule.get("ask_lead", False)
            }
    
    return {
        "reply": pack.get("fallback", "Je n'ai pas bien compris 🤔"),
        "ask_lead": False
    }

@app.post("/api/health")
def api_health():
    """Endpoint de santé de l'API."""
    return jsonify({
        "ok": True,
        "timestamp": None,  # Ajoutez datetime si nécessaire
        "database": "connected" if g.get('db') else "disconnected"
    })

@app.post("/api/chat")
@rate_limit(max_requests=100)
def api_chat():
    """Endpoint de chat conversationnel."""
    try:
        data = request.get_json(force=True) or {}
        message = (data.get("message") or "").strip()
        history = data.get("history") or []
        slug = (data.get("pack") or "").strip().lower()

        # Détermination du pack à utiliser
        if not slug:
            if current_user.is_authenticated:
                try:
                    row = get_bot(int(current_user.id))
                    if row:
                        metier = (dict(row).get("metier") or "").strip()
                        slug = METIER_SLUGS.get(metier, "agent_immobilier_pack")
                    else:
                        slug = "agent_immobilier_pack"
                except Exception:
                    slug = "agent_immobilier_pack"
            else:
                slug = "agent_immobilier_pack"

        pack = load_pack(slug) or {}

        # Message d'ouverture
        if not history or not message:
            return jsonify({
                "reply": pack.get("opening", "Bonjour 👋"),
                "ask_lead": False
            })

        # Traitement du message
        response = apply_rules(message, pack, history)
        return jsonify(response)
    
    except Exception as e:
        logger.error(f"Erreur API chat : {e}")
        return jsonify({
            "reply": "Désolé, une erreur est survenue 😔",
            "ask_lead": False,
            "error": True
        }), 500

# ---------------------------------------------------------------------
# Gestion des erreurs
# ---------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    """Page 404."""
    return render_template("404.html"), 404 if Path(app.template_folder, "404.html").exists() else ("Page non trouvée", 404)

@app.errorhandler(500)
def server_error(e):
    """Page 500."""
    logger.error(f"Erreur 500 : {e}")
    return render_template("500.html"), 500 if Path(app.template_folder, "500.html").exists() else ("Erreur serveur", 500)

# ---------------------------------------------------------------------
# Run local
# ---------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
