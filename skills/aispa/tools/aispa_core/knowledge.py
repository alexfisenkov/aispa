"""Curated, source-bound lessons from accepted code, never a current test PASS.

The host owns locks and journal writes. Native identities are declared by the
host, as with task roles; this module cannot authenticate a model or spawn one.
Recording follows final acceptance and precedes finish. Historical knowledge is
queried explicitly, bounded, and excluded when any cited source has changed.
"""
import copy
import hashlib
import json
import re

from . import carryover, cleanroom, discovery
from .contracts import gate_valid, verified
from .portable import relative_name, match_name
from .store import Invalid, digest, timestamp


SCHEMA = 1
KINDS = ("command", "trap", "decision", "glossary")
MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_RECORDS = 64
MAX_HOT_ENTRIES = 128
MAX_HOT_BYTES = 512 * 1024
MAX_ARCHIVE_DEPTH = 64
MAX_ENTRIES = 32
MAX_SOURCES = 12
MAX_TEXT_CHARS = 1200
MAX_QUERY_CHARS = 24000
MAX_QUERY_ENTRIES = 24
KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


def _require(condition, message):
    if not condition:
        raise Invalid("AISpa knowledge: " + message)


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _safe_text(value, maximum=MAX_TEXT_CHARS):
    _require(isinstance(value, str) and value.strip() and len(value) <= maximum,
             "нужен непустой ограниченный текст")
    _require(not discovery.SECRET_CONTENT.search(value) and not carryover.SECRET.search(value),
             "возможный секрет в записи")
    return value.strip()


def _artifact(store, value):
    raw = _json(value)
    _require(len(raw) <= MAX_ARTIFACT_BYTES, "история знаний превышает лимит; не сокращайте её молча")
    discovery._safe_path(store.run, "knowledge")
    return store.artifact("knowledge", raw, ".json")


