# betty_rules/memory.py
# Mémoire en RAM par tenant (remplaçable par Redis plus tard)

import time
from typing import Dict, Any

MEM: Dict[str, Dict[str, Any]] = {}
TTL = 3600  # 1h

def get_session(tenant: str) -> Dict[str, Any]:
    """Retourne (et rafraîchit) la session pour un tenant."""
    now = time.time()
    ses = MEM.get(tenant) or {"slots": {}, "state": "idle", "last": now}
    ses["last"] = now
    MEM[tenant] = ses
    # garbage collect très simple
    for k, v in list(MEM.items()):
        if now - v.get("last", now) > TTL:
            MEM.pop(k, None)
    return ses
