"""Dashboard progress and deliberately approximate estimates; never acceptance decisions."""
from statistics import median
import math

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


# Deliberately broad planning assumptions, not measured task durations.
PLAN_SECONDS = {"fast": 600, "light": 900, "standard": 1800, "heavy": 3600}
PHASE_REMAINING = {"critic_1": .45, "repair": .65, "critic_2": .25, "task_acceptance": .12}


def _seconds(task, at, projected):
    if not projected:
        return elapsed(task, at)
    value = task.get("elapsedSeconds")
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def estimate(tasks, status, at, *, projected=False):
    """Raw records by default; projected=True consumes already observed clocks.

    A display projection retains activeSince for the browser, but its elapsed
    value already includes that interval. Never add it again for an estimate.
    """
    if status in ("accepted", "delivered_with_remaining"):
        return {"reason": "finished"}
    if status != "running":
        return {"reason": status}
    if not tasks:
        return {"reason": "no_plan"}
    outstanding = [t for t in tasks if t["status"] != "completed"]
    if not outstanding:
        return {"reason": "final_review"}
    by_tier = {}
    for tier in PLAN_SECONDS:
        values = [_seconds(t, at, projected) for t in tasks if t["status"] == "completed" and t.get("tier", "heavy") == tier]
        by_tier[tier] = [value for value in values if value is not None and value > 0]
    waves, durations, tier_estimates = {}, {}, {}
    for index, task in enumerate(outstanding):
        spent = _seconds(task, at, projected) or 0
        tier = task.get("tier", "heavy")
        samples = by_tier.get(tier, [])
        task_typical = median(samples) if samples else PLAN_SECONDS.get(tier, PLAN_SECONDS["heavy"])
        if tier not in tier_estimates:
            tier_estimates[tier] = {"tier": tier, "remainingTasks": 0, "sampleCount": len(samples),
                                    "basis": "observed_tasks" if samples else "plan_assumption",
                                    "confidence": "approximate" if len(samples) >= 2 else "low",
                                    "typicalSeconds": task_typical}
        tier_estimates[tier]["remainingTasks"] += 1
        # An overrun is not a claim that only a few seconds remain.
        remaining = task_typical if spent >= task_typical else max(task_typical * .25, task_typical - spent)
        share = PHASE_REMAINING.get(task.get("phase"))
        if share is not None and not task.get("phaseStale"):
            phase = task.get("phaseTiming") or {}
            phase_spent = _seconds(phase, at, projected) or 0
            portion = task_typical * share
            remaining = portion if phase_spent >= portion else max(task_typical * .08, portion - phase_spent)
        tid = task.get("id", "estimate-" + str(index))
        durations[tid] = remaining
        waves.setdefault(task["wave"], []).append((tid, task))
    parallel = 0
    for members in waves.values():
        members_by_id = dict(members)
        memo, visiting = {}, set()
        def path(tid):
            if tid in memo:
                return memo[tid]
            if tid in visiting:
                raise ValueError("cyclic estimate plan")
            visiting.add(tid)
            deps = [dep for dep in members_by_id[tid].get("dependsOn", []) if dep in members_by_id]
            memo[tid] = durations[tid] + max((path(dep) for dep in deps), default=0)
            visiting.remove(tid)
            return memo[tid]
        try:
            parallel += max(path(tid) for tid in members_by_id)
        except ValueError:
            return {"reason": "invalid_plan"}
    serial = sum(durations.values())
    reserve = max(60, parallel * .25)
    calibrated = all(row["confidence"] == "approximate" for row in tier_estimates.values())
    sources = {row["basis"] for row in tier_estimates.values()}
    lower, upper = (.7, 1.6) if calibrated else (.4, 2.5)
    used_samples = [value for tier in tier_estimates for value in by_tier.get(tier, [])]
    return {"reason": "estimate", "sampleCount": len(used_samples),
            "medianTaskSeconds": median(used_samples) if used_samples and len(tier_estimates) == 1 else None,
            "basis": next(iter(sources)) if len(sources) == 1 else "mixed",
            "confidence": "approximate" if calibrated else "low", "phaseAware": True,
            "tierEstimates": list(tier_estimates.values()),
            "waitingForBlockers": any(t["status"] == "blocked" for t in outstanding),
            "waitIncluded": False,
            "lowerSeconds": max(1, round(parallel * lower + reserve)),
            "upperSeconds": max(1, round(serial * upper + reserve * 2)),
            "reviewReserveSeconds": round(reserve)}
