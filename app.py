from __future__ import annotations
import os, io, sqlite3, secrets, json, logging, re
from pathlib import Path
from typing import Optional
from contextlib import contextmanager
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    Response, send_from_directory, jsonify, g, make_response
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    current_user, login_required
)

try:
    import requests
except Exception:
    requests = None
import urllib.request
import stripe, yaml

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
def pick_db_path() -> Path:
    if os.getenv("DB_PATH"): return Path(os.getenv("DB_PATH"))
    if any(os.getenv(k) for k in ("VERCEL","VERCEL_URL","AWS_LAMBDA_FUNCTION_NAME")):
        return Path("/tmp/payments.db")
    return BASE_DIR / "payments.db"

DB_PATH = pick_db_path()
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))

# Stripe
def get_env_int(key: str, default: int) -> int:
    try: return int(os.getenv(key, str(default)))
    except: return default

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_CENTS = get_env_int("STRIPE_PRICE_CENTS", 999)
STRIPE_CURRENCY = os.getenv("STRIPE_CURRENCY", "eur")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or "http://localhost:5000"

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "root"

# ---------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------
@contextmanager
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
    yield g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT,
            metier TEXT,
            avatar_url TEXT,
            color_hex TEXT,
            shape TEXT,
            persona TEXT,
            welcome_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.commit()
init_db()

def db_one(sql, params=()):
    with get_db() as c:
        cur=c.execute(sql,params); return cur.fetchone()
def db_exec(sql, params=()):
    with get_db() as c:
        c.execute(sql,params); c.commit(); return True

# ---------------------------------------------------------------------
# UTIL
# ---------------------------------------------------------------------
class User(UserMixin):
    def __init__(self,id_,email): self.id=id_; self.email=email
@login_manager.user_loader
def load_user(user_id):
    row=db_one("SELECT * FROM users WHERE id=?", (user_id,))
    return User(row["id"],row["email"]) if row else None

def get_bot(user_id:int)->Optional[sqlite3.Row]:
    return db_one("SELECT * FROM bots WHERE user_id=?",(user_id,))

def sanitize_color(c:str)->str:
    c=(c or "").strip()
    if not c.startswith('#'): c='#'+c
    return c if len(c) in (4,7) else '#4F46E5'

def is_guest_user():
    if not current_user.is_authenticated: return True
    return str(current_user.email).endswith("@guest.local")

# ---------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------
@app.get("/")
def root():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    email=f"guest-{secrets.token_urlsafe(8)}@guest.local"
    db_exec("INSERT OR IGNORE INTO users(email) VALUES(?)",(email,))
    row=db_one("SELECT * FROM users WHERE email=?",(email,))
    login_user(User(row["id"],row["email"]))
    return redirect(url_for("dashboard"))

@app.route("/dashboard", methods=["GET","POST"])
@login_required
def dashboard():
    metiers=["Avocate","Agent Immo","Médecine","Comptable","Psychologue","Coiffeur","Coach sportif"]
    row=get_bot(int(current_user.id)); bot=dict(row) if row else None
    if request.method=="GET":
        cfg={
            "name":bot.get("name","Mon Betty Bot") if bot else "Mon Betty Bot",
            "color_hex":bot.get("color_hex","#4F46E5") if bot else "#4F46E5",
            "greeting":bot.get("welcome_text","Bonjour 👋") if bot else "Bonjour 👋",
            "avatar_url":bot.get("avatar_url","/avatar/agent_immo") if bot else "/avatar/agent_immo"
        }
        return render_template("dashboard.html",metiers=metiers,bot=bot,cfg=cfg)

    name=request.form.get("bot_name") or "Mon Betty Bot"
    metier=request.form.get("pack_slug","Agent Immo")
    color_hex=sanitize_color(request.form.get("color_hex") or "#4F46E5")
    welcome=request.form.get("greeting") or "Bonjour 👋"
    db_exec("""INSERT OR REPLACE INTO bots(user_id,name,metier,color_hex,welcome_text)
               VALUES(?,?,?,?,?)""",(current_user.id,name,metier,color_hex,welcome))
    flash("✅ Bot sauvegardé","success")
    return redirect(url_for("preview"))

@app.get("/preview")
@login_required
def preview():
    row=get_bot(int(current_user.id))
    if not row: return redirect(url_for("dashboard"))
    bot=dict(row)
    return render_template("preview.html",bot=bot)

# ---------------------------------------------------------------------
# SIGNUP / PAY / STRIPE
# ---------------------------------------------------------------------
@app.route("/signup", methods=["GET","POST"])
def signup():
    if request.method=="GET":
        return render_template("signup.html")
    email=(request.form.get("email") or "").strip().lower()
    email2=(request.form.get("email_confirm") or "").strip().lower()
    if email!=email2 or not email:
        flash("Email invalide","warning")
        return redirect(url_for("signup"))
    row=db_one("SELECT * FROM users WHERE email=?", (email,))
    if row:
        login_user(User(row["id"],row["email"]))
    else:
        db_exec("INSERT INTO users(email) VALUES(?)",(email,))
        row=db_one("SELECT * FROM users WHERE email=?",(email,))
        login_user(User(row["id"],row["email"]))
    flash("Compte créé","success")
    return redirect(url_for("pay"))

