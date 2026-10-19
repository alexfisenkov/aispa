"""Read-only projection. A view never decides whether a requirement is accepted."""
from .contracts import basis, gate_valid, verified
from .store import atomic_json, timestamp
from .progress import display_stage, estimate
from .timing import elapsed
from .team import task_proof_valid
from . import carryover

PHASES = {"implementation": "Исполнитель", "critic_1": "Критик · круг 1", "repair": "Исправление",
          "critic_2": "Критик · круг 2", "task_acceptance": "Приёмщик задачи"}

LABELS = {"running": "В работе", "paused": "Приостановлено", "blocked": "Есть препятствие",
          "cancelled": "Отменено", "accepted": "Все действующие требования приняты",
          "delivered_with_remaining": "Основная часть готова, есть остатки"}


def project(store, state):
    observed = timestamp()
    gates = {name: gate_valid(store, state, name) for name in ("coverage", "plan", "acceptance")}
    current = {t["id"]: task_proof_valid(store, state, t["id"]) for t in state["tasks"] if t["status"] == "completed"}
    if not all(current.values()):
        gates["acceptance"] = False
    status = state["status"]
    if status in ("accepted", "delivered_with_remaining") and not gates["acceptance"]:
        status = "stale"
    rows = []
    for req in state["requirements"]:
        holders = [t for t in state["tasks"] if req["id"] in t["requirements"]]
        implementation = "unplanned" if not holders else (
            "implemented" if all(t["status"] == "completed" for t in holders) else "unfinished")
        if implementation == "implemented" and not all(current.get(t["id"]) for t in holders):
            implementation = "needs_check"
        rows.append({"id": req["id"], "text": req["text"], "core": req["id"] in state.get("core", []),
                     "kind": req["kind"], "scope": req["scope"], "implementation": implementation,
                     **{k: req[k] for k in ("parent", "decision") if k in req}})
    assessment = state["gates"].get("acceptance", {})
    current_assessment = bool(assessment and assessment.get("basis") == basis(store, state, "acceptance"))
    remaining = {
        "checks": assessment.get("failures", []) if current_assessment else [],
        "blockedTasks": [t["id"] for t in state["tasks"] if t["status"] == "blocked"],
        "excluded": [r["id"] for r in state["requirements"] if r["scope"] != "active"],
        "unmapped": assessment.get("unmapped", []) if current_assessment else [],
        "inherited": [{"id": item["id"], "status": item["status"], "text": item["requirement"]["text"]}
                      for item in carryover.unresolved(store, state)],
    }
    tasks = [dict(t, elapsedSeconds=elapsed(t, observed), phaseLabel=PHASES.get(t.get("phase"), "Исполнитель"),
                  proofCurrent=current.get(t["id"], False),
                  criticRounds=len((state.get("taskHistory", {}).get(t["id"]) or [{}])[-1].get("rounds", [])))
             for t in state["tasks"]]
    estimate_tasks = [dict(t, status="pending") if t["status"] == "completed" and not current.get(t["id"]) else t
                      for t in state["tasks"]]
    return {"version": state["version"], "title": state["title"], "run": state["run"],
            "status": status, "label": LABELS.get(status, "Проект изменён; приёмка устарела"),
            "stage": state["stage"], "displayStage": display_stage(state, gates),
            "updatedAt": state["updatedAt"], "observedAt": observed, "gates": gates,
            "verified": verified(store, state), "requirements": rows, "tasks": tasks,
            "remaining": remaining, "remainingAssessed": current_assessment, "decisions": state["notes"],
            "elapsedSeconds": elapsed(state, observed), "finishedAt": state.get("finishedAt"),
            "estimate": estimate(estimate_tasks, status, observed), "activity": state.get("activity"),
            "waiting": state.get("waiting"), "runDirectory": "runs/" + state["run"]}


def publish(store, state):
    view = project(store, state)
    atomic_json(store.home / "status.json", view)
    return view
