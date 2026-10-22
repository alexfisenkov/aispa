"""Source-bound, flattened handoffs between runs; historical evidence is not proof.

The caller owns the Journal lock. Capture the saved terminal state BEFORE create,
attach to the empty new run, then append. Dispositions also need a caller append.
Only immutable handoff/receipt artifacts are authoritative; derived views are not.
"""
import copy
import hashlib
import json
from pathlib import Path
import re

from .contracts import nonempty, require
from . import execution_history
from .store import Invalid, digest, timestamp
from .portable import is_link, relative_name
from .team import historical_proof_valid, task_proof_valid


SCHEMA = 1
TERMINAL = {"accepted", "delivered_with_remaining", "cancelled"}
OUTSTANDING = {"outstanding", "mapped", "deferred"}
MAX_SOURCE_BYTES = 1024 * 1024
MAX_HANDOFF_BYTES = 16 * 1024 * 1024
MAX_REASON_CHARS = 1200
MAX_EXECUTION_VIEW = 20
SECRET = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|\b\d{8,12}:[A-Za-z0-9_-]{30,}|"
    r"-----BEGIN [^-]*PRIVATE KEY-----|"
    r"(?i:\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)"
    r"\s*[=:]\s*[\"']?[A-Za-z0-9_./+=-]{12,}))")


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _safe(root, name):
    try:
        relative = Path(relative_name(name))
    except ValueError as exc:
        raise Invalid("Carryover: нужен относительный путь без ..") from exc
    path = root
    require(not is_link(path), "Carryover: ссылка вместо корня запрещена")
    for part in relative.parts:
        require(not (part.startswith(".env") or part.lower().endswith((".pem", ".key"))),
                "Carryover: секретный файл не может быть источником")
        path = path / part
        require(not is_link(path), "Carryover: символические ссылки запрещены")
    require(path.resolve().is_relative_to(root.resolve()), "Carryover: источник вне прогона")
    return path


def _run(store):
    require(store.run is not None, "Carryover: нет прогона")
    try:
        return _safe(store.root, store.run.relative_to(store.root).as_posix())
    except ValueError as exc:
        raise Invalid("Carryover: прогон вне проекта") from exc


def _read(store, artifact, limit=MAX_HANDOFF_BYTES):
    require(isinstance(artifact, dict) and isinstance(artifact.get("file"), str),
            "Carryover: некорректный источник")
    path = _safe(_run(store), artifact["file"])
    require(path.is_file() and path.stat().st_size <= limit, "Carryover: источник отсутствует или слишком велик")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == artifact.get("sha256"),
            "Carryover: изменён неизменный источник: " + artifact["file"])
    return raw