@app.get("/pay")
@login_required
def pay():
    row=get_bot(int(current_user.id)); bot=dict(row) if row else None
    return render_template("pay.html",bot=bot,stripe_enabled=bool(STRIPE_SECRET_KEY))

@app.post("/pay/stripe")
@login_required
def pay_stripe():
    if is_guest_user():
        flash("Créez votre compte avant de payer.","warning")
        return redirect(url_for("pay"))
    if not STRIPE_SECRET_KEY:
        flash("Paiement indisponible","error")
        return redirect(url_for("pay"))

    bot=dict(get_bot(int(current_user.id)) or {})
    metier=(bot.get("metier") or "Générique")
    product=f"Abonnement Betty {metier}"
    success_url=f"{PUBLIC_BASE_URL.rstrip('/')}/confirm?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url=f"{PUBLIC_BASE_URL.rstrip('/')}/pay"
    session=stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{
            "price_data":{
                "currency":STRIPE_CURRENCY,
                "recurring":{"interval":"month"},
                "unit_amount":STRIPE_PRICE_CENTS,
                "product_data":{"name":product}
            },
            "quantity":1
        }],
        success_url=success_url,
        cancel_url=cancel_url
    )
    return redirect(session.url,303)

# ---------------------------------------------------------------------
# CONFIRMATION APRES PAIEMENT
# ---------------------------------------------------------------------
@app.get("/confirm")
@login_required
def confirm():
    session_id=request.args.get("session_id")
    if not session_id:
        flash("Session introuvable","warning")
        return redirect(url_for("pay"))

    try:
        checkout=stripe.checkout.Session.retrieve(session_id,expand=["subscription","customer"])
        payment_status=(checkout.get("payment_status") or "").lower()
        sub=checkout.get("subscription")
        sub_id=getattr(sub,"id",None) if sub else None
        sub_status=getattr(sub,"status",None) if sub else None

        if sub_id:
            db_exec("UPDATE users SET stripe_subscription_id=? WHERE id=?",(sub_id,int(current_user.id)))

        row=get_bot(int(current_user.id)); bot=dict(row) if row else {}
        metier=bot.get("metier") or "Agent Immo"
        welcome_js=json.dumps(bot.get("welcome_text","Bonjour 👋"))
        base_js=json.dumps(PUBLIC_BASE_URL.rstrip("/"))
        pack_js=json.dumps(metier.lower().replace(" ","_")+"_pack")

        embed_code=f"""<div id="betty-widget" style="border:1px solid #e5e7eb;border-radius:12px;max-width:360px;padding:12px;font-family:system-ui,-apple-system,Segoe UI,Roboto">
  <div id="betty-thread" style="height:260px;overflow:auto;padding:8px;background:#f9fafb;border-radius:8px;margin-bottom:8px"></div>
  <form id="betty-form">
    <input id="betty-input" type="text" placeholder="Posez une question..." style="width:100%;padding:.6rem;border:1px solid #d1d5db;border-radius:8px">
  </form>
</div>
<script>
(function(){{
  const t=document.getElementById('betty-thread');const f=document.getElementById('betty-form');const i=document.getElementById('betty-input');
  function add(m,w){{const p=document.createElement('p');p.textContent=(w?w+': ':'')+m;t.appendChild(p);t.scrollTop=t.scrollHeight;}}
  add({welcome_js},"Betty");
  f.addEventListener('submit',async e=>{{e.preventDefault();const m=i.value.trim();if(!m)return;add(m,"Vous");i.value='';
    try{{const r=await fetch({base_js}+'/api/chat',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message:m,history:[],pack:{pack_js}}})}});
    const d=await r.json();add(d.reply||'…','Betty');}}catch(err){{add('Erreur de connexion.','Betty');}}}});
}})();
</script>"""

        flash("✅ Paiement confirmé","success")
        return render_template("confirm.html",embed_code=embed_code,
                               session_id=session_id,sub_id=sub_id,sub_status=sub_status,
                               payment_status=payment_status)
    except Exception as e:
        logger.error(e,exc_info=True)
        flash("Erreur confirmation","error")
        return redirect(url_for("pay"))

# ---------------------------------------------------------------------
# API CHAT
# ---------------------------------------------------------------------
@app.post("/api/chat")
def api_chat():
    data=request.get_json(force=True)
    msg=(data.get("message") or "").lower()
    if "bonjour" in msg: return jsonify({"reply":"Bonjour 👋","ask_lead":False})
    return jsonify({"reply":"Je vous écoute.","ask_lead":False})

# ---------------------------------------------------------------------
# RUN
# ---------------------------------------------------------------------
if __name__=="__main__":
    app.run(debug=True,host="0.0.0.0",port=5000)
