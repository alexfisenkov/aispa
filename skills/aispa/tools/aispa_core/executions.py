"""Append-only references to commands actually executed by AISpa.

The host owns the Journal lock. ``record`` follows the executor's journal save;
the host must append the mutated state as ``execution-recorded``. This history
describes past executions and never grants a current verification PASS.
"""
import copy
import hashlib
import json
import re

from . import carryover, cleanroom, discovery
from .contracts import artifact_valid
from .store import Invalid, confined, digest, timestamp


SCHEMA = 1
PURPOSES = ("install", "run", "check", "test")
KINDS = ("project", "task", "cleanrun")
HOT_LIMIT = 32
MAX_VIEW = 100
MAX_BYTES = 1024 * 1024
SHA = re.compile(r"[0-9a-f]{64}\Z")


def _require(condition, message):
    if not condition:
        raise Invalid("AISpa executions: " + message)


def _raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write(store, name, value):
    raw = _raw(value)
    _require(len(raw) <= MAX_BYTES, "слишком большой артефакт истории")
    sha = hashlib.sha256(raw).hexdigest()
    path = confined(store.run, "executions/" + name + "-" + sha + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        _require(path.read_bytes() == raw, "изменён артефакт истории")
    return {"file": path.relative_to(store.run).as_posix(), "sha256": sha}


def _read(store, ref, prefix):
    _require(isinstance(ref, dict) and isinstance(ref.get("file"), str)
             and ref["file"].startswith("executions/" + prefix + "-")
             and ref["file"].endswith(".json") and isinstance(ref.get("sha256"), str)
             and SHA.fullmatch(ref["sha256"]), "неверная ссылка истории")
    path = confined(store.run, ref["file"])
    _require(path.is_file() and path.stat().st_size <= MAX_BYTES, "артефакт истории отсутствует или слишком велик")
    raw = path.read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == ref["sha256"], "изменён артефакт истории")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise Invalid("AISpa executions: повреждён JSON истории") from exc
    _require(isinstance(value, dict), "неверный артефакт истории")
    return value


def _last_event(store, state):
    journal = store.read_journal()
    last = journal["last"]
    _require(last["state"]["run"] == state["run"]
             and digest(last["state"]) == digest(state), "журнал изменился или состояние не сохранено")
    return last, journal["previous"]


def _started(previous, event, kind, task_id):
    """A task completion must not relabel the stale project receipt beside it."""
    _require(isinstance(previous, dict) and previous.get("sequence", 0) + 1 == event.get("sequence"),
             "нет начала соответствующего исполнения")
    start_state = previous.get("state", {})
    activity = start_state.get("activity") or {}
    if kind == "cleanrun":
        _require(previous.get("event") == "clean-verification-started"
                 and activity.get("kind") == "clean-verification" and not start_state.get("cleanrun"),
                 "нет начала проверки чистого клона")
    else:
        _require(previous.get("event") == "verification-started" and activity.get("kind") == "verification"
                 and activity.get("task") == (task_id if kind == "task" else None),
                 "вид или задача исполнения не совпадает с реально запущенной командой")
        if kind == "task":
            attempts = start_state.get("taskHistory", {}).get(task_id, [])
            _require(attempts and attempts[-1].get("verification") is None,
                     "проверка задачи не была начата")
        else:
            _require(start_state.get("verification") is None, "общая проверка не была начата")


def _receipt(store, state, kind, task_id):
    _require(kind in KINDS, "неверный вид проверки")
    if kind == "task":
        _require(isinstance(task_id, str) and task_id, "нужен ID задачи")
        attempts = state.get("taskHistory", {}).get(task_id, [])
        _require(isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict), "попытка задачи отсутствует")
        receipt = attempts[-1].get("verification")
        _require(isinstance(receipt, dict) and receipt.get("task") == task_id
                 and receipt.get("attempt") == attempts[-1].get("id")
                 and receipt.get("candidate") == receipt.get("code"), "receipt не соответствует попытке")
    else:
        _require(task_id is None, "ID задачи здесь недопустим")
        receipt = state.get("verification" if kind == "project" else "cleanrun")
    _require(isinstance(receipt, dict), "receipt отсутствует")
    validate_command(receipt.get("command"))
    _require(artifact_valid(store, receipt.get("output", {})), "вывод выполненной команды изменён")
    if kind == "cleanrun":
        _require(receipt.get("run") == state["run"] and cleanroom._receipt_valid(store, receipt),
                 "receipt чистого клона изменён")
    else:
        _require(isinstance(receipt.get("code"), str) and SHA.fullmatch(receipt["code"]),
                 "неверный снимок кода")
    return receipt


