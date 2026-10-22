"""Dashboard progress and deliberately approximate estimates; never acceptance decisions."""
from statistics import median

from .timing import elapsed


def display_stage(state, gates):
    if not state.get("sealed"):
        return "requirements" if state["requirements"] else "brief"
    if not state["spec"]:
        return "spec"
    if not gates["coverage"]:
        return "coverage"
    if not gates["plan"]:
        return "plan"
    return "acceptance" if state["stage"] == "acceptance" else "build"


def estimate(tasks, status, at):
    if status in ("accepted", "delivered_with_remaining"):
        return {"reason": "finished"}
    if status != "running":
        return {"reason": status}
    if any(t["status"] == "blocked" for t in tasks):
        return {"reason": "blocked"}
    if not tasks:
        return {"reason": "no_plan"}
    outstanding = [t for t in tasks if t["status"] != "completed"]
    if not outstanding:
        return {"reason": "final_review"}
    samples = [elapsed(t, at) for t in tasks if t["status"] == "completed"]
    samples = [s for s in samples if s is not None and s > 0]
    if len(samples) < 2:
        return {"reason": "insufficient_data", "sampleCount": len(samples)}
    typical = median(samples)
    by_tier = {}
    for tier in ("light", "standard", "heavy"):
        values = [elapsed(t, at) for t in tasks if t["status"] == "completed" and t.get("tier", "heavy") == tier]
        values = [value for value in values if value is not None and value > 0]
        if len(values) >= 2:
            by_tier[tier] = median(values)
    waves = {}
    for task in outstanding:
        spent = elapsed(task, at) or 0
        task_typical = by_tier.get(task.get("tier", "heavy"), typical)
        # An overrun is not a claim that only a few seconds remain.
        remaining = task_typical if spent >= task_typical else max(task_typical * .25, task_typical - spent)
        waves.setdefault(task["wave"], []).append(remaining)
    parallel = sum(max(times) for times in waves.values())
    serial = sum(sum(times) for times in waves.values())
    reserve = max(60, parallel * .25)
    return {"reason": "estimate", "sampleCount": len(samples), "medianTaskSeconds": typical,
            "lowerSeconds": round(parallel * .7 + reserve),
            "upperSeconds": round(serial * 1.6 + reserve * 2),
            "reviewReserveSeconds": round(reserve)}
