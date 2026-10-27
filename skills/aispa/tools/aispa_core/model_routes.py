"""One route table for subagent models: role class x runtime -> model and effort.

This is the only place in the engine with subagent model IDs and efforts. The
reference ``references/model-routing.md`` mirrors it; a test compares both.
The owner approved exactly one model and one effort per class and runtime:
the table has no substitutes. The coordinator (main chat) only gets advice.
Runs without the ``modelRoutes`` policy flag never consult this module.
"""
import shlex

from .store import Invalid, timestamp

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


# --- Enforcement (modelRoutes): the engine refuses, it never substitutes. ---

EFFORTS = ("low", "medium", "high")
BLOCKED = "BLOCKED_MODEL_CAPABILITY"
# Known IDs and aliases that never go to a subagent; any ID with these words is
# refused as well, so nothing new is accepted by a prefix or a similar name.
FORBIDDEN_MODELS = frozenset({"inherit", "fable", "best", "claude-fable-5-1", "gpt-6-astra"})
FORBIDDEN_WORDS = ("fable", "astra")
MODEL_BLOCKS = "modelBlocks"


def enabled(state):
    return state.get("policy", {}).get("modelRoutes") is True


def _present(value):
    return isinstance(value, str) and bool(value.strip())


def canonical(runtime, model):
    """Approved ID of a requested one: only the dated Claude Code alias map, never a prefix."""
    return CLAUDE_ALIASES.get(model, model) if runtime == "claude-code" else model


def forbidden(model):
    value = model.strip().casefold()
    return value in FORBIDDEN_MODELS or any(word in value for word in FORBIDDEN_WORDS)


def _need(role, runtime, row):
    return "Для роли %s в среде %s нужна %s · %s" % (role, runtime, row["model"], row["effort"])


def _fold(value):
    return value.strip().casefold() if _present(value) else value


def is_coordinator(role, external=False):
    """The coordinator is the main chat in any spelling of its role name; an external role never is."""
    return external is False and _present(role) and _fold(role) == "coordinator"


def _single_coordinator(state, session, native_id):
    """One coordinator per host session; with an accepted host session only there, case-insensitively."""
    host = state.get("hostSession")
    if _present(host) and _fold(session) != _fold(host):
        raise Invalid("Координатор — главный чат сессии хоста %s; другую сессию так не зарегистрировать, "
                      "помощника регистрируйте его рабочей ролью" % host.strip())
    if not _present(session) or not _present(native_id):
        return
    for row in state.get("participants", {}).values():
        if (isinstance(row, dict) and is_coordinator(row.get("role"), row.get("external", False))
                and _fold(row.get("session")) == _fold(session)
                and row.get("nativeId") != native_id.strip()):
            raise Invalid("Координатор этой сессии хоста уже зарегистрирован; помощника регистрируйте его рабочей ролью")


def check_assignment(state, role, runtime, model, reasoning, *, session, native_id, task=None, attempt=None,
                     external=False):
    """Refuse a subagent model or effort off the table; the coordinator is only one per host session.

    Returns ``{"routeClass", "model", "effort"}`` with the approved values (None
    outside claude-code and codex, where only the prohibitions apply).
    """
    # An external role named "coordinator" in any spelling is a subagent: it gets the prohibitions, not the seat.
    if is_coordinator(role, external):
        _single_coordinator(state, session, native_id)
        return {"routeClass": "coordinator", "model": None, "effort": None}
    # An external phase role (explicit external name) has no class: only the prohibitions apply.
    cls = route_class(state, role, task, attempt) if role in ROLE_CLASS else None
    runtime = _fold(runtime)
    if state.get("runtime") in RUNTIMES and runtime not in RUNTIMES:
        # The table binds claude-code and codex; another host would only dodge it.
        raise Invalid("В прогоне среды %s субагента %s назначают в claude-code или codex: среда %s не входит "
                      "в таблицу маршрутов (references/model-routing.md)" % (state["runtime"], role, runtime))
    row = ROUTES[cls].get(runtime) if cls else None
    if not _present(model) or not _present(reasoning):
        hint = ("--model %s --reasoning %s" % (row["model"], row["effort"]) if row else
                "--model <ID среды> --reasoning low|medium|high")
        raise Invalid("Для роли %s укажите --model и --reasoning из таблицы маршрутов "
                      "(references/model-routing.md): %s" % (role, hint))
    model, reasoning = model.strip(), reasoning.strip()
    if forbidden(model):
        raise Invalid("Модель %s субагенту запрещена: Fable, Astra, inherit и best — только для главного чата; %s"
                      % (model, _need(role, runtime, row) if row else "выберите модель среды не выше Opus · high или Sol · high"))
    if reasoning not in EFFORTS:
        raise Invalid("Усилие %s субагенту запрещено: допустимо только low, medium или high" % reasoning)
    if row is not None and (canonical(runtime, model) != row["model"] or reasoning != row["effort"]):
        raise Invalid(_need(role, runtime, row) + "; замен в таблице нет (references/model-routing.md)")
    return {"routeClass": cls, "model": row and row["model"], "effort": row and row["effort"]}


