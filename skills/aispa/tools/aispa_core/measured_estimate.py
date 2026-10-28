"""Remaining time from measurements only (policy flag measuredTiming).

No duration constant is read here. A class of work is estimated from this
run's finished samples; until the run has any for that class, from earlier
runs on this computer (calibration file); without both, the answer is "not
known yet" and the row carries no numbers. An unmeasured part leaves the upper
bound open instead of inventing one.

Classes: tasks by weight plus the critic mark (heavy+critic, heavy, standard,
light); stages outside tasks (branchReview, finalRepair, blind).
"""
from datetime import datetime, timezone
import math

from . import calibration as calibration_file
from . import timing, work_activity
from .work_activity import IDLE_CAP_SECONDS

KINDS = ("tasks", "branchReview", "finalRepair", "blind")
STAGES = KINDS[1:]
CLASSES = ("heavy+critic", "heavy", "standard", "light")
STAGE_OF_PHASE = {"integration": "branchReview", "blind": "blind"}
REPAIR_FINISHED = frozenset({"passed", "failed", "closed", "done", "checked", "accepted", "rejected",
                             "exhausted", "abandoned", "lost", "cancelled"})
OWNER_WAITS = ("owner_question", "owner_wait")
MAX_STAGE_SAMPLES = 64
MIN_CALIBRATION_SECONDS = 15.0  # Below this a "task" is a fixture, not work worth remembering.
_PROBES = {}


def enabled(state):
    return bool(state.get("policy", {}).get("measuredTiming"))


def interval(values):
    """(low, high): one value as is; 2-4 min..max; 5+ the 25th..80th percentile (linear)."""
    ordered = sorted(values)
    if len(ordered) < 5:
        return ordered[0], ordered[-1]

    def percentile(share):
        position = (len(ordered) - 1) * share
        left = int(math.floor(position))
        right = min(left + 1, len(ordered) - 1)
        return round(ordered[left] + (ordered[right] - ordered[left]) * (position - left), 3)
    return percentile(.25), percentile(.80)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _moment(value):
    return work_activity.parse(value) if isinstance(value, str) else None


def _waiting(state):
    return timing.waiting(state)


def _active(state):
    return state.get("status") == "running" and not _waiting(state)


def weight_tier(state, task):
    """The one source of a task's weight: the latest attempt's tier, the plan's tier before any attempt."""
    history = state.get("taskHistory", {}).get(task.get("id"), [])
    tier = history[-1].get("tier") if history else None
    return tier if tier is not None else task.get("tier")


def task_class(state, task):
    """The engine's weight of the latest attempt (what the panel shows); the plan tier only before one."""
    tier = weight_tier(state, task)
    tier = {"fast": "light", "light": "light", "standard": "standard"}.get(tier, "heavy")
    return tier if tier != "heavy" else ("heavy+critic" if _had_critic(state, task) else "heavy")


def _had_critic(state, task):
    kind = task.get("completionKind")
    if kind:
        return kind == "critic_acceptance"
    history = state.get("taskHistory", {}).get(task.get("id"), [])
    if not history:
        return True  # Not known yet: a heavy task is expected to meet a critic.
    attempt = history[-1]
    if attempt.get("rounds"):
        return True
    try:
        from . import task_tiers
        return "critic1" in task_tiers.roles(attempt)
    except Exception:
        return True


def _without_idle(seconds, started, ended, segments):
    start, end = _moment(started), _moment(ended)
    if seconds is None or start is None or end is None or not segments:
        return seconds
    return max(0.0, seconds - work_activity.overlap_seconds(segments, start, end))


def _spent(task, at, segments):
    value = timing.elapsed(task, at)
    return _without_idle(value, task.get("startedAt"), at, segments) or 0.0


def _task_seconds(task, segments):
    value = task.get("elapsedSeconds")
    if not _number(value) or value <= 0:
        return None
    return _without_idle(float(value), task.get("startedAt"), task.get("finishedAt"), segments)


