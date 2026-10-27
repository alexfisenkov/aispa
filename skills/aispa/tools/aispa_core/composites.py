"""Composite task commands: `task-open` and `task-close` call the same methods as the
separate commands, in the same order, each step saved by its own event.

The module only sequences: it adds no checks, events or receipts of its own. Every
step is the method a separate command calls and is saved by that method's own
`flow.save(...)`. A failing step stops the command and names itself; the steps
before it stay recorded, and a repeated command skips what the state already shows
as done (task running, participant assigned, report accepted, usage imported).
"""
from contextlib import contextmanager
import hashlib
from pathlib import Path

from . import controls, packets, participants
from .contracts import require
from .store import Invalid, confined, digest
from .team import ROLES

# Task phase -> the role that acts next in it.
PHASE_ROLES = {"implementation": "worker", "repair": "worker", "critic_1": "critic1",
               "critic_2": "critic2", "task_acceptance": "acceptor"}
USAGE_FORMATS = {"codex": "codex", "claude-code": "claude"}


@contextmanager
def _step(command, steps, name):
    """Name the failing step; resource refusals and other typed errors pass through untouched."""
    try:
        yield
    except Invalid as error:
        if type(error) is not Invalid or str(error).startswith("RESOURCE_LIMIT "):
            raise
        done = ", ".join(row["step"] for row in steps if row["status"] == "done") or "нет"
        raise Invalid("%s: остановлено на шаге «%s»: %s. Выполненные шаги записаны: %s; "
                      "повторите команду, она продолжит с этого шага" % (command, name, error, done)) from error


def _mark(steps, name, status="done"):
    steps.append({"step": name, "status": status})


def _attempt(flow, tid):
    return flow.s["taskHistory"][tid][-1]


def _file_ref(store, ref):
    return {"file": ref["file"], "sha256": ref["sha256"], "absolutePath": str(confined(store.run, ref["file"]))}


def open_task(flow, tid, role, *, runtime, native_id, session, model, reasoning, model_evidence=None):
    """context (worker basis) -> start -> assign -> role card -> role packet; one receipt."""
    require(role in ROLES, "Неизвестная роль")
    task = flow.task(tid)
    steps = []
    receipt = {"steps": steps}
    store, state = flow.store, flow.s
    if role == "worker":
        if task["status"] == "running":
            _mark(steps, "context", "skipped")
            _mark(steps, "start", "skipped")
        else:
            with _step("task-open", steps, "context"):
                # No role yet: the worker's card is made by `start` and the assignment, as in a separate sequence.
                receipt["basis"] = packets.build(store, state, flow.pipeline, "task", tid)["basis"]
            _mark(steps, "context")
            with _step("task-open", steps, "start"):
                flow.start(tid, receipt["basis"])
            _mark(steps, "start")
    with _step("task-open", steps, "assign"):
        values = (runtime, native_id, session)
        identity = (digest([runtime.strip().casefold(), native_id.strip(), session.strip()])
                    if all(isinstance(value, str) for value in values) else None)
        if identity is not None and _attempt(flow, tid)["actors"].get(role) == identity:
            receipt["actor"] = identity
            skipped = True
        else:
            receipt["actor"] = flow.pipeline.assign(tid, role, runtime, native_id, session, model=model,
                                                    reasoning=reasoning, model_evidence=model_evidence)
            skipped = False
    _mark(steps, "assign", "skipped" if skipped else "done")
    if role == "worker":
        # The form comes only after the targeted check (`template worker`): it carries that check's receipts.
        with _step("task-open", steps, "card"):
            packet = packets.build(store, state, flow.pipeline, "task", tid, role)
            card = packet["roles"].get(role)
            require(card is not None, "У роли нет карточки; повторите команду")
            receipt["card"] = _file_ref(store, card)
        _mark(steps, "card")
        with _step("task-open", steps, "packet"):
            receipt["context"] = packets.write(store, packet)
        _mark(steps, "packet")
    else:
        with _step("task-open", steps, "packet"):
            bundle = packets.write_task_bundle(store, state, flow.pipeline, tid, role)
            receipt.update(card=bundle["card"], context=bundle["context"], template=bundle["template"])
        _mark(steps, "card")
        _mark(steps, "packet")
    return receipt


