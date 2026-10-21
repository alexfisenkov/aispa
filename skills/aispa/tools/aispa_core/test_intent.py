"""Source-bound RED intent before a task's first submission.

This records a test contract and command provenance. It cannot decide whether
the test actually expresses the requested behavior; independent reviewers do.
"""
import copy
import json
import re

from .contracts import artifact_valid, task_basis, task_contract, task_verified, require
from .portable import relative_name
from .store import Invalid, confined, file_hash, digest, timestamp, canonical_zone


SCHEMA = 1
SHA = re.compile(r"[0-9a-f]{64}\Z")
PURPOSES = frozenset({"docs", "config", "integration", "existing_behavior"})
MAX_TESTS = 32
MAX_REVISIONS = 16
MAX_REASON = 1600
MAX_TEST_BYTES = 2 * 1024 * 1024


def _text(value, label, limit=MAX_REASON):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= limit,
            "Test intent: неверное поле " + label)
    return value.strip()


def _attempt(state, task_id):
    task = next((t for t in state.get("tasks", []) if t.get("id") == task_id), None)
    history = state.get("taskHistory", {}).get(task_id, [])
    require(task is not None and isinstance(history, list) and history
            and isinstance(history[-1], dict), "Test intent: задача или попытка отсутствует")
    return task, history[-1]


def _file(store, source):
    require(isinstance(source, dict) and set(source) == {"path", "sha256"}
            and isinstance(source["sha256"], str) and SHA.fullmatch(source["sha256"]),
            "Test intent: нужен путь и SHA-256 источника")
    try:
        path = relative_name(source["path"])
        canonical_zone(path)
        from .design_contract import safe_reference_name
        safe_reference_name(path)
        file = confined(store.root, path)
    except (TypeError, ValueError) as exc:
        raise Invalid("Test intent: небезопасный путь источника") from exc
    require(path == source["path"] and file.is_file() and file.stat().st_size <= MAX_TEST_BYTES
            and file_hash(file) == source["sha256"], "Test intent: тестовый источник изменён")
    return path


def capture_baseline(store, state, task_id):
    task, attempt = _attempt(state, task_id)
    if attempt.get("testBaseline"):
        require(artifact_valid(store, attempt["testBaseline"]), "Test intent: повреждена исходная точка задачи")
        return attempt["testBaseline"]
    value = {"run": state["run"], "task": task_id, "attempt": attempt["id"],
             "basis": task_basis(store, state, task_id),
             "records": store.code_records(task["zones"] + task.get("readZones", []), include_common=True)}
    attempt["testBaseline"] = store.artifact("test-baselines", json.dumps(value, ensure_ascii=False).encode(), ".json")
    return attempt["testBaseline"]


