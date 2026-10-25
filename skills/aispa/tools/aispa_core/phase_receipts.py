"""Immutable phase authorship without duplicate actors or allocated token costs."""
import json

from . import participants
from .contracts import artifact_valid, require
from .store import Invalid, timestamp

ROLES = {"idea": "idea", "brief-analyst": "brief-analyst",
         "brief-reviewer": "brief-reviewer", "brief-map": "brief-reviewer",
         "discovery": "discovery", "coverage": "coverage", "planner": "planner",
         "blind": "blind", "acceptance": "blind"}
COORDINATED = frozenset({"idea", "brief-analyst", "discovery", "planner"})


def enabled(state):
    return bool(state.get("policy", {}).get("phaseReceipts"))


def suitable(actor, phase):
    return bool(actor and (actor.get("role") == ROLES.get(phase)
                or actor.get("role") == "coordinator" and phase in COORDINATED))


def authorize(store, state, phase, actor=None):
    """Resolve before mutations; an omitted actor is legal only if unambiguous."""
    if not enabled(state):
        return None
    require(phase in ROLES, "Неизвестная фаза авторства")
    if actor is None:
        choices = [p["id"] for p in participants.list_participants(store, state) if suitable(p, phase)]
        require(len(choices) == 1, "Нужен --actor: нет единственного подходящего участника фазы " + phase)
        actor = choices[0]
    require(suitable(participants.resolve(store, state, actor), phase),
            "Участник не вправе подписывать фазу " + phase)
    return actor


def report_actor(store, state, phase, report, actor=None):
    if not enabled(state):
        return None
    supplied = report.get("actor", report.get("reviewer"))
    if actor is None and supplied is not None:
        matches = [p["id"] for p in participants.list_participants(store, state)
                   if supplied in (p["id"], p.get("nativeId")) and suitable(p, phase)]
        require(len(matches) == 1, "Автор отчёта не связан с единственным участником фазы " + phase)
        actor = matches[0]
    actor = authorize(store, state, phase, actor)
    who = participants.resolve(store, state, actor)
    require(supplied is None or supplied in (actor, who["nativeId"]),
            "Автор отчёта отличается от участника фазы")
    return actor


def current_artifact(state, phase):
    keys = {"idea": "ideaReview", "brief-analyst": "briefExtraction",
            "brief-reviewer": "briefReview", "brief-map": "briefMap", "discovery": "discovery"}
    if phase in keys:
        return state.get(keys[phase])
    if phase == "blind":
        return (state.get("blindReview") or {}).get("report")
    name = {"planner": "plan", "coverage": "coverage", "acceptance": "acceptance"}.get(phase)
    return state.get("gates", {}).get(name, {}).get("report")


def current_basis(store, state, phase):
    from . import briefmap, ideas, blind
    from .contracts import basis
    if phase == "idea":
        return ideas.basis(state)
    if phase in ("brief-analyst", "brief-reviewer"):
        return briefmap.source_basis(state)
    if phase == "brief-map":
        return briefmap.reconciliation_basis(state)
    if phase == "discovery":
        return (state.get("discovery") or {}).get("basis")
    if phase == "blind":
        return blind.basis(store, state)
    return basis(store, state, "plan" if phase == "planner" else phase)


def record(store, state, phase, artifact, *, actor=None, basis=None):
    """Called only as part of recording a validated phase report."""
    if not enabled(state):
        return None
    actor = authorize(store, state, phase, actor)
    require(artifact_valid(store, artifact) and artifact == current_artifact(state, phase),
            "Квитанция авторства требует текущий сохранённый отчёт")
    expected = current_basis(store, state, phase)
    require(basis is None or basis == expected, "Основание авторства устарело")
    history = state.get("phaseReceipts", [])
    require(isinstance(history, list), "Повреждена история авторства")
    row = {"schema": 1, "run": state["run"], "phase": phase, "actor": actor,
           "artifact": dict(artifact), "basis": expected, "at": timestamp(),
           "previous": history[-1]["record"]["sha256"] if history else None}
    row["record"] = store.artifact("phase-receipts", (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode(), ".json")
    state["phaseReceipts"] = [*history, row]
    return row


def _history(store, state):
    previous = None
    history = state.get("phaseReceipts", [])
    require(isinstance(history, list), "Повреждена история авторства")
    for row in history:
        require(isinstance(row, dict) and artifact_valid(store, row.get("record", {})), "Повреждена квитанция авторства")
        require(store.json(row["record"]) == {k: v for k, v in row.items() if k != "record"}
                and row.get("run") == state["run"] and row.get("previous") == previous,
                "Нарушена неизменность или последовательность авторства")
        require(suitable(participants.resolve(store, state, row.get("actor")), row.get("phase"))
                and artifact_valid(store, row.get("artifact", {})), "Нет действительного автора или отчёта фазы")
        previous = row["record"]["sha256"]
    return history


def current(store, state, phase):
    if not enabled(state):
        return None
    try:
        require(all(artifact_valid(store, ref) for ref in state.get("briefs", [])), "Изменён исходный бриф фазы")
        rows = [r for r in _history(store, state) if r["phase"] == phase]
        row = rows[-1] if rows else None
        if (row and row["artifact"] == current_artifact(state, phase)
                and row["basis"] == current_basis(store, state, phase)):
            return row
    except (Invalid, OSError, ValueError, KeyError, TypeError):
        pass
    return None


def valid(store, state, phase):
    return not enabled(state) or current(store, state, phase) is not None


def summary(store, state):
    expected = [p for p in ROLES if current_artifact(state, p)]
    # Collection alone is evidence gathering, not authored discovery analysis.
    if state.get("discoveryCollection") and "discovery" not in expected:
        expected.append("discovery")
    rows = {phase: current(store, state, phase) for phase in expected}
    return {"authors": {phase: row["actor"] for phase, row in rows.items() if row},
            "missingPhases": sorted(phase for phase, row in rows.items() if row is None),
            "costAllocation": "none; usage counted once per actual actor and call"}
