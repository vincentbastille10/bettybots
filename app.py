# app.py — Betty Bots (UI propre, métiers fiables, FSM serveur)
import os
import time
import sqlite3
import smtplib
import json
import uuid
from email.message import EmailMessage
from urllib.parse import quote

import requests
import stripe
from flask import Flask, render_template, request, redirect, jsonify, url_for
from dotenv import load_dotenv

# Moteur de règles (packs YAML)
from betty_rules.dialog_manager import reply as rule_reply

# Préchargement optionnel des packs si dispo (no-op sinon)
try:
    from betty_rules import loader as _packs_loader
except Exception:
    _packs_loader = None

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app = Flask(
    __name__,
    static_folder=os.path.join(BASE_DIR, "static"),
    template_folder=os.path.join(BASE_DIR, "templates"),
)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
BASE_URL = os.environ.get("BASE_URL") or os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000")

# Stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# PayPal
PAYPAL_ENV = (os.environ.get("PAYPAL_ENV", "sandbox") or "sandbox").lower()
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_PLAN_ID = os.environ.get("PAYPAL_PLAN_ID", "")
if PAYPAL_ENV == "sandbox":
    PAYPAL_OAUTH = "https://api-m.sandbox.paypal.com/v1/oauth2/token"
    PAYPAL_SUBS  = "https://api-m.sandbox.paypal.com/v1/billing/subscriptions/"
else:
    PAYPAL_OAUTH = "https://api-m.paypal.com/v1/oauth2/token"
    PAYPAL_SUBS  = "https://api-m.paypal.com/v1/billing/subscriptions/"

# SMTP
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
BRAND_NAME = os.environ.get("BRAND_NAME", "Betty Bots")

# SQLite
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "payments.sqlite3"))

# -----------------------------------------------------------------------------
# Métiers / Packs — aucun tech visible en UI
# -----------------------------------------------------------------------------
ROLE_ALIAS = {
    # Santé
    "medecin": "medecine_pack",
    "médecin": "medecine_pack",
    "psychologue": "psychologue_pack",
    "psy": "psychologue_pack",
    # Droit/Chiffre
    "avocat": "avocat_pack",
    "comptable": "comptable_pack",
    # Immobilier
    "immobilier": "agent_immobilier_pack",
    "agent immobilier": "agent_immobilier_pack",
    "agent_immobilier": "agent_immobilier_pack",
    # → complète ici la liste des 20 métiers si besoin
}
DEFAULT_ROLE = "psychologue_pack"

DISPLAY_LABELS = {
    "psychologue_pack": "Psychologue",
    "medecine_pack": "Médecin",
    "avocat_pack": "Avocat",
    "comptable_pack": "Comptable",
    "agent_immobilier_pack": "Agent immobilier",
}

def canonical_role(role_label: str) -> str:
    if not role_label:
        return DEFAULT_ROLE
    key = role_label.strip().lower()
    return ROLE_ALIAS.get(key, DEFAULT_ROLE)

def role_to_label(role: str) -> str:
    return DISPLAY_LABELS.get(role, "Assistant")

# Précharge les packs si l’API existe (pas bloquant)
try:
    if _packs_loader and hasattr(_packs_loader, "preload"):
        _packs_loader.preload(set(DISPLAY_LABELS.keys()) | {DEFAULT_ROLE})
except Exception as _e:
    print("[packs] preload skipped:", _e)

# -----------------------------------------------------------------------------
# DB helpers
# -----------------------------------------------------------------------------
def _db_conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        tenant TEXT PRIMARY KEY,
        name   TEXT,
        email  TEXT,
        role   TEXT,
        color  TEXT,
        avatar TEXT,
        updated_at INTEGER
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS subs (
        tenant   TEXT PRIMARY KEY,
        provider TEXT,
        status   TEXT,
        email    TEXT,
        plan_id  TEXT,
        created_at INTEGER
    )""")
    # état de conversation par tenant+session
    c.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        tenant TEXT,
        session_id TEXT,
        stage TEXT,
        payload TEXT,
        updated_at INTEGER,
        PRIMARY KEY (tenant, session_id)
    )""")
    # leads capturés (tous métiers)
    c.execute("""
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant TEXT,
        name TEXT,
        email TEXT,
        phone TEXT,
        intent TEXT,
        created_at INTEGER
    )""")
    return c

