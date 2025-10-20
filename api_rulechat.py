
from flask import Blueprint, request, jsonify
from betty_rules import load_pack, Memory
from betty_rules.dialog_manager import DialogManager

bp = Blueprint("rulechat", __name__)

_mem = Memory()
_loaded = {}

def _get_pack(name: str):
    if name not in _loaded:
        _loaded[name] = load_pack(name)
    return _loaded[name]

@bp.route("/api/rulechat", methods=["POST"])
def rulechat():
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id") or request.remote_addr or "anon"
    text = data.get("text", "").strip()
    domain = data.get("domain", "avocate_pack")
    if not text:
        return jsonify({"error": "missing text"}), 400
    pack = _get_pack(domain)
    dm = DialogManager(_mem, pack)
    # slot injection if provided (e.g., from frontend forms)
    slot = data.get("slot"); value = data.get("value")
    if slot and value:
        dm.inject_slot(session_id, slot, value)
    out = dm.step(session_id, text)
    return jsonify({"ok": True, "response": out})
