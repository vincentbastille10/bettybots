# app.py
import os
import time
import sqlite3
import smtplib
from email.message import EmailMessage
from urllib.parse import quote

import requests
import stripe
from flask import Flask, render_template, request, redirect, jsonify, url_for
from dotenv import load_dotenv

# -------------------------------------------------------------------
# Initialisation
# -------------------------------------------------------------------
load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app = Flask(
    __name__,
    static_folder=os.path.join(BASE_DIR, "static"),
    template_folder=os.path.join(BASE_DIR, "templates"),
)

# -------------------------------------------------------------------
# Config générique
# -------------------------------------------------------------------
BASE_URL = os.environ.get("BASE_URL") or os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000")

# Stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# PayPal (sandbox par défaut tant que tu testes)
PAYPAL_ENV = (os.environ.get("PAYPAL_ENV", "sandbox") or "sandbox").lower()  # "sandbox" ou "live"
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_PLAN_ID = os.environ.get("PAYPAL_PLAN_ID", "")

if PAYPAL_ENV == "sandbox":
    PAYPAL_OAUTH = "https://api-m.sandbox.paypal.com/v1/oauth2/token"
    PAYPAL_SUBS  = "https://api-m.sandbox.paypal.com/v1/billing/subscriptions/"
else:
    PAYPAL_OAUTH = "https://api-m.paypal.com/v1/oauth2/token"
    PAYPAL_SUBS  = "https://api-m.paypal.com/v1/billing/subscriptions/"

# SMTP (email après paiement)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", SMTP_USER or "support@example.com")
BRAND_NAME = os.environ.get("BRAND_NAME", "Betty Bots")

# SQLite
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "payments.sqlite3"))

# -------------------------------------------------------------------
# DB helpers
# -------------------------------------------------------------------
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
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS subs (
            tenant   TEXT PRIMARY KEY,
            provider TEXT,
            status   TEXT,
            email    TEXT,
            plan_id  TEXT,
            created_at INTEGER
        )
    """)
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
                             color=COALESCE(?, color), avatar=COALESCE(?, avatar), updated_at=?
            WHERE tenant=?
        """, (name, email, role, color, avatar, now, tenant))
    else:
        c.execute("""
            INSERT INTO users(tenant, name, email, role, color, avatar, updated_at)
            VALUES(?,?,?,?,?,?,?)
        """, (tenant, name, email, role or "psychologue", color or "#2563eb", avatar or "", now))
    c.commit(); c.close()

def get_user(tenant):
    c = _db_conn()
    row = c.execute("SELECT tenant,name,email,role,color,avatar,updated_at FROM users WHERE tenant=?", (tenant,)).fetchone()
    c.close()
    return row

def upsert_sub(tenant: str, provider: str, status: str, email: str, plan_id: str):
    tenant = (tenant or "").strip()
    if not tenant:
        return
    c = _db_conn()
    c.execute("""
        INSERT INTO subs(tenant, provider, status, email, plan_id, created_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(tenant) DO UPDATE SET
          provider=excluded.provider,
          status=excluded.status,
          email=excluded.email,
          plan_id=excluded.plan_id
    """, (tenant, provider, status, email or "", plan_id or "", int(time.time())))
    c.commit()
    c.close()

def get_sub(tenant: str):
    c = _db_conn()
    row = c.execute("SELECT tenant,provider,status,email,plan_id,created_at FROM subs WHERE tenant=?", (tenant,)).fetchone()
    c.close()
    return row

# -------------------------------------------------------------------
# Utils
# -------------------------------------------------------------------
def build_snippet(tenant, role, color, avatar):
    # Pas de dépendance à url_for ici → robuste dans les webhooks
    embed_src = f"{BASE_URL.rstrip('/')}/static/embed.js"
    attrs = [
        f'src="{embed_src}"',
        f'data-tenant="{tenant}"',
        f'data-role="{role}"',
        f'data-color="{color}"',
    ]
    if avatar:
        attrs.append(f'data-avatar="{avatar}"')
    return f"<script {' '.join(attrs)}></script>"

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

# -------------------------------------------------------------------
# Parcours
# -------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def welcome():
    # Formulaire nom/email
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
    role   = request.form.get("role") or "psychologue"
    color  = request.form.get("color") or "#2563eb"
    avatar = request.form.get("avatar") or ""
    u = get_user(tenant)
    if not u:
        return redirect(url_for("welcome"))
    upsert_user(tenant, u[1], u[2], role=role, color=color, avatar=avatar)
    return redirect(url_for("preview", tenant=tenant))

