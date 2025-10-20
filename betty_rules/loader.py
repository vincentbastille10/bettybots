import os
import yaml

# Cache mémoire simple
_cache = {}

# Normalisation souple des rôles → nom de fichier
# (tu peux compléter librement)
ROLE_TO_FILE = {
    "psychologue": "psychologue_pack",
    "agent immobilier": "agent_immobilier_pack",
    "agent-immobilier": "agent_immobilier_pack",
    "agent_immo": "agent_immobilier_pack",
    "avocat": "avocat_pack",
    "avocate": "avocat_pack",
    "medecin": "medecin_pack",
    "médecin": "medecin_pack",
    "comptable": "comptable_pack",
    "danse": "danse_pack",
    "danse (assistant·e)": "danse_pack",
    "danse (assistante)": "danse_pack",
    "danse (assistant)": "danse_pack",
}

def _normalize(s: str) -> str:
    s = (s or "").strip().lower()
    # variantes fréquentes
    s = s.replace(" / ", " ").replace("/", " ").replace("_", " ").replace("-", " ")
    s = s.replace("avocat avocate", "avocat")
    s = s.replace("agent immo", "agent immobilier")
    s = s.replace("médecin", "medecin")
    s = " ".join(s.split())  # compact
    return s

def _resolve_pack_name(name_or_role: str) -> str:
    """
    Accepte:
      - un rôle: "Avocat / Avocate", "agent-immobilier", "médecin"...
      - un nom de pack: "avocat_pack", "medecin_pack"
      - un chemin absolu/relatif se terminant par .yaml
    Retourne un nom de pack SANS extension (ou un chemin .yaml si fourni).
    """
    s = (name_or_role or "").strip()
    if not s:
        return "default_pack"

    # Si on nous donne déjà un .yaml → on le renvoie tel quel (chemin)
    if s.endswith(".yaml"):
        return s

    base = _normalize(s)

    # Si l'appelant fournit déjà *_pack → on le garde
    if base.endswith(" pack"):
        return base.replace(" ", "_")

    # Mapping rôle → fichier
    if base in ROLE_TO_FILE:
        return ROLE_TO_FILE[base]

    # Fallback: on tente "<role>_pack"
    return f"{base.replace(' ', '_')}_pack"

def load_pack(name_or_role: str) -> dict:
    """
    Charge un pack YAML en fonction d'un rôle ou d'un nom de pack.
    - Rôle: "avocat", "agent immobilier", "medecin"...
    - Pack: "avocat_pack", "avocat_pack.yaml" ou chemin absolu .yaml
    """
    key = _resolve_pack_name(name_or_role)

    # Cache
    if key in _cache:
        return _cache[key]

    # Résolution du chemin
    here = os.path.dirname(__file__)
    # Si on a un chemin/nom se terminant par .yaml → on respecte
    if key.endswith(".yaml"):
        path = key if os.path.isabs(key) else os.path.abspath(os.path.join(here, "..", "packs", key))
    else:
        # sinon on ajoute l'extension et le dossier packs/
        path = os.path.abspath(os.path.join(here, "..", "packs", f"{key}.yaml"))

    # Lecture
    if not os.path.exists(path):
        # Pack absent → renvoyer une structure vide mais valide
        data = {"faqs": [], "intents": [], "lead_form": []}
        _cache[key] = data
        return data

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    _cache[key] = data
    return data
