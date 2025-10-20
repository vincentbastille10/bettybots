
from collections import defaultdict

class Memory:
    """Very simple in-process memory keyed by session_id."""
    def __init__(self):
        self.sessions = defaultdict(lambda: {"slots": {}, "state": None, "history": []})
    def get(self, session_id: str):
        return self.sessions[session_id]
    def set_slot(self, session_id: str, key: str, value):
        s = self.get(session_id)
        s["slots"][key] = value
    def get_slot(self, session_id: str, key: str, default=None):
        return self.get(session_id)["slots"].get(key, default)
    def set_state(self, session_id: str, state: str | None):
        self.get(session_id)["state"] = state
    def get_state(self, session_id: str):
        return self.get(session_id)["state"]
    def add_history(self, session_id: str, who: str, text: str):
        self.get(session_id)["history"].append({"who": who, "text": text})
