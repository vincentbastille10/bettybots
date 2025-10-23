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
    send_from_directory,
)
import yaml

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
PACKS_DIR = TEMPLATES_DIR / "packs"

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET", "dev_key")

DEFAULT_CFG: Dict[str, str] = {
    "name": "Mon Betty Bot",
    "pack_slug": "agent_immo",
    "avatar_url": "",
    "color_hex": "#4F46E5",
    "persona": "Assistant",
    "window_size": "medium",
    "tagline": "Bonjour 👋 Je peux vous aider ?",
}


def get_cfg() -> Dict[str, str]:
    stored = session.get("bot_cfg", {})
    merged = {**DEFAULT_CFG, **{k: v for k, v in stored.items() if isinstance(v, str)}}
    return merged


def set_cfg(data: Dict[str, str]) -> None:
    session["bot_cfg"] = {**DEFAULT_CFG, **data}


def list_packs() -> List[str]:
    if not PACKS_DIR.exists():
        return [DEFAULT_CFG["pack_slug"]]
    return sorted(p.stem for p in PACKS_DIR.glob("*.yaml"))


def load_pack(slug: str) -> Dict[str, Any]:
    pack_path = PACKS_DIR / f"{slug}.yaml"
    if not pack_path.exists():
        return {}
    with pack_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def apply_rules(message: str, history: List[str], pack: Dict[str, Any], persona: str) -> Dict[str, Any]:
    opening = str(pack.get("opening") or "Bonjour 👋")
    fallback = str(pack.get("fallback") or "Je n’ai pas bien compris 🤔")
    rules = pack.get("rules") or []

    ask_lead = False
    if not history:
        reply_text = opening
    else:
        lower_message = message.lower()
        reply_text = fallback
        for rule in rules:
            contains = str(rule.get("contains") or "").lower()
            if contains and contains in lower_message:
                reply_text = str(rule.get("then") or fallback)
                ask_lead = bool(rule.get("ask_lead"))
                break
    prefixed = f"[Persona: {persona}] {reply_text}"
    return {"reply": prefixed, "ask_lead": ask_lead}


@app.get("/favicon.ico")
def favicon() -> Response:
    icon_path = app.static_folder and Path(app.static_folder) / "favicon.ico"
    if icon_path and icon_path.exists():
        return send_from_directory(Path(app.static_folder), "favicon.ico")
    return Response(status=204)


@app.errorhandler(404)
def handle_not_found(error):
    if request.path == "/":
        return redirect(url_for("dashboard"))
    return error, 404


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        cfg = {k: request.form.get(k, DEFAULT_CFG[k]) or DEFAULT_CFG[k] for k in DEFAULT_CFG}
        set_cfg(cfg)
        return redirect(url_for("preview"))

    cfg = get_cfg()
    packs = list_packs()
    return render_template_string(
        """
        <!doctype html>
        <html lang=\"fr\">
        <head>
          <meta charset=\"utf-8\">
          <title>Configurer mon bot</title>
          <style>
            body{font-family:system-ui, sans-serif;background:#0f1115;color:#f1f5f9;margin:0;padding:40px;display:flex;justify-content:center}
            form{max-width:640px;width:100%;background:#181b22;padding:32px;border-radius:16px;box-shadow:0 14px 60px rgba(15,15,20,.35);display:grid;gap:18px}
            label{display:flex;flex-direction:column;font-size:14px;color:#cbd5f5;gap:8px}
            input,select,textarea{padding:12px;border-radius:10px;border:1px solid #2d3240;background:#0f1117;color:#f8fafc;font-size:15px}
            button{padding:14px;border:0;border-radius:12px;background:#4F46E5;color:white;font-weight:600;font-size:16px;cursor:pointer}
            h1{margin:0 0 10px 0;font-size:26px;color:white;text-align:center}
            p{margin:0;color:#94a3b8;text-align:center}
          </style>
        </head>
        <body>
          <form method=\"post\">
            <div>
              <h1>Louez votre Betty Bot</h1>
              <p>Configurez votre bot métier plug-and-play en quelques secondes.</p>
            </div>
            <label>Nom du bot
              <input name=\"name\" value=\"{{ cfg.name }}\" placeholder=\"Nom du bot\" required>
            </label>
            <label>Pack métier
              <select name=\"pack_slug\">
                {% for slug in packs %}
                <option value=\"{{ slug }}\" {% if slug == cfg.pack_slug %}selected{% endif %}>{{ slug }}</option>
                {% endfor %}
              </select>
            </label>
            <label>Avatar (URL)
              <input name=\"avatar_url\" value=\"{{ cfg.avatar_url }}\" placeholder=\"https://...\">
            </label>
            <label>Couleur principale (hexadécimal)
              <input name=\"color_hex\" value=\"{{ cfg.color_hex }}\" placeholder=\"#4F46E5\">
            </label>
            <label>Personnalité
              <input name=\"persona\" value=\"{{ cfg.persona }}\" placeholder=\"Assistant\">
            </label>
            <label>Taille de fenêtre
              <select name=\"window_size\">
                {% for size in ["small","medium","large"] %}
                <option value=\"{{ size }}\" {% if size == cfg.window_size %}selected{% endif %}>{{ size }}</option>
                {% endfor %}
              </select>
            </label>
            <label>Phrase d'accroche
              <textarea name=\"tagline\" rows=\"3\">{{ cfg.tagline }}</textarea>
            </label>
            <button type=\"submit\">Enregistrer et prévisualiser →</button>
          </form>
        </body>
        </html>
        """,
        cfg=cfg,
        packs=packs,
    )


