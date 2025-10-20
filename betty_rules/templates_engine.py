# betty_rules/templates_engine.py
from __future__ import annotations
from typing import Dict

def render(template: str, ctx: Dict[str, str] | None = None) -> str:
    """
    Remplacement ultra simple: "{{ role }}" etc.
    Pas de Jinja ici pour rester déterministe et sans dépendances.
    """
    if not template:
        return ""
    out = str(template)
    for k, v in (ctx or {}).items():
        out = out.replace("{{ "+k+" }}", str(v))
        out = out.replace("{{"+k+"}}", str(v))
    return out
