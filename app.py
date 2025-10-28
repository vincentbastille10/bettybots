# app.py  — Betty Bots (corrigé)  [PART 1/4]
from __future__ import annotations

import os
import json
import sqlite3
import secrets
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from flask import (
    Flask, request, session, redirect, url_for, render_template,
    jsonify, g, abort, make_response
)
from werkzeug.middleware.proxy_fix import ProxyFix

# Stripe (optionnel en dev : protège les imports si pas installé)
try:
    import stripe  # type: ignore
except Exception:  # pragma: no cover
    stripe = None  # le code gère l'absence de stripe en dev

load_dotenv()

# -----------------------------------------------------------------------------
# App & secrets
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("BETTY_DB_PATH", str(BASE_DIR / "bettybots.sqlite3"))

app = Flask(__name__, static_folder="static", template_folder="templates")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # si déployé derrière proxy
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))

# -----------------------------------------------------------------------------
# Stripe config (safe en l'absence de lib)
# -----------------------------------------------------------------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID_1990 = os.getenv("STRIPE_PRICE_ID_1990", "")  # 19,90 €
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# -----------------------------------------------------------------------------
# Logging propre
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("betty")

# -----------------------------------------------------------------------------
# SQLite helpers
# -----------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(exception: Optional[BaseException]):
    conn = g.pop("db", None)
    if conn:
        conn.close()

def init_db():
    conn = get_db()
    cur = conn.cursor()
    # table bots (clé = bot_id; key publique pour l’iframe; slug, avatar…)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bots(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_email TEXT,
        key TEXT UNIQUE,
        slug TEXT,
        name TEXT,
        color_hex TEXT,
        persona TEXT,
        avatar_url TEXT,
        welcome_text TEXT,
        created_at TEXT,
        updated_at TEXT
    );
    """)
    # table leads (collectés depuis preview/embed)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS leads(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id INTEGER,
        name TEXT,
        email TEXT,
        message TEXT,
        extra_json TEXT,
        created_at TEXT
    );
    """)
    conn.commit()

with app.app_context():
    init_db()

# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def normalize_metier(v: Optional[str]) -> str:
    if not v:
        return "agent_immobilier"
    v = v.strip().lower()
    mapping = {
        "agent immo": "agent_immobilier",
        "agent_immobilier": "agent_immobilier",
        "immobilier": "agent_immobilier",
        "avocat": "avocat",
        "médecin": "medecin",
        "medecin": "medecin",
        "docteur": "medecin",
        "coiffeur": "coiffeur",
        "coach": "coach_sportif",
        "coach_sportif": "coach_sportif",
    }
    return mapping.get(v, v.replace(" ", "_"))

def generate_public_key() -> str:
    return secrets.token_urlsafe(24)

def current_cfg() -> Dict[str, Any]:
    """Configuration en session, avec défauts sûrs pour la preview."""
    cfg = dict(session.get("cfg") or {})
    cfg.setdefault("slug", "agent_immobilier")
    cfg.setdefault("name", "Mon Betty Bot")
    cfg.setdefault("color_hex", "#4F46E5")
    cfg.setdefault("persona", "neutre")
    cfg.setdefault("avatar_url", None)
    cfg.setdefault("welcome_text", None)
    # flags UI preview
    cfg.setdefault("show_controls", True)
    cfg.setdefault("inject_hide_css", False)
    cfg.setdefault("show_brand", True)
    return cfg

# app.py  — Betty Bots (corrigé)  [PART 2/4]

# -----------------------------------------------------------------------------
# Dashboard (accueil simple)
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    # ton template dashboard.html existant, sinon page minimale
    try:
        return render_template("dashboard.html", cfg=current_cfg())
    except Exception:
        # fallback ultra simple si template manquant
        return "<h1>Dashboard</h1><p>POST /dashboard pour enregistrer le pack puis /preview</p>"

# -----------------------------------------------------------------------------
# POST /dashboard  — Enregistre la config dans la session puis redirige preview
# -----------------------------------------------------------------------------
@app.route("/dashboard", methods=["POST"])
def dashboard_save():
    form = request.form
    cfg = session.get("cfg") or {}

    pack_slug = normalize_metier(
        form.get("pack_slug") or form.get("slug") or cfg.get("slug") or "agent_immobilier"
    )

    cfg.update({
        "slug": pack_slug,
        "name": form.get("name") or cfg.get("name") or "Mon Betty Bot",
        "color_hex": form.get("color_hex") or cfg.get("color_hex") or "#4F46E5",
        "persona": form.get("persona") or cfg.get("persona") or "neutre",
        "avatar_url": form.get("avatar_url") or cfg.get("avatar_url"),
        "welcome_text": form.get("welcome_text") or cfg.get("welcome_text"),
        # flags preview : la preview garde les contrôles
        "show_controls": True,
        "inject_hide_css": False,
        "show_brand": True,
    })
    session["cfg"] = cfg
    session.modified = True
    log.info("Dashboard saved to session: %s", cfg)
    return redirect(url_for("preview"))

