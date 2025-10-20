
from .templates_engine import render_template

class DialogManager:
    def __init__(self, memory, pack):
        self.mem = memory
        self.pack = pack

    def _current_flow(self, session_id):
        return self.mem.get_state(session_id)

    def _start_flow_for_intent(self, intent):
        flows = self.pack.get("dialogue", {}).get("flows", {})
        # choose flow with same name or a default mapping table
        if intent in flows:
            return intent
        # otherwise pick the first flow
        return next(iter(flows.keys()), None)

    def step(self, session_id: str, user_text: str):
        meta = self.pack.get("meta", {})
        disclaimers = meta.get("disclaimers", [])
        state = self._current_flow(session_id)
        context = self.mem.get(session_id)
        ctx = {"**": "", **context["slots"]}

        if not state:
            # detect intent
            from .nlu_rules import match_intent
            intent, slots, conf = match_intent(user_text, self.pack)
            if not intent or conf < 0.2:
                return {"reply": "Je veux être sûr·e de bien comprendre. Pouvez-vous préciser votre demande en quelques mots ?",
                        "done": False}
            # seed slots
            for k,v in slots.items():
                self.mem.set_slot(session_id, k, v)
            flow = self._start_flow_for_intent(intent)
            self.mem.set_state(session_id, flow)
            state = flow

        # run flow
        node_list = self.pack.get("dialogue", {}).get("flows", {}).get(state, [])
        # find first unmet ask/check
        for node in node_list:
            if "ask" in node and "slot" in node:
                slot = node["slot"]
                if not self.mem.get_slot(session_id, slot, None):
                    # ask question
                    return {"reply": node["ask"], "expecting_slot": slot, "done": False}
            if "check" in node:
                # very small rule language: "slot!=empty" / "slot==empty" only
                check = node["check"]
                then = node.get("then")
                slot = check.split(">")[0].split("<")[0].split("==")[0].split("!=")[0].strip()
                val = self.mem.get_slot(session_id, slot, "")
                cond_true = False
                if "== empty" in check or "==empty" in check:
                    cond_true = (val == "" or val is None)
                elif "!= empty" in check or "!=empty" in check:
                    cond_true = (val not in ("", None))
                # simplistic numeric comparison
                elif ">" in check:
                    try:
                        threshold = int(check.split(">")[1].replace("?", "").strip())
                        cond_true = int(val or 0) > threshold
                    except Exception:
                        cond_true = False
                if cond_true and then:
                    # look for a label node name to jump — here we just return a warning template
                    tmpl = self.pack.get("templates", {}).get("say", {}).get(then, None)
                    if tmpl:
                        msg = render_template(tmpl, ctx)
                        return {"reply": msg, "done": False}

            if "say_template" in node:
                tmpl_name = node["say_template"]
                tmpl = self.pack.get("templates", {}).get("say", {}).get(tmpl_name, "")
                msg = render_template(tmpl, ctx)
                return {"reply": msg, "done": False}

            if "next" in node and node["next"] == "offer_meeting":
                txt = self.pack.get("handoff", {}).get("offer_meeting", {}).get("message",
                    "Je peux organiser un échange. Quels créneaux vous conviennent ?")
                return {"reply": txt, "handoff": True, "done": True}

        # if flow consumed
        self.mem.set_state(session_id, None)
        final = "Je vous envoie un récapitulatif par email si vous le souhaitez."
        if disclaimers:
            final += " " + " ".join(disclaimers[:1])
        return {"reply": final, "done": True}

    def inject_slot(self, session_id: str, slot: str, value: str):
        self.mem.set_slot(session_id, slot, value)
