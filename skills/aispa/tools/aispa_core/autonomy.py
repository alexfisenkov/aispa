"""One briefing boundary per new run; no authority or delivery is inferred."""
from .contracts import require


def enabled(state):
    return state.get("policy", {}).get("firstBriefingClosure") is True


def close_briefing(state, at):
    """Latch only after seal's existing guards pass; resealing never reopens it."""
    if not enabled(state) or state.get("firstBriefingClosedAt"):
        return False
    state["firstBriefingClosedAt"] = at
    return True


def view(state):
    """Small shared contract for the host, role cards and derived context."""
    if not enabled(state):
        return None
    closed = state.get("firstBriefingClosedAt")
    may_ask = (state.get("mode") in ("important", "interview") and not closed
               and not state.get("sealed") and not state.get("tasks")
               and state.get("status") == "running" and not state.get("waiting"))
    return {"mode": state.get("mode"), "briefingClosedAt": closed, "mayAsk": may_ask,
            "decisions": "Decide within the agreed scope and record with decision; report choices at the end.",
            "blockers": "Record the blocked step, preserve requirements and continue independent work.",
            "newOwnerInput": "Apply new owner instructions without reopening an agent-initiated interview.",
            "authority": {"existingPermissionsOnly": True, "hostApprovalRulesUnchanged": True,
                          "scopeAndRequirementsUnchanged": True}}


def require_questions_allowed(state):
    if enabled(state):
        require(view(state)["mayAsk"],
                "Вопросы разрешены только в important/interview до первого завершения брифа; "
                "после него фиксируйте решения и блокеры, продолжая независимую работу")


def start_request(state):
    """The caller emits this only for the first successful seal, never as a receipt."""
    require(enabled(state) and state.get("firstBriefingClosedAt"), "Бриф ещё не завершён")
    return [{"action": "notify-autonomous-start", "surface": "conversation",
             "message": "Бриф готов. Дальше работаю самостоятельно в согласованных границах — "
                        "можете отойти. Принятые решения, результат и блокеры покажу в финале.",
             "requiresReply": False}]
