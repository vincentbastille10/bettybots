# betty_rules/dialog_manager.py
# Orchestrateur "rule-based" pour Betty (sans LLM)
from __future__ import annotations

from typing import Dict, Any, List
import re

from .memory import get_session
from .loader import load_pack
from .nlu_rules import best_match, detect_intent
from .templates_engine import render

# ------------------------------------------------------------
# Détection simple des coordonnées / nom
# ------------------------------------------------------------
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:(?:\+|00)33|0)\s*[1-9](?:[\s\.\-]*\d{2}){4}")
# Nom stricte (2 mots ou plus, lettres/accents/apostrophes/traits)
NAME_STRICT_RE  = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ' -]{2,}\s+[A-Za-zÀ-ÖØ-öø-ÿ' -]{2,}$")
# Fallback : tokenise tous mots alphabétiques (au moins 2 lettres)
NAME_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ'-]{2,}")

def _autocapture_slots(ses: Dict[str, Any], text: str) -> None:
    """Tente de remplir email / téléphone / nom à partir du texte libre."""
    t = (text or "").strip()

    # email
    if not ses["slots"].get("email"):
        m = EMAIL_RE.search(t)
        if m:
            ses["slots"]["email"] = m.group(0)

    # téléphone FR basique
    if not ses["slots"].get("telephone"):
        # remplace les espaces "fins" éventuels avant matching
        flat = t.replace("\u202f", " ").strip()
        m = PHONE_RE.search(flat)
        if m:
            ses["slots"]["telephone"] = m.group(0)

    # nom / prénom
    if not ses["slots"].get("nom"):
        line = " ".join(t.split())
        # 1) essai strict
        if NAME_STRICT_RE.match(line):
            ses["slots"]["nom"] = line
        else:
            # 2) fallback tolérant : prends les 2 premiers tokens alphabétiques
            tokens = NAME_TOKEN_RE.findall(line)
            if len(tokens) >= 2:
                ses["slots"]["nom"] = f"{tokens[0]} {tokens[1]}"

# ------------------------------------------------------------
# Paramètres par défaut
# ------------------------------------------------------------
DEFAULT_LEAD_SLOTS: List[str] = ["nom", "email", "telephone", "besoin", "budget", "delai"]

LEAD_ASK = {
    "nom": "Pour commencer, quel est votre nom et prénom ?",
    "email": "Merci. Quelle est votre adresse e-mail pour vous recontacter ?",
    "telephone": "Souhaitez-vous laisser un numéro de téléphone pour un rappel ?",
    "besoin": "Pouvez-vous résumer votre besoin en quelques mots ?",
    "budget": "Avez-vous un budget indicatif ?",
    "delai": "Quel est votre délai idéal ?",
}

def _ask_for(slot: str) -> str:
    return LEAD_ASK.get(slot, "D’accord, j’ai besoin d’une information supplémentaire.")

def _next_missing(slots: Dict[str, Any], order: List[str]) -> str | None:
    for k in order:
        v = slots.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            return k
    return None

def _normalize_role(role: str) -> str:
    """Normalise les variantes pour mapper sur les fichiers YAML (voir loader)."""
    s = (role or "").strip().lower()
    s = s.replace(" / ", " ").replace("/", " ").replace("_", " ").replace("-", " ")
    s = s.replace("médecin", "medecin")
    s = s.replace("avocat / avocate", "avocat")
    s = s.replace("agent immo", "agent immobilier")
    return " ".join(s.split())

# ------------------------------------------------------------
# Flux lead
# ------------------------------------------------------------
def _start_lead(ses, slots_order) -> str:
    ses["state"] = "lead"
    slot = _next_missing(ses["slots"], slots_order)
    return _ask_for(slot) if slot else "Merci, j’ai noté vos coordonnées ✅"

def _continue_lead_flow(ses, text: str, slots_order: List[str]) -> str:
    _autocapture_slots(ses, text)
    slot = _next_missing(ses["slots"], slots_order)
    if slot:
        ses["state"] = "lead"
        return _ask_for(slot)
    ses["state"] = "idle"
    return "Merci, j’ai bien noté vos coordonnées. Un conseiller vous recontacte très vite ✅"

# ------------------------------------------------------------
# Réponse principale
# ------------------------------------------------------------
def reply(tenant: str, role: str, text: str) -> str:
    """
    Entrée API :
      - tenant : identifiant client (mémoire session)
      - role   : métier affiché (ex: 'Agent immobilier', 'Avocat / Avocate', 'Medecin', …)
      - text   : message utilisateur
    Retour : réponse texte.
    """
    ses = get_session(tenant)
    ses.setdefault("slots", {})
    ses.setdefault("state", "idle")

    role_norm = _normalize_role(role)

    # Charge le pack YAML du rôle
    pack = load_pack(role_norm) or {}
    faqs = pack.get("faqs") or []
    intents = pack.get("intents") or []
    slots_order = pack.get("lead_form") or DEFAULT_LEAD_SLOTS

    user = (text or "").strip()
    if not user:
        # Pas de texte => message neutre (sans boucler)
        if not ses["slots"].get("email"):
            return "Je peux vous renseigner et vous mettre en relation. Souhaitez-vous me laisser un e-mail pour vous recontacter ?"
        return "Je vous écoute 🙂"

    # Si on est déjà en collecte de lead → priorité
    if ses.get("state") == "lead":
        return _continue_lead_flow(ses, user, slots_order)

    # Intent explicite (rdv / rappel / contact / devis / visite…)
    intent = detect_intent(user, intents)
    if intent in {"start_lead", "rdv", "contact", "devis", "visite"}:
        return _start_lead(ses, slots_order)

    # FAQ : meilleure correspondance
    best = best_match(user, faqs, k=1)
    if best:
        top = best[0]
        answer = render(top.get("a", ""), {"role": role_norm})
        # incitation douce à laisser des coordonnées si on n'a rien
        if not (ses["slots"].get("email") or ses["slots"].get("telephone")):
            answer += "\n\nSouhaitez-vous être recontacté·e ? Je peux enregistrer vos coordonnées."
        return answer or "Je n’ai pas la réponse exacte, mais je peux vous mettre en relation rapidement."

    # Sinon → proposer la mise en relation (déclenche la collecte)
    return _start_lead(ses, slots_order)
