# betty_rules/dialog_manager.py
# Orchestrateur rule-based pour Betty : détection d'intentions, gestion de lead, et persistance.

from __future__ import annotations
from typing import Dict, Any, List
import re

from .memory import get_session, save_session
from .loader import load_pack
from .nlu_rules import best_match, detect_intent
from .templates_engine import render

# ------------------------------------------------------------
# Regex utilitaires
# ------------------------------------------------------------
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:(?:\+|00)33|0)\s*[1-9](?:[\s\.\-]*\d{2}){4}")
NAME_STRICT_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ' -]{2,}\s+[A-Za-zÀ-ÖØ-öø-ÿ' -]{2,}$")
NAME_TOKEN_RE  = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ'-]{2,}")

AFFIRM = {"oui", "ok", "daccord", "d'accord", "yes", "bien", "parfait", "je veux", "je suis daccord", "je suis d'accord"}

# ------------------------------------------------------------
# Capture automatique (nom, email, téléphone)
# ------------------------------------------------------------
def _autocapture_slots(ses: Dict[str, Any], text: str) -> None:
    """Essaie d'extraire automatiquement les coordonnées depuis le texte utilisateur."""
    t = (text or "").strip()

    # email
    if not ses["slots"].get("email"):
        m = EMAIL_RE.search(t)
        if m:
            ses["slots"]["email"] = m.group(0)

    # téléphone (espaces tolérés)
    if not ses["slots"].get("telephone"):
        flat = t.replace("\u202f", " ").strip()
        m = PHONE_RE.search(flat)
        if m:
            ses["slots"]["telephone"] = m.group(0)

    # nom
    if not ses["slots"].get("nom"):
        line = " ".join(t.split())
        if NAME_STRICT_RE.match(line):
            ses["slots"]["nom"] = line
        else:
            tokens = NAME_TOKEN_RE.findall(line)
            if len(tokens) >= 2:
                ses["slots"]["nom"] = f"{tokens[0]} {tokens[1]}"

# ------------------------------------------------------------
# Structure du formulaire de lead
# ------------------------------------------------------------
DEFAULT_LEAD_SLOTS = ["nom", "email", "telephone", "besoin", "budget", "delai"]

LEAD_ASK = {
    "nom": "Pour commencer, quel est votre nom et prénom ?",
    "email": "Merci. Quelle est votre adresse e-mail pour vous recontacter ?",
    "telephone": "Souhaitez-vous laisser un numéro de téléphone pour un rappel ?",
    "besoin": "Pouvez-vous préciser votre besoin ? (achat, vente, estimation...)",
    "budget": "Avez-vous un budget indicatif ?",
    "delai": "Quel est votre délai idéal ?",
}

def _ask_for(slot: str) -> str:
    return LEAD_ASK.get(slot, "Pouvez-vous me préciser ce point ?")

def _next_missing(slots: Dict[str, Any], order: List[str]) -> str | None:
    """Renvoie le prochain champ manquant dans l'ordre défini."""
    for k in order:
        v = slots.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            return k
    return None

# ------------------------------------------------------------
# Normalisation du rôle (corrigée)
# ------------------------------------------------------------
def _normalize_role(role: str) -> str:
    """Normalise le rôle pour éviter les artefacts ('immobilierbilier')."""
    s = (role or "").strip().lower()
    s = " ".join(s.split())  # supprime espaces multiples

    synonyms = {
        "agent immo": "agent immobilier",
        "agent immobilier": "agent immobilier",
        "avocat / avocate": "avocat",
        "avocate": "avocat",
        "avocat": "avocat",
        "médecin": "medecin",
        "medecin": "medecin",
        "comptable": "comptable",
        "psychologue": "psychologue",
        "danse (assistante)": "danse",
        "danse": "danse",
    }
    return synonyms.get(s, s)

# ------------------------------------------------------------
# Logique principale de collecte de lead
# ------------------------------------------------------------
def _start_lead(tenant: str, ses: Dict[str, Any], slots_order) -> str:
    ses["state"] = "lead"
    slot = _next_missing(ses["slots"], slots_order)
    save_session(tenant, ses)
    return _ask_for(slot) if slot else "Merci, j’ai noté vos coordonnées ✅"

def _continue_lead_flow(tenant: str, ses: Dict[str, Any], text: str, slots_order: List[str]) -> str:
    _autocapture_slots(ses, text)
    slot = _next_missing(ses["slots"], slots_order)
    if slot:
        ses["state"] = "lead"
        save_session(tenant, ses)
        return _ask_for(slot)
    ses["state"] = "idle"
    save_session(tenant, ses)
    return "Merci, j’ai bien noté vos coordonnées. Un conseiller vous recontactera très vite ✅"

# ------------------------------------------------------------
# Fonction principale de réponse
# ------------------------------------------------------------
def reply(tenant: str, role: str, text: str) -> str:
    """
    tenant : identifiant client (clé session)
    role   : profil de bot (affiché)
    text   : message utilisateur
    """
    ses = get_session(tenant)
    ses.setdefault("slots", {})
    ses.setdefault("state", "idle")

    role_norm = _normalize_role(role)

    # Charger le pack YAML
    pack = load_pack(role_norm) or {}
    faqs = pack.get("faqs") or []
    intents = pack.get("intents") or []
    slots_order = pack.get("lead_form") or DEFAULT_LEAD_SLOTS

    user = (text or "").strip()
    if not user:
        if not ses["slots"].get("email"):
            return "Je peux vous renseigner ou vous mettre en relation. Souhaitez-vous me laisser un e-mail pour vous recontacter ?"
        return "Je vous écoute 🙂"

    # PRIORITÉ : si on est déjà en collecte → continuer
    if ses.get("state") == "lead":
        return _continue_lead_flow(tenant, ses, user, slots_order)

    # Auto-capture AVANT toute décision (nom/mail/tel)
    _autocapture_slots(ses, user)

    # Si des slots manquent encore, on enclenche la collecte si :
    #  - l'utilisateur fournit une info (email/tel/nom capturé), OU
    #  - le message n'est pas une question, OU
    #  - c'est une affirmation (ok/oui/d'accord)
    missing_slot = _next_missing(ses["slots"], slots_order)
    user_clean = " ".join(user.lower().split())
    if missing_slot:
        gave_info = bool(
            EMAIL_RE.search(user) or PHONE_RE.search(user) or NAME_STRICT_RE.search(user) or len(NAME_TOKEN_RE.findall(user)) >= 2
        )
        is_question = "?" in user
        is_affirm = user_clean in AFFIRM
        if gave_info or not is_question or is_affirm:
            return _start_lead(tenant, ses, slots_order)

    # Intent explicite de démarrage (rdv, contact, visite, etc.)
    intent = detect_intent(user, intents)
    if intent in {"start_lead", "rdv", "contact", "devis", "visite"}:
        return _start_lead(tenant, ses, slots_order)

    # FAQ
    best = best_match(user, faqs, k=1)
    if best:
        top = best[0]
        answer = render(top.get("a", ""), {"role": role_norm})
        if not (ses["slots"].get("email") or ses["slots"].get("telephone")):
            answer += "\n\nSouhaitez-vous être recontacté·e ? Je peux enregistrer vos coordonnées."
        save_session(tenant, ses)
        return answer or "Je n’ai pas la réponse exacte, mais je peux vous mettre en relation."

    # Sinon, on démarre la collecte
    return _start_lead(tenant, ses, slots_order)