@app.route("/preview")
def preview():
    tenant = (request.args.get("tenant") or "").strip()
    u = get_user(tenant)
    if not u:
        return redirect(url_for("welcome"))
    _, name, email, role, color, avatar, _ = u
    return render_template("preview.html", tenant=tenant, name=name, email=email, role=role, color=color, avatar=avatar)

@app.route("/pay")
def pay():
    tenant = (request.args.get("tenant") or "").strip()
    u = get_user(tenant)
    if not u:
        return redirect(url_for("welcome"))
    _, name, email, role, color, avatar, _ = u
    return render_template(
        "pay.html",
        tenant=tenant, role=role, color=color, avatar=avatar,
        stripe_price_id=STRIPE_PRICE_ID,
        paypal_plan_id=PAYPAL_PLAN_ID,
        paypal_client_id=PAYPAL_CLIENT_ID,
        paypal_env=PAYPAL_ENV
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
    _, name, email, role, color, avatar, _ = u
    return render_template("bot.html", tenant=tenant, role=role, color=color, avatar=avatar)

# -------------------------------------------------------------------
# Stripe
# -------------------------------------------------------------------
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
    _, name, email, role, color, avatar, _ = u

    success_url = f"{BASE_URL}/bot?" + qstr({"tenant": tenant, "paid": 1})
    cancel_url  = f"{BASE_URL}/pay?tenant={quote(tenant)}"

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=tenant,
            customer_email=email or None,
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
                # email de confirmation avec snippet
                u = get_user(tenant)
                if u:
                    _, name, email, role, color, avatar, _ = u
                    to = email or email_from_stripe
                    snippet = build_snippet(tenant, role, color, avatar)
                    html = f"""
                    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial">
                      <h2>🎉 Paiement validé — {BRAND_NAME}</h2>
                      <p>Bonjour {name},</p>
                      <p>Merci pour votre abonnement. Voici le bloc à intégrer sur votre site :</p>
                      <pre style="background:#0b1220;color:#e5e7eb;padding:12px;border-radius:8px;white-space:pre-wrap">{snippet}</pre>
                      <p>Collez-le <b>avant &lt;/body&gt;</b> dans votre site (Wix, WordPress, Webflow…).</p>
                      <p>Retrouvez-le aussi ici : <a href="{BASE_URL}/bot?tenant={tenant}&paid=1">{BASE_URL}/bot?tenant={tenant}&paid=1</a></p>
                      <hr/>
                      <p>Un reçu/facture Stripe vous est envoyé automatiquement.</p>
                      <p>— L’équipe {BRAND_NAME}</p>
                    </div>
                    """
                    if to:
                        try:
                            send_email(to, f"{BRAND_NAME} — Paiement validé", html)
                        except Exception as e:
                            print("Email send error:", e)

    except Exception as e:
        print("Stripe webhook processing error:", e)

    return "ok", 200

# -------------------------------------------------------------------
# PayPal
# -------------------------------------------------------------------
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
                  <h2>🎉 Paiement validé — {BRAND_NAME}</h2>
                  <p>Bonjour {name},</p>
                  <p>Merci pour votre abonnement. Voici le bloc à intégrer sur votre site :</p>
                  <pre style="background:#0b1220;color:#e5e7eb;padding:12px;border-radius:8px;white-space:pre-wrap">{snippet}</pre>
                  <p>Collez-le <b>avant &lt;/body&gt;</b> dans votre site (Wix, WordPress, Webflow…).</p>
                  <p>Retrouvez-le aussi ici : <a href="{BASE_URL}/bot?tenant={tenant}&paid=1">{BASE_URL}/bot?tenant={tenant}&paid=1</a></p>
                  <hr/>
                  <p>Un reçu/facture PayPal vous est envoyé automatiquement.</p>
                  <p>— L’équipe {BRAND_NAME}</p>
                </div>
                """
                if to:
                    try:
                        send_email(to, f"{BRAND_NAME} — Paiement validé", html)
                    except Exception as e:
                        print("Email send error:", e)

            return jsonify({"ok": True})

        return jsonify({"ok": False, "reason": status or "unknown"}), 400

    except Exception as e:
        return jsonify({"ok": False, "reason": str(e)}), 400

# -------------------------------------------------------------------
# Health
# -------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "ts": int(time.time())})

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("DEBUG")))
