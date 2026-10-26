"""Recorded wall-clock intervals. Explicit pauses are excluded; CPU time is not measured."""
from datetime import datetime
import math

FIELDS = ("startedAt", "activeSince", "elapsedSeconds", "finishedAt", "attempts")
TASK_PHASES = ("implementation", "critic_1", "repair", "critic_2", "task_acceptance")
WAIT_KINDS = frozenset({"owner_question", "owner_wait", "resource_wait", "blocked", "paused"})


def waiting(state):
    value = state.get("waiting")
    return isinstance(value, dict) and value.get("kind") in WAIT_KINDS


def begin_wait(state, kind, at):
    from .contracts import require
    require(kind in WAIT_KINDS, "Timing: unknown waiting kind")
    if waiting(state):
        old = state["waiting"]["kind"]
        stop(state.setdefault("waitTiming", {}).setdefault(old, {}), at)
    stop(state, at)
    for task in state.get("tasks", []):
        stop(task, at)
        if task.get("phaseTiming"):
            stop(task["phaseTiming"], at)
    if state.get("workProgress"):
        stop(state["workProgress"], at)
    state["waiting"] = {"kind": kind, "since": at}
    start(state.setdefault("waitTiming", {}).setdefault(kind, {}), at)


def end_wait(state, at):
    if waiting(state):
        stop(state.setdefault("waitTiming", {}).setdefault(state["waiting"]["kind"], {}), at)
    state["waiting"] = None
    if state.get("status") == "running":
        start(state, at)
        for task in state.get("tasks", []):
            if task.get("status") == "running":
                start(task, at)
                if task.get("phaseTiming"):
                    start(task["phaseTiming"], at)
        if state.get("workProgress") and not state["workProgress"].get("finishedAt"):
            start(state["workProgress"], at)


def summary(state, at):
    endpoint = state.get("finishedAt") or at
    return {"wallSeconds": difference(endpoint, state["startedAt"]) if state.get("startedAt") else None,
            "activeSeconds": elapsed(state, at),
            "waitSeconds": {kind: elapsed(row, at) for kind, row in state.get("waitTiming", {}).items()},
            "measurement": "recorded wall-clock intervals; actor intervals are not summed"}


def difference(end, start):
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


def elapsed(record, at):
    if not record.get("startedAt") and "elapsedSeconds" not in record:
        return None
    value = record.get("elapsedSeconds", 0.0)
    # A projected legacy/queued task may explicitly contain null. Unknown
    # clocks stay unknown; booleans and non-finite values are not durations.
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    if record.get("activeSince"):
        try:
            value += difference(at, record["activeSince"])
        except (TypeError, ValueError, OverflowError):
            return None
    return round(value, 3)


def start(record, at):
    record.setdefault("startedAt", at)
    record.setdefault("elapsedSeconds", 0.0)
    if not record.get("activeSince"):
        record["activeSince"] = at
    record.pop("finishedAt", None)


def stop(record, at, completed=False):
    value = elapsed(record, at)
    if value is not None:
        record["elapsedSeconds"] = value
    record.pop("activeSince", None)
    if completed:
        record["finishedAt"] = at


def start_phase(task, at):
    """Start a measured phase only after the task actually starts."""
    phase = task.get("phase")
    if phase not in TASK_PHASES:
        return
    clock = task.get("phaseTiming")
    if clock is None or clock.get("phase") != phase:
        task["phaseTiming"] = {"phase": phase, "startedAt": at, "activeSince": at,
                               "elapsedSeconds": 0.0}
    else:
        start(clock, at)


def sync_phase(task, at, run_running):
    """Persist an existing clock's transition before the journal append.

    Legacy tasks without a phase clock stay unknown. A read or an unrelated
    save must never invent their earlier phase start.
    """
    clock = task.get("phaseTiming")
    if clock is None:
        return
    active = run_running and task.get("status") == "running"
    if clock.get("phase") != task.get("phase"):
        stop(clock, at)
        if active:
            start_phase(task, at)
        else:
            task.pop("phaseTiming", None)
    elif active:
        start(clock, at)
    else:
        stop(clock, at)


def phase_view(task, at):
    clock = task.get("phaseTiming")
    if clock is None:
        return None
    return {**clock, "elapsedSeconds": elapsed(clock, at)}
