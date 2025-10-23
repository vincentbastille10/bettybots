from __future__ import annotations
import os
import io
import sqlite3
import secrets
from pathlib import Path
from typing import Optional
from contextlib import contextmanager
from functools import wraps
import logging

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, Response, send_from_directory, jsonify, g, make_response, abort
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    current_user, login_required
)

# --- réseau pour proxy images
try:
    import requests  # Render l’a en général
except Exception:  # fallback sans requests
    requests = None
import urllib.request

import stripe
import yaml

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
        pass

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db() -> None:
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
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_bots_user_id ON bots(user_id)""")
        conn.commit()
        logger.info("✅ Base de données initialisée")

def db_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    try:
        with get_db() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchone()
    except sqlite3.Error as e:
        logger.error(f"Erreur DB (db_one): {e}")
        return None

def db_exec(sql: str, params: tuple = ()) -> bool:
    try:
        with get_db() as conn:
            conn.execute(sql, params)
            conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"Erreur DB (db_exec): {e}")
        return False

def get_bot(user_id: int) -> Optional[sqlite3.Row]:
    return db_one("SELECT * FROM bots WHERE user_id=? LIMIT 1", (user_id,))

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
    try:
        row = db_one("SELECT * FROM users WHERE id=?", (int(user_id),))
        if row:
            return User(row["id"], row["email"])
    except (ValueError, TypeError) as e:
        logger.error(f"Erreur load_user: {e}")
    return None

# ---------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------

def sanitize_color(color: str) -> str:
    color = (color or "").strip()
    if not color.startswith('#'):
        color = '#' + color
    if len(color) in (4,7) and all(c in '0123456789ABCDEFabcdef#' for c in color):
        return color
    return '#4F46E5'

def sanitize_url(url: str) -> str:
    url = (url or "").strip()
    if url and (url.startswith('http://') or url.startswith('https://')):
        return url[:500]
    return ""

def rate_limit(max_requests: int = 100):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            return f(*args, **kwargs)
        return wrapped
    return decorator

# ---------------------------------------------------------------------
# Avatars fournis (liens originaux Postimg) + mapping slug
# ---------------------------------------------------------------------

EXTERNAL_AVATARS = {
    # ⬇️ Liens EXACTS fournis par toi (hébergeur Postimg)
    "agent_immo": "https://i.postimg.cc/zBWtZ8MH/Betty-Agent-immo-copie.jpg",
    "avocat":     "https://i.postimg.cc/bv4CBs6h/Betty-Avocate-copie.jpg",
    "medecin":    "https://i.postimg.cc/PxZ3sTcL/Betty-Medecine-copie.jpg",
}

METIER_TO_SLUG = {
    "Agent Immo": "agent_immo",
    "Avocate": "avocat",
    "Médecine": "medecin",
    "Comptable": "agent_immo",   # fallback
    "Psychologue": "agent_immo", # fallback
    "Coiffeur": "agent_immo",    # fallback
    "Coach sportif": "agent_immo" # fallback
}

SLUG_TO_METIER = {v:k for k,v in METIER_TO_SLUG.items()}
DEFAULT_SLUG = "agent_immo"

# ---------------------------------------------------------------------
# Proxy d'avatars (same-origin) avec User-Agent + fallback SVG
# ---------------------------------------------------------------------

# petit SVG de secours (2,2 KB)
_PLACEHOLDER_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg width="512" height="512" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
 <defs><linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="#ddd"/><stop offset="1" stop-color="#bbb"/></linearGradient></defs>
 <rect width="512" height="512" fill="url(#g)"/>
 <circle cx="256" cy="196" r="96" fill="#888"/>
 <rect x="96" y="316" width="320" height="140" rx="28" fill="#888"/>
</svg>
"""

# user-agent commun pour éviter les blocages côté hébergeur
_UA = "Mozilla/5.0 (compatible; BettyBotImageProxy/1.0; +https://bettybots.example)"

