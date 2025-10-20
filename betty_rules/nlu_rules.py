
import re

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()

def match_intent(text: str, pack: dict):
    """Return (intent_name, slots_dict, confidence) using simple keyword matching from pack."""
    t = normalize(text)
    best = (None, {}, 0.0)
    intents = pack.get("nlu", {}).get("intents", {}) or {}
    for name, spec in intents.items():
        score = 0
        slots = {}
        for syn in spec.get("synonyms", []):
            if syn in t:
                score += 1
        # naive slot capture by probing ??? placeholders (e.g., 'date?' means ask, do not capture here)
        if score > best[2]:
            best = (name, slots, float(score))
    # Confidence scaled crudely: #hits / (2 + max_syns)
    if best[0]:
        maxsyn = max(2, len(intents.get(best[0], {}).get("synonyms", [])))
        conf = min(0.99, best[2] / (maxsyn))
        return best[0], best[1], conf
    return None, {}, 0.0