# -----------------------------------------------------------------------------
# GET /preview  — Test du bot (prend UNIQUEMENT la session)
# -----------------------------------------------------------------------------
@app.route("/preview")
def preview():
    cfg = current_cfg()
    # IMPORTANT : Pas de badges couleur/persona visibles ici (géré par template)
    return render_template("preview.html", cfg=cfg, bot=None)

# -----------------------------------------------------------------------------
# GET /pay  — Crée la session Stripe avec METADATA (slug/avatar/etc.)
# -----------------------------------------------------------------------------
@app.route("/pay")
def pay():
    if not stripe or not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID_1990:
        abort(503, "Stripe non configuré")

    cfg = current_cfg()
    metadata = {
        "pack_slug": cfg.get("slug", "agent_immobilier"),
        "avatar_url": cfg.get("avatar_url") or "",
        "welcome_text": cfg.get("welcome_text") or "",
        "name": cfg.get("name") or "Mon Betty Bot",
        "color_hex": cfg.get("color_hex") or "#4F46E5",
        "persona": cfg.get("persona") or "neutre",
    }

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID_1990, "quantity": 1}],
            success_url=url_for("pay_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("preview", _external=True),
            metadata=metadata,
        )
    except Exception as e:  # pragma: no cover
        log.exception("Stripe error: %s", e)
        abort(500, "Erreur paiement")

    return redirect(checkout_session.url, code=303)

# -----------------------------------------------------------------------------
# GET /thanks  — Page de succès, affiche le code d'intégration
# -----------------------------------------------------------------------------
@app.route("/thanks")
def pay_success():
    session_id = request.args.get("session_id")
    if not session_id or not stripe:
        abort(400)

    try:
        s = stripe.checkout.Session.retrieve(session_id, expand=["subscription", "customer"])
    except Exception:  # pragma: no cover
        abort(400)

    # On retrouve/assigne un bot_id + clé publique
    conn = get_db()
    cur = conn.cursor()

    # email client si dispo côté Stripe
    owner_email = None
    try:
        owner_email = (s.customer_details or {}).get("email")
    except Exception:
        owner_email = None

    # récupère metadata du checkout (pack, avatar…)
    meta = dict(getattr(s, "metadata", {}) or {})
    slug = normalize_metier(meta.get("pack_slug") or "agent_immobilier")
    name = meta.get("name") or "Mon Betty Bot"
    color_hex = meta.get("color_hex") or "#4F46E5"
    persona = meta.get("persona") or "neutre"
    avatar_url = meta.get("avatar_url") or None
    welcome_text = meta.get("welcome_text") or None

    public_key = generate_public_key()

    cur.execute("""
        INSERT INTO bots(owner_email, key, slug, name, color_hex, persona, avatar_url, welcome_text, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
    """, (owner_email, public_key, slug, name, color_hex, persona, avatar_url, welcome_text, now_iso(), now_iso()))
    conn.commit()

    # Code d'intégration (iframe) – même host (Vercel) ou domaine public
    iframe_src = url_for("chat_embed", _external=True)  # /chat
    embed_code = f'<iframe src="{iframe_src}?bot={cur.lastrowid}&key={public_key}" width="420" height="580" style="border:0;border-radius:18px;box-shadow:0 0 25px rgba(0,0,0,.4);" title="{name}"></iframe>'

    try:
        return render_template("success.html",
                               pack=slug,
                               paid_status="Payé / Abonnement actif",
                               embed_code=embed_code)
    except Exception:
        # fallback si template manquant
        return f"<h1>Merci pour votre abonnement !</h1><pre>{embed_code}</pre>"

# app.py  — Betty Bots (corrigé)  [PART 3/4]

# -----------------------------------------------------------------------------
# STRIPE Webhook — sécurise la persistance des métadonnées
# -----------------------------------------------------------------------------
@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if not stripe:
        return "", 200  # en dev sans stripe

    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:  # pragma: no cover
        log.warning("Webhook invalid: %s", e)
        return "", 400

    if event["type"] == "checkout.session.completed":
        obj = event["data"]["object"]
        meta = dict(obj.get("metadata") or {})
        slug = normalize_metier(meta.get("pack_slug") or "agent_immobilier")
        avatar_url = meta.get("avatar_url") or None
        welcome_text = meta.get("welcome_text") or None
        name = meta.get("name") or "Mon Betty Bot"
        color_hex = meta.get("color_hex") or "#4F46E5"
        persona = meta.get("persona") or "neutre"

        conn = get_db()
        cur = conn.cursor()

        # si tu relies le session_id -> bot_id, tu peux faire un UPDATE ici.
        # Ici, on ne fait rien : la page /thanks a déjà créé l'entrée.
        # Tu peux mettre en place une logique plus stricte si besoin.

        log.info("Webhook completed: slug=%s", slug)

    return "", 200