def _na_guard(store, state, task, attempt, report):
    """Obvious code changes cannot masquerade as documentation-only work."""
    from . import executions
    from pathlib import PurePosixPath
    ref = attempt.get("testBaseline")
    require(ref and artifact_valid(store, ref), "Test intent: нет исходного снимка запуска задачи")
    base = json.loads(store.text(ref))
    require(base.get("run") == state["run"] and base.get("task") == task["id"]
            and base.get("attempt") == attempt["id"], "Test intent: чужой исходный снимок")
    old = dict(base["records"])
    now = dict(store.code_records(task["zones"] + task.get("readZones", []), include_common=True))
    noncode_suffixes = {".md", ".rst", ".txt", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg",
                       ".lock", ".csv", ".tsv", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico"}
    noncode_names = {"README", "LICENSE", "NOTICE", ".gitignore", ".gitattributes", ".editorconfig"}
    changed_code = [p for p in old.keys() | now.keys()
                    if old.get(p) != now.get(p) and PurePosixPath(p).suffix.lower() not in noncode_suffixes
                    and PurePosixPath(p).name not in noncode_names]
    if not changed_code:
        return
    require(all(row["purpose"] in ("existing_behavior", "integration") for row in report["notApplicable"]),
            "Test intent: изменения кода нельзя объявить только документацией или конфигурацией")
    precheck = report.get("prechangeExecution")
    require(isinstance(precheck, dict), "Test intent: изменённому существующему поведению нужна проверка до изменений")
    receipt = executions.resolve(store, state, precheck)
    require(receipt.get("kind") == "task" and receipt.get("task") == task["id"]
            and receipt.get("purpose") == "check"
            and receipt.get("attempt") == attempt["id"] and receipt.get("successful") is True
            and receipt.get("candidate") == base["basis"],
            "Test intent: исходная проверка должна успешно выполняться до изменений задачи")
    current = attempt.get("verification")
    if current and current.get("exitCode") == 0:
        require(current.get("command") == receipt["command"],
                "Test intent: неизменность поведения проверяется той же командой до и после")


def _criteria(task, report):
    expected = task.get("criteria")
    require(isinstance(expected, list) and expected and all(isinstance(c, str) and c.strip() for c in expected)
            and len(set(expected)) == len(expected), "Test intent: у задачи нет однозначных критериев")
    require(report.get("criteria") == expected, "Test intent: нужны все точные критерии задачи")
    return set(expected)


def _first_submit(events, task_id, attempt_id):
    for event in events:
        if event.get("event") != "task-submitted:" + task_id:
            continue
        history = event.get("state", {}).get("taskHistory", {}).get(task_id, [])
        if history and history[-1].get("id") == attempt_id:
            return event["sequence"]
    return None


def _intent_event(events, task_id, attempt_id, ref):
    for event in events:
        history = event.get("state", {}).get("taskHistory", {}).get(task_id, [])
        if not history or history[-1].get("id") != attempt_id:
            continue
        if ref in history[-1].get("testIntentHistory", []):
            return event["sequence"]
    return None


def _saved(store, ref):
    require(isinstance(ref, dict) and artifact_valid(store, ref)
            and isinstance(ref.get("file"), str) and ref["file"].startswith("test-intent/"),
            "Test intent: изменён сохранённый контракт")
    try:
        value = json.loads(store.text(ref))
    except (ValueError, OSError, TypeError) as exc:
        raise Invalid("Test intent: повреждён JSON контракта") from exc
    require(isinstance(value, dict) and value.get("schema") == SCHEMA,
            "Test intent: неизвестная схема контракта")
    return value


def _report(store, state, task, attempt, report):
    require(isinstance(report, dict) and report.get("mode") in ("behavior_change", "not_applicable")
            and report.get("attempt") == attempt["id"]
            and report.get("basis") == task_basis(store, state, task["id"]),
            "Test intent: отчёт относится к другой попытке или снимку")
    expected = _criteria(task, report)
    extra = {"failureChecks"} if state.get("policy", {}).get("errorBoundaryChecks") else set()
    if report["mode"] == "behavior_change":
        require(set(report) == {"mode", "attempt", "basis", "criteria", "redExecution", "newTestFiles"} | extra,
                "Test intent: неверная схема RED")
        tests = report["newTestFiles"]
        require(isinstance(tests, list) and 0 < len(tests) <= MAX_TESTS,
                "Test intent: нужны новые тесты с SHA")
        seen, covered = set(), set()
        for row in tests:
            require(isinstance(row, dict) and set(row) == {"path", "sha256", "expectedFailure", "criteria"},
                    "Test intent: тесту нужны путь, SHA, ожидаемая ошибка и критерии")
            path = _file(store, {k: row[k] for k in ("path", "sha256")})
            require(path not in seen and isinstance(row["criteria"], list) and row["criteria"]
                    and all(isinstance(c, str) for c in row["criteria"])
                    and len(set(row["criteria"])) == len(row["criteria"])
                    and set(row["criteria"]) <= expected,
                    "Test intent: повторный тест или чужой критерий")
            _text(row["expectedFailure"], "expectedFailure", 500)
            seen.add(path)
            covered.update(row["criteria"])
        require(covered == expected, "Test intent: часть критериев не покрыта новыми тестами")
        require(isinstance(report["redExecution"], dict), "Test intent: нужна ссылка на реальный RED")
    else:
        require({"mode", "attempt", "basis", "criteria", "notApplicable"} | extra <= set(report)
                <= {"mode", "attempt", "basis", "criteria", "notApplicable", "prechangeExecution"} | extra,
                "Test intent: неверная схема исключения")
        reasons = report["notApplicable"]
        require(isinstance(reasons, list) and len(reasons) == len(expected),
                "Test intent: нужна причина для каждого критерия")
        seen = set()
        for row in reasons:
            require(isinstance(row, dict) and set(row) == {"criterion", "purpose", "source", "reason"}
                    and isinstance(row["criterion"], str) and isinstance(row["purpose"], str)
                    and row["criterion"] in expected and row["criterion"] not in seen
                    and row["purpose"] in PURPOSES,
                    "Test intent: причина не связана с критерием и назначением")
            _text(row["reason"], "reason")
            _file(store, row["source"])
            seen.add(row["criterion"])
        _na_guard(store, state, task, attempt, report)
    if extra:
        from . import failure_checks
        failure_checks.validate(task, report, lambda source: _file(store, source))


def _red(store, state, task_id, attempt_id, report):
    from . import executions
    body = executions.resolve(store, state, report["redExecution"])
    require(body.get("kind") == "task" and body.get("purpose") == "check"
            and body.get("task") == task_id and body.get("attempt") == attempt_id
            and type(body.get("exitCode")) is int and body["exitCode"] > 0
            and not body.get("successful")
            and not any(body.get(key) for key in
                        ("timedOut", "truncated", "redacted", "changedCode", "changedSource", "launchError"))
            and body.get("candidate") == body.get("code"),
            "Test intent: нужен проверенный RED этой задачи без технической ошибки")
    output = store.text(body["output"])
    command = " ".join(body["command"]).replace("\\", "/")
    normalized_output = output.replace("\\", "/")
    for row in report["newTestFiles"]:
        require(row["expectedFailure"] in output
                and (row["path"] in command or row["path"] in normalized_output),
                "Test intent: RED не показывает ожидаемую ошибку указанного теста")
    return body


def record(store, state, task_id, report):
    """Record initial RED or an evidence-preserving pre-critic revision.

    The caller must append the mutated state to Journal after this returns.
    """
    require(state.get("policy", {}).get("testIntent") is True and state.get("status") == "running",
            "Test intent: контракт нужен только активной задаче новой политики")
    task, attempt = _attempt(state, task_id)
    require(task.get("status") == "running" and not attempt.get("rounds")
            and not attempt.get("acceptance") and not attempt.get("terminal"),
            "Test intent: контракт заморожен после начала критики")
    _report(store, state, task, attempt, report)
    journal = store.read_journal()
    journal["events"] = store.task_timeline(task_id)
    require(digest(journal["last"]["state"]) == digest(state),
            "Test intent: сначала сохраните исполнение в журнал")
    previous_refs = attempt.get("testIntentHistory", [])
    require(isinstance(previous_refs, list) and len(previous_refs) < MAX_REVISIONS,
            "Test intent: история повреждена или исчерпана")
    previous = _saved(store, previous_refs[-1]) if previous_refs else None
    first_submit = _first_submit(journal["events"], task_id, attempt["id"])
    require(previous is not None or first_submit is None,
            "Test intent: первый контракт записывают до первой сдачи")
    if previous:
        require(attempt.get("testIntent") == previous_refs[-1]
                and previous.get("run") == state["run"] and previous.get("task") == task_id
                and previous.get("attempt") == attempt["id"]
                and previous.get("taskContract") == task_contract(state, task_id),
                "Test intent: прежний контракт повреждён или устарел")
        require(previous["mode"] == report["mode"] or first_submit is None,
                "Test intent: режим после первой сдачи менять нельзя")
    red = _red(store, state, task_id, attempt["id"], report) if report["mode"] == "behavior_change" else None
    if red:
        changed_tests = (previous is None or previous["mode"] != "behavior_change"
                         or [(r["path"], r["sha256"]) for r in previous["report"]["newTestFiles"]]
                         != [(r["path"], r["sha256"]) for r in report["newTestFiles"]])
        if changed_tests:
            require(red["candidate"] == task_basis(store, state, task_id),
                    "Test intent: новый RED должен предшествовать следующей реализации")
            if previous and previous["mode"] == "behavior_change":
                previous_event = _intent_event(journal["events"], task_id, attempt["id"], previous_refs[-1])
                require(red["id"] != previous["redExecutionId"]
                        and type(previous_event) is int and red["journal"]["sequence"] > previous_event,
                        "Test intent: изменённым тестам нужен новый RED после прежнего контракта")
        else:
            require(red["id"] == previous["redExecutionId"],
                    "Test intent: неизменённые тесты сохраняют тот же RED")
    contract = {"schema": SCHEMA, "run": state["run"], "task": task_id,
                "attempt": attempt["id"], "taskContract": task_contract(state, task_id),
                "mode": report["mode"], "report": copy.deepcopy(report),
                "sourceBasis": red["candidate"] if red else task_basis(store, state, task_id),
                "initialSourceBasis": previous["initialSourceBasis"] if previous else
                                      (red["candidate"] if red else task_basis(store, state, task_id)),
                "redExecutionId": red["id"] if red else None,
                "redExecution": copy.deepcopy(report["redExecution"]) if red else None,
                "redJournalSequence": red["journal"]["sequence"] if red else None,
                "recordedAfterSequence": journal["last"]["sequence"],
                "revision": len(previous_refs) + 1, "at": timestamp()}
    ref = store.artifact("test-intent", (json.dumps(contract, ensure_ascii=False,
        sort_keys=True, indent=2) + "\n").encode(), ".json")
    attempt["testIntent"] = ref
    attempt["testIntentHistory"] = [*previous_refs, ref]
    return copy.deepcopy(ref)


def _valid(store, state, task_id, *, require_green, allow_source_changes=False, historical=False):
    task, attempt = _attempt(state, task_id)
    history = attempt.get("testIntentHistory")
    require(isinstance(history, list) and 0 < len(history) <= MAX_REVISIONS
            and attempt.get("testIntent") == history[-1],
            "Test intent: контракт отсутствует")
    journal = store.read_journal()
    journal["events"] = store.task_timeline(task_id)
    require(digest(journal["last"]["state"]) == digest(state),
            "Test intent: состояние не сохранено")
    contracts = [_saved(store, ref) for ref in history]
    baseline_ref = attempt.get("testBaseline")
    require(baseline_ref and artifact_valid(store, baseline_ref), "Test intent: исходный снимок изменён")
    baseline = json.loads(store.text(baseline_ref))
    require(baseline.get("run") == state["run"] and baseline.get("task") == task_id
            and baseline.get("attempt") == attempt["id"], "Test intent: чужой исходный снимок")
    first_submit = _first_submit(journal["events"], task_id, attempt["id"])
    event_sequences = [_intent_event(journal["events"], task_id, attempt["id"], ref) for ref in history]
    require(all(type(n) is int for n in event_sequences)
            and event_sequences == sorted(set(event_sequences))
            and (first_submit is None or event_sequences[0] < first_submit),
            "Test intent: первый контракт не записан до сдачи")
    for index, contract in enumerate(contracts):
        require(contract.get("run") == state["run"] and contract.get("task") == task_id
                and contract.get("attempt") == attempt["id"] and contract.get("revision") == index + 1
                and contract.get("recordedAfterSequence", 0) < event_sequences[index]
                and contract.get("initialSourceBasis") == contracts[0].get("initialSourceBasis"),
                "Test intent: история контракта расходится с журналом")
        if contract["mode"] == "behavior_change":
            historical_red = _red(store, state, task_id, attempt["id"], contract["report"])
            require(historical_red["id"] == contract.get("redExecutionId")
                    and historical_red["candidate"] == contract.get("sourceBasis")
                    and historical_red["journal"]["sequence"] == contract.get("redJournalSequence")
                    and historical_red["journal"]["sequence"] < event_sequences[index],
                    "Test intent: исторический RED источник изменён")
        previous_tests = ([(r["path"], r["sha256"]) for r in contracts[index - 1]["report"].get("newTestFiles", [])]
                          if index else [])
        current_tests = [(r["path"], r["sha256"]) for r in contract["report"].get("newTestFiles", [])]
        if index and contract["mode"] == "behavior_change" and current_tests != previous_tests:
            require(contract.get("redExecutionId") != contracts[index - 1].get("redExecutionId")
                    and contract.get("redJournalSequence", 0) > event_sequences[index - 1],
                    "Test intent: изменённые тесты не прошли новый RED")
    latest = contracts[-1]
    require(latest.get("taskContract") == task_contract(state, task_id)
            and latest.get("mode") in ("behavior_change", "not_applicable")
            and latest.get("report", {}).get("criteria") == task.get("criteria"),
            "Test intent: контракт задачи изменился")
    report = latest["report"]
    if state.get("policy", {}).get("errorBoundaryChecks"):
        from . import failure_checks
        failure_checks.validate(task, report)
    if latest["mode"] == "not_applicable" and not historical:
        _na_guard(store, state, task, attempt, report)
    sources = (report["newTestFiles"] if latest["mode"] == "behavior_change" else
               [row["source"] for row in report["notApplicable"]])
    if state.get("policy", {}).get("errorBoundaryChecks"):
        sources = [*sources, *failure_checks.sources(report["failureChecks"])]
    if not allow_source_changes:
        for source in sources:
            _file(store, {key: source[key] for key in ("path", "sha256")})
    if latest["mode"] == "behavior_change":
        red = _red(store, state, task_id, attempt["id"], report)
        require(red["id"] == latest["redExecutionId"]
                and red["candidate"] == latest["sourceBasis"],
                "Test intent: RED источник изменён")
        if require_green:
            require(task_basis(store, state, task_id) != latest["sourceBasis"],
                    "Test intent: GREEN кандидат совпадает с RED")
    if require_green:
        require(task_verified(store, state, task_id), "Test intent: нужна свежая GREEN проверка задачи")
    return True


def valid(store, state, task_id, *, require_green=True, allow_source_changes=False):
    """Fail closed for policy runs; old runs remain unaffected."""
    if not state.get("policy", {}).get("testIntent"):
        return True
    try:
        return _valid(store, state, task_id, require_green=require_green, allow_source_changes=allow_source_changes)
    except (Invalid, OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
        return False


def history_valid(store, state, task_id):
    """Validate preserved evidence without judging a descendant's current code."""
    if not state.get("policy", {}).get("testIntent"):
        return True
    try:
        return _valid(store, state, task_id, require_green=False, allow_source_changes=True, historical=True)
    except (Invalid, OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
        return False


def source_changes(store, state, task_id):
    """Expose repair changes without claiming the original RED tested them."""
    _task, attempt = _attempt(state, task_id)
    ref = attempt.get("testIntent")
    if not ref:
        return []
    value = _saved(store, ref)
    report = value["report"]
    sources = (report["newTestFiles"] if value["mode"] == "behavior_change"
               else [row["source"] for row in report["notApplicable"]])
    if state.get("policy", {}).get("errorBoundaryChecks"):
        from . import failure_checks
        sources = [*sources, *failure_checks.sources(report["failureChecks"])]
    changes = {}
    for source in sources:
        path = confined(store.root, relative_name(source["path"]))
        current = file_hash(path) if path.is_file() else None
        if current != source["sha256"]:
            changes[source["path"]] = {"path": source["path"], "before": source["sha256"], "after": current}
    return list(changes.values())


def view(store, state, task_id):
    """Compact status without raw command output or expected-failure text."""
    _task, attempt = _attempt(state, task_id)
    ref = attempt.get("testIntent")
    if not ref:
        return {"present": False, "valid": not state.get("policy", {}).get("testIntent"),
                "mode": None, "attempt": attempt["id"]}
    try:
        contract = _saved(store, ref)
        mode = contract["mode"]
        sources = (contract["report"].get("newTestFiles", []) if mode == "behavior_change" else
                   contract["report"].get("notApplicable", []))
        return {"present": True, "valid": valid(store, state, task_id), "mode": mode,
                "attempt": attempt["id"], "revision": contract["revision"],
                "sourceBasis": contract["sourceBasis"], "sourceCount": len(sources),
                "failureCaseCount": len(contract["report"].get("failureChecks", {}).get("cases", []))}
    except (Invalid, OSError, ValueError, TypeError, KeyError):
        return {"present": True, "valid": False, "mode": None, "attempt": attempt["id"]}