@app.get("/avatar/<slug>")
def avatar_proxy(slug: str):
    slug = (slug or "").lower().strip()
    url = EXTERNAL_AVATARS.get(slug, EXTERNAL_AVATARS[DEFAULT_SLUG])

    try:
        if requests:
            s = requests.Session()
            headers = {"User-Agent": _UA, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}
            r = s.get(url, headers=headers, timeout=10, allow_redirects=True)
            if r.status_code != 200 or not r.content:
                logger.warning(f"Avatar fetch non-200 ({r.status_code}) pour {slug} → placeholder")
                return _avatar_response(_PLACEHOLDER_SVG, "image/svg+xml")
            data = r.content
            ctype = r.headers.get("Content-Type", "image/jpeg") or "image/jpeg"
            return _avatar_response(data, ctype)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                ctype = getattr(getattr(resp, "headers", None), "get_content_type", lambda: "image/jpeg")()
                return _avatar_response(data, ctype or "image/jpeg")
    except Exception as e:
        logger.error(f"Erreur proxy avatar {slug}: {e}")
        return _avatar_response(_PLACEHOLDER_SVG, "image/svg+xml")

def _avatar_response(data: bytes, ctype: str) -> Response:
    resp = Response(data, mimetype=ctype)
    # Cache 24h côté navigateur
    resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
    return resp

# ---------------------------------------------------------------------
# Favicon
# ---------------------------------------------------------------------

@app.get("/favicon.ico")
def favicon() -> Response:
    fav_dir = BASE_DIR / "static"
    if (fav_dir / "favicon.ico").exists():
        return send_from_directory(fav_dir, "favicon.ico")
    return Response(status=204)

# ---------------------------------------------------------------------
# Accueil
# ---------------------------------------------------------------------

@app.get("/")
def root():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
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
# Dashboard (Page 1)
# ---------------------------------------------------------------------

@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    metiers = ["Avocate", "Agent Immo", "Médecine", "Comptable", "Psychologue", "Coiffeur", "Coach sportif"]
    try:
        row = get_bot(int(current_user.id))
        bot = dict(row) if row else None

        cfg = None
        if bot:
            shape_to_size = {"circle": "s", "square": "m", "rounded": "l"}
            # déduire slug depuis metier
            slug = METIER_TO_SLUG.get(bot.get("metier") or "", DEFAULT_SLUG)
            # avatar_url interne same-origin
            avatar_url = f"/avatar/{slug}"
            # avatar_key (si le HTML l'utilise)
            avatar_key = 0
            if slug == "avocat":
                avatar_key = 1
            elif slug == "medecin":
                avatar_key = 2

            cfg = {
                "name": bot.get("name", "Mon Betty Bot"),
                "slug": {"agent_immo":"agent_immobilier","avocat":"avocat","medecin":"medecin"}.get(slug,"agent_immobilier"),
                "avatar_key": avatar_key,
                "color_hex": bot.get("color_hex", "#4F46E5"),
                "persona": bot.get("persona", "neutre"),
                "widget_size": shape_to_size.get(bot.get("shape", "square"), "m"),
                "greeting": bot.get("welcome_text", "Bonjour 👋"),
                "avatar_url": avatar_url
            }
    except Exception:
        bot, cfg = None, None

    if request.method == "GET":
        return render_template("dashboard.html", metiers=metiers, bot=bot)

    # --- POST : sauvegarde + redirection robuste /preview
    logger.info(f"📝 POST /dashboard par user {current_user.id} — form={dict(request.form)}")

    name = (request.form.get("bot_name") or "Mon Betty Bot").strip()[:100]
    pack_slug = (request.form.get("pack_slug") or "agent_immobilier").strip()
    # normalisation vers nos slugs internes
    pack_to_internal = {"agent_immobilier":"agent_immo","avocat":"avocat","medecin":"medecin",
                        "coiffeur":"agent_immo","coach_sportif":"agent_immo"}
    internal_slug = pack_to_internal.get(pack_slug, DEFAULT_SLUG)
    metier = SLUG_TO_METIER.get(internal_slug, "Agent Immo")

    # avatar same-origin stocké
    avatar_url = f"/avatar/{internal_slug}"

    color_hex = sanitize_color(request.form.get("color_hex") or "#4F46E5")
    persona = (request.form.get("persona") or "neutre").strip()[:500]
    widget_size = (request.form.get("widget_size") or "m").strip()
    shape_map = {"s": "circle", "m": "square", "l": "rounded"}
    shape = shape_map.get(widget_size, "square")
    welcome_txt = (request.form.get("greeting") or "Bonjour 👋").strip()[:500]

    try:
        existing = (bot is not None)
        if existing:
            ok = db_exec("""
                UPDATE bots 
                SET name=?, metier=?, avatar_url=?, color_hex=?, shape=?, persona=?, welcome_text=?
                WHERE user_id=?
            """, (name, metier, avatar_url, color_hex, shape, persona, welcome_txt, current_user.id))
        else:
            ok = db_exec("""
                INSERT INTO bots(user_id,name,metier,avatar_url,color_hex,shape,persona,welcome_text)
                VALUES(?,?,?,?,?,?,?,?)
            """, (current_user.id, name, metier, avatar_url, color_hex, shape, persona, welcome_txt))

        if not ok:
            flash("❌ Erreur lors de la sauvegarde", "error")
            return render_template("dashboard.html", metiers=metiers, bot=bot), 400

        flash("✅ Configuration sauvegardée !", "success")

        target = url_for("preview")
        if request.headers.get("HX-Request") == "true":
            resp = make_response("", 204)
            resp.headers["HX-Redirect"] = target
            return resp

        resp = redirect(target, code=303)
        resp.headers["Refresh"] = f'0; url={target}'
        resp.set_data(
            f'<!doctype html><meta http-equiv="refresh" content="0;url={target}">'
            f'<script>try{{window.top.location.href="{target}";}}catch(e){{location.href="{target}";}}</script>'
        )
        return resp

    except Exception as e:
        logger.error(f"❌ Exception sauvegarde bot : {e}", exc_info=True)
        flash("❌ Erreur inattendue lors de la sauvegarde", "error")
        return render_template("dashboard.html", metiers=metiers, bot=bot), 500

