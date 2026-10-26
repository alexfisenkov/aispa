"""Source-bound native token accounting; no prices or estimated tokens."""
import hashlib
import json
from pathlib import Path

from . import participants
from .contracts import artifact_valid, require
from .store import timestamp, Invalid


COUNTERS = ("inputTokens", "outputTokens", "cachedInputTokens", "uncachedInputTokens",
            "cacheCreationInputTokens", "cacheReadInputTokens", "reasoningOutputTokens")
MAX_LOG_BYTES = 256 * 1024 * 1024


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _natural(value, name):
    require(type(value) is int and value >= 0, "Usage: неверный счётчик " + name)
    return value


def _normalize(value):
    for key in ("actor", "callId", "source"):
        require(isinstance(value.get(key), str) and value[key].strip(), "Usage: нужны actor, callId и source")
    total = _natural(value.get("inputTokens"), "inputTokens")
    output = _natural(value.get("outputTokens"), "outputTokens")
    cached = _natural(value.get("cachedInputTokens"), "cachedInputTokens")
    require(cached <= total, "Cached input превышает весь input")
    creation = value.get("cacheCreationInputTokens")
    read = value.get("cacheReadInputTokens")
    if creation is not None:
        creation = _natural(creation, "cacheCreationInputTokens")
    if read is not None:
        read = _natural(read, "cacheReadInputTokens")
    uncached = _natural(value.get("uncachedInputTokens", total - cached), "uncachedInputTokens")
    reasoning = value.get("reasoningOutputTokens")
    if reasoning is not None:
        reasoning = _natural(reasoning, "reasoningOutputTokens")
    require((creation is None or read is None or creation + read == cached)
            and (creation is None or creation <= cached)
            and (read is None or read <= cached)
            and uncached + cached == total,
            "Usage: входные счётчики не складываются в общий input")
    require(reasoning is None or reasoning <= output, "Reasoning является частью output")
    row = {k: value[k] for k in ("actor", "callId", "source")}
    row.update(inputTokens=total, outputTokens=output, cachedInputTokens=cached,
               uncachedInputTokens=uncached, cacheCreationInputTokens=creation,
               cacheReadInputTokens=read, reasoningOutputTokens=reasoning)
    for key in ("model", "runtime", "sourceHash", "nativeCallId"):
        if key in value:
            require(isinstance(value[key], str) and value[key].strip(), "Usage: неверное поле " + key)
            row[key] = value[key]
    if "attribution" in value:
        row["attribution"] = value["attribution"]
    return row


def _attribution(store, state, row, actor):
    value = row.get("attribution")
    if value is None:
        return "unknown"
    require(isinstance(value, dict) and isinstance(value.get("source"), dict)
            and artifact_valid(store, value["source"]), "Usage: phase attribution source изменён")
    binding = {k: v for k, v in value.items() if k != "source"}
    require(store.json(value["source"]) == binding and actor
            and value.get("actor") == row["actor"] and value.get("callId") == row["callId"]
            and value.get("run") == state["run"] and value.get("session") == actor["session"],
            "Usage: phase attribution принадлежит другому вызову/run/session")
    from . import work_progress, timing
    allowed = set(work_progress.PHASES) | set(timing.TASK_PHASES)
    require(value.get("phase") in allowed, "Usage: unknown phase")
    if value.get("task") is not None or value.get("attempt") is not None:
        history = state.get("taskHistory", {}).get(value.get("task"), [])
        require(any(a.get("id") == value.get("attempt") for a in history), "Usage: чужая task/attempt attribution")
    return value["phase"]


def _validate_saved(store, rows):
    seen = set()
    native_seen = set()
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("record"), dict)
                and artifact_valid(store, row["record"]),
                "Повреждён источник метрики")
        require(store.json(row["record"]) == {k: v for k, v in row.items() if k != "record"},
                "Метрика отличается от источника")
        key = (row.get("actor"), row.get("callId"))
        require(key not in seen, "Повторный вызов в журнале usage")
        seen.add(key)
        if row.get("nativeCallId") and row.get("runtime"):
            native = (row["runtime"], row["nativeCallId"])
            require(native not in native_seen, "Native response ID учтён более одного раза")
            native_seen.add(native)


