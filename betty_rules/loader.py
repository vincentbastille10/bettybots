
import yaml, os

_cache = {}

def load_pack(name_or_path: str) -> dict:
    """Load a YAML pack by filename (in ./packs) or absolute path."""
    if name_or_path in _cache:
        return _cache[name_or_path]
    if os.path.isfile(name_or_path):
        path = name_or_path
    else:
        here = os.path.dirname(__file__)
        packs = os.path.abspath(os.path.join(here, "..", "packs"))
        path = os.path.join(packs, f"{name_or_path}.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    _cache[name_or_path] = data
    return data