def _run_task_samples(state, segments):
    samples = {}
    for task in state.get("tasks", []):
        if task.get("status") != "completed":
            continue
        seconds = _task_seconds(task, segments)
        if seconds:
            samples.setdefault(task_class(state, task), []).append(seconds)
    return samples


def _normalize(calibration):
    if isinstance(calibration, dict):
        return list(calibration.get("samples") or []), list(calibration.get("notes") or [])
    return list(calibration or []), []


def _pool(kind, klass, run_values, rows, runtime):
    """(values, source). This run's samples win; otherwise earlier runs, same runtime first."""
    if run_values:
        return run_values, "run"
    chosen = [row for row in rows if row.get("kind") == kind and row.get("class") == klass]
    chosen = [row for row in chosen if row.get("runtime") == runtime] or chosen
    return [row["seconds"] for row in chosen], ("calibration" if chosen else None)


def _basis(sources):
    sources = {source for source in sources if source}
    return None if not sources else sources.pop() if len(sources) == 1 else "mixed"


def _row(kind, **values):
    return {"kind": kind, "lowerSeconds": None, "upperSeconds": None, "basis": None,
            "sampleCount": 0, "status": "pending", **values}


def _critical_path(members):
    """Longest chain inside one wave over lower bounds; None for a dependency cycle."""
    by_id = {tid: deps for tid, (deps, _) in members.items()}
    memo, visiting = {}, set()

    def path(tid):
        if tid in memo:
            return memo[tid]
        if tid in visiting:
            raise ValueError("cycle")
        visiting.add(tid)
        deps = [dep for dep in by_id[tid] if dep in members]
        memo[tid] = members[tid][1] + max((path(dep) for dep in deps), default=0.0)
        visiting.remove(tid)
        return memo[tid]
    try:
        return max(path(tid) for tid in members)
    except ValueError:
        return None


def _tasks_row(state, at, rows, runtime, segments, notes):
    run_values = _run_task_samples(state, segments)
    outstanding = [t for t in state.get("tasks", []) if t.get("status") != "completed"]
    if not outstanding:
        return _row("tasks", lowerSeconds=0, upperSeconds=0, status="done"), [], [], False
    lows, highs, waves = {}, {}, {}
    unmeasured, overrunning, used, sources = [], [], {}, []
    for index, task in enumerate(outstanding):
        tid = task.get("id", "estimate-%d" % index)
        klass = task_class(state, task)
        values, source = _pool("task", klass, run_values.get(klass, []), rows, runtime)
        if not values:
            unmeasured.append(tid)
            lows[tid], highs[tid] = 0.0, None
        else:
            used[klass] = len(values)
            sources.append(source)
            low, high = interval(values)
            spent = _spent(task, at, segments) if task.get("status") == "running" else 0.0
            if task.get("status") == "running" and spent > high:
                overrunning.append(tid)
                lows[tid] = highs[tid] = 0.0
            else:
                lows[tid], highs[tid] = max(0.0, low - spent), max(0.0, high - spent)
        waves.setdefault(task.get("wave", 1), {})[tid] = (task.get("dependsOn", []), lows[tid])
    lower = 0.0
    for members in waves.values():
        path = _critical_path(members)
        if path is None:
            return None, [], [], True
        lower += path
    if not used:
        return _row("tasks"), unmeasured, overrunning, False
    upper = None if unmeasured else sum(highs.values())
    if unmeasured:
        notes.append("Для задач без замеров класса (%s) верхняя граница неизвестна: %s" % (
            ", ".join(sorted({task_class(state, t) for t in outstanding if t.get("id") in unmeasured})),
            ", ".join(unmeasured)))
    row = _row("tasks", lowerSeconds=round(lower), upperSeconds=None if upper is None else round(upper),
               basis=_basis(sources), sampleCount=sum(used.values()),
               status="running" if any(t.get("status") == "running" for t in outstanding) else "pending")
    return row, unmeasured, overrunning, False