def _sha(flow, report_path):
    return hashlib.sha256(flow.read_input(report_path).encode()).hexdigest()


def _accepted(attempt, role, sha):
    """The record of this exact report when the state already holds it, else None."""
    if role == "worker":
        records = [attempt.get("submitted"), attempt.get("verifiedCompletion"), attempt.get("repair")]
    elif role == "acceptor":
        records = [attempt.get("acceptance")]
    else:
        number = int(role[-1])
        records = attempt["rounds"][number - 1:number]
    return next((row for row in records if isinstance(row, dict) and (row.get("report") or {}).get("sha256") == sha), None)


def _record(flow, tid, role, path, sha):
    """Run the method the separate command runs: submit or repair, critic round, acceptance."""
    attempt = _attempt(flow, tid)
    if role == "worker":
        method = flow.pipeline.repair if attempt["rounds"] else flow.pipeline.submit
        result = method(tid, path)
        return True, result.get("report"), result.get("completionKind")
    if role == "acceptor":
        ok = flow.complete_task(tid, path)
        return ok is not False, _attempt(flow, tid)["acceptance"]["report"], None
    number = int(role[-1])
    ok = flow.pipeline.review(tid, number, path)
    return ok is not False, _attempt(flow, tid)["rounds"][number - 1]["report"], None


def _usage_done(flow, path, fmt, participant):
    """True when this exact log of this participant is already imported (repeat after a later failure)."""
    source = Path(path)
    try:
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()
    except OSError:
        return False
    return any(row.get("sourceHash") == sha and row.get("format") == fmt and row.get("participant") == participant
               and row.get("sourceKey") == key and not row.get("final")
               and not row.get("includesFinalCoordinatorReply") for row in flow.s.get("usageImports", []))


def _next(flow, tid):
    task = flow.task(tid)
    if task["status"] not in ("completed", "blocked"):
        role = PHASE_ROLES.get(task.get("phase"))
        return {"role": role} if role else {}
    result = {}
    if task["status"] == "blocked":
        result["blocked"] = task.get("note")
    try:
        result["ready"] = flow.ready()
    except Invalid as error:
        result["readyUnavailable"] = str(error)
    return result


def close_task(flow, tid, role, report_path, *, usage_path=None):
    """submit or critic review -> documents --refresh -> usage-import (if a log is given) -> next step."""
    require(role in ROLES, "Неизвестная роль")
    flow.task(tid)
    steps = []
    receipt = {"steps": steps}
    with _step("task-close", steps, "report"):
        sha = _sha(flow, report_path)
        record = _accepted(_attempt(flow, tid), role, sha)
        if record is not None:
            ok = True if role in ("worker", "acceptor") else record.get("passed") is True
            report, kind = record["report"], record.get("completionKind")
        else:
            ok, report, kind = _record(flow, tid, role, report_path, sha)
    _mark(steps, "report", "done" if record is None else "skipped")
    receipt.update(ok=ok, report=report, phase=flow.task(tid).get("phase"))
    if kind:
        receipt["completionKind"] = kind
    with _step("task-close", steps, "documents"):
        controls.documents_refresh(flow)
    _mark(steps, "documents")
    if usage_path:
        with _step("task-close", steps, "usage"):
            actor = _attempt(flow, tid)["actors"].get(role)
            identity = participants.resolve(flow.store, flow.s, actor)
            fmt = USAGE_FORMATS.get(identity["runtime"]) if identity else None
            require(fmt is not None, "Для runtime участника нет формата usage; импортируйте лог командой usage-import")
            skipped = _usage_done(flow, usage_path, fmt, actor)
            if not skipped:
                controls.usage_import(flow, usage_path, format=fmt, participant=actor)
        _mark(steps, "usage", "skipped" if skipped else "done")
    receipt["next"] = _next(flow, tid)
    return receipt