# -----------------------------------------------------------------------------
# EMPLACEMENT DU CHAT EMBED — <iframe src="/chat?bot=...&key=...">
# -----------------------------------------------------------------------------
@app.route("/chat")
def chat_embed():
    """Fenêtre finale (livrée au client après paiement)."""
    bot_id = request.args.get("bot", type=int)
    key = request.args.get("key")
    if not bot_id or not key:
        abort(400)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bots WHERE id=? AND key=?", (bot_id, key))
    row = cur.fetchone()
    if not row:
        abort(404)

    bot = dict(row)
    # On affiche un template dédié embed.html (UI compacte, bandeau Spectra)
    return render_template("embed.html", bot=bot)

# -----------------------------------------------------------------------------
# API Lead — preview & embed déposent ici (mail à faire si besoin)
# -----------------------------------------------------------------------------
@app.route("/api/lead", methods=["POST"])
def api_lead():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    message = (data.get("message") or "").strip()
    extra = data.get("extra") or {}

    conn = get_db()
    cur = conn.cursor()

    bot_id = None
    try:
        bot_id = int(extra.get("bot_id")) if "bot_id" in extra else None
    except Exception:
        bot_id = None

    cur.execute("""
        INSERT INTO leads(bot_id, name, email, message, extra_json, created_at)
        VALUES(?,?,?,?,?,?)
    """, (bot_id, name, email, message, json.dumps(extra, ensure_ascii=False), now_iso()))
    conn.commit()

    return jsonify({"ok": True})

# -----------------------------------------------------------------------------
# API Chat — “petit LLM” pack-aware (fallback déterministe)
# -----------------------------------------------------------------------------
def tiny_pack_brain(pack: str, user_text: str) -> str:
    t = (user_text or "").strip().lower()

    if pack == "avocat":
        if any(k in t for k in ["divorce", "séparation", "garde"]):
            return "Dossier famille : je note. Avez-vous une urgence (audience proche) ?"
        if any(k in t for k in ["licenciement", "prud'h", "travail"]):
            return "Dossier travail : avez-vous reçu une lettre de licenciement ? À quelle date ?"
        return "Pouvez-vous préciser le type de dossier (famille, travail, pénal…) et le degré d’urgence ?"

    if pack == "medecin":
        if any(k in t for k in ["douleur", "fièvre", "épaule", "dos", "toux"]):
            return "Je note votre motif. Souhaitez-vous un rendez-vous plutôt matin ou après-midi ?"
        return "Quel est votre motif de consultation et vos disponibilités ?"

    if pack == "coiffeur":
        if any(k in t for k in ["coupe", "color", "mèches", "balayage"]):
            return "D’accord. Quel jour vous conviendrait et à quelle heure ?"
        return "Quel service souhaitez-vous et quand êtes-vous disponible ?"

    if pack == "coach_sportif":
        if any(k in t for k in ["perte de poids", "mincir", "se muscler", "performance"]):
            return "Objectif noté. Quel est votre créneau favori dans la semaine ?"
        return "Quel est votre objectif (perte de poids, performance, entretien) et vos créneaux ?"

    # agent_immobilier (défaut)
    if any(k in t for k in ["acheter", "achat"]):
        return "Achat noté. Quel budget et sur quelle zone cherchez-vous ?"
    if any(k in t for k in ["vendre", "vente"]):
        return "Vente notée. À quelle adresse se situe le bien et votre délai ?"
    if "louer" in t or "location" in t:
        return "Location notée. Quel loyer cible et quel secteur ?"
    return "Souhaitez-vous acheter, vendre ou louer ? Sur quelle zone ?"

@app.route("/api/chat", methods=["POST"])
def api_chat():
    payload = request.get_json(force=True, silent=True) or {}
    pack = normalize_metier(payload.get("pack") or "agent_immobilier")
    user_text = (payload.get("message") or "")[:500]  # coupe dur
    # persona/couleur ignorés côté logique simple

    # Ici tu peux brancher un vrai modèle “petit LLM” (Together/Mistral) avec un limiteur.
    reply = tiny_pack_brain(pack, user_text)
    return jsonify({"reply": reply})

# app.py  — Betty Bots (corrigé)  [PART 4/4]

# -----------------------------------------------------------------------------
# CORS très léger pour l’iframe si besoin (optionnel)
# -----------------------------------------------------------------------------
@app.after_request
def add_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp

# -----------------------------------------------------------------------------
# Route basique d’avatar proxy (optionnelle) — /avatar/<slug>
# -----------------------------------------------------------------------------
@app.route("/avatar/<slug>")
def avatar_proxy(slug: str):
    """Permet d’avoir une URL stable même si l’avatar n’est pas fourni.
    En prod, remplace par une vraie statique CDN si dispo.
    """
    slug = normalize_metier(slug)
    # mapping d'images par défaut
    defaults = {
        "agent_immobilier": "static/avatars/agent_immo.jpg",
        "avocat": "static/avatars/avocat.jpg",
        "medecin": "static/avatars/medecin.jpg",
        "coiffeur": "static/avatars/coiffeur.jpg",
        "coach_sportif": "static/avatars/coach.jpg",
    }
    path = defaults.get(slug) or defaults["agent_immobilier"]
    try:
        with open(path, "rb") as f:
            resp = make_response(f.read())
            resp.headers["Content-Type"] = "image/jpeg"
            return resp
    except Exception:
        abort(404)

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