def validate_command(command):
    """Run before journaling or spawning; history must not discover secrets late."""
    _require(isinstance(command, list) and 0 < len(command) <= 64
             and isinstance(command[0], str) and command[0].strip()
             and all(isinstance(arg, str) and "\0" not in arg and len(arg) <= 2000
                     for arg in command), "неверные аргументы команды")
    text = " ".join(command)
    _require(len(text) <= 8000 and not discovery.SECRET_CONTENT.search(text)
             and not carryover.SECRET.search(text), "возможный секрет в аргументах")


def _index(state):
    index = state.get("executions")
    if index is None:
        return {"schema": SCHEMA, "count": 0, "recent": [], "archive": None}
    _require(isinstance(index, dict) and index.get("schema") == SCHEMA
             and type(index.get("count")) is int and index["count"] >= 0
             and isinstance(index.get("recent"), list) and len(index["recent"]) <= HOT_LIMIT
             and (index.get("archive") is None or isinstance(index["archive"], dict)), "повреждён индекс истории")
    return index


def _refs(store, state):
    index = _index(state)
    seen = set()
    for ref in reversed(index["recent"]):
        _require(isinstance(ref, dict) and ref.get("sha256") not in seen, "повторная ссылка истории")
        seen.add(ref["sha256"])
        yield ref
    page_ref = index["archive"]
    while page_ref is not None:
        _require(page_ref.get("sha256") not in seen, "цикл архива истории")
        seen.add(page_ref["sha256"])
        page = _read(store, page_ref, "archive")
        _require(page.get("schema") == SCHEMA and page.get("run") == state["run"]
                 and isinstance(page.get("refs"), list) and 0 < len(page["refs"]) <= HOT_LIMIT,
                 "повреждена страница архива")
        for ref in reversed(page["refs"]):
            _require(isinstance(ref, dict) and ref.get("sha256") not in seen, "повторная ссылка истории")
            seen.add(ref["sha256"])
            yield ref
        page_ref = page.get("previous")


def record(store, state, *, kind, purpose="check", task_id=None):
    """Capture the latest genuine executor receipt; caller then appends state."""
    _require(state.get("status") == "running" and purpose in PURPOSES, "неверное назначение или закрытый прогон")
    last, previous = _last_event(store, state)
    expected = "clean-verification-recorded" if kind == "cleanrun" else "verification-executed"
    _require(last.get("event") == expected, "записывайте историю сразу после выполненной команды")
    receipt = _receipt(store, state, kind, task_id)
    _started(previous, last, kind, task_id)
    if kind == "task":
        _require(last["state"].get("taskHistory", {}).get(task_id, [{}])[-1].get("verification") == receipt,
                 "receipt задачи отсутствует в последнем событии")
    else:
        field = "cleanrun" if kind == "cleanrun" else "verification"
        _require(last["state"].get(field) == receipt, "receipt отсутствует в последнем событии")
    index = _index(state)
    body = {"schema": SCHEMA, "run": state["run"], "kind": kind, "purpose": purpose,
            "task": task_id, "attempt": receipt.get("attempt"), "command": copy.deepcopy(receipt["command"]),
            "receiptSha256": digest(receipt), "output": copy.deepcopy(receipt["output"]),
            "code": receipt.get("sourceCode" if kind == "cleanrun" else "code"),
            "candidate": receipt.get("candidate"), "exitCode": receipt.get("exitCode"),
            "timedOut": bool(receipt.get("timedOut")), "truncated": bool(receipt.get("truncated")),
            "redacted": bool(receipt.get("redacted")), "changedCode": bool(receipt.get("changedCode")),
            "changedSource": bool(receipt.get("changedSource")),
            **({"launchError": bool(receipt["launchError"])} if "launchError" in receipt else {}),
            "successful": receipt.get("exitCode") == 0 and not any(receipt.get(key) for key in
                          ("timedOut", "truncated", "redacted", "changedCode", "changedSource"))
                          and (kind != "cleanrun" or receipt.get("passed") is True),
            "at": receipt.get("at"), "recordedAt": timestamp(),
            "journal": {"sequence": last["sequence"], "hash": last["hash"]}}
    _require(isinstance(body["code"], str) and SHA.fullmatch(body["code"]), "неверный code SHA")
    body["id"] = digest({k: v for k, v in body.items() if k != "id"})
    ref = {**_write(store, "record", body), "id": body["id"]}
    recent = [*index["recent"], ref]
    archive = index["archive"]
    if len(recent) > HOT_LIMIT:
        page = {"schema": SCHEMA, "run": state["run"], "refs": recent[:HOT_LIMIT], "previous": archive}
        archive = _write(store, "archive", page)
        recent = recent[HOT_LIMIT:]
    state["executions"] = {"schema": SCHEMA, "count": index["count"] + 1,
                           "recent": recent, "archive": archive}
    return copy.deepcopy(ref)


