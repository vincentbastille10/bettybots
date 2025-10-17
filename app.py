import os, json, time, secrets, re, logging, datetime, smtplib
from pathlib import Path
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory

# ─────────────────────────────
# Initialisation
# ─────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
TENANTS_DIR = BASE_DIR / "tenants"
TENANTS_DIR.mkdir(exist_ok=True)
PROMPTS_PATH = BASE_DIR / "prompts.json"
LEADS_DIR = BASE_DIR / "leads"
LEADS_DIR.mkdir(exist_ok=True)

# Variables d'environnement
BRAND           = os.getenv("BRAND_NAME", "Betty Bots")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:5000")
SUPPORT_EMAIL   = os.getenv("SUPPORT_EMAIL", "support@spectramedia.ai")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL       = os.getenv("LLM_MODEL", "gpt-4o-mini")
SMTP_USER       = os.getenv("SMTP_USER", "vinylestorefrance@gmail.com")
SMTP_PASS       = os.getenv("SMTP_PASS", "")

app = Flask(__name__, static_folder="static", template_folder="templates")
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────
# Helpers
# ─────────────────────────────
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _now_iso() -> str:
    """Retourne l’heure UTC ISO 8601."""
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()

def slugify(txt: str) -> str:
    """Crée un slug à partir du texte (max 60 caractères)."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (txt or "").strip().lower()).strip("-")
    return s[:60] or f"user-{int(time.time())}"

def tenant_path(tenant_id: str) -> Path:
    return TENANTS_DIR / f"{tenant_id}.json"

def lead_path(tenant_id: str) -> Path:
    safe = re.sub(r"[^a-z0-9\-]+", "-", tenant_id.lower())
    return LEADS_DIR / f"{safe}.jsonl"

def append_jsonl(path: Path, row: dict) -> None:
    """Ajoute une ligne JSON dans un fichier .jsonl."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

def read_prompts() -> dict:
    """Lit prompts.json ou renvoie un dictionnaire de secours."""
    if PROMPTS_PATH.exists():
        try:
            return json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Valeurs par défaut utilisées si le fichier est absent ou illisible
    return {
        "immobilier": "Tu es Betty Immo. Accueil pro, objectif : prise de contact / visite.",
        "danse": "Tu es Betty Danse. Accueil chaleureux, conversion essai / inscription.",
        "mecanique": "Tu es Betty Garage. Qualification + RDV atelier.",
        "nutrition": "Tu es Betty Nutrition. Conseils prudents, propose un suivi.",
        "avocat": "Tu es Betty Avocat (accueil). Qualifie, propose RDV, pas d'avis juridique."
    }

def read_tenant(tenant_id: str) -> dict:
    """Charge la configuration d’un tenant."""
    p = tenant_path(tenant_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_tenant(cfg: dict) -> str:
    """Enregistre (ou met à jour) un fichier tenant et renvoie l’ID."""
    tid = cfg.get("tenant_id") or f"{slugify(cfg.get('email','')) or 'client'}-{secrets.token_hex(3)}"
    p = tenant_path(tid)
    base = {}
    if p.exists():
        try:
            base = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            base = {}
    base.update(cfg)
    base["tenant_id"] = tid
    p.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    return tid

# ─────────────────────────────
# ROUTES PRINCIPALES
# ─────────────────────────────
@app.get("/")
def landing():
    """Page d’accueil (landing) avec liste des profils disponibles."""
    prompts = read_prompts()
    # On passe uniquement la marque et la liste des clés
    return render_template(
        "landing.html",
        brand=BRAND,
        prompts=list(prompts.keys())
    )

@app.post("/signup")
def signup():
    """Création d’un bot + enregistrement client après la landing."""
    email = (request.form.get("email") or "").strip().lower()
    full_name = (request.form.get("full_name") or "Client").strip()
    profile = (request.form.get("profile") or "immobilier").strip()

    tid = save_tenant({
        "tenant_id": slugify(email) if email else None,
        "email": email or "",
        "full_name": full_name or "Client",
        "brand": BRAND,
        "subscription": {"active": True, "plan": "starter", "price": 9.99},
        "prompt_profile": profile,
        "prompt_custom": "",
        "welcome": "👋 Bonjour, je suis Betty. Comment puis-je vous aider ?",
        "avatar_url": "",
        "accent": "#7aa2ff"
    })
    return redirect(url_for("dashboard", tenant_id=tid))

@app.get("/dashboard/<tenant_id>")
def dashboard(tenant_id):
    """Affiche le tableau de bord du tenant."""
    cfg = read_tenant(tenant_id)
    if not cfg:
        return ("Inconnu", 404)
    prompts = read_prompts()
    # On injecte PUBLIC_BASE_URL pour que le script d’intégration soit absolu
    return render_template(
        "dashboard.html",
        brand=BRAND,
        cfg=cfg,
        prompts=prompts,
        PUBLIC_BASE_URL=PUBLIC_BASE_URL.rstrip("/")
    )

@app.post("/api/tenant/<tenant_id>/update")
def api_update_tenant(tenant_id):
    """Met à jour les réglages du bot (profil, message d’accueil, etc.)."""
    cfg = read_tenant(tenant_id)
    if not cfg:
        return jsonify({"ok": False, "error": "unknown tenant"}), 404

    data = request.get_json(silent=True) or {}
    for k in ["prompt_profile", "prompt_custom", "welcome", "avatar_url", "accent"]:
        if k in data:
            cfg[k] = (data[k] or "").strip()
    save_tenant(cfg)
    return jsonify({"ok": True, "tenant_id": tenant_id})

@app.get("/t/<tenant_id>")
def chat_ui(tenant_id):
    """Interface client du chat."""
    cfg = read_tenant(tenant_id)
    if not cfg:
        return ("Inconnu", 404)
    if not (cfg.get("subscription") or {}).get("active", False):
        return (f"<h2>{BRAND}</h2><p>Abonnement inactif.</p>", 402)
    return render_template("chat.html", brand=BRAND, cfg=cfg, tenant_id=tenant_id)

# ─────────────────────────────
# CHAT LLM
# ─────────────────────────────
@app.post("/api/chat/<tenant_id>")
def api_chat(tenant_id):
    """Interagit avec l’API OpenAI pour répondre aux utilisateurs."""
    cfg = read_tenant(tenant_id)
    if not cfg:
        return jsonify({"error": "unknown tenant"}), 404
    if not (cfg.get("subscription") or {}).get("active", False):
        return jsonify({"error": "subscription_inactive", "reply": "Abonnement inactif."}), 402

    user_msg = (request.json or {}).get("message", "")
    prompts = read_prompts()
    prof_key = cfg.get("prompt_profile") or "immobilier"
    system_prompt = prompts.get(prof_key, prompts.get("immobilier", ""))
    if cfg.get("prompt_custom"):
        system_prompt += f"\n\nContrainte additionnelle : {cfg['prompt_custom']}"

    if not OPENAI_API_KEY:
        return jsonify({"reply": f"[Démo] {cfg.get('welcome') or 'Bonjour.'} (LLM non configuré)."})

    try:
        import requests
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        body = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg}
            ]
        }
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers=headers, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        reply = data["choices"][0]["message"]["content"].strip()
        return jsonify({"reply": reply})
    except Exception as e:
        app.logger.error(f"LLM error: {e}")
        return jsonify({"reply": "[fallback] Merci pour votre message. Nous revenons vers vous rapidement."})

