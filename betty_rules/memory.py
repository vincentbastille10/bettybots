# betty_rules/memory.py
# Session utilisateur PERSISTANTE via SQLite (partagée entre workers)
from __future__ import annotations
import os, json, time, sqlite3
from typing import Dict, Any

# même DB que app.py par défaut
def _db_path() -> str:
    # 1) respect d'une variable d'env éventuelle
    env = os.environ.get("DB_PATH") or os.environ.get("BETTY_DB_PATH")
    if env:
        return env
    # 2) sinon, fichier payments.sqlite3 à la racine du projet
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(root, "payments.sqlite3")

def _conn():
    p = _db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p)
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions(
            tenant TEXT PRIMARY KEY,
            slots  TEXT,
            state  TEXT,
            updated_at INTEGER
        )
    """)
    return c

def get_session(tenant: str) -> Dict[str, Any]:
    c = _conn()
    row = c.execute("SELECT slots, state FROM chat_sessions WHERE tenant=?", (tenant,)).fetchone()
    if not row:
        ses = {"slots": {}, "state": "idle"}
        c.execute(
            "INSERT OR REPLACE INTO chat_sessions(tenant, slots, state, updated_at) VALUES(?,?,?,?)",
            (tenant, json.dumps(ses["slots"]), ses["state"], int(time.time()))
        )
        c.commit(); c.close()
        return ses
    slots_json, state = row
    try:
        slots = json.loads(slots_json or "{}")
    except Exception:
        slots = {}
    c.close()
    return {"slots": slots, "state": state or "idle"}

def save_session(tenant: str, ses: Dict[str, Any]) -> None:
    c = _conn()
    c.execute(
        "INSERT OR REPLACE INTO chat_sessions(tenant, slots, state, updated_at) VALUES(?,?,?,?)",
        (tenant, json.dumps(ses.get("slots") or {}), ses.get("state") or "idle", int(time.time()))
    )
    c.commit(); c.close()