def _resolve(store, state, ref, journal):
    _require(isinstance(ref, dict) and isinstance(ref.get("sha256"), str)
             and SHA.fullmatch(ref["sha256"]), "нужна точная SHA-ссылка исполнения")
    match = next((item for item in _refs(store, state) if item.get("sha256") == ref["sha256"]
                  and ("id" not in ref or item.get("id") == ref["id"])), None)
    _require(match is not None, "исполнение не записано в этом прогоне")
    body = _read(store, match, "record")
    _require(body.get("schema") == SCHEMA and body.get("run") == state["run"]
             and body.get("id") == match.get("id")
             and body["id"] == digest({k: v for k, v in body.items() if k != "id"})
             and artifact_valid(store, body.get("output", {})), "повреждена запись исполнения")
    # The immutable artifact and index must point back to an actual saved
    # executor event, rather than merely to a plausible receipt object.
    _require(journal["last"]["state"]["run"] == state["run"]
             and digest(journal["last"]["state"]) == digest(state),
             "журнал изменился или состояние не сохранено")
    anchor = body.get("journal", {})
    _require(type(anchor.get("sequence")) is int and anchor["sequence"] > 0,
             "нет события исполнения")
    sequence = anchor["sequence"]
    event, previous = store.event_pair(sequence)
    expected = "clean-verification-recorded" if body["kind"] == "cleanrun" else "verification-executed"
    _require(event is not None and event.get("hash") == anchor.get("hash")
             and event.get("event") == expected, "событие исполнения отсутствует")
    _started(previous, event, body["kind"], body.get("task"))
    if body["kind"] == "task":
        attempts = event["state"].get("taskHistory", {}).get(body["task"], [])
        receipt = attempts[-1].get("verification") if attempts else None
    else:
        receipt = event["state"].get("cleanrun" if body["kind"] == "cleanrun" else "verification")
    _require(isinstance(receipt, dict) and digest(receipt) == body["receiptSha256"]
             and receipt.get("command") == body["command"] and receipt.get("output") == body["output"]
             and receipt.get("exitCode") == body["exitCode"], "receipt не соответствует событию")
    expected_success = (receipt.get("exitCode") == 0
                        and not any(receipt.get(key) for key in
                                    ("timedOut", "truncated", "redacted", "changedCode", "changedSource"))
                        and (body["kind"] != "cleanrun" or receipt.get("passed") is True))
    _require(body.get("kind") in KINDS and body.get("purpose") in PURPOSES
             and body.get("task") == (receipt.get("task") if body["kind"] == "task" else None)
             and body.get("attempt") == receipt.get("attempt")
             and body.get("candidate") == receipt.get("candidate")
             and body.get("code") == receipt.get("sourceCode" if body["kind"] == "cleanrun" else "code")
             and body.get("at") == receipt.get("at")
             and body.get("successful") is expected_success
             and body.get("launchError", False) is bool(receipt.get("launchError"))
             and all(body.get(field) is bool(receipt.get(field)) for field in
                     ("timedOut", "truncated", "redacted", "changedCode", "changedSource")),
             "метаданные исполнения не соответствуют receipt")
    return body


def resolve(store, state, ref):
    """Resolve only an indexed current-run execution on a fully verified chain."""
    return _resolve(store, state, ref, store.read_journal())


def view(store, state, *, kind=None, purpose=None, limit=20):
    """Bounded summaries and exact refs; no process output or host paths."""
    _require(kind is None or kind in KINDS, "неверный вид выборки")
    _require(purpose is None or purpose in PURPOSES, "неверное назначение выборки")
    _require(type(limit) is int and 1 <= limit <= MAX_VIEW, "неверный лимит выборки")
    journal = store.read_journal()
    items = []
    for ref in _refs(store, state):
        body = _resolve(store, state, ref, journal)
        if (kind and body["kind"] != kind) or (purpose and body["purpose"] != purpose):
            continue
        items.append({"ref": {"id": body["id"], "sha256": ref["sha256"]},
                      "kind": body["kind"], "purpose": body["purpose"], "task": body["task"],
                      "attempt": body["attempt"], "exitCode": body["exitCode"],
                      "successful": body["successful"], "code": body["code"],
                      "candidate": body["candidate"], "outputSha256": body["output"]["sha256"],
                      "at": body["at"]})
        if len(items) >= limit:
            break
    return {"schema": SCHEMA, "historicalOnly": True, "count": _index(state)["count"],
            "executions": items, "rule": "Historical execution is not a current PASS."}