def _stage_samples(state, stage):
    return [s["seconds"] for s in state.get("stageSamples", []) if isinstance(s, dict)
            and s.get("stage") == stage and _number(s.get("seconds")) and s["seconds"] > 0]


def _range_row(kind, status, values, source, spent, notes):
    """Numbers for a stage; `spent` is what a running stage has already used."""
    if status == "done":
        return _row(kind, lowerSeconds=0, upperSeconds=0, status="done", basis=source, sampleCount=len(values))
    if not values:
        return _row(kind, status=status)
    low, high = interval(values)
    if spent > high:
        notes.append("Этап %s идёт дольше обычного; его остаток числом не прибавляется" % kind)
        low = high = spent
    return _row(kind, lowerSeconds=round(max(0.0, low - spent)), upperSeconds=round(max(0.0, high - spent)),
                basis=source, sampleCount=len(values), status=status)


def _open_repairs(state):
    return [r for r in state.get("finalRepairs") or [] if isinstance(r, dict) and r.get("status") not in REPAIR_FINISHED]


def _stage_rows(state, at, rows, runtime, segments, notes):
    result = {}
    progress = state.get("workProgress") if isinstance(state.get("workProgress"), dict) else {}
    for kind in ("branchReview", "blind"):
        mine = _stage_samples(state, kind)
        running = STAGE_OF_PHASE.get(progress.get("phase")) == kind and not progress.get("finishedAt")
        finished = bool(mine) and not (kind == "blind" and any(r.get("kind") == "blind" for r in _open_repairs(state)))
        if running:
            status, spent = "running", _spent(progress, at, segments)
        else:
            status, spent = ("done" if finished else "pending"), 0.0
        values, source = _pool("stage", kind, mine, rows, runtime)
        result[kind] = _range_row(kind, status, mine if status == "done" else values, "run" if status == "done" else source,
                                  spent, notes)
    mine = _stage_samples(state, "finalRepair")
    values, source = _pool("stage", "finalRepair", mine, rows, runtime)
    open_repairs = _open_repairs(state)
    if not open_repairs:
        row = _range_row("finalRepair", "conditional", values, source, 0.0, notes)
    else:
        parts = []
        for repair in open_repairs:
            clock = state.get("stageClocks", {}).get("finalRepair:%s" % repair.get("id"), {})
            begun = _moment(clock.get("startedAt"))
            spent = max(0.0, (_moment(at) - begun).total_seconds()) if begun else 0.0
            if begun:
                spent = max(0.0, spent - work_activity.overlap_seconds(segments, begun, _moment(at)))
            parts.append(_range_row("finalRepair", "running", values, source, spent, notes))
        row = parts[0]
        if len(parts) > 1 and all(p["lowerSeconds"] is not None for p in parts):
            row = {**row, "lowerSeconds": sum(p["lowerSeconds"] for p in parts),
                   "upperSeconds": sum(p["upperSeconds"] for p in parts)}
    result["finalRepair"] = row
    return result


def _idle(segments):
    return sum((end - start).total_seconds() for start, end in segments)


def _work_seconds(state, at, segments):
    active = timing.elapsed(state, at)
    return None if active is None else round(max(0.0, active - _idle(segments)), 3)


def work_seconds(state, at, idle_probe=None):
    """Active run time minus idle time that did not count as work; None if the run has no clock."""
    return _work_seconds(state, at, work_activity.uncounted_segments(state, at, idle_probe))


def _work(state, at, segments):
    waits = state.get("waitTiming") or {}

    def waited(kinds):
        return round(sum(timing.elapsed(waits.get(kind, {}), at) or 0.0 for kind in kinds), 3)
    return {"workSeconds": _work_seconds(state, at, segments),
            "idleSeconds": round(_idle(segments) + waited(("resource_wait",)), 3),
            "ownerWaitSeconds": waited(OWNER_WAITS)}


