# betty_rules/nlu_rules.py
from __future__ import annotations
from typing import List, Dict, Any, Tuple
import re
from difflib import SequenceMatcher

_word_re = re.compile(r"[a-zA-ZÀ-ÖØ-öø-ÿ0-9]+")

def _norm(s: str) -> str:
    return (s or "").lower().strip()

def _tokens(s: str) -> set:
    return set(_word_re.findall(_norm(s)))

def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0

def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()

def _sim(a: str, b: str) -> float:
    # moyenne Jaccard + difflib
    return 0.5 * _jaccard(a, b) + 0.5 * _ratio(a, b)

def detect_intent(user_text: str, intents: List[Dict[str, Any]], threshold: float = 0.55) -> str | None:
    """
    intents: [{"name":"start_lead","patterns":["rdv","rappel",...]}, ...]
    Retourne le name du meilleur intent si score >= threshold.
    """
    u = _norm(user_text)
    if not u or not intents:
        return None
    best_name, best_score = None, 0.0
    for it in intents:
        name = it.get("name")
        patterns = it.get("patterns") or []
        for p in patterns:
            sc = _sim(u, p)
            if sc > best_score:
                best_score, best_name = sc, name
    return best_name if best_score >= threshold else None

def best_match(user_text: str, faqs: List[Dict[str, Any]], k: int = 1) -> List[Dict[str, Any]]:
    """
    faqs: [{"q":"...","a":"...","tags":[...]}, ...]
    Retourne la liste des k meilleurs éléments, triés par score desc, avec champ _score.
    """
    u = _norm(user_text)
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for fx in faqs or []:
        q = fx.get("q") or ""
        score = _sim(u, q)
        if score > 0:
            item = dict(fx)
            item["_score"] = round(score, 4)
            scored.append((score, item))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [it for _, it in scored[:max(1, k)]]
