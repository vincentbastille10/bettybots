import os
import logging
from flask import Flask, render_template, jsonify, request, abort
import stripe

# -------- Config de l'app --------
# templates/ est à la racine du repo → on remonte d'un dossier depuis /api
app = Flask(__name__, template_folder="../templates", static_folder=None)

# -------- ENV obligatoires --------
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")  # sk_test_... ou sk_live_...
STRIPE_PRICE_ID   = os.environ.get("STRIPE_PRICE_ID")    # price_...
DOMAIN            = os.environ.get("DOMAIN")             # ex: https://<ton-projet>.vercel.app

if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
    # On log clairement côté Vercel au démarrage
    logging.error("ENV manquantes: STRIPE_SECRET_KEY and/or STRIPE_PRICE_ID")
stripe.api_key = STRIPE_SECRET_KEY or ""

# -------- Routes pages --------
@app.get("/")
def home():
    return render_template("index.html")

@app.get("/pay")
def pay_page():
    return render_template("pay.html")

@app.get("/success")
def success_page():
    return render_template("success.html")

@app.get("/cancel")
def cancel_page():
    return render_template("cancel.html")

@app.get("/api/health")
def health():
    return "ok"

# -------- Endpoint Stripe --------
@app.post("/api/pay")
def create_checkout():
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        return jsonify({"error": "Configuration Stripe incomplète (ENV)"}), 500

    # Optionnel: lecture d'options depuis le front (quantity, coupon, etc.)
    data = {}
    try:
        if request.is_json:
            data = request.get_json(silent=True) or {}
    except Exception:
        pass

    mode = data.get("mode", "subscription")  # "subscription" ou "payment"
    quantity = int(data.get("quantity", 1))

    try:
        session = stripe.checkout.Session.create(
            mode=mode,
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": quantity}],
            success_url=f"{DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{DOMAIN}/cancel",
            automatic_tax={"enabled": True}
        )
        return jsonify({"url": session.url})
    except Exception as e:
        logging.exception("Stripe Checkout error")
        # Renvoi lisible au front durant le debug (à retirer en prod si tu veux)
        return jsonify({"error": str(e)}), 400

# Vercel n'a pas besoin de if __name__ == "__main__"