def unavailable_block(state, role, runtime, capability, *, task=None, attempt=None):
    """Blocker record when the host does not offer the approved model of this role; never a substitute."""
    if role not in ROLE_CLASS:
        return None
    cls = route_class(state, role, task, attempt)
    row = ROUTES.get(cls, {}).get(runtime)
    models = capability.get("models") if isinstance(capability, dict) else None
    if row is None or not isinstance(models, list):
        return None
    resolutions = capability.get("resolutions") if isinstance(capability.get("resolutions"), dict) else {}
    offered = {canonical(runtime, m) for m in models if isinstance(m, str)}
    offered |= {resolutions.get(m) for m in models if isinstance(m, str)}
    if row["model"] in offered:
        return None
    task_id = task.get("id") if isinstance(task, dict) else None
    return {"code": BLOCKED, "role": role, "runtime": runtime, "routeClass": cls, "task": task_id,
            "model": row["model"], "effort": row["effort"],
            "message": ("%s: для роли %s в среде %s нужна %s · %s, но среда её не предлагает; "
                        "другая модель не подставляется" % (BLOCKED, role, runtime, row["model"], row["effort"])),
            "options": [
                {"id": "enable-model", "text": "Включите модель %s в среде %s и повторите назначение" % (row["model"], runtime)},
                {"id": "owner-allows-other", "text": "Новыми словами разрешите другую модель для роли %s; сам движок её не подставит" % role},
                {"id": "leave-in-remaining", "text": "Оставьте задачу в остатках; независимая работа продолжается"}]}


def _capability(store, evidence):
    from .contracts import artifact_valid
    if not (isinstance(evidence, dict) and set(evidence) == {"source"} and artifact_valid(store, evidence["source"])):
        return None  # model evidence validation refuses it with its own message
    try:
        value = store.json(evidence["source"])
    except (OSError, ValueError, TypeError):
        return None
    return value.get("capability") if isinstance(value, dict) else None


def enforce(store, state, role, runtime, native_id, session, model, reasoning, evidence, *,
            task=None, attempt=None, save=None, external=False):
    """One gate for every subagent assignment. Without modelRoutes nothing changes (empty dict).

    A missing approved model is written to ``state["modelBlocks"]`` and saved
    through ``save`` before the refusal, so the blocker survives the error.
    """
    if not enabled(state):
        return {}
    route = check_assignment(state, role, runtime, model, reasoning, session=session, native_id=native_id,
                             task=task, attempt=attempt, external=external)
    if route["routeClass"] not in ("coordinator", None) and evidence is not None:
        block = unavailable_block(state, role, runtime.strip().casefold(), _capability(store, evidence),
                                  task=task, attempt=attempt)
        if block is not None:
            state.setdefault(MODEL_BLOCKS, []).append(dict(block, at=timestamp()))
            if save is not None:
                save("model-capability-blocked:" + role)
            raise Invalid(block["message"])
    return route


def confirm(route, role, runtime, metadata):
    """A host resolution may not turn an approved request into another model."""
    if not route or route.get("model") is None:
        return
    resolved = metadata.get("resolvedRequestedModel")
    if resolved is not None and resolved != route["model"]:
        raise Invalid("%s; среда разрешила запрос в %s" % (_need(role, runtime.strip().casefold(), route), resolved))


def routing_block(state, role, task=None, attempt=None):
    """Model part of a role card's routing, for phase roles and the knowledge writer."""
    from .packets import ROUTE_RULE
    row = route(state, role, task, attempt)
    value = {"routeClass": row["class"], "model": row["model"], "effort": row["effort"], "route": row,
             "advisory": row["advisory"], "selection": ROUTE_RULE, "reference": "references/model-routing.md",
             "reason": "Класс роли " + row["class"] + " по таблице маршрутов"}
    if "agent" in row:
        value["agent"] = row["agent"]
    return value


def require_script_receipt(state, task, attempt):
    """Scripted mechanics close only on the `verify --task` receipt of the plan's own command."""
    script = task.get("scriptCheck") if isinstance(task, dict) else None
    if not enabled(state) or script is None:
        return
    receipt = (attempt or {}).get("verification") or {}
    if receipt.get("command") != shlex.split(script):
        raise Invalid("Задачу %s закрывает только квитанция verify --task %s с командой из scriptCheck: %s"
                      % (task["id"], task["id"], script))
