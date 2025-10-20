# betty_rules/__init__.py
# Point d'entrée propre du package : n’exporte que ce qui existe vraiment.
from .loader import load_pack
from .dialog_manager import reply

__all__ = ["load_pack", "reply"]
