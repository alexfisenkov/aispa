"""Read-only projection. A view never decides whether a requirement is accepted."""
from .contracts import active, gate_valid, verified
from .store import atomic_json, timestamp

LABELS = {"running": "В работе", "paused": "Приостановлено", "blocked": "Есть препятствие",
          "cancelled": "Отменено", "accepted": "Все действующие требования приняты",
          "delivered_with_remaining": "Основная часть готова, есть остатки"}


def project(store, state):
    gates = {name: gate_valid(store, state, name) for name in ("coverage", "plan", "acceptance")}
    status = state["status"]
    if status in ("accepted", "delivered_with_remaining") and not gates["acceptance"]:
        status = "stale"
    rows = []
    for req in state["requirements"]:
        holders = [t for t in state["tasks"] if req["id"] in t["requirements"]]
        implementation = "unplanned" if not holders else (
            "implemented" if all(t["status"] == "completed" for t in holders) else "unfinished")
        rows.append({"id": req["id"], "text": req["text"], "core": req["id"] in state.get("core", []),
                     "kind": req["kind"], "scope": req["scope"], "implementation": implementation})
    return {"version": state["version"], "title": state["title"], "run": state["run"],
            "status": status, "label": LABELS.get(status, "Проект изменён; приёмка устарела"),
            "stage": state["stage"], "updatedAt": state["updatedAt"], "observedAt": timestamp(), "gates": gates,
            "verified": verified(store, state), "requirements": rows, "tasks": state["tasks"],
            "remaining": state.get("remaining", {}), "decisions": state["notes"],
            "waiting": state.get("waiting"), "runDirectory": "runs/" + state["run"]}


def publish(store, state):
    view = project(store, state)
    atomic_json(store.home / "status.json", view)
    return view