def slug_email(email: str) -> str:
    return (email or "").lower().replace("@", "-").replace(".", "-").replace("+", "-").strip("-")

def upsert_user(tenant, name, email, role=None, color=None, avatar=None):
    c = _db_conn()
    now = int(time.time())
    row = c.execute("SELECT tenant FROM users WHERE tenant=?", (tenant,)).fetchone()
    if row:
        c.execute("""
            UPDATE users SET name=?, email=?, role=COALESCE(?, role),
                             color=COALESCE(?, color), avatar=COALESCE(?, avatar),
                             updated_at=? WHERE tenant=?
        """, (name, email, role, color, avatar, now, tenant))
    else:
        c.execute("""
            INSERT INTO users(tenant,name,email,role,color,avatar,updated_at)
            VALUES(?,?,?,?,?,?,?)
        """, (tenant, name, email, role or DEFAULT_ROLE, color or "#2563eb", avatar or "", now))
    c.commit(); c.close()

def get_user(tenant):
    c = _db_conn()
    row = c.execute("SELECT tenant,name,email,role,color,avatar,updated_at FROM users WHERE tenant=?", (tenant,)).fetchone()
    c.close()
    return row

def upsert_sub(tenant: str, provider: str, status: str, email: str, plan_id: str):
    if not tenant:
        return
    c = _db_conn()
    c.execute("""
        INSERT INTO subs(tenant,provider,status,email,plan_id,created_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(tenant) DO UPDATE SET
          provider=excluded.provider,
          status=excluded.status,
          email=excluded.email,
          plan_id=excluded.plan_id
    """, (tenant, provider, status, email or "", plan_id or "", int(time.time())))
    c.commit(); c.close()

def get_sub(tenant: str):
    c = _db_conn()
    row = c.execute("SELECT tenant,provider,status,email,plan_id,created_at FROM subs WHERE tenant=?", (tenant,)).fetchone()
    c.close()
    return row

# --- Conversation state -------------------------------------------------------
def get_session_id(req):
    sid = (req.args.get("sid") or req.headers.get("X-Chat-Session") or "").strip()
    return sid or str(uuid.uuid4())

def load_conv(tenant, session_id):
    c = _db_conn()
    row = c.execute("SELECT stage,payload FROM conversations WHERE tenant=? AND session_id=?", (tenant, session_id)).fetchone()
    c.close()
    if not row:
        return {"stage": "start", "payload": {}}
    stage, payload = row
    return {"stage": stage or "start", "payload": json.loads(payload or "{}")}

def save_conv(tenant, session_id, stage, payload):
    c = _db_conn()
    now = int(time.time())
    c.execute("""
    INSERT INTO conversations(tenant,session_id,stage,payload,updated_at)
    VALUES(?,?,?,?,?)
    ON CONFLICT(tenant,session_id) DO UPDATE SET
      stage=excluded.stage, payload=excluded.payload, updated_at=excluded.updated_at
    """, (tenant, session_id, stage, json.dumps(payload, ensure_ascii=False), now))
    c.commit(); c.close()

def reset_conversations(tenant):
    c = _db_conn()
    c.execute("DELETE FROM conversations WHERE tenant=?", (tenant,))
    c.commit(); c.close()

def store_lead(tenant, name, email, phone, intent):
    c = _db_conn()
    c.execute("INSERT INTO leads(tenant,name,email,phone,intent,created_at) VALUES(?,?,?,?,?,?)",
              (tenant, name, email, phone, intent, int(time.time())))
    c.commit(); c.close()

# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------
def build_snippet(tenant, role, color, avatar):
    # UI clean: seul le tenant est exposé
    embed_src = f"{BASE_URL.rstrip('/')}/static/embed.js"
    return f'<script src="{embed_src}" data-tenant="{tenant}"></script>'

def send_email(to_email: str, subject: str, html_body: str):
    if not (SMTP_USER and SMTP_PASS and to_email):
        return False
    msg = EmailMessage()
    msg["From"] = f"{BRAND_NAME} <{SMTP_USER}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content("Votre client e-mail n'affiche pas le HTML. Ouvrez ce message dans un client compatible.")
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    return True

def qstr(d: dict) -> str:
    return "&".join([f"{k}={quote(str(v))}" for k, v in d.items()])

# -----------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def welcome():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip()
        if not (name and email):
            return render_template("welcome.html", error="Merci de remplir votre nom et votre email.")
        tenant = slug_email(email)
        upsert_user(tenant, name, email)
        return redirect(url_for("dashboard", tenant=tenant))
    return render_template("welcome.html")

@app.route("/dashboard")
def dashboard():
    tenant = (request.args.get("tenant") or "").strip()
    if not tenant:
        return redirect(url_for("welcome"))
    u = get_user(tenant)
    name = u[1] if u else ""
    email = u[2] if u else ""
    return render_template("dashboard.html", tenant=tenant, name=name, email=email)

@app.route("/save", methods=["POST"])
def save_settings():
    tenant = (request.form.get("tenant") or "").strip()
    role_label = request.form.get("role") or "psychologue"   # libellé humain
    color  = request.form.get("color") or "#2563eb"
    avatar = request.form.get("avatar") or ""

    u = get_user(tenant)
    if not u:
        return redirect(url_for("welcome"))

    old_role = u[3] or DEFAULT_ROLE
    new_role = canonical_role(role_label)

    # si le métier change → purge de l'état de conversation
    if new_role != old_role:
        reset_conversations(tenant)

    upsert_user(tenant, u[1], u[2], role=new_role, color=color, avatar=avatar)
    return redirect(url_for("preview", tenant=tenant))

@app.route("/preview")
def preview():
    tenant = (request.args.get("tenant") or "").strip()
    u = get_user(tenant)
    if not u:
        return redirect(url_for("welcome"))
    _, name, email, role, color, avatar, _ = u
    return render_template("preview.html",
                           tenant=tenant, name=name, email=email,
                           role_label=role_to_label(role), color=color, avatar=avatar)

@app.route("/chat")
def chat_page():
    tenant = (request.args.get("tenant") or "").strip()
    u = get_user(tenant)
    if not u:
        return redirect(url_for("welcome"))
    _, _, _, role, color, avatar, _ = u
    return render_template("chat.html",
                           tenant=tenant, role_label=role_to_label(role), color=color, avatar=avatar)

@app.route("/pay")
def pay():
    tenant = (request.args.get("tenant") or "").strip()
    u = get_user(tenant)
    if not u:
        return redirect(url_for("welcome"))
    _, name, email, role, color, avatar, _ = u
    return render_template(
        "pay.html",
        tenant=tenant, role_label=role_to_label(role), color=color, avatar=avatar,
        paypal_env=PAYPAL_ENV, paypal_client_id=PAYPAL_CLIENT_ID, paypal_plan_id=PAYPAL_PLAN_ID
    )

@app.route("/bot")
def bot_page():
    tenant = (request.args.get("tenant") or "").strip()
    if not tenant:
        return redirect(url_for("welcome"))

    sub = get_sub(tenant)
    if not sub or sub[2] not in ("active", "trialing"):
        return redirect(url_for("pay", tenant=tenant))

    u = get_user(tenant)
    if not u:
        return redirect(url_for("welcome"))
    _, name, email, role, color, avatar, _ = u

    paid_flag = request.args.get("paid") == "1"
    return render_template("bot.html",
                           tenant=tenant, role_label=role_to_label(role),
                           color=color, avatar=avatar, paid=paid_flag)

