# betty_rules/dialog_manager.py
# Orchestrateur "rule-based" pour Betty
from __future__ import annotations
from typing import Dict, Any, List

from .memory import get_session
from .loader import load_pack
from .nlu_rules import best_match, detect_intent
from .templates_engine import render

# Slots par défaut si le pack n'en définit pas
DEFAULT_LEAD_SLOTS: List[str] = ["nom", "email", "telephone", "besoin", "budget", "delai"]

# Prompts de collecte pour chaque slot
LEAD_ASK = {
    "nom": "Pour commencer, quel est votre nom et prénom ?",
    "email": "Merci. Quelle est votre adresse e-mail pour vous recontacter ?",
    "telephone": "Souhaitez-vous laisser un numéro de téléphone pour un rappel ?",
    "besoin": "Pouvez-vous résumer votre besoin en quelques mots ?",
    "budget": "Avez-vous un budget indicatif ?",
    "delai": "Quel est votre délai idéal ?",
}

# Heuristiques de capture à la volée
def _autocapture_slots(ses: Dict[str, Any], text: str) -> None:
    t = (text or "").strip()
    if "@" in t and not ses["slots"].get("email"):
        ses["slots"]["email"] = t
    if any(t.startswith(p) for p in ("06", "07")) and not ses["slots"].get("telephone"):
        ses["slots"]["telephone"] = t

def _next_missing(slots: Dict[str, Any], order: List[str]) -> str | None:
    for k in order:
        if not slots.get(k):
            return k
    return None

def _ask_for(slot: str) -> str:
    return LEAD_ASK.get(slot, "D’accord, j’ai besoin d’une information supplémentaire.")

def _normalize_role(role: str) -> str:
    s = (role or "").strip().lower()
    s = s.replace(" / ", " ").replace("/", " ").replace("_", " ").replace("-", " ")
    s = s.replace("avocat avocate", "avocat")
    s = s.replace("agent immo", "agent immobilier")
    s = s.replace("médecin", "medecin")
    return " ".join(s.split())

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

def reply(tenant: str, role: str, text: str) -> str:
    """
    Entrée API :
      - tenant : identifiant client (mémoire session)
      - role   : métier affiché ("Avocat / Avocate", "agent immobilier", "medecin", etc.)
      - text   : message utilisateur
    Retour : réponse texte.
    """
    ses = get_session(tenant)
    role_norm = _normalize_role(role)

    # Charge pack YAML
    pack = load_pack(role_norm) or {}
    faqs = pack.get("faqs") or []
    intents = pack.get("intents") or []
    slots_order = pack.get("lead_form") or DEFAULT_LEAD_SLOTS

    user = (text or "").strip()
    if not user:
        if not ses["slots"].get("email"):
            return "Je peux vous renseigner et vous mettre en relation. Souhaitez-vous me laisser un e-mail pour vous recontacter ?"
        return "Je vous écoute 🙂"

    # Si on est déjà en collecte → priorité
    if ses.get("state") == "lead":
        return _continue_lead_flow(ses, user, slots_order)

    # Intent explicite (rdv/contact/…)
    intent = detect_intent(user, intents)
    if intent in {"start_lead", "rdv", "contact"}:
        return _start_lead(ses, slots_order)

    # FAQ par similarité (top 1)
    best = best_match(user, faqs, k=1)
    if best:
        top = best[0]
        answer = render(top.get("a", ""), {"role": role_norm})
        if not ses["slots"].get("email"):
            answer += "\n\nSouhaitez-vous être recontacté·e ? Je peux enregistrer vos coordonnées."
        return answer or "Je n’ai pas la réponse exacte, mais je peux vous mettre en relation rapidement."

    # Pas de match → proposer la mise en relation
    return _start_lead(ses, slots_order)
