"""One route table for subagent models: role class x runtime -> model and effort.

This is the only place in the engine with subagent model IDs and efforts. The
reference ``references/model-routing.md`` mirrors it; a test compares both.
The owner approved exactly one model and one effort per class and runtime:
the table has no substitutes. The coordinator (main chat) only gets advice.
Runs without the ``modelRoutes`` policy flag never consult this module.
"""
from .store import Invalid

CLASSES = ("coordinator", "judge", "sensitive_worker", "worker", "mechanical_scripted", "mechanical", "research")
RUNTIMES = ("claude-code", "codex")
# Classes a task worker may receive; the task decides which (route_class).
WORKER_CLASSES = frozenset({"sensitive_worker", "worker", "mechanical_scripted", "mechanical"})

ROUTES = {
    "judge": {"claude-code": {"model": "claude-opus-5-5", "effort": "high"},
              "codex": {"model": "gpt-6.1-sol", "effort": "high"}},
    "sensitive_worker": {"claude-code": {"model": "claude-opus-5-5", "effort": "high"},
                         "codex": {"model": "gpt-6.1-sol", "effort": "high"}},
    "worker": {"claude-code": {"model": "claude-sonnet-5-5", "effort": "medium"},
               "codex": {"model": "gpt-6.1-terra", "effort": "medium"}},
    "mechanical_scripted": {"claude-code": {"model": "claude-haiku-5-5", "effort": "low"},
                            "codex": {"model": "gpt-6-luna", "effort": "low"}},
    "mechanical": {"claude-code": {"model": "claude-sonnet-5-5", "effort": "low"},
                   "codex": {"model": "gpt-6.1-terra", "effort": "low"}},
    "research": {"claude-code": {"model": "claude-opus-5-5", "effort": "medium"},
                 "codex": {"model": "gpt-6.1-terra", "effort": "high"}},
}

COORDINATOR_ADVICE = {
    "claude-code": {"model": "claude-opus-5-5", "effortBriefPlan": "high", "effortLater": "medium"},
    "codex": {"model": "gpt-6.1-sol", "effortBriefPlan": "high", "effortLater": "medium"},
}
COORDINATOR_NOTE = "Совет, не требование: Fable или Astra — только ваш осознанный выбор."

_JUDGE = ("critic1", "critic2", "acceptor", "blind", "integration-reviewer", "branch-reviewer",
          "brief-analyst", "brief-reviewer", "coverage", "idea", "planner", "recovery-reviewer")
_RESEARCH = ("discovery", "source-reader", "knowledge-writer")
# Task roles other than worker are fixed; the worker class is refined by its task.
ROLE_CLASS = {"coordinator": "coordinator", "worker": "worker",
              **{role: "judge" for role in _JUDGE}, **{role: "research" for role in _RESEARCH}}

# A dated, closed map: an alias is accepted only when listed here, never by prefix.
CLAUDE_ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
CLAUDE_AGENT_FILES = {"judge": "aispa-judge", "sensitive_worker": "aispa-sensitive-worker",
                      "worker": "aispa-worker", "mechanical_scripted": "aispa-mechanic-scripted",
                      "mechanical": "aispa-mechanic", "research": "aispa-researcher"}


def route_class(state, role, task=None, attempt=None):
    """Class of a role; a task worker is sensitive, mechanical (scripted or not) or ordinary."""
    if role not in ROLE_CLASS:
        raise Invalid("Неизвестная роль для таблицы моделей: %s" % role)
    if role != "worker" or task is None:
        return ROLE_CLASS[role]
    from . import task_tiers  # task_tiers imports contracts; resolved at call time
    if task_tiers.sensitive_task(state, task):
        return "sensitive_worker"
    if task_tiers.model_need(state, task, attempt) == "mechanical":
        return "mechanical_scripted" if task.get("scriptCheck") else "mechanical"
    return "worker"


def route(state, role, task=None, attempt=None):
    """Approved model and effort of this role in the run's runtime; advice only for the coordinator."""
    cls = route_class(state, role, task, attempt)
    runtime = state.get("runtime")
    if cls == "coordinator":
        advice = COORDINATOR_ADVICE.get(runtime)
        return {"class": cls, "runtime": runtime, "model": advice and advice["model"],
                "effort": advice and advice["effortBriefPlan"], "advisory": True}
    row = ROUTES[cls].get(runtime)
    if row is None:
        # The table is binding only in claude-code and codex; other runtimes keep their advice.
        return {"class": cls, "runtime": runtime, "model": None, "effort": None, "advisory": True}
    value = {"class": cls, "runtime": runtime, "model": row["model"], "effort": row["effort"]}
    if runtime == "claude-code":
        value["agent"] = CLAUDE_AGENT_FILES[cls]
    value["advisory"] = False
    return value


def coordinator_advice(runtime):
    """What `init` tells the student about the main chat; the engine never refuses the coordinator."""
    advice = COORDINATOR_ADVICE.get(runtime)
    if advice is None:
        return None
    return {"runtime": runtime, **advice, "advisory": True, "note": COORDINATOR_NOTE,
            "reference": "references/model-routing.md"}