# ---------------------------------------------------------------------
# Preview (Page 2)
# ---------------------------------------------------------------------

@app.get("/preview")
@login_required
def preview():
    try:
        row = get_bot(int(current_user.id))
        if not row:
            flash("⚠️ Configure d'abord ton bot", "warning")
            return redirect(url_for("dashboard"))
        bot = dict(row)

        shape_to_size = {"circle": "s", "square": "m", "rounded": "l"}
        internal_slug = METIER_TO_SLUG.get(bot.get("metier") or "", DEFAULT_SLUG)

        # avatar same-origin assuré
        avatar_url = f"/avatar/{internal_slug}"

        # avatar_key si ton HTML s’en sert encore
        avatar_key = 0
        if internal_slug == "avocat":
            avatar_key = 1
        elif internal_slug == "medecin":
            avatar_key = 2

        cfg = {
            "name": bot.get("name", "Mon Betty Bot"),
            "slug": {"agent_immo":"agent_immobilier","avocat":"avocat","medecin":"medecin"}.get(internal_slug,"agent_immobilier"),
            "avatar_key": avatar_key,
            "color_hex": bot.get("color_hex", "#4F46E5"),
            "persona": bot.get("persona", "neutre"),
            "widget_size": shape_to_size.get(bot.get("shape", "square"), "m"),
            "greeting": bot.get("welcome_text", "Bonjour 👋"),
            "avatar_url": avatar_url
        }

        # on fournit à la fois bot (brut) et cfg (clé/valeurs utilisées par les templates)
        bot["avatar_url"] = avatar_url
        return render_template("preview.html", bot=bot, cfg=cfg)

    except Exception as e:
        logger.error(f"Erreur preview : {e}", exc_info=True)
        flash("❌ Erreur lors du chargement de la prévisualisation", "error")
        return redirect(url_for("dashboard"))

# ---------------------------------------------------------------------
# Paiement (Page 3)
# ---------------------------------------------------------------------

