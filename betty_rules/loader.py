# betty_rules/loader.py
# Chargement tolérant des packs YAML (FAQ + intents) pour chaque métier.

from __future__ import annotations
import os
import yaml
from typing import Dict, List, Optional

_cache: Dict[str, dict] = {}

# Rôle normalisé (dialog_manager._normalize_role) -> "base name" attendu
# On ne met PAS l'extension ici; on génère plusieurs variantes ensuite.
ROLE_TO_BASENAME = {
    "psychologue": "psychologue",
    "agent immobilier": "agent_immobilier",
    "avocat": "avocat",
    "medecin": "medecin",          # fichier préféré: medecin_pack.yaml
    "comptable": "comptable",
    "danse": "danse",
}

def _proj_root() -> str:
    here = os.path.dirname(__file__)
    return os.path.abspath(os.path.join(here, ".."))

def _candidate_dirs() -> List[str]:
    root = _proj_root()
    return [
        os.path.join(root, "templates", "packs"),
        os.path.join(root, "packs"),
        os.path.join(root, "static", "packs"),
    ]

def _candidate_filenames(key: str) -> List[str]:
    """
    key peut être un rôle normalisé ('agent immobilier') ou un nom de pack ('agent_immobilier').
    On renvoie une liste de noms de fichiers candidats (ordre de préférence).
    """
    key = (key or "").strip().lower()

    names: List[str] = []
    # 1) si c'est un rôle connu, partir de son "basename"
    base = ROLE_TO_BASENAME.get(key)
    if base:
        names += [f"{base}_pack.yaml", f"{base}.yaml"]
        # variantes tolérées
        if base == "medecin":
            names += ["medecine_pack.yaml", "medecine.yaml"]
        return names

    # 2) sinon: le key est probablement déjà un "basename" ou un nom de fichier
    #    enlever extension si fournie
    if key.endswith(".yaml"):
        names.append(key)
    else:
        names += [f"{key}_pack.yaml", f"{key}.yaml"]

    # si l'utilisateur écrit "médecin/medecine" directement
    if "medecine" in key and "medecine.yaml" not in names:
        names += ["medecine_pack.yaml", "medecine.yaml"]
    if "medecin" in key and "medecin.yaml" not in names:
        names += ["medecin_pack.yaml", "medecin.yaml"]

    # déduire éventuellement version underscore <-> espace
    if " " in key:
        names += [f"{key.replace(' ', '_')}_pack.yaml", f"{key.replace(' ', '_')}.yaml"]
    if "_" in key:
        names += [f"{key.replace('_', ' ')}_pack.yaml", f"{key.replace('_', ' ')}.yaml"]

    # supprimer doublons en préservant l'ordre
    seen = set()
    uniq: List[str] = []
    for n in names:
        if n not in seen:
            uniq.append(n); seen.add(n)
    return uniq

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
    Paramètre:
      - rôle normalisé (ex: "agent immobilier"), ou
      - nom de pack (ex: "agent_immobilier" / "agent_immobilier_pack.yaml")
    """
    key = (name_or_role or "").strip().lower()
    if key in _cache:
        return _cache[key]

    files = _candidate_filenames(key)
    dirs  = _candidate_dirs()

    for folder in dirs:
        for fname in files:
            path = os.path.join(folder, fname)
            if os.path.isfile(path):
                data = _try_open(path)
                _cache[key] = data or {}
                return _cache[key]

    print("[packs] introuvable pour:", key)
    print("        cherché fichiers:", ", ".join(files))
    print("        dans dossiers   :", " | ".join(dirs))
    _cache[key] = {}
    return _cache[key]

def clear_cache():
    _cache.clear()