# ─────────────────────────────
# LEADS : envoi au client + stockage local
# ─────────────────────────────
@app.post("/api/lead/<tenant_id>")
def api_store_lead(tenant_id):
    """Enregistre un prospect et envoie un mail au propriétaire."""
    payload = request.get_json(silent=True) or {}
    nom    = (payload.get("nom") or "").strip()
    email  = (payload.get("email") or "").strip().lower()
    besoin = (payload.get("besoin") or "").strip()

    if not nom:
        return jsonify(ok=False, error="nom manquant"), 400
    if not EMAIL_RE.match(email):
        return jsonify(ok=False, error="email invalide"), 400
    if not besoin:
        return jsonify(ok=False, error="besoin manquant"), 400

    meta = {
        "ts": _now_iso(),
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "ua": request.headers.get("User-Agent", "-"),
        "tenant_id": tenant_id,
    }
    row = {"nom": nom, "email": email, "besoin": besoin, **meta}
    append_jsonl(lead_path(tenant_id), row)

    cfg = read_tenant(tenant_id) or {}
    client_email = cfg.get("email", "").strip()
    leads = cfg.get("leads", [])
    leads.append({"nom": nom, "email": email, "besoin": besoin, "ts": meta["ts"]})
    cfg["leads"] = leads[-100:]
    save_tenant(cfg)

    # Envoi par mail au client (le propriétaire du bot)
    if client_email and EMAIL_RE.match(client_email):
        try:
            subject = f"🎯 Nouveau lead capté par votre bot Betty ({cfg.get('prompt_profile','bot')})"
            body = f"""
Bonjour {cfg.get('full_name','')},

Votre bot Betty vient de capturer un nouveau contact :

👤 Nom : {nom}
📧 Email : {email}
💬 Besoin : {besoin}

Conservez bien ce mail : ce contact vous appartient exclusivement.

—
Spectra Media / {BRAND}
"""
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = f"{BRAND} <no-reply@spectramedia.ai>"
            msg["To"] = client_email

            with smtplib.SMTP("smtp.gmail.com", 587) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
            app.logger.info(f"Lead envoyé à {client_email}")
        except Exception as e:
            app.logger.error(f"Erreur envoi lead: {e}")

    return jsonify(ok=True)

@app.get("/leads/<tenant_id>")
def api_list_leads(tenant_id):
    """Retourne la liste des leads pour un tenant."""
    path = lead_path(tenant_id)
    out = []
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    out.append(json.loads(line))
    return jsonify(count=len(out), leads=out)

# ─────────────────────────────
# UTILS
# ─────────────────────────────
@app.get("/tenants")
def list_tenants():
    files = sorted([p.name for p in TENANTS_DIR.glob("*.json")])
    return jsonify({"count": len(files), "files": files})

@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder, filename)

# ─────────────────────────────
# MAIN
# ─────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