def estimate(state, at, *, calibration=None, idle_probe=None):
    """The remaining-time answer for the panel; see interfaces.md, "Время"."""
    rows, notes = _normalize(calibration)
    notes = list(notes)
    segments = work_activity.uncounted_segments(state, at, idle_probe)
    work = _work(state, at, segments)
    status = state.get("status")
    if status in ("accepted", "delivered_with_remaining"):
        return {"reason": "finished", "work": work, "notes": notes}
    if status != "running":
        return {"reason": status, "work": work, "notes": notes}
    if not state.get("tasks"):
        return {"reason": "no_plan", "work": work, "notes": notes}
    runtime = state.get("runtime")
    tasks_row, unmeasured, overrunning, invalid = _tasks_row(state, at, rows, runtime, segments, notes)
    if invalid:
        return {"reason": "invalid_plan", "work": work, "notes": notes}
    stages = _stage_rows(state, at, rows, runtime, segments, notes)
    ordered = [tasks_row, stages["branchReview"], stages["finalRepair"], stages["blind"]]
    included = [row for row in ordered if row["status"] in ("pending", "running")]
    base = {"rows": ordered, "overrunning": overrunning, "unmeasuredTasks": unmeasured,
            "afterOverrun": bool(overrunning), "work": work, "notes": notes, "waitIncluded": False,
            "completedTasks": sum(t.get("status") == "completed" for t in state["tasks"]),
            "waitingForBlockers": any(t.get("status") == "blocked" for t in state["tasks"])}
    if overrunning:
        notes.append("Идёт дольше обычного: %s; остаток считается после неё" % ", ".join(overrunning))
    if not included:
        return {**base, "reason": "final_review", "basis": None, "lowerSeconds": 0, "upperSeconds": 0, "sampleCount": 0}
    measured = [row for row in included if row["lowerSeconds"] is not None]
    count = sum(row["sampleCount"] for row in ordered)
    if not measured or not any(row["sampleCount"] for row in included):
        return {**base, "reason": "insufficient_data", "basis": None, "lowerSeconds": None,
                "upperSeconds": None, "sampleCount": count}
    open_ended = len(measured) < len(included) or any(row["upperSeconds"] is None for row in included)
    return {**base, "reason": "estimate", "basis": _basis(row["basis"] for row in included),
            "lowerSeconds": sum(row["lowerSeconds"] for row in measured),
            "upperSeconds": None if open_ended else sum(row["upperSeconds"] for row in included),
            "sampleCount": count}


def for_state(store, state, at):
    """estimate() with this computer's calibration file and a throttled look at project files."""
    key = str(store.root)
    probe = _PROBES.get(key)
    if probe is None:
        probe = _PROBES[key] = work_activity.ThrottledProbe(work_activity.make_probe(store.root))
    return estimate(state, at, calibration=calibration_file.inspect(), idle_probe=probe)


def progress_stage_sample(state, at, segments=()):
    """(key, stage, seconds) for a finished stage interval not yet in stageSamples, else None."""
    record = state.get("workProgress")
    if not isinstance(record, dict) or not record.get("finishedAt") or not record.get("startedAt"):
        return None
    stage = STAGE_OF_PHASE.get(record.get("phase"))
    key = "%s:%s" % (record.get("phase"), record["startedAt"])
    if stage is None or any(s.get("key") == key for s in state.get("stageSamples", [])):
        return None
    seconds = _without_idle(timing.elapsed(record, at), record["startedAt"], record["finishedAt"], segments)
    return key, stage, seconds


