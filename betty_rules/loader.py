# betty_rules/loader.py
# Charge les packs YAML en cherchant dans plusieurs emplacements possibles.
# Priorité : ./templates/packs  -> ./packs  -> ./static/packs
#
# Utilisation :
#   from betty_rules.loader import load_pack
#   data = load_pack("agent immobilier")   # via mapping ROLE_TO_FILE
#   data = load_pack("agent_immobilier")   # ou nom direct (sans extension)

import os
import yaml
from typing import Dict, Optional

_cache: Dict[str, dict] = {}

# Mapping "rôle normalisé" -> nom de fichier pack
# (le rôle est normalisé par dialog_manager._normalize_role)
ROLE_TO_FILE = {
    "psychologue": "psychologue_pack.yaml",
    "agent immobilier": "agent_immobilier_pack.yaml",
    "avocat": "avocat_pack.yaml",
    "medecin": "medecine_pack.yaml",
    "comptable": "comptable_pack.yaml",
    "danse": "danse_pack.yaml",
}

def _proj_root() -> str:
    """
    betty_rules/loader.py -> parent (racine du projet).
    """
    here = os.path.dirname(__file__)
    return os.path.abspath(os.path.join(here, ".."))

def _candidate_dirs() -> list:
    """
    Emplacements possibles des packs (dans cet ordre de priorité).
    """
    root = _proj_root()
    return [
        os.path.join(root, "templates", "packs"),
        os.path.join(root, "packs"),
        os.path.join(root, "static", "packs"),
    ]

def _resolve_filename(name_or_role: str) -> list:
    """
    Produit une liste de noms de fichiers possibles pour un rôle/nom donné.
    - Si c'est un rôle connu: utilise ROLE_TO_FILE[rôle]
    - Sinon, essaie plusieurs variantes: xxx_pack.yaml, xxx.yaml
    """
    # 1) role -> fichier
    mapped = ROLE_TO_FILE.get(name_or_role)
    if mapped:
        return [mapped]

    base = name_or_role.strip()
    # retirer éventuelle extension déjà fournie
    if base.endswith(".yaml"):
        return [base]

    # variantes tolérantes
    return [f"{base}_pack.yaml", f"{base}.yaml"]

def _try_open(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            print(f"[packs] chargé: {os.path.basename(path)} "
                  f"(faqs={len(data.get('faqs') or [])}, intents={len(data.get('intents') or [])})")
            return data
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[packs] erreur lecture {path}: {e}")
        return {}

def load_pack(name_or_role: str) -> dict:
    """
    Charge un pack YAML en cherchant dans:
      - ./templates/packs
      - ./packs
      - ./static/packs
    Le paramètre peut être un rôle ("agent immobilier") ou un nom de pack ("agent_immobilier").
    """
    key = name_or_role.strip().lower()
    if key in _cache:
        return _cache[key]

    candidates = _resolve_filename(key)
    for folder in _candidate_dirs():
        for fname in candidates:
            path = os.path.join(folder, fname)
            if os.path.isfile(path):
                data = _try_open(path)
                _cache[key] = data or {}
                return _cache[key]

    # rien trouvé -> log utile
    print("[packs] introuvable pour:", key)
    print("        cherché fichiers:", ", ".join(candidates))
    print("        dans dossiers   :", " | ".join(_candidate_dirs()))
    _cache[key] = {}
    return _cache[key]

def clear_cache():
    _cache.clear()
