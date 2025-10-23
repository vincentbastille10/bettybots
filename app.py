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

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
PACKS_DIR = TEMPLATES_DIR / "packs"

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET", "dev_secret_change_me")

DEFAULT_GREETING = "Bonjour, je suis Betty, votre assistante AI. Que puis-je faire pour vous aujourd’hui ?"
DEFAULT_PACK = "agent_immobilier"

DEFAULT_CFG: Dict[str, Any] = {
    "name": "Mon Betty Bot",
    "avatar_key": 0,
    "color_hex": "#4F46E5",
    "persona": "neutre",
    "widget_size": "m",
    "window_size": "m",
    "pack_slug": DEFAULT_PACK,
    "slug": DEFAULT_PACK,
    "greeting": DEFAULT_GREETING,
    "tagline": DEFAULT_GREETING,
}


def _ensure_cfg_aliases(cfg: Dict[str, Any]) -> Dict[str, Any]:
    slug = str(cfg.get("pack_slug") or cfg.get("slug") or DEFAULT_PACK)
    greeting = str(cfg.get("greeting") or cfg.get("tagline") or DEFAULT_GREETING)
    widget_size = str(cfg.get("widget_size") or cfg.get("window_size") or "m")
    cfg["pack_slug"] = slug
    cfg["slug"] = slug
    cfg["greeting"] = greeting
    cfg["tagline"] = greeting
    cfg["widget_size"] = widget_size
    cfg["window_size"] = widget_size
    try:
        cfg["avatar_key"] = max(0, min(2, int(cfg.get("avatar_key", 0))))
    except (TypeError, ValueError):
        cfg["avatar_key"] = 0
    return cfg


def get_cfg() -> Dict[str, Any]:
    stored = session.get("bot_cfg")
    cfg: Dict[str, Any] = dict(DEFAULT_CFG)
    if isinstance(stored, dict):
        cfg.update(stored)
    return _ensure_cfg_aliases(cfg)


def set_cfg(data: Dict[str, Any]) -> None:
    cfg = dict(DEFAULT_CFG)
    cfg.update(data)
    session["bot_cfg"] = _ensure_cfg_aliases(cfg)


def sanitize_choice(value: str, allowed: List[str], default: str) -> str:
    if value in allowed:
        return value
    return default


def sanitize_color(value: str, default: str) -> str:
    if isinstance(value, str) and value.startswith("#") and 4 <= len(value) <= 7:
        return value
    return default


def sanitize_text(value: str, default: str) -> str:
    text = (value or "").strip()
    return text if text else default


def load_pack(slug: str) -> Dict[str, Any]:
    slug = sanitize_text(slug, DEFAULT_PACK)
    path = PACKS_DIR / f"{slug}.yaml"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def apply_rules(message: str, pack: Dict[str, Any]) -> Dict[str, Any]:
    rules = pack.get("rules") if isinstance(pack, dict) else None
    rules = rules if isinstance(rules, list) else []
    fallback = str(pack.get("fallback") or "Je n’ai pas bien compris 🤔")
    text = (message or "").lower()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        contains = str(rule.get("contains") or "").lower()
        if contains and contains in text:
            reply = str(rule.get("then") or fallback)
            ask_lead = bool(rule.get("ask_lead"))
            return {"reply": reply, "ask_lead": ask_lead}
    return {"reply": fallback, "ask_lead": False}


@app.get("/")
def root() -> Response:
    return redirect(url_for("dashboard"))


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status=204)


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        form = request.form
        cfg = get_cfg()
        avatar_key = 0
        try:
            avatar_key = int(form.get("avatar_key", cfg["avatar_key"]))
        except (TypeError, ValueError):
            avatar_key = cfg["avatar_key"]
        avatar_key = max(0, min(2, avatar_key))

        widget_size = sanitize_choice(
            form.get("widget_size", cfg["widget_size"]),
            ["s", "m", "l"],
            cfg["widget_size"],
        )
        persona = sanitize_choice(
            form.get("persona", cfg["persona"]),
            ["neutre", "chaleureuse", "directe", "experte"],
            cfg["persona"],
        )
        pack_slug = sanitize_choice(
            form.get("pack_slug", cfg["pack_slug"]),
            [
                "agent_immobilier",
                "avocat",
                "medecin",
                "coiffeur",
                "coach_sportif",
            ],
            cfg["pack_slug"],
        )

        updated = {
            "name": sanitize_text(form.get("bot_name"), cfg["name"]),
            "avatar_key": avatar_key,
            "color_hex": sanitize_color(form.get("color_hex"), cfg["color_hex"]),
            "persona": persona,
            "widget_size": widget_size,
            "window_size": widget_size,
            "pack_slug": pack_slug,
            "slug": pack_slug,
            "greeting": sanitize_text(form.get("greeting"), cfg["greeting"]),
        }
        updated["tagline"] = updated["greeting"]
        set_cfg(updated)
        return redirect(url_for("preview"))

    cfg = get_cfg()
    return render_template("dashboard.html", cfg=cfg)


@app.get("/preview")
def preview():
    cfg = get_cfg()
    return render_template("preview.html", cfg=cfg, bot=cfg)


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
  BettyBot.mount('#betty', {
    name: "{{ cfg.name | e }}",
    avatarKey: {{ cfg.avatar_key | int }},
    color: "{{ cfg.color_hex | e }}",
    persona: "{{ cfg.persona | e }}",
    widgetSize: "{{ cfg.widget_size | e }}",
    pack: "{{ cfg.pack_slug | e }}",
    greeting: "{{ cfg.greeting | e }}"
  });
</script>
<div id=\"betty\"></div>
        """.strip(),
        cfg=cfg,
    )

    return render_template_string(
        """
<!doctype html>
<html lang=\"fr\">
<head>
  <meta charset=\"utf-8\">
  <title>Snippet d'intégration</title>
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <style>
    :root { color-scheme: dark; }
    body {
      margin: 0;
      padding: 40px;
      background: #0f1115;
      color: #e2e8f0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, 'Helvetica Neue', sans-serif;
      display: flex;
      justify-content: center;
    }
    main { max-width: 880px; width: 100%; }
    h1 { font-size: 22px; margin: 0 0 14px 0; }
    pre {
      background: #161a24;
      padding: 20px;
      border-radius: 14px;
      white-space: pre-wrap;
      word-break: break-word;
      border: 1px solid #242a38;
    }
    code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;
      font-size: 13px;
      line-height: 1.55;
    }
    .hint { color: #94a3b8; margin: 0 0 18px 0; }
  </style>
</head>
<body>
  <main>
    <h1>Intégration du bot</h1>
    <p class=\"hint\">Copiez-collez ce code sur votre site pour monter le bot.</p>
    <pre><code>{{ snippet }}</code></pre>
  </main>
</body>
</html>
        """,
        snippet=snippet,
    )


@app.post("/api/chat")
def api_chat():
    payload = request.get_json(silent=True) or {}
    history = payload.get("history")
    if not isinstance(history, list):
        history = []
    message = sanitize_text(payload.get("message"), "")

    cfg = get_cfg()
    pack = load_pack(cfg.get("pack_slug", DEFAULT_PACK))

    if not history:
        opening = str(pack.get("opening") or "Bonjour 👋")
        reply = f"[Persona: {cfg['persona']}] {opening}"
        return jsonify({"reply": reply, "ask_lead": False})

    outcome = apply_rules(message, pack)
    reply_text = f"[Persona: {cfg['persona']}] {outcome['reply']}"
    return jsonify({"reply": reply_text, "ask_lead": bool(outcome.get("ask_lead"))})


if __name__ == "__main__":
    app.run(debug=True)
