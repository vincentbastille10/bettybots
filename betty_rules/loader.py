# betty_rules/loader.py
import os, yaml

_cache = {}

# correspondance rôle → nom de fichier
ROLE_TO_FILE = {
    "psychologue": "psychologue_pack.yaml",
    "agent immobilier": "agent_immobilier_pack.yaml",
    "avocat": "avocat_pack.yaml",
    "medecin": "medecin_pack.yaml",
    "comptable": "comptable_pack.yaml",
    "danse": "danse_pack.yaml",
}

def _packs_dir() -> str:
    here = os.path.dirname(__file__)
    return os.path.abspath(os.path.join(here, "..", "packs"))

def load_pack(role_norm: str) -> dict:
    """Charge un pack YAML pour le rôle normalisé (ex: 'agent immobilier')."""
    if role_norm in _cache:
        return _cache[role_norm]

    packs = _packs_dir()
    fname = ROLE_TO_FILE.get(role_norm)
    if not fname:
        print(f"[packs] aucun mapping de fichier pour role='{role_norm}'")
        _cache[role_norm] = {}
        return _cache[role_norm]

    path = os.path.join(packs, fname)
    if not os.path.isfile(path):
        print(f"[packs] introuvable: {path}")
        _cache[role_norm] = {}
        return _cache[role_norm]

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            print(f"[packs] chargé: {os.path.basename(path)} (faqs={len(data.get('faqs') or [])}, intents={len(data.get('intents') or [])})")
            _cache[role_norm] = data
            return data
    except Exception as e:
        print(f"[packs] erreur lecture {path}: {e}")
        _cache[role_norm] = {}
        return _cache[role_norm]