@app.get("/pay")
@login_required
def pay():
    try:
        row = get_bot(int(current_user.id))
        bot = dict(row) if row else None
    except Exception:
        bot = None
    return render_template("pay.html", bot=bot, stripe_enabled=bool(STRIPE_SECRET_KEY))

@app.post("/pay/stripe")
@login_required
def pay_stripe() -> Response:
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

        session_params = {
            "mode": "subscription",
            "line_items": [{
                "price_data": {
                    "currency": STRIPE_CURRENCY,
                    "recurring": {"interval": "month"},
                    "unit_amount": STRIPE_PRICE_CENTS,
                    "product_data": {"name": product_name}
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
        if avatar:
            # Stripe accepte les URLs absolues; si besoin, convertir en absolu
            if avatar.startswith("/"):
                base = (PUBLIC_BASE_URL or "").rstrip("/")
                if base:
                    session_params["line_items"][0]["price_data"]["product_data"]["images"] = [f"{base}{avatar}"]
            else:
                session_params["line_items"][0]["price_data"]["product_data"]["images"] = [avatar]

        session = stripe.checkout.Session.create(**session_params)
        logger.info(f"✅ Session Stripe créée pour user {current_user.id}")
        return redirect(session.url, code=303)

    except stripe.error.StripeError as e:
        logger.error(f"Erreur Stripe : {e}")
        flash(f"❌ Erreur de paiement : {str(e)}", "error")
        return redirect(url_for("pay"))
    except Exception as e:
        logger.error(f"Erreur inattendue paiement : {e}", exc_info=True)
        flash("❌ Erreur inattendue lors du paiement", "error")
        return redirect(url_for("pay"))

# ---------------------------------------------------------------------
# Confirmation (Page 4)
# ---------------------------------------------------------------------

@app.get("/confirm")
@login_required
def confirm():
    session_id = request.args.get("session_id")
    if session_id:
        logger.info(f"✅ Confirmation paiement - Session : {session_id}")
    return render_template("confirm.html", session_id=session_id)

# ---------------------------------------------------------------------
# API - Chat et Health
# ---------------------------------------------------------------------

METIER_SLUGS = {
    "Avocate": "avocat_pack",
    "Agent Immo": "agent_immobilier_pack",
    "Médecine": "medecine_pack",
    "Comptable": "comptable_pack",
    "Psychologue": "psychologue_pack",
}

def load_pack(slug: str) -> dict:
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
    message_lower = (message or "").lower().strip()
    for rule in pack.get("rules", []):
        trigger = (rule.get("if") or "").lower()
        if trigger and trigger in message_lower:
            return {"reply": rule.get("then", "Je vous écoute 👂"),
                    "ask_lead": rule.get("ask_lead", False)}
    return {"reply": pack.get("fallback", "Je n'ai pas bien compris 🤔"),
            "ask_lead": False}

@app.post("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "timestamp": None,
        "database": "connected" if g.get('db') else "disconnected"
    })

@app.post("/api/chat")
@rate_limit(max_requests=100)
def api_chat():
    try:
        data = request.get_json(force=True) or {}
        message = (data.get("message") or "").strip()
        history = data.get("history") or []
        slug = (data.get("pack") or "").strip().lower()

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

        if not history or not message:
            return jsonify({"reply": pack.get("opening", "Bonjour 👋"), "ask_lead": False})

        response = apply_rules(message, pack, history)
        return jsonify(response)

    except Exception as e:
        logger.error(f"Erreur API chat : {e}", exc_info=True)
        return jsonify({"reply": "Désolé, une erreur est survenue 😔",
                        "ask_lead": False, "error": True}), 500

# ---------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    tpl_404 = Path(app.template_folder or "", "404.html")
    if tpl_404.exists():
        return render_template("404.html"), 404
    return "Page non trouvée", 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Erreur 500 : {e}", exc_info=True)
    tpl_500 = Path(app.template_folder or "", "500.html")
    if tpl_500.exists():
        return render_template("500.html"), 500
    return "Erreur serveur", 500

# ---------------------------------------------------------------------
# Run local
# ---------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