def record(store, state, value):
    """Record one measured call; accepts the legacy three-counter payload."""
    require(isinstance(value, dict), "Usage должен быть объектом")
    row = _normalize(value)
    actor = participants.resolve(store, state, row["actor"])
    # Old alpha runs can contain task IDs without sealed records. Keep their
    # append/read contract, but never count that as verified participation.
    require(actor is not None or row["actor"] in state.get("actors", {}),
            "Usage требует известного участника")
    require(actor is None or "runtime" not in row or row["runtime"] == actor["runtime"],
            "Usage runtime отличается от участника")
    _attribution(store, state, row, actor)
    prior = state.setdefault("usage", [])
    _validate_saved(store, prior)
    require((row["actor"], row["callId"]) not in {(r["actor"], r["callId"]) for r in prior},
            "Метрика вызова уже записана")
    if row.get("nativeCallId") and row.get("runtime"):
        require((row["runtime"], row["nativeCallId"]) not in
                {(r.get("runtime"), r.get("nativeCallId")) for r in prior},
                "Native response ID уже учтён")
    row["at"] = timestamp()
    row["record"] = store.artifact("usage", _encoded(row), ".json")
    prior.append(row)
    return summary(store, state)


def _sum(rows):
    def number(row, key):
        if key == "uncachedInputTokens":
            return row.get(key, row["inputTokens"] - row["cachedInputTokens"])
        return row.get(key)
    result = {}
    for key in COUNTERS:
        values = [number(row, key) for row in rows]
        result[key] = None if any(v is None for v in values) else sum(values)
    return result


def _expected_roles(state):
    stages = (("ideaReview", "idea"), ("discoveryCollection", "discovery"),
              ("discovery", "discovery"),
              ("coverageReview", "coverage"), ("planReview", "planner"),
              ("blindReview", "blind"), ("knowledge", "knowledge-writer"))
    needed = {role for key, role in stages if state.get(key)}
    if state.get("gates", {}).get("coverage"):
        needed.add("coverage")
    if state.get("briefs"):
        needed.add("coordinator")
    if state.get("briefExtraction") or state.get("briefMap"):
        needed.add("brief-analyst")
    if state.get("briefReview") or state.get("briefMap"):
        needed.add("brief-reviewer")
    if state.get("branchReview"):
        needed.add("branch-reviewer")
    if state.get("journalRecovery"):
        needed.add("recovery-reviewer")
    return needed


