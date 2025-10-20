
import re

def render_template(tpl: str, context: dict) -> str:
    def repl(m):
        key = m.group(1).strip()
        return str(context.get(key, ""))
    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, tpl)
