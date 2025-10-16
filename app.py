import os, json, time, secrets, re, logging
from pathlib import Path
from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
TENANTS_DIR = BASE_DIR / "tenants"
TENANTS_DIR.mkdir(exist_ok=True)
PROMPTS_PATH = BASE_DIR / "prompts.json"

BRAND            = os.getenv("BRAND_NAME", "Betty Bots")
PUBLIC_BASE_URL  = os.getenv("PUBLIC_BASE_URL", "http://localhost:5000")
SUPPORT_EMAIL    = os.getenv("SUPPORT_EMAIL", "support@spectramedia.ai")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL        = os.getenv("LLM_MODEL", "gpt-4o-mini")

app = Flask(__name__, static_folder="static", template_folder="templates")
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────
# Helpers
# ─────────────────────────────

def slugify(txt: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (txt or "").strip().lower()).strip("-")
    return s[:60] or f"user-{int(time.time())}"

def tenant_path(tenant_id: str) -> Path:
    return TENANTS_DIR / f"{tenant_id}.json"

def read_prompts() -> dict:
    if PROMPTS_PATH.exists():
        try:
            return json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "immobilier": "Tu es Betty Immo. Accueil pro, objectif : prise de contact / visite.",
        "danse": "Tu es Betty Danse. Accueil chaleureux, conversion essai / inscription.",
        "mecanique": "Tu es Betty Garage. Qualification + RDV atelier.",
        "nutrition": "Tu es Betty Nutrition. Conseils prudents, propose un suivi.",
        "avocat": "Tu es Betty Avocat (accueil). Qualifie, propose RDV, pas d'avis juridique."
    }

def read_tenant(tenant_id: str) -> dict:
    p = tenant_path(tenant_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_tenant(cfg: dict) -> str:
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
# Routes publiques : Landing → Signup → Dashboard → Chat
# ─────────────────────────────

@app.get("/")
def landing():
    prompts = read_prompts()
    return render_template("landing.html", brand=BRAND, prompts=list(prompts.keys()))

@app.post("/signup")
def signup():
    """Simule un abonnement à 9,99€ et crée un tenant minimal."""
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
    cfg = read_tenant(tenant_id)
    if not cfg:
        return ("Inconnu", 404)
    prompts = read_prompts()
    return render_template("dashboard.html", brand=BRAND, cfg=cfg, prompts=prompts)

@app.post("/api/tenant/<tenant_id>/update")
def api_update_tenant(tenant_id):
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
    cfg = read_tenant(tenant_id)
    if not cfg:
        return ("Inconnu", 404)
    if not (cfg.get("subscription") or {}).get("active", False):
        return (f"<h2>{BRAND}</h2><p>Abonnement inactif.</p>", 402)
    return render_template("chat.html", brand=BRAND, cfg=cfg, tenant_id=tenant_id)

@app.post("/api/chat/<tenant_id>")
def api_chat(tenant_id):
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
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        body = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg}
            ]
        }
        r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        reply = data["choices"][0]["message"]["content"].strip()
        return jsonify({"reply": reply})
    except Exception as e:
        app.logger.error(f"LLM error: {e}")
        return jsonify({"reply": "[fallback] Merci pour votre message. Nous revenons vers vous rapidement."})

@app.get("/tenants")
def list_tenants():
    files = sorted([p.name for p in TENANTS_DIR.glob("*.json")])
    return jsonify({"count": len(files), "files": files})

@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder, filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