def summary(store, state):
    """Compact observed totals and explicit missing-coverage markers."""
    rows = state.get("usage", [])
    _validate_saved(store, rows)
    known = {p["id"]: p for p in participants.list_participants(store, state)}
    declared_ids = set(state.get("participants", {})) | set(state.get("actors", {}))
    memory = state.get("knowledge") or {}
    if isinstance(memory, dict):
        history = memory.get("writerHistory", [])
        if not isinstance(history, list):
            history = []
        declared_ids.update(p["id"] for p in [memory.get("writer"), *history]
                            if isinstance(p, dict) and isinstance(p.get("id"), str))
    invalid_ids = sorted(aid for aid in declared_ids - set(known) if isinstance(aid, str))
    measured_ids = {row["actor"] for row in rows}
    groups = {"byRole": {}, "byParticipant": {}, "byModel": {}, "byPhase": {}}
    unattributed_calls = 0
    for row in rows:
        actor = known.get(row["actor"])
        role = actor.get("role") if actor else "unverified"
        model = row.get("model") or "unknown"
        phase = _attribution(store, state, row, actor)
        unattributed_calls += phase == "unknown"
        for group, key in (("byRole", role), ("byParticipant", row["actor"]), ("byModel", model), ("byPhase", phase)):
            groups[group].setdefault(key, []).append(row)
    grouped = {group: {key: {"measuredCalls": len(items), **_sum(items)}
                       for key, items in sorted(values.items())}
               for group, values in groups.items()}
    history = state.get("usageImports", [])
    for item in history:
        require(isinstance(item, dict) and isinstance(item.get("record"), dict)
                and artifact_valid(store, item["record"]),
                "Повреждена контрольная точка usage")
        require(store.json(item["record"]) == {k: v for k, v in item.items() if k != "record"},
                "Контрольная точка usage отличается от источника")
    latest_sources = {}
    for item in history:
        latest_sources[(item.get("format"), item.get("sourceKey", item.get("sourceHash")),
                        item.get("participant"))] = item
    imports = list(latest_sources.values())
    missing_participants = sorted(set(known) - measured_ids)
    roles = {p["role"] for p in known.values()}
    from . import phase_receipts
    phases = phase_receipts.summary(store, state) if phase_receipts.enabled(state) else {"authors": {}, "missingPhases": []}
    phase_roles = {phase_receipts.ROLES[phase] for phase in phases["authors"]}
    missing_roles = sorted(_expected_roles(state) - roles - phase_roles)
    unattributed_phases = sorted(phase for phase, actor in phases["authors"].items() if actor not in measured_ids)
    uncovered = sum(i["unmappedCalls"] + i["incompleteCalls"] + i["unsupportedCalls"] for i in imports)
    terminal = state.get("status") in ("accepted", "delivered_with_remaining", "cancelled")
    final_sources = bool(imports) and all(i["final"] for i in imports)
    final_coordinator = any(i.get("includesFinalCoordinatorReply") for i in imports)
    checkpoint_participants = {i["participant"] for i in imports if i["final"]}
    checkpoint_calls = {(i["participant"], call_id) for i in imports if i["final"]
                        for call_id in i.get("callIds", [])}
    complete = bool(terminal and final_sources and final_coordinator and rows
                    and not missing_participants and not missing_roles and not invalid_ids and not uncovered
                    and not phases["missingPhases"] and not unattributed_phases
                    and set(known) <= checkpoint_participants
                    and all(row["actor"] in known and (row["actor"], row["callId"]) in checkpoint_calls
                            for row in rows))
    if state.get("policy", {}).get("managedExecution") and unattributed_calls:
        complete = False
    totals = _sum(rows) if rows else {key: None for key in COUNTERS}
    return {"measuredCalls": len(rows), "complete": complete, "unit": "tokens",
            **totals, **grouped,
            "coverage": {"registeredParticipants": len(known), "measuredParticipants": len(measured_ids & set(known)),
                         "missingParticipants": missing_participants, "missingRoles": missing_roles,
                         "missingPhases": phases["missingPhases"], "unattributedPhases": unattributed_phases,
                         "phaseAuthors": phases["authors"],
                         "unattributedCalls": unattributed_calls,
                         "invalidParticipantRecords": invalid_ids,
                         "unverifiedActors": sorted(measured_ids - set(known)),
                         "unmappedCalls": sum(i["unmappedCalls"] for i in imports),
                         "incompleteCalls": sum(i["incompleteCalls"] for i in imports),
                         "unsupportedCalls": sum(i["unsupportedCalls"] for i in imports),
                         "ignoredCumulativeEvents": sum(i.get("ignoredCumulativeEvents", 0) for i in imports),
                         "sourceCheckpoints": len(imports), "finalSources": final_sources,
                         "finalCoordinatorReplyIncluded": final_coordinator,
                         "activeRun": state.get("status") == "running"},
            "costStatus": "unknown",
            "note": "Only recorded native measurements; absent calls, phase attribution and savings are not inferred"}


