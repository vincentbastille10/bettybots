# app.py
import os
import time
import sqlite3
import requests
import stripe
from flask import Flask, render_template, request, redirect, jsonify
from dotenv import load_dotenv

# -----------------------------------------
# ENV & Flask
# -----------------------------------------
load_dotenv()
app = Flask(__name__, static_folder="static", template_folder="templates")

# -----------------------------------------
# Config applicative
# -----------------------------------------
# BASE_URL prioritaire, sinon PUBLIC_BASE_URL (comme sur Render)
BASE_URL = os.environ.get("BASE_URL") or os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000")

# Stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")              # ex: price_xxx
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")  # ex: whsec_xxx

# PayPal
# >>> IMPORTANT : mets PAYPAL_ENV=sandbox en ce moment (et live plus tard)
PAYPAL_ENV = (os.environ.get("PAYPAL_ENV", "sandbox") or "sandbox").lower()  # "sandbox" ou "live"
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_PLAN_ID = os.environ.get("PAYPAL_PLAN_ID", "")                # ex: P-XXXX

if PAYPAL_ENV == "sandbox":
    PAYPAL_OAUTH = "https://api-m.sandbox.paypal.com/v1/oauth2/token"
    PAYPAL_SUBS  = "https://api-m.sandbox.paypal.com/v1/billing/subscriptions/"
else:
    PAYPAL_OAUTH = "https://api-m.paypal.com/v1/oauth2/token"
    PAYPAL_SUBS  = "https://api-m.paypal.com/v1/billing/subscriptions/"

# SQLite (persistance simple)
DB_PATH = os.environ.get("DB_PATH", "payments.sqlite3")

# -----------------------------------------
# SQLite helpers
# -----------------------------------------
def _db_conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("""
        CREATE TABLE IF NOT EXISTS subs (
            tenant TEXT PRIMARY KEY,
            provider TEXT,
            status TEXT,
            email TEXT,
            plan_id TEXT,
            created_at INTEGER
        )
    """)
    return c

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
    tenant = (tenant or "").strip()
    if not tenant:
        return None
    c = _db_conn()
    row = c.execute(
        "SELECT tenant, provider, status, email, plan_id, created_at FROM subs WHERE tenant=?",
        (tenant,)
    ).fetchone()
    c.close()
    return row

# -----------------------------------------
# Routes pages
# -----------------------------------------
@app.route("/")
def home():
    return redirect("/dashboard")

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

@app.route("/pay")
def pay():
    """
    Page de paiement (Stripe Checkout + PayPal Subscriptions).
    Reçoit les préférences depuis /dashboard via querystring.
    """
    tenant = (request.args.get("tenant") or "").strip() or "demo-tenant"
    role   = request.args.get("role", "psychologue")
    color  = request.args.get("color", "#2563eb")
    avatar = request.args.get("avatar", "")

    return render_template(
        "pay.html",
        tenant=tenant,
        role=role,
        color=color,
        avatar=avatar,
        stripe_price_id=STRIPE_PRICE_ID,
        paypal_plan_id=PAYPAL_PLAN_ID,
        paypal_client_id=PAYPAL_CLIENT_ID,
        paypal_env=PAYPAL_ENV,   # <-- pour charger la bonne SDK (sandbox/live)
    )

@app.route("/bot")
def bot_page():
    """
    Page finale : affiche le bloc d'intégration <script ...> + prévisualisation.
    Accès autorisé seulement si abonnement actif (Stripe/PayPal).
    """
    tenant = (request.args.get("tenant") or "").strip()
    if not tenant:
        return redirect("/dashboard")

    sub = get_sub(tenant)
    if not sub or sub[2] not in ("active", "trialing"):
        # Non abonné → renvoyer vers /pay en conservant les préférences
        qs = request.query_string.decode("utf-8")
        return redirect(f"/pay?{qs}")

    role   = request.args.get("role", "psychologue")
    color  = request.args.get("color", "#2563eb")
    avatar = request.args.get("avatar", "")
    return render_template("bot.html", tenant=tenant, role=role, color=color, avatar=avatar)