@app.get("/preview")
def preview():
    cfg = get_cfg()
    avatar_fallback = url_for("static", filename="img/betty_avatar1.png") if app.static_folder else ""
    avatar_url = cfg.get("avatar_url") or avatar_fallback
    return render_template(
        "preview.html",
        bot=cfg,
        cfg={**cfg, "avatar_url": avatar_url, "shape": "rounded", "slug": cfg.get("pack_slug", "")},
        pack_slug=cfg.get("pack_slug"),
        avatar_url=avatar_url,
    )


@app.post("/api/chat")
def api_chat():
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    history_raw = payload.get("history")
    history = history_raw if isinstance(history_raw, list) else []

    if not message and history:
        return jsonify({"reply": "", "ask_lead": False})

    cfg = get_cfg()
    pack = load_pack(cfg.get("pack_slug", DEFAULT_CFG["pack_slug"]))
    result = apply_rules(message, history, pack, cfg.get("persona", DEFAULT_CFG["persona"]))
    return jsonify(result)


@app.get("/pay")
def pay():
    return redirect(url_for("embed"))


@app.get("/embed")
def embed():
    cfg = get_cfg()
    snippet = render_template_string(
        """
<script src=\"https://cdn.example.com/bettybot.js\"></script>
<script>
  BettyBot.mount(\"#betty\", {
    name: \"{{ bot.name | e }}\",
    avatar: \"{{ bot.avatar_url | e }}\",
    color: \"{{ bot.color_hex | e }}\",
    persona: \"{{ bot.persona | e }}\",
    windowSize: \"{{ bot.window_size | e }}\",
    pack: \"{{ bot.pack_slug | e }}\",
    tagline: \"{{ bot.tagline | e }}\"
  });
</script>
<div id=\"betty\"></div>
        """,
        bot=cfg,
    )
    return render_template_string(
        """
        <!doctype html>
        <html lang=\"fr\">
        <head>
          <meta charset=\"utf-8\">
          <title>Snippet d'intégration</title>
          <style>
            body{margin:0;padding:40px;background:#0f1115;color:#e2e8f0;font-family:system-ui, sans-serif;display:flex;justify-content:center}
            pre{background:#1a1f2b;padding:24px;border-radius:16px;max-width:720px;width:100%;white-space:pre-wrap}
            code{font-family:"SFMono-Regular",Consolas,monospace;font-size:14px;line-height:1.6;color:#f8fafc}
          </style>
        </head>
        <body>
          <pre><code>{{ snippet }}</code></pre>
        </body>
        </html>
        """,
        snippet=snippet.strip(),
    )


if __name__ == "__main__":
    app.run(debug=False)