def _native_counters(usage, kind):
    if not isinstance(usage, dict):
        return None
    if kind == "claude":
        uncached = usage.get("input_tokens")
        created = usage.get("cache_creation_input_tokens", 0)
        read = usage.get("cache_read_input_tokens", 0)
        output = usage.get("output_tokens")
        values = (uncached, created, read, output)
        if not all(type(v) is int and v >= 0 for v in values):
            return None
        total = uncached + created + read
        return dict(inputTokens=total, outputTokens=output,
                    cachedInputTokens=created + read, uncachedInputTokens=uncached,
                    cacheCreationInputTokens=created, cacheReadInputTokens=read,
                    reasoningOutputTokens=None)
    total, output = usage.get("input_tokens"), usage.get("output_tokens")
    read = usage.get("cached_input_tokens", 0)
    created = usage.get("cache_write_input_tokens")
    reasoning = usage.get("reasoning_output_tokens")
    if not all(type(v) is int and v >= 0 for v in (total, output, read)):
        return None
    if created is not None and (type(created) is not int or created < 0):
        return None
    if reasoning is not None and (type(reasoning) is not int or reasoning < 0):
        return None
    if read + (created or 0) > total or reasoning is not None and reasoning > output:
        return None
    reported_total = usage.get("total_tokens")
    if reported_total is not None and (type(reported_total) is not int or reported_total != total + output):
        return None
    return dict(inputTokens=total, outputTokens=output, cachedInputTokens=read + (created or 0),
                uncachedInputTokens=total - read - (created or 0), cacheCreationInputTokens=created,
                cacheReadInputTokens=read, reasoningOutputTokens=reasoning)


def _extract(row, kind):
    if not isinstance(row, dict):
        return None
    if kind == "claude":
        if row.get("type") != "assistant" or not isinstance(row.get("message"), dict):
            return None
        message = row["message"]
        if "usage" not in message:
            return None
        return (message.get("id"), message.get("usage"), message.get("model"),
                row.get("sessionId") or row.get("session_id"), None)
    payload = row.get("payload") if row.get("type") in ("event_msg", "response_item", "token_usage_record") else row
    if not isinstance(payload, dict):
        return None
    if payload.get("type") == "token_usage_record":
        return (payload.get("response_id") or payload.get("responseId") or payload.get("id"),
                payload.get("usage", payload), payload.get("model"),
                payload.get("session_id"), payload.get("thread_id"))
    if row.get("type") == "token_usage_record" and "usage" in payload:
        return (payload.get("response_id") or payload.get("responseId") or payload.get("id"),
                payload["usage"], payload.get("model"),
                payload.get("session_id"), payload.get("thread_id"))
    if payload.get("type") == "token_count":
        info = payload.get("info")
        if not isinstance(info, dict) or not ("last_token_usage" in info or "total_token_usage" in info):
            return None
        return (payload.get("response_id") or info.get("response_id"),
                info.get("last_token_usage"), payload.get("model"),
                payload.get("session_id") or info.get("session_id"), payload.get("thread_id"))
    return None


