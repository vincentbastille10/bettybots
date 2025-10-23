from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Dict, List

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    session,
    url_for,
)
import yaml

# ---------------------------------------------------------------------
# Config de base
# ---------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
PACKS_DIR = TEMPLATES_DIR / "packs"

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET", "dev_key_change_me")

# Config par défaut du bot (si champs manquants)
DEFAULT_CFG: Dict[str, str] = {
    "name": "Mon Betty Bot",
    "pack_slug": "agent_immo",               # -> templates/packs/agent_immo.yaml
    "avatar_url": "",
    "color_hex": "#4F46E5",
    "persona": "Assistant",
    "window_size": "medium",                 # small | medium | large
    "tagline": "Bonjour 👋 Je peux vous aider ?",
}

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def get_cfg() -> Dict[str, str]:
    """Récupère la configuration en session, merge avec les valeurs par défaut."""
    stored = session.get("bot_cfg") or {}
    merged = {**DEFAULT_CFG, **{k: v for k, v in stored.items() if isinstance(v, str)}}
    return merged


def set_cfg(data: Dict[str, str]) -> None:
    """Enregistre la configuration en session."""
    session["bot_cfg"] = {**DEFAULT_CFG, **data}


def list_packs() -> List[str]:
    """Liste les packs YAML disponibles dans templates/packs/ (sans l’extension)."""
    if not PACKS_DIR.exists():
        return [DEFAULT_CFG["pack_slug"]]
    return sorted(p.stem for p in PACKS_DIR.glob("*.yaml"))


def load_pack(slug: str) -> Dict[str, Any]:
    """Charge un pack YAML (opening/fallback/rules)."""
    path = PACKS_DIR / f"{slug}.yaml"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def apply_rules(message: str, history: List[str], pack: Dict[str, Any], persona: str) -> Dict[str, Any]:
    """
    Demo logique:
      - si history est vide -> opening
      - sinon: parcourt rules[].contains (case-insensitive) pour répondre avec then/ask_lead
      - défaut -> fallback
      - préfixe par [Persona: ...]
    """
    opening = str(pack.get("opening") or "Bonjour 👋")
    fallback = str(pack.get("fallback") or "Je n’ai pas bien compris 🤔")
    rules = pack.get("rules") or []

    if not history:
        reply = opening
        ask_lead = False
    else:
        msg = (message or "").lower()
        reply = fallback
        ask_lead = False
        for rule in rules:
            contains = str(rule.get("contains") or "").lower()
            if contains and contains in msg:
                reply = str(rule.get("then") or fallback)
                ask_lead = bool(rule.get("ask_lead"))
                break

    return {"reply": f"[Persona: {persona}] {reply}", "ask_lead": ask_lead}


# ---------------------------------------------------------------------
# favicon (évite des 500 si pas de fichier)
# ---------------------------------------------------------------------
@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status=204)


# ---------------------------------------------------------------------
# /dashboard — config du bot (GET : affiche, POST : enregistre → /preview)
# ---------------------------------------------------------------------
@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        cfg = get_cfg()
        data = {
            "name": request.form.get("name", cfg["name"]).strip() or DEFAULT_CFG["name"],
            "pack_slug": request.form.get("pack_slug", cfg["pack_slug"]).strip() or DEFAULT_CFG["pack_slug"],
            "avatar_url": request.form.get("avatar_url", cfg["avatar_url"]).strip(),
            "color_hex": request.form.get("color_hex", cfg["color_hex"]).strip() or DEFAULT_CFG["color_hex"],
            "persona": request.form.get("persona", cfg["persona"]).strip() or DEFAULT_CFG["persona"],
            "window_size": request.form.get("window_size", cfg["window_size"]).strip() or DEFAULT_CFG["window_size"],
            "tagline": request.form.get("tagline", cfg["tagline"]).strip() or DEFAULT_CFG["tagline"],
        }
        set_cfg(data)
        return redirect(url_for("preview"))

    # GET
    cfg = get_cfg()
    packs = list_packs()
    # Rappels: tu as déjà ton templates/dashboard.html — on le réutilise.
    return render_template("dashboard.html", cfg=cfg, packs=packs)


# ---------------------------------------------------------------------
# /preview — page d’essai (utilise templates/preview.html)
# ---------------------------------------------------------------------
@app.get("/preview")
def preview():
    cfg = get_cfg()
    # Le template doit afficher le bot avec message d’accueil et 2 boutons:
    # - Retour à la configuration (/dashboard)
    # - Aller au paiement (/pay)
    return render_template("preview.html", bot=cfg, cfg=cfg)


# ---------------------------------------------------------------------
# /pay — placeholder → redirige vers /embed
# ---------------------------------------------------------------------
@app.get("/pay")
def pay():
    # Pas d’intégration Stripe ici pour ne rien casser : on bascule direct vers /embed.
    return redirect(url_for("embed"))


# ---------------------------------------------------------------------
# /embed — page d’intégration : affiche uniquement le snippet à copier
# ---------------------------------------------------------------------
@app.get("/embed")
def embed():
    cfg = get_cfg()
    snippet = render_template_string(
        """
<script src="https://cdn.example.com/bettybot.js"></script>
<script>
  BettyBot.mount("#betty", {
    name: "{{ bot.name | e }}",
    avatar: "{{ bot.avatar_url | e }}",
    color: "{{ bot.color_hex | e }}",
    persona: "{{ bot.persona | e }}",
    windowSize: "{{ bot.window_size | e }}",
    pack: "{{ bot.pack_slug | e }}",
    tagline: "{{ bot.tagline | e }}"
  });
</script>
<div id="betty"></div>
        """,
        bot=cfg,
    ).strip()

    # Page minimaliste qui ne montre QUE le snippet à copier-coller
    return render_template_string(
        """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Snippet d'intégration</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root{color-scheme:dark}
    body{margin:0;padding:40px;background:#0f1115;color:#e2e8f0;font-family:system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, 'Helvetica Neue', sans-serif;display:flex;justify-content:center}
    main{max-width:880px;width:100%}
    h1{font-size:22px;margin:0 0 14px 0}
    pre{background:#161a24;padding:20px;border-radius:14px;white-space:pre-wrap;word-break:break-word;border:1px solid #242a38}
    code{font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;font-size:13px;line-height:1.55}
    .hint{color:#94a3b8;margin:0 0 18px 0}
  </style>
</head>
<body>
  <main>
    <h1>Intégration du bot</h1>
    <p class="hint">Copiez-collez ce code sur votre site pour monter le bot.</p>
    <pre><code>{{ snippet }}</code></pre>
  </main>
</body>
</html>
        """,
        snippet=snippet,
    )


# ---------------------------------------------------------------------
# /api/chat — endpoint de test (lit pack YAML + personnalité)
# ---------------------------------------------------------------------
@app.post("/api/chat")
def api_chat():
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    history = payload.get("history")
    history = history if isinstance(history, list) else []

    cfg = get_cfg()
    pack = load_pack(cfg.get("pack_slug", DEFAULT_CFG["pack_slug"]))

    # Si aucune historique → message d’ouverture
    if not history:
        opening = str(pack.get("opening") or "Bonjour 👋")
        return jsonify({"reply": f"[Persona: {cfg['persona']}] {opening}", "ask_lead": False})

    result = apply_rules(message, history, pack, cfg.get("persona", DEFAULT_CFG["persona"]))
    return jsonify(result)


# ---------------------------------------------------------------------
# Exécution locale
# ---------------------------------------------------------------------
if __name__ == "__main__":
    # Debug = True en local ; en prod, le serveur (Render/Vercel) fournit son propre runner.
    app.run(debug=True)