def _text(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise Invalid("Carryover: текстовый источник должен быть UTF-8") from exc
    require(not SECRET.search(text), "Carryover: возможный секрет в источнике; перенос остановлен")
    return text


def _load(store, artifact):
    try:
        value = json.loads(_text(_read(store, artifact)))
    except (ValueError, UnicodeError) as exc:
        raise Invalid("Carryover: повреждён JSON источника") from exc
    require(isinstance(value, dict), "Carryover: ожидается объект")
    return value


def _artifacts(value):
    """Only hash-bound references, never arbitrary paths mentioned by input text."""
    if isinstance(value, dict):
        if "file" in value and "sha256" in value:
            yield {k: value[k] for k in ("file", "sha256")}
        else:
            for child in value.values():
                yield from _artifacts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _artifacts(child)


def _journal(store, state):
    _safe(_run(store), "events.jsonl")
    journal = store.read_journal(include_events=True)
    last_state = journal["last"]["state"]
    scopes, retries = {}, {}
    try:
        for row in journal["events"]:
            event_state = row["state"]
            require(event_state["run"] == state["run"] == store.run.name,
                    "Carryover: журнал другого прогона")
            # Retain decisions, not repeated full states from the journal.
            for req in event_state["requirements"]:
                if req.get("decision"):
                    value = {"requirement": req["id"], **req["decision"]}
                    scopes[digest(value)] = value
            for tid, history in event_state.get("taskHistory", {}).items():
                for attempt in history:
                    auth = attempt.get("authorization")
                    if auth:
                        value = {"task": tid, "attempt": attempt["id"], "brief": auth["brief"],
                                 "quote": auth["quote"], "previousAttempt": auth["previousAttempt"]}
                        retries[digest(value)] = value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Invalid("Carryover: не удалось проверить журнал") from exc
    require(digest(last_state) == digest(state),
            "Carryover: сначала сохраните последнее состояние прогона")
    last = {k: journal["last"][k] for k in ("sequence", "hash", "at")}
    return {"last": last, "sha256": journal["sha256"],
            "scopes": list(scopes.values()), "retries": list(retries.values())}


def _source(store, state, artifact, kind):
    text = _text(_read(store, artifact, MAX_SOURCE_BYTES))
    origin = {"run": state["run"], "file": artifact["file"], "sha256": artifact["sha256"]}
    return {"id": digest(origin), "origin": origin, "kind": kind, "text": text}


def _merge_sources(target, sources):
    for source in sources:
        require(isinstance(source, dict) and isinstance(source.get("origin"), dict)
                and isinstance(source.get("text"), str), "Carryover: повреждён источник handoff")
        origin = source["origin"]
        require(source.get("id") == digest(origin)
                and hashlib.sha256(source["text"].encode()).hexdigest() == origin.get("sha256")
                and not SECRET.search(source["text"]), "Carryover: повреждён или небезопасен источник handoff")
        require(source["id"] not in target or target[source["id"]] == source,
                "Carryover: конфликт происхождения источника")
        target[source["id"]] = copy.deepcopy(source)



def _payload_valid(payload):
    require(isinstance(payload, dict) and payload.get("schema") == SCHEMA
            and payload.get("digest") == digest({k: v for k, v in payload.items() if k != "digest"}),
            "Carryover: повреждён handoff")
    require(len(_json(payload)) <= MAX_HANDOFF_BYTES, "Carryover: handoff превышает безопасный размер")
    require(not SECRET.search(_json(payload).decode()), "Carryover: возможный секрет в handoff")
    sources = {}
    _merge_sources(sources, payload.get("sources", []))
    items = payload.get("items")
    require(isinstance(items, list) and all(isinstance(i, dict) for i in items)
            and len({i.get("id") for i in items}) == len(items), "Carryover: повторные или некорректные элементы")
    for item in items:
        require(item.get("id") == "C-" + digest(item.get("origin"))[:24]
                and item.get("status") in OUTSTANDING | {"dropped", "resolved", "accepted_historical"}
                and isinstance(item.get("requirement"), dict)
                and isinstance(item.get("history"), list), "Carryover: некорректный элемент ledger")
        source = item.get("source")
        require(source is None or source.get("source") in sources
                and source.get("quote") in sources[source["source"]]["text"],
                "Carryover: потеряна исходная цитата")
    if "executionRemainders" in payload:
        execution_history.validate(payload["executionRemainders"], sources)
    return payload


def _resolution_current(store, state, item):
    """A current-run resolution must still prove exactly what its receipt closed."""
    resolution, mapping = item.get("resolution", {}), item.get("mapping", {})
    if resolution.get("run") != state["run"]:
        return True  # A verified terminal handoff is history, never current proof.
    if state.get("status") == "cancelled" or mapping.get("run") != state["run"]:
        return False
    req = next((r for r in state["requirements"] if r["id"] == mapping.get("requirement")), None)
    if req is None or req.get("scope", "active") != "active" or digest(req) != mapping.get("requirementDigest"):
        return False
    tasks = _tasks(state, req)
    proofs = {r["task"]: r.get("proof") for r in resolution.get("evidence", [])}
    return bool(tasks and set(proofs) == {task["id"] for task in tasks}
                and all(task.get("proof") == proofs[task["id"]]
                        and task_proof_valid(store, state, task["id"]) for task in tasks))


def _ledger(store, state, *, effective=True):
    carry = state.get("carryover")
    if not carry:
        return {"items": [], "sources": [], "runs": [], "decisions": []}
    require(carry.get("schema") == SCHEMA, "Carryover: неподдерживаемая схема")
    base = _payload_valid(_load(store, carry["handoff"]))
    items = {i["id"]: copy.deepcopy(i) for i in base["items"]}
    execution_items = {i["id"]: copy.deepcopy(i) for i in base.get("executionRemainders", [])}
    sources = {}
    _merge_sources(sources, base["sources"])
    for artifact in carry.get("decisions", []):
        receipt = _load(store, artifact)
        ident = receipt.get("item")
        if isinstance(ident, str) and ident in execution_items:
            require(receipt.get("schema") == SCHEMA and receipt.get("run") == state["run"]
                    and receipt.get("before") == digest(execution_items[ident]),
                    "Carryover: повреждена или переставлена цепочка решений об исполнении")
            _merge_sources(sources, receipt.get("sources", []))
            execution_items[ident] = execution_history.apply_disposition(execution_items[ident], receipt, state["run"], sources)
            continue
        require(receipt.get("schema") == SCHEMA and receipt.get("run") == state["run"]
                and ident in items and receipt.get("before") == digest(items[ident]),
                "Carryover: повреждена или переставлена цепочка решений")
        after = receipt.get("after", {})
        require(after.get("id") == ident and after.get("origin") == items[ident]["origin"]
                and after.get("requirement") == items[ident]["requirement"]
                and after.get("source") == items[ident]["source"], "Carryover: переписан исходный контракт")
        _merge_sources(sources, receipt.get("sources", []))
        items[ident] = after
    result = {**base, "items": list(items.values()), "sources": list(sources.values())}
    if execution_items:
        result["executionRemainders"] = list(execution_items.values())
    if effective:
        for item in result["items"]:
            if item["status"] == "resolved" and not _resolution_current(store, state, item):
                # Do not edit or erase the earlier decision. The effective view
                # reopens it; capture persists that open state into the next run.
                item["status"] = "mapped" if item.get("mapping", {}).get("run") == state["run"] else "outstanding"
                item["resolutionStale"] = {"run": state["run"],
                    "reason": "Current requirement, task proof or run disposition no longer matches the resolution."}
    result["digest"] = digest({k: v for k, v in result.items() if k != "digest"})
    _payload_valid(result)
    return result


def _attempts(state, tasks):
    result = []
    for task in tasks:
        for attempt in state.get("taskHistory", {}).get(task["id"], []):
            result.append({"run": state["run"], "task": task["id"], "attempt": attempt["id"],
                           "createdAt": attempt.get("createdAt"), "rounds": len(attempt.get("rounds", [])),
                           "terminal": bool(attempt.get("terminal")),
                           "actors": sorted(set(attempt.get("actors", {}).values()) | set(attempt.get("writers", []))),
                           "accepted": bool((attempt.get("acceptance") or {}).get("passed")),
                           **({"completionKind": "verified_completion", "verifiedCompletion": bool(attempt["verifiedCompletion"].get("passed"))}
                              if attempt.get("verifiedCompletion") else {}),
                           **({"tier": attempt["tier"]} if "tier" in attempt else {})})
    return result


def _unique(values):
    return list({digest(value): value for value in values}.values())


def _tasks(state, req):
    return [task for task in state["tasks"] if req["id"] in task["requirements"]
            or req.get("kind") == "constraint"]


def _open_reasons(store, state, req, tasks, journal, reports):
    """Extract historical failure context, never new acceptance criteria.

    Every excerpt retains its exact report/checkpoint pointer. There is no silent
    reason-count cap: the existing whole-handoff size guard fails closed instead.
    """
    reasons = []
    checkpoint = {"run": state["run"], "file": "events.jsonl", "sha256": journal["sha256"],
                  "sequence": journal["last"]["sequence"], "eventDigest": journal["last"]["hash"]}

    def add(kind, value, source, pointer, **metadata):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        if not text.strip():
            return
        _text(text.encode())  # Check the entire text before limiting the excerpt.
        reasons.append({"kind": kind, "text": text[:MAX_REASON_CHARS],
                        "textSha256": hashlib.sha256(text.encode()).hexdigest(),
                        "omittedChars": max(0, len(text) - MAX_REASON_CHARS),
                        "source": copy.deepcopy(source), "pointer": pointer,
                        "historical": True, **metadata})

    def report_reasons(artifact, *, task=None, attempt=None, role="acceptance", final=False):
        key = (artifact["file"], artifact["sha256"])
        if key not in reports:
            reports[key] = _load(store, artifact)  # Verify hash and secrets before extracting anything.
        report = reports[key]
        source = {"run": state["run"], **{k: artifact[k] for k in ("file", "sha256")}}
        metadata = {"role": role}
        if task is not None:
            metadata["task"] = task
        if attempt is not None:
            metadata["attempt"] = attempt
        for index, check in enumerate(report.get("checks", [])):
            if check.get("status") != "passed" and (not final or check.get("requirement") == req["id"]):
                add("failed_check", check.get("note", ""), source, f"/checks/{index}/note",
                    requirement=check.get("requirement"), criterion=check.get("criterion"),
                    status=check.get("status"), **metadata)
        for index, finding in enumerate(report.get("findings", [])):
            add("critic_finding", finding.get("text", ""), source, f"/findings/{index}/text",
                finding=finding.get("id"), severity=finding.get("severity"),
                criterion=finding.get("criterion"), **metadata)
        for index, missing in enumerate(report.get("unmapped", [])):
            add("unmapped", missing, source, f"/unmapped/{index}", **metadata)
        for index, trace in enumerate(report.get("trace", [])):
            if trace.get("result") == "drift":
                add("source_drift", trace.get("note", ""), source, f"/trace/{index}/note",
                    requirement=trace.get("requirement"), **metadata)

    for task in tasks:
        if task.get("status") == "blocked" and task.get("note"):
            index = next(i for i, current in enumerate(state["tasks"]) if current["id"] == task["id"])
            add("task_blocker", task["note"], checkpoint, f"/state/tasks/{index}/note", task=task["id"])
        for attempt in state.get("taskHistory", {}).get(task["id"], []):
            for record in attempt.get("rounds", []):
                report_reasons(record["report"], task=task["id"], attempt=attempt["id"],
                               role="critic" + str(record.get("number", "")))
            accepted = attempt.get("acceptance")
            # A lean acceptance reuses the critic report already read above.
            if accepted and accepted.get("completionKind") != "critic_acceptance":
                report_reasons(accepted["report"], task=task["id"], attempt=attempt["id"], role="acceptor")
    final = state.get("gates", {}).get("acceptance", {})
    if final.get("report"):
        report_reasons(final["report"], final=True)
    else:
        # Older states can have a recorded remaining list without a final report.
        for index, check in enumerate(state.get("remaining", {}).get("checks", [])):
            if check.get("requirement") == req["id"]:
                add("remaining_check", check.get("note", ""), checkpoint,
                    f"/state/remaining/checks/{index}/note", requirement=req["id"],
                    criterion=check.get("criterion"), status=check.get("status"))
    return _unique(reasons)


def capture(previous_store, previous_state):
    """Return a bounded serializable snapshot of the latest saved terminal run."""
    require(previous_state.get("status") in TERMINAL, "Carryover: предыдущий прогон ещё не завершён")
    journal = _journal(previous_store, previous_state)
    artifacts = _unique(list(_artifacts(previous_state)))
    for artifact in artifacts:
        _read(previous_store, artifact)
    inherited = _ledger(previous_store, previous_state)
    sources = {}
    _merge_sources(sources, inherited["sources"])
    briefs = {}
    for brief in previous_state["briefs"]:
        source = _source(previous_store, previous_state, brief, "owner_brief")
        _merge_sources(sources, [source])
        briefs[brief["id"]] = source["id"]
    if previous_state.get("spec"):
        _merge_sources(sources, [_source(previous_store, previous_state, previous_state["spec"], "specification")])
    items = copy.deepcopy(inherited["items"])
    mapped = {}
    reports = {}
    for item in items:
        for event in item["history"]:
            if event["run"] == previous_state["run"] and event["action"] == "map":
                mapped[event["requirement"]] = item
        mapping = item.get("mapping")
        if mapping and mapping["run"] == previous_state["run"]:
            mapped[mapping["requirement"]] = item
    for req in previous_state["requirements"]:
        tasks = _tasks(previous_state, req)
        attempts = _attempts(previous_state, tasks)
        reasons = _open_reasons(previous_store, previous_state, req, tasks, journal, reports)
        if req["id"] in mapped:
            item = mapped[req["id"]]
            item["attempts"] = _unique(item.get("attempts", []) + attempts)
            item["openReasons"] = _unique(item.get("openReasons", []) + reasons)
            continue
        original = req
        if req.get("kind") == "addition":
            original = next((r for r in previous_state["requirements"] if r["id"] == req.get("parent")), {})
        source = {"source": briefs[original["brief"]], "quote": original["quote"]} if original.get("brief") in briefs else None
        require(source is not None, "Carryover: требование потеряло исходный бриф")
        status = {"dropped": "dropped", "deferred": "deferred"}.get(req.get("scope"), "outstanding")
        failures = previous_state.get("gates", {}).get("acceptance", {}).get("failures", [])
        accepted = tasks and all(historical_proof_valid(previous_store, previous_state, task["id"]) for task in tasks)
        if status == "outstanding" and accepted and not any(f.get("requirement") == req["id"] for f in failures):
            status = "accepted_historical"
        origin = {"run": previous_state["run"], "requirement": req["id"]}
        items.append({"id": "C-" + digest(origin)[:24], "origin": origin,
                      "requirement": copy.deepcopy(req), "source": source,
                      "status": status, "attempts": attempts, "history": [], "mapping": None,
                      "openReasons": reasons,
                      "historicalEvidence": [{"run": previous_state["run"], "task": task["id"],
                          "reportSha256": task.get("proof", {}).get("report", {}).get("sha256")}
                          for task in tasks if task.get("proof")], "currentProof": False})
    decisions = copy.deepcopy(inherited.get("decisions", []))
    for kind, values in (("owner_scope", journal["scopes"]), ("owner_retry", journal["retries"])):
        for decision in values:
            require(decision.get("brief") in briefs and decision.get("quote") in sources[briefs[decision["brief"]]]["text"],
                    "Carryover: решение владельца потеряло источник")
            decisions.append({"run": previous_state["run"], "kind": kind, **decision,
                              "source": briefs[decision["brief"]]})
    for note in previous_state.get("notes", []):
        decisions.append({"run": previous_state["run"], "kind": "agent_note", "note": copy.deepcopy(note)})
    checkpoint = {"run": previous_state["run"], "status": previous_state["status"], "title": previous_state["title"],
                  "sequence": journal["last"]["sequence"], "eventDigest": journal["last"]["hash"],
                  "stateDigest": digest(previous_state), "at": journal["last"]["at"]}
    payload = {"schema": SCHEMA, "previous": checkpoint,
               "runs": _unique(inherited.get("runs", []) + [checkpoint]),
               "sources": list(sources.values()), "items": items, "decisions": _unique(decisions),
               "capturedArtifacts": artifacts, "evidencePolicy": "Historical only; new run needs fresh local native roles and reviews."}
    execution = execution_history.capture(previous_store, previous_state, inherited, sources, briefs)
    if execution:
        payload["executionRemainders"] = execution
        payload["sources"] = list(sources.values())
    payload["digest"] = digest(payload)
    return _payload_valid(payload)


def attach(store, state, payload):
    """Copy the full immutable snapshot into a fresh run, without copying scope/proof."""
    _payload_valid(payload)
    require(not state.get("carryover") and not state["briefs"] and not state["requirements"]
            and not state["tasks"] and state["status"] == "running",
            "Carryover: присоединение возможно только к пустому новому прогону")
    require(state["run"] != payload["previous"]["run"], "Carryover: нельзя наследовать собственный прогон")
    # A stale in-memory capture must not authorize attaching tampered sources.
    previous = type(store)(store.root)
    previous.run = _safe(store.root, ".aispa/runs/" + payload["previous"]["run"])
    try:
        _safe(previous.run, "events.jsonl")
        old_state = previous.read_journal()["last"]["state"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Invalid("Carryover: предыдущий журнал недоступен") from exc
    journal = _journal(previous, old_state)
    require(journal["last"]["hash"] == payload["previous"]["eventDigest"], "Carryover: после capture предыдущий прогон изменился")
    for artifact in payload["capturedArtifacts"]:
        _read(previous, artifact)
    # Exact recomputation prevents a supplied payload from omitting or inventing
    # execution remainders while retaining a valid previous journal checkpoint.
    inherited = _ledger(previous, old_state)
    original_sources = {}
    _merge_sources(original_sources, inherited["sources"])
    original_briefs = {}
    for brief in old_state["briefs"]:
        source = _source(previous, old_state, brief, "owner_brief")
        _merge_sources(original_sources, [source])
        original_briefs[brief["id"]] = source["id"]
    expected_execution = execution_history.capture(previous, old_state, inherited, original_sources, original_briefs)
    require(payload.get("executionRemainders", []) == expected_execution,
            "Carryover: остатки исполнения отличаются от сохранённого исходного прогона")
    _safe(_run(store), "carryover")
    handoff = store.artifact("carryover", _json(payload), ".json")
    state["carryover"] = {"schema": SCHEMA, "handoff": handoff, "decisions": []}
    return state["carryover"]


def view(store, state):
    """Validated full ledger plus concise pointer, never current acceptance claims."""
    ledger = _ledger(store, state)
    items = ledger["items"]
    return {"schema": SCHEMA, "handoff": copy.deepcopy(state.get("carryover", {}).get("handoff")),
            **({"executionRemainders": execution_view(store, state)["items"],
                "executionOmitted": max(0, len(ledger["executionRemainders"]) - MAX_EXECUTION_VIEW)}
               if ledger.get("executionRemainders") else {}),
            "summary": {**({"executionOutstanding": sum(execution_history.disposition(row) != "dropped"
                                                       for row in ledger["executionRemainders"]),
                            "executionOwnerDropped": sum(execution_history.disposition(row) == "dropped"
                                                       for row in ledger["executionRemainders"])}
                            if ledger.get("executionRemainders") else {}),
                        "runs": len(ledger["runs"]), "items": len(items),
                        "outstanding": sum(i["status"] in OUTSTANDING for i in items),
                        "historicallyAccepted": sum(i["status"] in ("accepted_historical", "resolved") for i in items),
                        "currentProofInherited": False},
            "items": copy.deepcopy(items), "sources": copy.deepcopy(ledger["sources"]),
            "decisions": copy.deepcopy(ledger["decisions"]), "runs": copy.deepcopy(ledger["runs"])}


def unresolved(store, state):
    """Deferred obligations remain visible; dropped/resolved obligations do not."""
    return [copy.deepcopy(item) for item in _ledger(store, state)["items"] if item["status"] in OUTSTANDING]



def unresolved_execution(store, state):
    """Separate historical execution debts; no product scope or inherited PASS."""
    return execution_history.compact(_ledger(store, state), unresolved_only=True)


def execution_view(store, state, limit=MAX_EXECUTION_VIEW):
    """Bounded summary; the hash-bound full handoff remains the complete archive."""
    require(type(limit) is int and 0 <= limit <= 100, "Carryover: execution view limit должен быть 0..100")
    rows = execution_history.compact(_ledger(store, state))
    return {"items": rows[:limit], "total": len(rows), "omitted": max(0, len(rows) - limit),
            "outstanding": sum(row.get("disposition") != "dropped" for row in rows),
            "ownerDropped": sum(row.get("disposition") == "dropped" for row in rows),
            "archive": copy.deepcopy(state.get("carryover", {}).get("handoff")),
            "decisionReceipts": copy.deepcopy(state.get("carryover", {}).get("decisions", [])),
            "historicalOnly": True, "externalReceiptSupported": False}

def _source_relations(ledger, state):
    relations = []
    for item in ledger["items"]:
        mapping = item.get("mapping") or {}
        if mapping.get("run") != state["run"]:
            continue
        def include(source, quote):
            relations.append((source, quote, mapping["requirement"]))
        include(mapping["originalSource"]["source"], mapping["originalSource"]["quote"])
        for event in item["history"]:
            if event.get("source"):
                include(event["source"], None)
        for decision in ledger["decisions"]:
            if (decision.get("kind") == "owner_scope" and decision.get("run") == item["origin"]["run"]
                    and decision.get("requirement") == item["origin"]["requirement"]):
                include(decision["source"], None)
    return relations


def brief_sources(store, state):
    """Original owner words for blind review, with no ledger or review metadata.

    Include historical authorizations and scope decisions relevant to explicitly
    mapped current work. Current-run briefs already have ordinary B... IDs.
    """
    ledger = _ledger(store, state)
    wanted = {}
    for source, quote, _requirement in _source_relations(ledger, state):
        wanted.setdefault(source, [])
        if quote is not None and quote not in wanted[source]:
            wanted[source].append(quote)
    return [{"id": "H-" + source["id"], "text": source["text"], "origin": copy.deepcopy(source["origin"]),
             "applicableQuotes": wanted[source["id"]]}
            for source in ledger["sources"] if source["id"] in wanted
            and source["kind"] == "owner_brief" and source["origin"]["run"] != state["run"]]


def source_matches(store, state, source_id, quote, current_requirement):
    """Reconcile a historical observation without exposing the ledger to blind review."""
    if not nonempty(quote) or not isinstance(source_id, str) or not source_id.startswith("H-"):
        return False
    ledger = _ledger(store, state)
    source = next((s for s in ledger["sources"] if "H-" + s["id"] == source_id
                   and s["kind"] == "owner_brief" and s["origin"]["run"] != state["run"]), None)
    if source is None or quote not in source["text"]:
        return False
    return any(nonempty(original) and source_id == "H-" + sid and current_requirement == rid
               and (original in quote or quote in original)
               for sid, original, rid in _source_relations(ledger, state))


def _authorization(store, state, ledger, item, brief, quote):
    source = next((b for b in state["briefs"] if b["id"] == brief), None)
    require(source is not None and nonempty(quote), "Carryover: нужен новый текущий бриф владельца и точная цитата")
    saved = _source(store, state, source, "owner_brief")
    require(quote in saved["text"], "Carryover: цитата отсутствует в новом брифе")
    # Identical archived text isn't new owner authorization, even under a new ID.
    require(saved["origin"]["sha256"] not in {s["origin"]["sha256"] for s in ledger["sources"]
            if s["origin"]["run"] != state["run"] and s["kind"] == "owner_brief"},
            "Carryover: копия старого брифа не разрешает новую попытку")
    order = {b["id"]: index for index, b in enumerate(state["briefs"])}
    prior = [h for h in item["history"] if h["run"] == state["run"] and h.get("brief")]
    require(not prior or order[brief] > order[prior[-1]["brief"]],
            "Carryover: изменение решения требует более нового брифа владельца")
    return saved


def _compatible(item, requirement):
    old = item["requirement"]
    require(requirement.get("scope", "active") == "active"
            and requirement.get("text") == old["text"] and requirement.get("kind") == old["kind"]
            and (not old.get("core") or requirement.get("core"))
            and set(old["acceptance"]) <= set(requirement.get("acceptance", [])),
            "Carryover: новое требование ослабляет исходный текст, вид, ядро или критерии")



def _decide_execution(store, state, ledger, item, action, current_requirement, brief, quote):
    require(action in ("keep", "drop", "defer") and current_requirement is None,
            "Carryover: исполнение поддерживает только keep/drop/defer, без map/resolve и product requirement")
    require(execution_history.disposition(item) != "dropped", "Carryover: владелец уже снял этот остаток исполнения")
    event = {"run": state["run"], "action": action, "at": timestamp(), "binding": copy.deepcopy(item["origin"])}
    sources = []
    if action in ("drop", "defer"):
        source = _authorization(store, state, ledger, {"history": item.get("dispositions", [])}, brief, quote)
        prior_sources = {event["source"] for event in item.get("dispositions", []) if event.get("source")}
        prior_sources.add(item["source"]["source"])
        require(source["origin"]["sha256"] not in {
            prior["origin"]["sha256"] for prior in ledger["sources"] if prior["id"] in prior_sources},
            "Carryover: новое решение по этому исполнению требует новых слов владельца")
        sources.append(source)
        event.update(brief=brief, quote=quote, source=source["id"])
    else:
        require(brief is None and quote is None, "Carryover: keep не принимает неподтверждённую авторизацию")
    after = copy.deepcopy(item)
    after["dispositions"] = [*after.get("dispositions", []), event]
    receipt = {"schema": SCHEMA, "run": state["run"], "item": item["id"], "action": action,
               "before": digest(item), "after": after, "sources": sources}
    merged = {source["id"]: source for source in ledger["sources"]}
    _merge_sources(merged, sources)
    execution_history.apply_disposition(item, receipt, state["run"], merged)
    require(not SECRET.search(_json(receipt).decode()), "Carryover: возможный секрет в решении")
    _safe(_run(store), "carryover-decisions")
    artifact = store.artifact("carryover-decisions", _json(receipt), ".json")
    state["carryover"]["decisions"].append(artifact)
    return {"item": item["id"], "action": action, "status": after["status"],
            "disposition": execution_history.disposition(after), "timingOutcomeUnchanged": True,
            "receipt": artifact}

def decide(store, state, item_id, action, current_requirement=None, brief=None, quote=None):
    """Record keep/map/resolve/drop/defer; caller appends state after success.

    map is explicit new-owner authorization, not acceptance. Its receipt retains
    the exact original source alongside the current requirement's source. resolve
    is possible only with fresh current-run task proofs after that authorization.
    """
    require(state["status"] == "running", "Carryover: решения доступны только в работающем прогоне")
    require(action in ("keep", "map", "resolve", "drop", "defer"), "Carryover: неизвестное решение")
    ledger = _ledger(store, state, effective=False)
    execution = next((i for i in ledger.get("executionRemainders", []) if i["id"] == item_id), None)
    if execution is not None:
        return _decide_execution(store, state, ledger, execution, action, current_requirement, brief, quote)
    item = next((i for i in ledger["items"] if i["id"] == item_id), None)
    require(item is not None and (item["status"] in OUTSTANDING
            or item["status"] == "resolved" and not _resolution_current(store, state, item)),
            "Carryover: нет открытого обязательства")
    after = copy.deepcopy(item)
    event = {"run": state["run"], "action": action, "at": timestamp()}
    sources = []
    if action in ("map", "drop", "defer"):
        source = _authorization(store, state, ledger, item, brief, quote)
        sources.append(source)
        event.update(brief=brief, quote=quote, source=source["id"])
    if action == "map":
        req = next((r for r in state["requirements"] if r["id"] == current_requirement), None)
        require(req is not None, "Carryover: неизвестное текущее требование")
        _compatible(item, req)
        require(not any(i.get("mapping") and i["mapping"]["run"] == state["run"]
                        and i["mapping"]["requirement"] == current_requirement and i["id"] != item_id
                        for i in ledger["items"]), "Carryover: текущее требование уже связано с другим обязательством")
        require(not any(a.get("rounds") or a.get("submitted") for task in _tasks(state, req)
                        for a in state.get("taskHistory", {}).get(task["id"], [])),
                "Carryover: происхождение и новая попытка фиксируются до сдачи и критики")
        event["requirement"] = current_requirement
        after["mapping"] = {"run": state["run"], "requirement": current_requirement,
                            "requirementDigest": digest(req), "authorization": copy.deepcopy(event),
                            "originalSource": copy.deepcopy(item["source"])}
        after["status"] = "mapped"
    elif action == "resolve":
        mapping = item.get("mapping")
        require(mapping and mapping["run"] == state["run"], "Carryover: сначала сопоставьте обязательство с текущим требованием")
        require(current_requirement is None or current_requirement == mapping["requirement"], "Carryover: другое требование")
        req = next((r for r in state["requirements"] if r["id"] == mapping["requirement"]), None)
        require(req is not None and digest(req) == mapping["requirementDigest"], "Carryover: сопоставленное требование изменилось")
        _compatible(item, req)
        tasks = _tasks(state, req)
        require(tasks and all(task_proof_valid(store, state, task["id"]) for task in tasks),
                "Carryover: нужны свежие проверки и приёмка всех текущих задач")
        old_actors = {actor for attempt in item["attempts"] for actor in attempt["actors"]}
        for task in tasks:
            attempt = state["taskHistory"][task["id"]][-1]
            require(not old_actors.intersection(attempt["actors"].values()), "Carryover: новая попытка требует новых нативных ролей")
            require(attempt.get("createdAt", "") >= mapping["authorization"]["at"],
                    "Carryover: текущая попытка началась до разрешения владельца")
        event["requirement"] = mapping["requirement"]
        event["evidence"] = [{"run": state["run"], "task": t["id"], "proof": copy.deepcopy(t["proof"])} for t in tasks]
        after["status"] = "resolved"
        after.pop("resolutionStale", None)
        after["resolution"] = copy.deepcopy(event)
    else:
        after["status"] = {"keep": "deferred" if item["status"] == "deferred" else "outstanding",
                           "drop": "dropped", "defer": "deferred"}[action]
    after["history"].append(event)
    receipt = {"schema": SCHEMA, "run": state["run"], "item": item_id, "action": action,
               "before": digest(item), "after": after, "sources": sources}
    require(not SECRET.search(_json(receipt).decode()), "Carryover: возможный секрет в решении")
    _safe(_run(store), "carryover-decisions")
    artifact = store.artifact("carryover-decisions", _json(receipt), ".json")
    state["carryover"]["decisions"].append(artifact)
    return {"item": item_id, "action": action, "status": after["status"], "receipt": artifact}


def guard_current(store, state):
    """Block deterministic aliases; semantic attribution is the coordinator's job."""
    ledger = _ledger(store, state)
    for item in ledger["items"]:
        if item["status"] not in OUTSTANDING:
            continue
        old = item["requirement"]
        for req in state["requirements"]:
            if req.get("scope", "active") != "active":
                continue
            related = req.get("text") == old["text"]
            if not related:
                continue
            mapping = item.get("mapping")
            require(mapping and mapping["run"] == state["run"] and mapping["requirement"] == req["id"]
                    and mapping["requirementDigest"] == digest(req),
                    "Carryover: продолжение старого обязательства требует map и нового брифа владельца")
    return True