# -----------------------------------------------------------------------------
# API — Widget
# -----------------------------------------------------------------------------
@app.route("/api/widget-config", methods=["GET"])
def widget_config():
    tenant = (request.args.get("tenant") or "").strip()
    u = get_user(tenant)
    if not u:
        return jsonify({"ok": False, "reason": "unknown-tenant"}), 404
    _, name, email, role, color, avatar, _ = u
    return jsonify({
        "ok": True,
        "tenant": tenant,
        "name": name,
        "role_label": role_to_label(role),
        "color": color,
        "avatar": avatar or ""
    })

# -----------------------------------------------------------------------------
# API — Chat (FSM anti-répétition + bons métiers)
# -----------------------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    tenant = (data.get("tenant") or "").strip()
    text   = (data.get("text") or "").strip()
    sid    = data.get("sid") or get_session_id(request)

    if not tenant:
        return jsonify({"reply": "Missing tenant."}), 400
    u = get_user(tenant)
    if not u:
        return jsonify({"reply": "Unknown tenant."}), 404

    role = u[3] or DEFAULT_ROLE  # clé de pack interne
    conv = load_conv(tenant, sid)
    stage, p = conv["stage"], conv["payload"]

    # --- Déduplication anti-double event ---
    last_text = p.get("_last_text"); last_ts = p.get("_last_ts", 0.0)
    now = time.time()
    if last_text == text and (now - last_ts) < 2.0:
        return jsonify({"reply": None, "sid": sid})
    p["_last_text"], p["_last_ts"] = text, now

    def reply(msg, next_stage=None):
        save_conv(tenant, sid, next_stage or stage, p)
        return jsonify({"reply": msg, "sid": sid})

    # ------------------ Flows par métier ------------------
    # IMMOBILIER
    if role == "agent_immobilier_pack":
        if stage == "start":
            return reply("Bonjour 👋 Je vous aide pour votre projet immobilier. Pour commencer, quel est votre nom et prénom ?", "ask_name")
        if stage == "ask_name":
            if len(text) < 2:
                return reply("Je n’ai pas bien saisi. Pouvez-vous me donner votre nom et prénom ?")
            p["name"] = text
            return reply(f"Merci {p['name']}. Quelle est votre adresse e-mail pour vous recontacter ?", "ask_email")
        if stage == "ask_email":
            if "@" not in text or "." not in text:
                return reply("Pouvez-vous indiquer une adresse e-mail valide, s’il vous plaît ?")
            p["email"] = text
            return reply("Souhaitez-vous laisser un numéro de téléphone pour un rappel ? (facultatif)", "ask_phone")
        if stage == "ask_phone":
            digits = "".join(ch for ch in text if ch.isdigit())
            p["phone"] = digits if 8 <= len(digits) <= 15 else ""
            return reply("Pouvez-vous préciser votre besoin ? (achat, vente, estimation…)", "ask_intent")
        if stage == "ask_intent":
            if not text:
                return reply("Dites-moi simplement : achat, vente, estimation…")
            p["intent"] = text
            store_lead(tenant, p.get("name",""), p.get("email",""), p.get("phone",""), p.get("intent",""))
            save_conv(tenant, sid, "done", p)
            suivi = rule_reply(tenant, role, f"conseil_{p['intent']}".lower())
            return jsonify({
                "reply": f"Parfait 👍 J’ai tout noté.\n• Nom: {p['name']}\n• Email: {p['email']}\n• Téléphone: {p['phone'] or '—'}\n• Besoin: {p['intent']}\n\nUn conseiller vous recontactera très vite.\n\n{suivi or ''}",
                "sid": sid
            })
        # free chat après capture
        msg = rule_reply(tenant, role, text)
        return jsonify({"reply": msg, "sid": sid})

    # MEDECIN
    if role == "medecine_pack":
        if stage == "start":
            return reply("Bonjour 👋 Je peux vous orienter. Pour commencer, quel est votre nom et prénom ?", "ask_name")
        if stage == "ask_name":
            if len(text) < 2:
                return reply("Je n’ai pas bien saisi. Pouvez-vous me donner votre nom et prénom ?")
            p["name"] = text
            return reply(f"Merci {p['name']}. Quelle est votre adresse e-mail pour vous recontacter si besoin ?", "ask_email")
        if stage == "ask_email":
            if "@" not in text or "." not in text:
                return reply("Pouvez-vous indiquer une adresse e-mail valide, s’il vous plaît ?")
            p["email"] = text
            return reply("Souhaitez-vous laisser un numéro de téléphone ? (facultatif)", "ask_phone")
        if stage == "ask_phone":
            digits = "".join(ch for ch in text if ch.isdigit())
            p["phone"] = digits if 8 <= len(digits) <= 15 else ""
            return reply("Quel est le motif de votre demande ? (ex. prise de RDV, renouvellement ordonnance, symptômes…)", "ask_reason")
        if stage == "ask_reason":
            if not text:
                return reply("Quelques mots suffisent : RDV, symptômes, ordonnance…")
            p["intent"] = text
            store_lead(tenant, p.get("name",""), p.get("email",""), p.get("phone",""), p.get("intent",""))
            save_conv(tenant, sid, "done", p)
            suivi = rule_reply(tenant, role, f"tri_{p['intent']}".lower())
            return jsonify({
                "reply": f"Merci {p['name']}. J’ai noté votre demande : {p['intent']}.\nUn professionnel vous recontacte rapidement.\n\n{suivi or ''}",
                "sid": sid
            })
        msg = rule_reply(tenant, role, text)
        return jsonify({"reply": msg, "sid": sid})

    # AUTRES MÉTIERS → ouverture + moteur
    if stage == "start":
        opening = rule_reply(tenant, role, "opening")
        save_conv(tenant, sid, "free", p)
        return jsonify({"reply": opening or "Bonjour, comment puis-je vous aider ?", "sid": sid})
    msg = rule_reply(tenant, role, text)
    return jsonify({"reply": msg, "sid": sid})