def run_samples(state):
    """Calibration-shaped samples of everything a run finished: tasks and recorded stages."""
    at = state.get("finishedAt") or state.get("updatedAt") or datetime.now(timezone.utc).isoformat()
    segments = work_activity.uncounted_segments(state, at)
    rows = []
    for task in state.get("tasks", []):
        seconds = _task_seconds(task, segments) if task.get("status") == "completed" and task.get("finishedAt") else None
        if seconds:
            rows.append(_sample("task", task_class(state, task), seconds, state.get("runtime"), task["finishedAt"]))
    for stage in state.get("stageSamples", []):
        if isinstance(stage, dict) and _number(stage.get("seconds")) and stage["seconds"] > 0 and stage.get("finishedAt"):
            rows.append(_sample("stage", stage.get("stage"), stage["seconds"], state.get("runtime"), stage["finishedAt"]))
    return rows


def _sample(kind, klass, seconds, runtime, at):
    return {"kind": kind, "class": klass, "seconds": round(seconds, 3),
            "runtime": runtime if isinstance(runtime, str) and runtime else "unknown", "recordedAt": at}


class Tracker:
    """Per-Workflow bookkeeping at journal saves: idle marks, stage samples, calibration samples.

    prepare() runs before the journal append and changes only the state about
    to be saved; commit() writes the calibration file after the append
    succeeded. Nothing here may stop a save.
    """

    def __init__(self, state, probe=None):
        self.probe = probe
        self.root = None
        self.active = _active(state)
        self.seen = {self._key(t) for t in state.get("tasks", [])
                     if t.get("status") == "completed" and t.get("finishedAt")}

    @staticmethod
    def _key(task):
        return "%s:%s" % (task.get("id"), task.get("finishedAt"))

    def prepare(self, store, state, at):
        pending = []
        self.root = getattr(store, "root", None)
        try:
            probe = self.probe or work_activity.make_probe(store.root)
            previous = state.get("updatedAt")
            if self.active and previous:
                work_activity.record_gap(state, previous, at, probe)
            segments = work_activity.uncounted_segments(state, at)
            pending += self._tasks(state, at, segments)
            pending += self._progress_stage(state, at, segments)
            pending += self._repairs(state, at, segments)
        except Exception:
            pass
        self.active = _active(state)
        return [row for row in pending if row["seconds"] >= MIN_CALIBRATION_SECONDS]

    def _tasks(self, state, at, segments):
        pending = []
        for task in state.get("tasks", []):
            if task.get("status") != "completed" or not task.get("finishedAt") or self._key(task) in self.seen:
                continue
            self.seen.add(self._key(task))
            seconds = _task_seconds(task, segments)
            if seconds and len(state.get("taskHistory", {}).get(task.get("id"), [])) <= 1:
                pending.append(_sample("task", task_class(state, task), seconds, state.get("runtime"), at))
        return pending

    def _progress_stage(self, state, at, segments):
        found = progress_stage_sample(state, at, segments)
        if found is None:
            return []
        key, stage, seconds = found
        return self._stage_sample(state, key, stage, seconds, at)

    def _repairs(self, state, at, segments):
        pending = []
        repairs = [r for r in state.get("finalRepairs") or [] if isinstance(r, dict) and r.get("id")]
        for repair in repairs:
            key = "finalRepair:%s" % repair["id"]
            clock = state.setdefault("stageClocks", {}).setdefault(key, {"startedAt": at})
            if repair.get("status") in REPAIR_FINISHED and "finishedAt" not in clock:
                clock["finishedAt"] = at
                begun = _moment(clock["startedAt"])
                seconds = _without_idle((_moment(at) - begun).total_seconds(), clock["startedAt"], at, segments)
                pending += self._stage_sample(state, key, "finalRepair", seconds, at)
        return pending

    @staticmethod
    def _stage_sample(state, key, stage, seconds, at):
        if not seconds or seconds <= 0:
            return []
        rows = state.setdefault("stageSamples", [])
        rows.append({"key": key, "stage": stage, "seconds": round(seconds, 3), "finishedAt": at})
        del rows[:-MAX_STAGE_SAMPLES]
        return [_sample("stage", stage, seconds, state.get("runtime"), at)]

    def commit(self, pending):
        for sample in pending:
            calibration_file.record(sample, root=self.root)