def import_log(store, state, path, *, format, participant=None, final=False,
               includes_final_coordinator_reply=False):
    """Import one explicitly named native JSONL file, storing numeric evidence only.

    An unidentified cumulative Codex token_count is excluded. The caller must
    declare a source checkpoint and separately attest inclusion of the final
    coordinator reply; importing an active run never yields complete coverage.
    """
    require(format in ("codex", "claude"), "Неподдерживаемый формат native usage")
    require(type(final) is bool and type(includes_final_coordinator_reply) is bool,
            "Неверный флаг контрольной точки")
    actor = participants.resolve(store, state, participant) if participant else None
    require(participant is None or actor is not None, "Неизвестный участник источника usage")
    expected_runtime = {"codex": "codex", "claude": "claude-code"}[format]
    require(actor is None or actor.get("runtime") == expected_runtime,
            "Формат native log отличается от runtime участника")
    if actor is not None:
        candidates = [p for p in participants.list_participants(store, state)
                      if p.get("runtime") == expected_runtime and p.get("session") == actor["session"]]
        require(len(candidates) == 1 and candidates[0]["id"] == participant,
                "Несколько участников делят native session; автоматическая привязка usage неоднозначна")
    require(not includes_final_coordinator_reply or actor and actor["role"] == "coordinator" and final,
            "Финальный ответ координатора требует его закрытого источника")
    source = Path(path)
    require(source.is_file() and not source.is_symlink(), "Нужен явный обычный файл native JSONL")
    before = source.stat()
    require(before.st_size <= MAX_LOG_BYTES, "Native log слишком велик для адресного импорта")
    raw = source.read_bytes()
    after = source.stat()
    require(before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns,
            "Native log изменился во время чтения; повторите адресный импорт")
    sha = hashlib.sha256(raw).hexdigest()
    source_key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()
    if any(i["sourceHash"] == sha and i["format"] == format and i.get("participant") == participant
           and i.get("sourceKey") == source_key and i["final"] == final
           and i.get("includesFinalCoordinatorReply") == includes_final_coordinator_reply
           for i in state.get("usageImports", [])):
        return summary(store, state)
    observed, incomplete, unsupported, cumulative = {}, 0, 0, 0
    native_sessions = set()
    for line in raw.splitlines():
        if format == "codex" and b'"token_usage_record"' not in line and b'"token_count"' not in line:
            continue
        if format == "claude" and b'"assistant"' not in line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            incomplete += 1
            continue
        if format == "codex" and isinstance(row, dict) and row.get("type") == "event_msg":
            payload = row.get("payload")
            if (isinstance(payload, dict) and payload.get("type") == "token_count"
                    and not payload.get("response_id")
                    and not (isinstance(payload.get("info"), dict)
                             and payload["info"].get("response_id"))):
                cumulative += 1
                continue
        extracted = _extract(row, format)
        if extracted is None:
            continue
        call_id, native, model, session_id, thread_id = extracted
        if not isinstance(call_id, str) or not call_id.strip():
            unsupported += 1
            continue
        if isinstance(session_id, str) and session_id:
            native_sessions.add(session_id)
        if isinstance(thread_id, str) and thread_id:
            native_sessions.add(thread_id)
        counts = _native_counters(native, format)
        observed[(session_id, thread_id, call_id)] = (counts, model)
    incomplete += sum(counts is None for counts, _model in observed.values())
    latest = {identity: pair for identity, pair in observed.items() if pair[0] is not None}
    mapped = {}
    for (session_id, thread_id, call_id), pair in latest.items():
        source_session = session_id or thread_id
        consistent = not (session_id and thread_id and session_id != thread_id)
        if actor is not None and consistent and source_session == actor["session"]:
            mapped[call_id] = pair
    unmapped = len(latest) - len(mapped)
    if actor is not None:
        for call_id, (counts, model) in mapped.items():
            duplicate = next((r for r in state.get("usage", [])
                              if r.get("actor") == participant and r.get("callId") == call_id), None)
            if duplicate:
                require(all(duplicate.get(key) == counts[key] for key in
                            ("inputTokens", "outputTokens", "cachedInputTokens"))
                        and all(key not in duplicate or duplicate[key] is None or duplicate[key] == count
                                for key, count in counts.items()),
                        "Native call уже импортирован с другими счётчиками")
                continue
            item = {"actor": participant, "callId": call_id, "nativeCallId": call_id,
                    "source": "native:" + format, "sourceHash": sha, "runtime": actor["runtime"], **counts}
            if isinstance(model, str) and model.strip():
                item["model"] = model
            record(store, state, item)
    checkpoint = {"format": format, "sourceHash": sha, "sourceKey": source_key,
                  "run": state["run"],
                  "participant": participant, "session": actor["session"] if actor else None,
                  "nativeSessionHashes": sorted(hashlib.sha256(s.encode()).hexdigest() for s in native_sessions),
                  "observedCalls": len(latest), "callIds": sorted(mapped), "unmappedCalls": unmapped,
                  "incompleteCalls": incomplete, "unsupportedCalls": unsupported,
                  "ignoredCumulativeEvents": cumulative,
                  "final": final, "includesFinalCoordinatorReply": includes_final_coordinator_reply,
                  "at": timestamp()}
    checkpoint["record"] = store.artifact("usage-imports", _encoded(checkpoint), ".json")
    state.setdefault("usageImports", []).append(checkpoint)
    return summary(store, state)