# -----------------------------------------------------------------------------
# Stripe API
# -----------------------------------------------------------------------------
@app.route("/api/stripe/checkout", methods=["POST"])
def stripe_checkout():
    if not stripe.api_key or not STRIPE_PRICE_ID:
        return jsonify({"error": "Stripe non configuré (clé ou price manquant)."}), 400
    data = request.get_json(force=True, silent=True) or {}
    tenant = (data.get("tenant") or "").strip()
    if not tenant:
        return jsonify({"error": "tenant manquant"}), 400
    u = get_user(tenant)
    if not u:
        return jsonify({"error": "utilisateur introuvable"}), 400

    success_url = f"{BASE_URL}/bot?" + qstr({"tenant": tenant, "paid": 1})
    cancel_url  = f"{BASE_URL}/pay?tenant={quote(tenant)}"
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=tenant,
            customer_email=u[2] or None,
            metadata={"tenant": tenant}
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return "webhook secret manquant", 400
    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return str(e), 400

    etype = event.get("type")
    obj = event.get("data", {}).get("object", {}) or {}

    try:
        if etype in ("checkout.session.completed", "customer.subscription.created", "customer.subscription.updated"):
            tenant = (obj.get("client_reference_id") or (obj.get("metadata") or {}).get("tenant") or "").strip()
            email_from_stripe = ""
            if obj.get("customer_details"):
                email_from_stripe = obj["customer_details"].get("email") or ""
            if tenant:
                upsert_sub(tenant, provider="stripe", status="active", email=email_from_stripe, plan_id=STRIPE_PRICE_ID)
                u = get_user(tenant)
                if u:
                    _, name, email, role, color, avatar, _ = u
                    to = email or email_from_stripe
                    snippet = build_snippet(tenant, role, color, avatar)
                    html = f"""
                    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial">
                      <h2>🎉 Merci de vous être abonné — {BRAND_NAME}</h2>
                      <p>Bonjour {name},</p>
                      <p>Votre abonnement est actif. Voici votre code d’intégration :</p>
                      <pre style="background:#0b1220;color:#e5e7eb;padding:12px;border-radius:8px;white-space:pre-wrap">{snippet}</pre>
                      <p>Collez-le <b>avant &lt;/body&gt;</b> dans votre site.</p>
                      <p>Retrouvez-le aussi ici : <a href="{BASE_URL}/bot?tenant={tenant}&paid=1">{BASE_URL}/bot?tenant={tenant}&paid=1</a></p>
                      <hr/>
                      <p>Un reçu/facture Stripe vous est envoyé automatiquement.</p>
                    </div>
                    """
                    if to:
                        try:
                            send_email(to, f"{BRAND_NAME} — Abonnement confirmé", html)
                        except Exception as e:
                            print("Email send error:", e)
    except Exception as e:
        print("Stripe webhook processing error:", e)

    return "ok", 200