def _archive_artifact(store, value):
    """Content addressing makes capture/attach repeatable without rewriting files."""
    raw = _json(value)
    _require(len(raw) <= MAX_ARTIFACT_BYTES, "слишком большой архив; выполните knowledge-compact раньше")
    sha = hashlib.sha256(raw).hexdigest()
    name = "knowledge/archive-" + sha + ".json"
    path = discovery._safe_path(store.run, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        _require(path.read_bytes() == raw, "изменён существующий архив знаний")
    return {"run": store.run.name, "file": name, "sha256": sha}


def _read(store, ref):
    _require(isinstance(ref, dict), "нет артефакта знаний")
    name = ref.get("file")
    _require(isinstance(name, str) and name.startswith("knowledge/") and name.endswith(".json"),
             "неверный путь артефакта знаний")
    path = discovery._safe_path(store.run, name)
    with path.open("rb") as stream:
        raw = stream.read(MAX_ARTIFACT_BYTES + 1)
    _require(len(raw) <= MAX_ARTIFACT_BYTES and hashlib.sha256(raw).hexdigest() == ref.get("sha256"),
             "артефакт знаний изменён или слишком велик")
    value = json.loads(raw)
    _require(isinstance(value, dict), "артефакт знаний должен быть объектом")
    return value


def _used_identities(state):
    used = set(state.get("actors", {}))
    for actor in state.get("actors", {}).values():
        used.update(value for value in (actor.get("id"), actor.get("nativeId")) if isinstance(value, str))
    for history in state.get("taskHistory", {}).values():
        for attempt in history:
            used.update(attempt.get("actors", {}).values())
            used.update(attempt.get("writers", []))
            for replacement in attempt.get("actorHistory", []):
                used.update(value for value in (replacement.get("previous"), replacement.get("replacement"))
                            if isinstance(value, str))
    for review in [*state.get("blindHistory", []), state.get("blindReview")]:
        if isinstance(review, dict) and isinstance(review.get("reviewer"), str):
            used.add(review["reviewer"])
    return used


def _unused_identity(state, native_id, aid):
    used = _used_identities(state)
    _require(native_id not in used and aid not in used,
             "автор знаний должен отличаться от исполнителей, критиков и итогового приёмщика")


def _actor(store, state, actor):
    _require(isinstance(actor, dict) and actor.get("fresh") is True and actor.get("role") == "knowledge-writer"
             and actor.get("run") == state["run"], "повреждена роль автора знаний")
    _require(actor.get("id") == digest([actor.get("runtime"), actor.get("nativeId"), actor.get("session")]),
             "повреждена native identity автора знаний")
    _require(_read(store, actor.get("artifact")) == {k: v for k, v in actor.items() if k != "artifact"},
             "изменена карточка автора знаний")
    _unused_identity(state, actor["nativeId"], actor["id"])
    return actor


def assign(store, state, runtime, native_id, session, fresh=True, model=None):
    """Bind a fresh native writer. The caller must append before record()."""
    _require(state.get("status") == "running", "назначение возможно только в активном прогоне")
    runtime = _safe_text(runtime, 100).casefold()
    native_id, session = _safe_text(native_id, 300), _safe_text(session, 300)
    _require(not any(c.isspace() for c in native_id) and fresh is True, "нужен новый независимый native ID")
    if model is not None:
        model = _safe_text(model, 100)
    aid = digest([runtime, native_id, session])
    _unused_identity(state, native_id, aid)
    memory = state.get("knowledge", {"schema": SCHEMA, "records": [], "writerHistory": []})
    _require(memory.get("schema") == SCHEMA, "неподдерживаемая схема знаний")
    _require(len(memory.get("writerHistory", [])) < MAX_RECORDS,
             "история назначений заполнена; выполните knowledge-compact и повторите назначение")
    previous, archived_used = [], set()
    for _archive_store, archived_state, records in _walk(store, state):
        old_memory = archived_state.get("knowledge", {})
        previous.extend([*old_memory.get("writerHistory", []), old_memory.get("writer")])
        previous.extend(record.get("writer") for record in records)
        archived_used.update(_used_identities(archived_state))
    _require(native_id not in archived_used and aid not in archived_used, "native ID уже участвовал в предыдущем прогоне")
    _require(not any(isinstance(actor, dict) and (actor.get("id") == aid or actor.get("nativeId") == native_id)
                     for actor in previous), "этот автор знаний уже использовался; нужен свежий контекст")
    actor = {"id": aid, "runtime": runtime, "nativeId": native_id, "session": session, "fresh": True,
             "model": model, "role": "knowledge-writer", "run": state["run"], "at": timestamp()}
    actor["artifact"] = _artifact(store, actor)
    memory = copy.deepcopy(memory)
    if memory.get("writer"):
        memory.setdefault("writerHistory", []).append(memory["writer"])
    memory["writer"] = actor
    state["knowledge"] = memory
    return copy.deepcopy(actor)


def _sources(store, refs, names):
    _require(isinstance(refs, list) and 0 < len(refs) <= MAX_SOURCES, "нужны ограниченные точные источники")
    result, seen = [], set()
    for ref in refs:
        _require(isinstance(ref, dict), "неверный источник знания")
        try:
            name = relative_name(ref.get("path"))
        except ValueError as exc:
            raise Invalid("AISpa knowledge: небезопасный путь источника") from exc
        _require(name == ref["path"] and name not in seen and discovery._kind(name, explicit=True) is not None
                 and name in names, "источник не допускается или исключён Git: " + name)
        path = discovery._safe_path(store.root, name)
        with path.open("rb") as stream:
            raw = stream.read(discovery.MAX_FILE_BYTES + 1)
        _require(len(raw) <= discovery.MAX_FILE_BYTES, "источник слишком велик: " + name)
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise Invalid("AISpa knowledge: источник не UTF-8") from exc
        _require("\0" not in text and not discovery.SECRET_CONTENT.search(text) and not carryover.SECRET.search(text),
                 "источник содержит двоичные данные или возможный секрет")
        sha = hashlib.sha256(raw).hexdigest()
        _require(sha == ref.get("sha256"), "источник изменён: " + name)
        seen.add(name)
        result.append({"path": name, "sha256": sha})
    return sorted(result, key=lambda row: row["path"])


def _entry_shape(entry):
    _require(isinstance(entry, dict) and isinstance(entry.get("key"), str) and KEY.fullmatch(entry["key"])
             and entry.get("kind") in KINDS, "неверный key или kind знания")
    _safe_text(entry.get("text"))
    sources = entry.get("sources")
    _require(isinstance(sources, list) and 0 < len(sources) <= MAX_SOURCES, "нет источников знания")
    seen = set()
    for source in sources:
        _require(isinstance(source, dict), "неверный источник знания")
        name = source.get("path")
        try:
            canonical = relative_name(name)
        except ValueError as exc:
            raise Invalid("AISpa knowledge: небезопасный путь источника") from exc
        _require(name == canonical and name not in seen and discovery._kind(name, explicit=True) is not None
                 and isinstance(source.get("sha256"), str) and SHA.fullmatch(source["sha256"]), "неверный источник знания")
        seen.add(name)


def _command_shape(entry):
    if entry["kind"] != "command":
        _require("command" not in entry and "evidence" not in entry, "неверный тип свидетельства")
        return
    proof, command = entry.get("evidence"), entry.get("command")
    _require(isinstance(proof, dict) and proof.get("verifiedAtRecording") is True and proof.get("exitCode") == 0
             and proof.get("kind") in ("project", "cleanrun") and isinstance(command, list) and command
             and len(command) <= 64 and all(isinstance(arg, str) and "\0" not in arg and len(arg) <= 2000 for arg in command)
             and proof.get("command") == command
             and isinstance(proof.get("receiptSha256"), str) and SHA.fullmatch(proof["receiptSha256"])
             and isinstance(proof.get("code"), str) and SHA.fullmatch(proof["code"])
             and isinstance(proof.get("output"), dict) and isinstance(proof["output"].get("sha256"), str)
             and SHA.fullmatch(proof["output"]["sha256"]), "повреждено свидетельство команды")
    _safe_text(" ".join(command), 8000)


def _proof(store, state, entry):
    requested = entry.get("verification")
    _require(isinstance(requested, dict) and requested.get("kind") in ("project", "cleanrun"),
             "команде нужно свидетельство реальной проверки")
    receipt = state.get("verification" if requested["kind"] == "project" else "cleanrun")
    _require(isinstance(receipt, dict) and digest(receipt) == requested.get("sha256"), "чужое свидетельство команды")
    command = entry.get("command")
    _require(isinstance(command, list) and command and len(command) <= 64
             and all(isinstance(arg, str) and "\0" not in arg and len(arg) <= 2000 for arg in command)
             and command == receipt.get("command"), "команда не соответствует реально выполненной проверке")
    _safe_text(" ".join(command), 8000)
    _require(receipt.get("exitCode") == 0 and not any(receipt.get(key) for key in
             ("changedCode", "changedSource", "timedOut", "redacted")), "команда не прошла проверку")
    _require(verified(store, state) if requested["kind"] == "project" else cleanroom.valid(store, state),
             "свидетельство команды устарело или повреждено")
    return {"kind": requested["kind"], "receiptSha256": digest(receipt), "output": copy.deepcopy(receipt["output"]),
            "code": state["verification"]["code"], "at": receipt["at"], "exitCode": 0,
            "verifiedAtRecording": True, "command": copy.deepcopy(command)}


def _record_body(store, state, ref):
    body = _read(store, ref)
    _require(body.get("schema") == SCHEMA and body.get("run") == state["run"]
             and body.get("id") == ref.get("id") and body.get("basis") == ref.get("basis"), "чужая запись знаний")
    _require(body.get("id") == digest({k: v for k, v in body.items() if k != "id"}), "повреждён ID записи знаний")
    _actor(store, state, body.get("writer"))
    _require(isinstance(body.get("entries"), list) and len(body["entries"]) <= MAX_ENTRIES, "слишком много знаний")
    for entry in body["entries"]:
        _entry_shape(entry)
        _command_shape(entry)
        if entry["kind"] == "command":
            proof = entry.get("evidence", {})
            from .contracts import artifact_valid
            _require(artifact_valid(store, proof.get("output", {})), "изменён вывод проверки команды")
    return body


def _payload(payload):
    _require(isinstance(payload, dict) and payload.get("schema") == SCHEMA
             and payload.get("digest") == digest({k: v for k, v in payload.items() if k != "digest"})
             and len(_json(payload)) <= MAX_ARTIFACT_BYTES, "повреждён перенос знаний")
    _require(isinstance(payload.get("records"), list) and len(payload["records"]) <= MAX_RECORDS,
             "неверная история знаний")
    return payload


def _history(store, state):
    memory = state.get("knowledge")
    if not memory:
        return []
    _require(memory.get("schema") == SCHEMA and isinstance(memory.get("records"), list), "повреждены знания")
    inherited = _payload(_read(store, memory["inherited"]))["records"] if memory.get("inherited") else []
    records = copy.deepcopy(inherited)
    _require(len(memory["records"]) <= MAX_RECORDS, "локальная история знаний превышает лимит; knowledge-compact")
    records.extend(_record_body(store, state, ref) for ref in memory["records"])
    previous, ids = None, set()
    effective = {}
    for index, body in enumerate(records):
        _require(isinstance(body, dict) and body.get("schema") == SCHEMA and body.get("id") not in ids
                 and body.get("previous") == previous
                 and body.get("id") == digest({k: v for k, v in body.items() if k != "id"}), "нарушена история знаний")
        ids.add(body["id"])
        previous = body["id"]
        checkpoint = body.get("type") == "checkpoint"
        if checkpoint:
            _require(index == 0 and isinstance(body.get("archive"), dict)
                     and type(body.get("archivedRecordCount")) is int and body["archivedRecordCount"] >= 0,
                     "неверная контрольная точка знаний")
        for number, entry in enumerate(body["entries"]):
            _entry_shape(entry)
            _command_shape(entry)
            expected = effective.get(entry["key"])
            if checkpoint:
                _require(expected is None and isinstance(entry.get("id"), str) and SHA.fullmatch(entry["id"])
                         and isinstance(entry.get("record"), str) and SHA.fullmatch(entry["record"])
                         and isinstance(entry.get("originRun"), str), "повреждены исходные ID контрольной точки")
            else:
                if expected is None and entry.get("supersedes") is not None:
                    expected = _archived_keys(store, _archive_pointer(records), {entry["key"]}).get(entry["key"])
                _require(entry.get("supersedes") == (expected["id"] if expected else None)
                         and entry.get("id") == digest([body["run"], body["previous"], entry["key"], number, body["at"]]),
                         "нарушен порядок замены знаний")
            effective[entry["key"]] = entry
    return records


def _effective(records):
    entries = {}
    for record in records:
        for entry in record["entries"]:
            entries.pop(entry["key"], None)
            entries[entry["key"]] = {**copy.deepcopy(entry),
                                    "originRun": entry["originRun"] if record.get("type") == "checkpoint" else record["run"],
                                    "record": entry["record"] if record.get("type") == "checkpoint" else record["id"]}
    return entries


def _archive_pointer(records):
    return records[0].get("archive") if records and records[0].get("type") == "checkpoint" else None


def _archive_state(store, ref):
    _require(isinstance(ref, dict) and isinstance(ref.get("run"), str), "нет происхождения архива")
    try:
        run = relative_name(ref["run"])
    except ValueError as exc:
        raise Invalid("AISpa knowledge: недопустимый прогон архива") from exc
    _require("/" not in run and run == ref["run"], "архив должен принадлежать одному локальному прогону")
    archived_store = type(store)(store.root)
    archived_store.run = discovery._safe_path(store.home, "runs/" + run)
    body = _read(archived_store, ref)
    _require(body.get("schema") == SCHEMA and body.get("type") == "archive" and body.get("run") == run
             and isinstance(body.get("state"), dict) and body["state"].get("run") == run,
             "повреждено происхождение архива")
    return archived_store, body["state"]


def _walk(store, state, depth=None):
    """Follow only immutable local ancestry; no caller-supplied filesystem paths."""
    seen, level = set(), 0
    while True:
        records = _history(store, state)
        yield store, state, records
        ref = _archive_pointer(records)
        if ref is None or depth is not None and level >= depth:
            break
        marker = digest(ref)
        _require(marker not in seen, "цикл в архиве знаний")
        seen.add(marker)
        store, state = _archive_state(store, ref)
        level += 1


def _archived_keys(store, ref, keys):
    found, seen = {}, set()
    while ref is not None and set(found) != keys:
        marker = digest(ref)
        _require(marker not in seen, "цикл в архиве знаний")
        seen.add(marker)
        store, archived_state = _archive_state(store, ref)
        records = _history(store, archived_state)
        for key, entry in _effective(records).items():
            if key in keys and key not in found:
                found[key] = entry
        ref = _archive_pointer(records)
    return found


def _checkpoint(store, state, journal):
    records = _history(store, state)
    # This snapshot archives references, not duplicated record bodies. All prior
    # immutable records, writer cards and command outputs remain canonical.
    minimal = {"run": state["run"], "knowledge": copy.deepcopy(state["knowledge"]),
               "actors": {identity: {"nativeId": identity} for identity in sorted(_used_identities(state))}}
    archive = {"schema": SCHEMA, "type": "archive", "run": state["run"], "state": minimal,
               "journal": {"sequence": journal["last"]["sequence"], "eventDigest": journal["last"]["hash"],
                           "stateDigest": digest(state)}}
    ref = _archive_artifact(store, archive)
    effective, hot, used = _effective(records), [], 0
    for entry in reversed(list(effective.values())):
        size = len(_json(entry))
        if len(hot) < MAX_HOT_ENTRIES and used + size <= MAX_HOT_BYTES:
            hot.append(entry)
            used += size
    hot.reverse()
    archived_count = sum(1 for record in records if record.get("type") != "checkpoint")
    if records and records[0].get("type") == "checkpoint":
        archived_count += records[0]["archivedRecordCount"]
    body = {"schema": SCHEMA, "type": "checkpoint", "run": state["run"], "previous": None,
            "basis": records[-1]["basis"] if records else None, "at": state.get("updatedAt", state["createdAt"]),
            "entries": hot, "archive": ref, "archivedRecordCount": archived_count,
            "archivedTip": records[-1]["id"] if records else None,
            "curationRun": (records[-1].get("curationRun") if records[-1].get("type") == "checkpoint"
                            else records[-1]["run"]) if records else None,
            "curationRecord": (records[-1].get("curationRecord") if records[-1].get("type") == "checkpoint"
                               else records[-1]["id"]) if records else None,
            "overflowEntries": len(effective) - len(hot), "hotEntryCount": len(hot)}
    body["id"] = digest(body)
    return body


def compact(store, state):
    """Archive a saved prefix and replace only its derived in-memory projection."""
    _require(state.get("status") == "running" and state.get("knowledge"), "нужен активный прогон со знаниями")
    journal = carryover._journal(store, state)
    memory = state["knowledge"]
    if memory.get("inherited") and not memory.get("records") and not memory.get("writerHistory"):
        _history(store, state)
        return copy.deepcopy(memory["inherited"])
    checkpoint = _checkpoint(store, state, journal)
    payload = {"schema": SCHEMA, "records": [checkpoint]}
    payload["digest"] = digest(payload)
    ref = _artifact(store, payload)
    state["knowledge"] = {"schema": SCHEMA, "inherited": ref, "records": [], "writerHistory": [],
                          **({"writer": copy.deepcopy(memory["writer"])} if memory.get("writer") else {})}
    return copy.deepcopy(ref)


def record(store, state, report):
    """Append reviewed lessons after acceptance, without inheriting acceptance."""
    _require(state.get("status") == "running", "запись возможна перед завершением активного прогона")
    _require(all(task.get("status") in ("completed", "blocked") for task in state.get("tasks", [])),
             "сначала завершите работу над задачами или явно сохраните препятствие")
    _require(gate_valid(store, state, "acceptance") and verified(store, state), "нужна свежая успешная итоговая приёмка и проверка")
    # The actual command receipt must come from the saved journal, not a caller-supplied dict.
    carryover._journal(store, state)
    _require(isinstance(report, dict) and len(_json(report)) <= MAX_ARTIFACT_BYTES, "неверный отчёт знаний")
    writer = _actor(store, state, state.get("knowledge", {}).get("writer"))
    _require(report.get("writer") == writer["id"] and report.get("basis") == store.code(), "неверный автор или снимок кода")
    raw_entries, uncertainties = report.get("entries"), report.get("uncertainties", {})
    _require(isinstance(raw_entries, list) and len(raw_entries) <= MAX_ENTRIES and isinstance(uncertainties, dict)
             and set(uncertainties) <= set(KINDS), "неверные entries или uncertainties")
    records = _history(store, state)
    _require(len(state["knowledge"].get("records", [])) < MAX_RECORDS,
             "локальная история знаний заполнена; выполните knowledge-compact и повторите knowledge-record")
    effective, entries, keys, covered = _effective(records), [], set(), set()
    for entry in raw_entries:
        _entry_shape(entry)
    cold_keys = {entry["key"] for entry in raw_entries} - set(effective)
    if cold_keys:
        effective.update(_archived_keys(store, _archive_pointer(records), cold_keys))
    names, at = set(discovery._names(store)), timestamp()
    previous = records[-1]["id"] if records else None
    for number, entry in enumerate(raw_entries):
        _entry_shape(entry)
        _require(entry["key"] not in keys, "повторный key в одном отчёте")
        old = effective.get(entry["key"])
        _require(old is None or old["kind"] == entry["kind"], "при замене знания нельзя молча изменить его тип")
        clean = {"id": digest([state["run"], previous, entry["key"], number, at]), "key": entry["key"],
                 "kind": entry["kind"], "text": _safe_text(entry["text"]), "sources": _sources(store, entry["sources"], names),
                 "supersedes": old["id"] if old else None}
        if clean["kind"] == "command":
            clean.update(command=copy.deepcopy(entry.get("command")), evidence=_proof(store, state, entry))
        else:
            _require("command" not in entry and "verification" not in entry, "свидетельство команды допускается только для command")
        entries.append(clean)
        keys.add(entry["key"])
        covered.add(entry["kind"])
    missing = set(KINDS) - covered
    _require(missing <= set(uncertainties), "для каждой отсутствующей категории укажите явную неопределённость")
    unknown = {kind: _safe_text(text) for kind, text in uncertainties.items()}
    body = {"schema": SCHEMA, "run": state["run"], "basis": report["basis"], "previous": previous,
            "writer": copy.deepcopy(writer), "entries": entries, "uncertainties": unknown,
            "missingKinds": sorted(missing), "at": at}
    body["id"] = digest(body)
    ref = {**_artifact(store, body), "id": body["id"], "basis": body["basis"], "entries": len(entries)}
    state["knowledge"].setdefault("records", []).append(ref)
    return copy.deepcopy(ref)


def valid(store, state, *, check_sources=True):
    """A current curation exists; this is never a replacement for acceptance."""
    try:
        records = _history(store, state)
        if not records:
            return False
        last = records[-1]
        if last.get("type") == "checkpoint":
            if last.get("curationRun") != state["run"]:
                return False
            matched = None
            for _old_store, _old_state, archive_records in _walk(store, state):
                matched = next((row for row in archive_records if row.get("type") != "checkpoint"
                                and row["id"] == last.get("curationRecord")), None)
                if matched is not None:
                    break
            if matched is None:
                return False
            last = matched
        if last["run"] != state["run"]:
            return False
        if not check_sources:
            return True
        return last["basis"] == store.code()
    except (OSError, ValueError, TypeError, KeyError):
        return False


def query(store, state, *, paths=None, kinds=None, limit=8, max_chars=6000, include_archive=False, archive_depth=4):
    """Explicit selection only. A changed source suppresses the entire lesson."""
    _require(type(limit) is int and 1 <= limit <= MAX_QUERY_ENTRIES and type(max_chars) is int
             and 256 <= max_chars <= MAX_QUERY_CHARS, "неверные ограничения выборки")
    _require(type(include_archive) is bool and type(archive_depth) is int and 1 <= archive_depth <= MAX_ARCHIVE_DEPTH,
             "неверная глубина поиска архива")
    result = {"schema": SCHEMA, "entries": [], "omitted": {"stale": 0, "limit": 0},
              "historicalOnly": True, "rule": "Sources guide reading; rerun commands before claiming current success."}
    if not paths and not kinds:
        result["reason"] = "selection-required"
        return result
    _require(paths is None or isinstance(paths, list) and all(isinstance(path, str) for path in paths), "paths должен быть списком")
    _require(kinds is None or isinstance(kinds, list) and all(kind in KINDS for kind in kinds), "неверные kinds")
    try:
        selected_paths = [match_name(relative_name(path, root=True)) for path in (paths or [])]
    except ValueError as exc:
        raise Invalid("AISpa knowledge: небезопасный путь выборки") from exc
    records = _history(store, state)
    checkpoint = records[0] if records and records[0].get("type") == "checkpoint" else None
    result["archive"] = {"available": bool(checkpoint), "searchedPages": 0,
                         "archivedRecordCount": checkpoint["archivedRecordCount"] if checkpoint else 0,
                         "unread": _archive_pointer(records),
                         "rule": "Use --archive for bounded historical search; canonical files remain in the referenced local run."}
    effective, seen = {}, set()
    for page, (_source_store, _source_state, page_records) in enumerate(_walk(store, state, archive_depth if include_archive else 0)):
        for key, entry in _effective(page_records).items():
            if key not in seen:
                effective[key] = entry
                seen.add(key)
        result["archive"].update(searchedPages=page, unread=_archive_pointer(page_records))
    names, used = set(discovery._names(store)), 0
    for entry in sorted(effective.values(), key=lambda row: (row["kind"], row["key"])):
        if kinds and entry["kind"] not in kinds:
            continue
        if selected_paths and not any(path == "." or match_name(source["path"]) == path
                or match_name(source["path"]).startswith(path + "/") for path in selected_paths for source in entry["sources"]):
            continue
        try:
            _sources(store, entry["sources"], names)
        except (OSError, ValueError, TypeError, KeyError):
            result["omitted"]["stale"] += 1
            continue
        # Output paths in verification belong to an old run; publish exact hashes, not misleading local paths.
        if entry["kind"] == "command":
            entry["evidence"]["outputSha256"] = entry["evidence"].pop("output")["sha256"]
        size = len(json.dumps(entry, ensure_ascii=False))
        if len(result["entries"]) >= limit or used + size > max_chars:
            result["omitted"]["limit"] += 1
            continue
        used += size
        result["entries"].append(entry)
    result["entryChars"] = used
    return result


def capture(previous_store, previous_state):
    """Capture saved terminal history. Empty old versions have nothing to carry."""
    if not previous_state.get("knowledge"):
        return None
    _require(previous_state.get("status") in carryover.TERMINAL, "перенос возможен только из завершённого прогона")
    journal = carryover._journal(previous_store, previous_state)
    checkpoint = _checkpoint(previous_store, previous_state, journal)
    payload = {"schema": SCHEMA, "records": [checkpoint], "previous": {"run": previous_state["run"],
               "eventDigest": journal["last"]["hash"], "stateDigest": digest(previous_state)}}
    payload["digest"] = digest(payload)
    return _payload(payload)


def attach(store, state, payload):
    """Validate old closure again, then preserve its full immutable history."""
    if payload is None:
        return None
    _payload(payload)
    _require(not state.get("knowledge") and not state.get("briefs") and not state.get("tasks")
             and not state.get("requirements") and state.get("status") == "running", "перенос требует пустого нового прогона")
    previous = payload.get("previous", {})
    _require(previous.get("run") != state["run"], "нельзя наследовать текущий прогон")
    old_store = type(store)(store.root)
    old_store.run = discovery._safe_path(store.home, "runs/" + previous.get("run", ""))
    path = discovery._safe_path(old_store.run, "events.jsonl")
    last = None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            last = line
    old_state = json.loads(last)["state"]
    _require(capture(old_store, old_state) == payload, "исходная история изменилась после capture")
    state["knowledge"] = {"schema": SCHEMA, "writerHistory": [], "records": [], "inherited": _artifact(store, payload)}
    return copy.deepcopy(state["knowledge"])