# -----------------------------------------
# API Stripe
# -----------------------------------------
@app.route("/api/stripe/checkout", methods=["POST"])
def stripe_checkout():
    """
    Crée une session Stripe Checkout (subscription) et renvoie l'URL.
    success_url -> /bot ; cancel_url -> /pay
    """
    if not stripe.api_key or not STRIPE_PRICE_ID:
        return jsonify({"error": "Stripe non configuré (clé ou price manquant)."}), 400

    data = request.get_json(force=True, silent=True) or {}
    tenant = (data.get("tenant") or "demo-tenant").strip()
    role   = data.get("role", "psychologue")
    color  = data.get("color", "#2563eb")
    avatar = data.get("avatar", "")

    from urllib.parse import quote
    def qstr(d): return "&".join([f"{k}={quote(str(v))}" for k, v in d.items()])
    params = {"tenant": tenant, "role": role, "color": color, "avatar": avatar}

    success_url = f"{BASE_URL}/bot?" + qstr(params)
    cancel_url  = f"{BASE_URL}/pay?" + qstr(params)

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=tenant,
            metadata={"tenant": tenant}
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    """
    Webhook Stripe : marque l'abonnement 'active' à la création/mise à jour.
    Configure ton endpoint = BASE_URL + /webhooks/stripe
    """
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
            tenant = (obj.get("client_reference_id")
                      or (obj.get("metadata") or {}).get("tenant")
                      or "").strip()
            email = ""
            if obj.get("customer_details"):
                email = obj["customer_details"].get("email") or ""
            if tenant:
                upsert_sub(tenant, provider="stripe", status="active", email=email, plan_id=STRIPE_PRICE_ID)

        elif etype == "customer.subscription.deleted":
            # Si besoin: rebasculer en canceled (si tu stockes l'ID d'abonnement Stripe)
            pass

    except Exception as e:
        print("Stripe webhook processing error:", e)

    return "ok", 200

# -----------------------------------------
# API PayPal
# -----------------------------------------
def paypal_token() -> str:
    """Récupère un access_token PayPal via client_credentials."""
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise RuntimeError("PayPal non configuré (CLIENT_ID/SECRET manquants).")
    r = requests.post(
        PAYPAL_OAUTH,
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=30
    )
    r.raise_for_status()
    return r.json()["access_token"]

@app.route("/api/paypal/verify", methods=["POST"])
def paypal_verify():
    """
    Vérifie qu'une subscription PayPal (subscriptionID) est ACTIVE.
    Si oui, on marque le tenant 'active' et on renvoie ok:true.
    """
    data = request.get_json(force=True, silent=True) or {}
    tenant = (data.get("tenant") or "").strip()
    subscription_id = (data.get("subscriptionID") or "").strip()
    if not tenant or not subscription_id:
        return jsonify({"ok": False, "reason": "missing-tenant-or-subscription"}), 400

    try:
        token = paypal_token()
        r = requests.get(PAYPAL_SUBS + subscription_id,
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=30)
        if r.status_code != 200:
            return jsonify({"ok": False, "reason": "lookup-failed"}), 400

        info = r.json()
        status = info.get("status")
        email = (info.get("subscriber", {}) or {}).get("email_address", "")

        if status == "ACTIVE":
            upsert_sub(tenant, provider="paypal", status="active", email=email, plan_id=PAYPAL_PLAN_ID)
            return jsonify({"ok": True})

        return jsonify({"ok": False, "reason": status or "unknown"}), 400

    except Exception as e:
        return jsonify({"ok": False, "reason": str(e)}), 400

# -----------------------------------------
# Santé
# -----------------------------------------
@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "ts": int(time.time())})

# -----------------------------------------
# Entrée
# -----------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("DEBUG")))