# -----------------------------------------------------------------------------
# PayPal API
# -----------------------------------------------------------------------------
def paypal_token() -> str:
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise RuntimeError("PayPal non configuré (CLIENT_ID/SECRET manquants).")
    r = requests.post(PAYPAL_OAUTH, auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                      data={"grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]

@app.route("/api/paypal/verify", methods=["POST"])
def paypal_verify():
    data = request.get_json(force=True, silent=True) or {}
    tenant = (data.get("tenant") or "").strip()
    subscription_id = (data.get("subscriptionID") or "").strip()
    if not tenant or not subscription_id:
        return jsonify({"ok": False, "reason": "missing-tenant-or-subscription"}), 400

    try:
        token = paypal_token()
        r = requests.get(PAYPAL_SUBS + subscription_id, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if r.status_code != 200:
            return jsonify({"ok": False, "reason": "lookup-failed"}), 400

        info = r.json()
        status = info.get("status")
        email_pp = (info.get("subscriber", {}) or {}).get("email_address", "")

        if status == "ACTIVE":
            upsert_sub(tenant, provider="paypal", status="active", email=email_pp, plan_id=PAYPAL_PLAN_ID)
            u = get_user(tenant)
            if u:
                _, name, email, role, color, avatar, _ = u
                to = email or email_pp
                snippet = build_snippet(tenant, role, color, avatar)
                html = f"""
                <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial">
                  <h2>🎉 Merci de vous être abonné — {BRAND_NAME}</h2>
                  <p>Bonjour {name},</p>
                  <p>Votre abonnement est actif. Voici votre code d’intégration :</p>
                  <pre style="background:#0b1220;color:#e5e7eb;padding:12px;border-radius:8px;white-space:pre-wrap">{snippet}</pre>
                  <p>Collez-le <b>avant &lt;/body&gt;</b> dans votre site.</p>
                  <p>Retrouvez-le aussi ici : <a href="{BASE_URL}/bot?tenant={tenant}&paid=1">{BASE_URL}/bot?tenant={tenant}&paid=1</a></p>
                </div>
                """
                if to:
                    try:
                        send_email(to, f"{BRAND_NAME} — Abonnement confirmé", html)
                    except Exception as e:
                        print("Email send error:", e)
            return jsonify({"ok": True})

        return jsonify({"ok": False, "reason": status or "unknown"}), 400
    except Exception as e:
        return jsonify({"ok": False, "reason": str(e)}), 400

# -----------------------------------------------------------------------------
# Health
# -----------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "ts": int(time.time())})

# -----------------------------------------------------------------------------
# Entrée
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("DEBUG")))
