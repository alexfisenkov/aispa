"""Versioned per-source RED provenance for explicitly opted-in new runs.

Schema one is deliberately handled by test_intent's original implementation.
This module records schema two only for addressed reports. It validates command
provenance and declaration continuity; reviewers still judge test semantics.
"""
import copy
import json

from . import executions, failure_checks, scale
from .contracts import artifact_valid, require, task_basis, task_contract, task_verified
from .design_contract import safe_reference_name
from .portable import relative_name
from .store import Invalid, canonical_zone, confined, digest, timestamp


SCHEMA = 2
BASE_ROW = frozenset({"path", "sha256", "criteria", "expectedFailure"})
CASE_FIELDS = ("id", "boundary", "scenario", "expected", "criterion", "kind")


def addressed(state, report):
    """Legacy explicit report shapes retain their original admission rules."""
    return (state.get("policy", {}).get("incrementalTestIntent") is True
            and isinstance(report, dict) and report.get("mode") in ("behavior_change", "mixed")
            and "redExecution" not in report)


def _legacy():
    # Delayed import keeps the public recorder and schema-one consumers stable.
    from . import test_intent
    return test_intent


def _source(store, source, *, current=False):
    old = _legacy()
    require(isinstance(source, dict) and set(source) == {"path", "sha256"}
            and isinstance(source.get("sha256"), str) and old.SHA.fullmatch(source["sha256"]),
            "Incremental RED: нужен точный путь и SHA-256 источника")
    try:
        path = relative_name(source["path"])
        canonical_zone(path)
        safe_reference_name(path)
        confined(store.root, path)
    except (TypeError, ValueError) as error:
        raise Invalid("Incremental RED: небезопасный путь источника") from error
    require(path == source["path"], "Incremental RED: неканонический путь источника")
    if current:
        old._file(store, source)
    return path


def _in_scope(path, zones):
    name = canonical_zone(path)
    return any(zone == "." or name == zone or name.startswith(zone + "/")
               for zone in (canonical_zone(value) for value in zones))


def semantic(row, report):
    """Exact criteria order and only this source's automated cases are binding."""
    pointer = {key: row[key] for key in ("path", "sha256")}
    cases = []
    for case in report.get("failureChecks", {}).get("cases", []):
        kind = case.get("kind", "automated" if report["mode"] == "behavior_change" else None)
        if kind == "automated" and case.get("test") == pointer:
            cases.append({key: (kind if key == "kind" else case[key]) for key in CASE_FIELDS})
    return digest({**{key: row[key] for key in BASE_ROW},
                   "failureCases": sorted(cases, key=lambda case: case["id"])})


def _shape(store, state, task, attempt, report, *, current=False):
    old = _legacy()
    scale.check_value(report, state)
    require(isinstance(report, dict) and report.get("mode") in ("behavior_change", "mixed")
            and report.get("attempt") == attempt["id"]
            and isinstance(report.get("basis"), str) and old.SHA.fullmatch(report["basis"]),
            "Incremental RED: чужая попытка или неверный снимок")
    shape = {"mode", "attempt", "basis", "criteria", "newTestFiles"}
    expected = old._criteria(task, report)
    if report["mode"] == "mixed":
        require(state.get("policy", {}).get("mixedEvidence") is True,
                "Incremental RED: mixed требует закреплённой политики")
        shape.add("routes")
        expected = old._routes(report, expected, state)
    if state.get("policy", {}).get("errorBoundaryChecks"):
        shape.add("failureChecks")
    require(set(report) == shape, "Incremental RED: неверная схема адресного контракта")
    rows = report["newTestFiles"]
    require(isinstance(rows, list) and (scale.enabled(state) or len(rows) <= old.MAX_TESTS)
            and bool(rows) == bool(expected), "Incremental RED: неверный набор тестовых источников")
    seen, covered = set(), set()
    for row in rows:
        require(isinstance(row, dict) and set(row) in
                (BASE_ROW | {"redExecution"}, BASE_ROW | {"redFrom"}),
                "Incremental RED: источнику нужен ровно один RED или предшественник")
        path = _source(store, {key: row[key] for key in ("path", "sha256")}, current=current)
        require(path not in seen and _in_scope(path, task["zones"]),
                "Incremental RED: тест повторён или отсутствует законная зона записи")
        criteria = row["criteria"]
        require(isinstance(criteria, list) and criteria
                and all(isinstance(value, str) for value in criteria)
                and len(set(criteria)) == len(criteria) and set(criteria) <= expected,
                "Incremental RED: неверные критерии тестового источника")
        old._text(row["expectedFailure"], "expectedFailure", 500, state=state)
        require(isinstance(row.get("redExecution", row.get("redFrom")), dict),
                "Incremental RED: нужна точная ссылка происхождения RED")
        seen.add(path)
        covered.update(criteria)
    require(covered == expected, "Incremental RED: автоматические критерии покрыты не полностью")
    if "failureChecks" in shape:
        failure_checks.validate(task, report,
            lambda source: _source(store, source, current=current), state=state)


def _review_anchor(store, task, attempt, events):
    """Resolve the real first decision, rather than a newly resealed marker."""
    original, first = attempt["submitted"], attempt["rounds"][0]
    submitted_sequences = []
    for summary in events:
        if summary.get("event") not in ("task-submitted:" + task["id"], "task-critic-1:" + task["id"]):
            continue
        event, previous = store.event_pair(summary["sequence"])
        histories = event["state"].get("taskHistory", {}).get(task["id"], [])
        saved = histories[-1] if histories else {}
        if saved.get("id") != attempt["id"]:
            continue
        if summary["event"] == "task-submitted:" + task["id"]:
            if saved.get("submitted") == original and not saved.get("rounds"):
                submitted_sequences.append(summary["sequence"])
            continue
        before_histories = previous.get("state", {}).get("taskHistory", {}).get(task["id"], []) if previous else []
        before = before_histories[-1] if before_histories else {}
        # The first real critic1 event consumes this attempt's first decision.
        # Clearing rounds later cannot promote a newly sealed record to it.
        return (saved.get("submitted") == original and saved.get("rounds") == [first]
                and before.get("id") == attempt["id"] and not before.get("rounds")
                and before.get("submitted") == original
                and any(sequence < summary["sequence"] for sequence in submitted_sequences))
    return False


def _stage(store, state, task, attempt, *, submitted=False, events):
    require(task.get("status") == "running" and not attempt.get("terminal")
            and not attempt.get("acceptance") and not attempt.get("verifiedCompletion"),
            "Incremental RED: попытка завершена или не выполняется")
    rounds = attempt.get("rounds", [])
    require(isinstance(rounds, list), "Incremental RED: повреждена история критики")
    if not rounds:
        require(not submitted and not attempt.get("submitted"),
                "Incremental RED: после сдачи дождитесь первого non-PASS")
        return
    require(task.get("phase") == "repair" and attempt.get("submitted")
            and len(rounds) == 1 and rounds[0].get("number") == 1
            and rounds[0].get("passed") is False and not attempt.get("repair")
            and not attempt.get("repairHistory") and not attempt.get("actors", {}).get("critic2"),
            "Incremental RED: ревизия допустима после первого non-PASS до сдачи ремонта и critic2")
    from . import team
    original, first = attempt["submitted"], rounds[0]
    require(team._record_valid(store, original) and team._record_valid(store, first)
            and team._writer_valid(store, state, attempt, original.get("actor"))
            and team._actor_valid(store, state, attempt, "critic1")
            and first.get("actor") == attempt.get("actors", {}).get("critic1")
            and first.get("actor") not in attempt.get("writers", [])
            and first.get("basis") == original.get("basis")
            and original.get("testIntent") in attempt.get("testIntentHistory", [])
            and first.get("testIntent") == original.get("testIntent")
            and _review_anchor(store, task, attempt, events),
            "Incremental RED: нужен неизменный sealed первый non-PASS с исходной сдачей и независимым actor")


def _transition(previous, report, *, submitted):
    old = previous["report"]
    require(old["mode"] == report["mode"] and old["criteria"] == report["criteria"],
            "Incremental RED: режим и прежние критерии сохраняются")
    require({row["path"] for row in old["newTestFiles"]}
            <= {row["path"] for row in report["newTestFiles"]},
            "Incremental RED: прежний тестовый источник нельзя удалить")
    if old["mode"] == "mixed":
        before = {(row["criterion"], row["kind"]): row for row in old["routes"]}
        after = {(row["criterion"], row["kind"]): row for row in report["routes"]}
        require(all(after.get(key) == value for key, value in before.items())
                and (not submitted or before == after),
                "Incremental RED: прежние способы неизменны; добавление только до сдачи")
    before_cases = old.get("failureChecks", {}).get("cases", [])
    after_cases = {row["id"]: row for row in report.get("failureChecks", {}).get("cases", [])}
    for before in before_cases:
        after = after_cases.get(before["id"])
        require(after is not None and all(after.get(key) == before.get(key)
                for key in ("boundary", "criterion", "kind")),
                "Incremental RED: прежний отказной сценарий или его границу нельзя удалить")
        source_key = "test" if "test" in before else "source"
        require(source_key in after and after[source_key]["path"] == before[source_key]["path"],
                "Incremental RED: отказной сценарий потерял свой источник")


def _journal(store, state, task_id):
    journal = store.read_journal()
    require(digest(journal["last"]["state"]) == digest(state),
            "Incremental RED: сначала сохраните состояние в журнал")
    return journal, store.task_timeline(task_id)


def _epochs(store, state, task_id, attempt, *, current):
    if attempt.get("zoneExtensions"):
        from . import task_zone_extension
        task_zone_extension.history_valid(store, state, task_id, attempt)
        if current:
            task_zone_extension.require_exact_files(store, attempt)


def _red(store, state, task_id, attempt_id, row, ref):
    return _legacy()._red(store, state, task_id, attempt_id,
                         {"redExecution": ref, "newTestFiles": [row]})


def _bindings(store, state, task_id, attempt_id, report, *, previous=None,
              previous_ref=None, previous_sequence=None, recorded_after):
    old_bindings = {row["path"]: row for row in previous.get("redBindings", [])} if previous else {}
    bindings = []
    for row in report["newTestFiles"]:
        identity = semantic(row, report)
        inherited = row.get("redFrom")
        if inherited is not None:
            require(previous is not None and set(inherited) == {"intent", "path", "sha256", "semanticSha256"}
                    and inherited["intent"] == previous_ref and inherited["path"] == row["path"]
                    and inherited["sha256"] == row["sha256"] and inherited["semanticSha256"] == identity,
                    "Incremental RED: нужна точная строка непосредственно прежнего контракта")
            before = old_bindings.get(row["path"])
            require(before is not None and before["sha256"] == row["sha256"]
                    and before["semanticSha256"] == identity,
                    "Incremental RED: изменённые SHA или смысл требуют нового адресного RED")
            ref = before["redExecution"]
        else:
            ref = row["redExecution"]
        red = _red(store, state, task_id, attempt_id, row, ref)
        sequence = red["journal"]["sequence"]
        require(type(sequence) is int and sequence <= recorded_after,
                "Incremental RED: исполнение должно быть записано до контракта")
        if inherited is None:
            require(red["candidate"] == report["basis"]
                    and (previous_sequence is None or sequence > previous_sequence),
                    "Incremental RED: новому источнику нужен свежий RED текущего кандидата после прежней ревизии")
        binding = {**{key: copy.deepcopy(row[key]) for key in BASE_ROW},
                   "semanticSha256": identity, "redExecution": copy.deepcopy(ref),
                   "redExecutionId": red["id"], "redJournalSequence": sequence,
                   "sourceBasis": red["candidate"], "redFrom": copy.deepcopy(inherited)}
        if inherited is not None:
            require(all(binding[key] == before[key] for key in
                        ("redExecution", "redExecutionId", "redJournalSequence", "sourceBasis")),
                    "Incremental RED: прежнее исполнение или событие подменено")
        bindings.append(binding)
    return bindings


def _history(store, state, task_id, task, attempt, events):
    old = _legacy()
    refs = attempt.get("testIntentHistory", [])
    require(isinstance(refs, list) and refs and attempt.get("testIntent") == refs[-1],
            "Incremental RED: отсутствует точная история контрактов")
    old._revision_budget(state, len(refs))
    require(scale.enabled(state) or len(refs) <= old.MAX_REVISIONS,
            "Incremental RED: исчерпана история контрактов")
    baseline_ref = attempt.get("testBaseline")
    require(baseline_ref and artifact_valid(store, baseline_ref),
            "Incremental RED: исходный снимок попытки повреждён")
    baseline = store.json(baseline_ref)
    require(baseline.get("run") == state["run"] and baseline.get("task") == task_id
            and baseline.get("attempt") == attempt["id"],
            "Incremental RED: чужой исходный снимок")
    contracts = [old._saved(store, ref) for ref in refs]
    scale.check_value(contracts, state)
    sequences = [old._intent_event(events, task_id, attempt["id"], ref) for ref in refs]
    require(all(type(value) is int for value in sequences)
            and sequences == sorted(set(sequences)),
            "Incremental RED: история ревизий не соответствует событиям")
    first_submit = old._first_submit(events, task_id, attempt["id"])
    require(first_submit is None or sequences[0] < first_submit,
            "Incremental RED: первый контракт нужен до первой сдачи")
    for index, value in enumerate(contracts):
        event, _prior_event = store.event_pair(sequences[index])
        saved_state = event["state"]
        saved_task, saved_attempt = old._attempt(saved_state, task_id)
        require(value.get("schema") == SCHEMA and value.get("run") == state["run"]
                and saved_state.get("policy", {}).get("incrementalTestIntent") is True
                and isinstance(value.get("report"), dict)
                and isinstance(value.get("redBindings"), list)
                and value.get("task") == task_id and value.get("attempt") == attempt["id"]
                and value.get("revision") == index + 1
                and type(value.get("recordedAfterSequence")) is int
                and value["recordedAfterSequence"] < sequences[index]
                and value.get("initialSourceBasis") == contracts[0].get("initialSourceBasis")
                and value.get("taskContract") == task_contract(saved_state, task_id)
                and value.get("sourceBasis") == value.get("report", {}).get("basis")
                and saved_attempt.get("testIntent") == refs[index]
                and saved_attempt.get("testIntentHistory") == refs[:index + 1],
                "Incremental RED: сохранённая ревизия, попытка или происхождение изменены")
        submitted = first_submit is not None and sequences[index] > first_submit
        _stage(store, saved_state, saved_task, saved_attempt, submitted=submitted, events=events)
        report = value["report"]
        _shape(store, saved_state, saved_task, saved_attempt, report)
        previous = contracts[index - 1] if index else None
        if previous:
            _transition(previous, report, submitted=submitted)
        bindings = _bindings(store, state, task_id, attempt["id"], report,
            previous=previous, previous_ref=refs[index - 1] if index else None,
            previous_sequence=sequences[index - 1] if index else None,
            recorded_after=value["recordedAfterSequence"])
        require(value.get("redBindings") == bindings and value.get("mode") == report["mode"]
                and all(row["redJournalSequence"] < sequences[index] for row in bindings),
                "Incremental RED: сохранённые построчные RED не соответствуют истории")
    return contracts, sequences


def record(store, state, task_id, report):
    """Admit one addressed revision; the host appends the resulting state."""
    old = _legacy()
    require(addressed(state, report) and state.get("policy", {}).get("testIntent") is True
            and state.get("status") == "running", "Incremental RED: политика не включена для активного прогона")
    task, attempt = old._attempt(state, task_id)
    journal, events = _journal(store, state, task_id)
    first_submit = old._first_submit(events, task_id, attempt["id"])
    _stage(store, state, task, attempt, submitted=first_submit is not None, events=events)
    _epochs(store, state, task_id, attempt, current=True)
    require(report.get("basis") == task_basis(store, state, task_id),
            "Incremental RED: отчёт относится к другому кандидату")
    _shape(store, state, task, attempt, report, current=True)
    refs = old.require_revision_available(state, attempt)
    previous, previous_sequence = None, None
    if refs:
        contracts, sequences = _history(store, state, task_id, task, attempt, events)
        previous, previous_sequence = contracts[-1], sequences[-1]
        _transition(previous, report, submitted=first_submit is not None)
    else:
        require(first_submit is None, "Incremental RED: первый контракт нужен до сдачи")
    bindings = _bindings(store, state, task_id, attempt["id"], report,
        previous=previous, previous_ref=refs[-1] if refs else None,
        previous_sequence=previous_sequence, recorded_after=journal["last"]["sequence"])
    value = {"schema": SCHEMA, "run": state["run"], "task": task_id, "attempt": attempt["id"],
             "taskContract": task_contract(state, task_id), "mode": report["mode"],
             "report": copy.deepcopy(report), "redBindings": bindings, "sourceBasis": report["basis"],
             "initialSourceBasis": previous["initialSourceBasis"] if previous else report["basis"],
             "recordedAfterSequence": journal["last"]["sequence"], "revision": len(refs) + 1,
             "at": timestamp()}
    require(task_basis(store, state, task_id) == report["basis"]
            and store.read_journal()["last"]["hash"] == journal["last"]["hash"],
            "Incremental RED: кандидат или журнал изменились во время проверки контракта")
    ref = store.artifact("test-intent", (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
                                         + "\n").encode(), ".json")
    attempt["testIntent"] = ref
    attempt["testIntentHistory"] = [*refs, ref]
    return copy.deepcopy(ref)


def _green(store, state, task_id, task, attempt, latest, events):
    require(task_verified(store, state, task_id), "Incremental RED: нужна свежая полная GREEN проверка")
    receipt = attempt["verification"]
    actual, previous = None, None
    for event in reversed(events):
        if event.get("event") != "verification-executed":
            continue
        candidate_event, candidate_previous = store.event_pair(event["sequence"])
        history = candidate_event["state"].get("taskHistory", {}).get(task_id, [])
        activity = candidate_previous.get("state", {}).get("activity", {}) if candidate_previous else {}
        if (history and history[-1].get("id") == attempt["id"]
                and history[-1].get("verification") == receipt and activity.get("task") == task_id):
            actual, previous = candidate_event, candidate_previous
            break
    require(actual is not None, "Incremental RED: GREEN не связан с реальным событием исполнения")
    executions._started(previous, actual, "task", task_id)
    candidate = task_basis(store, state, task_id)
    require(receipt.get("candidate") == receipt.get("code") == candidate
            and all(row["sourceBasis"] != candidate for row in latest["redBindings"]),
            "Incremental RED: GREEN должен подтверждать реализацию после RED")
    require(artifact_valid(store, receipt.get("output", {})), "Incremental RED: вывод GREEN изменён")
    command = " ".join(receipt["command"]).replace("\\", "/")
    output = store.text(receipt["output"]).replace("\\", "/")
    require(all(row["path"] in command or row["path"] in output
                for row in latest["report"]["newTestFiles"]),
            "Incremental RED: GREEN должен выполнять все текущие объявленные источники")


def validate(store, state, task_id, *, require_green, allow_source_changes=False, historical=False):
    """Historical validation preserves evidence; live validation checks every SHA."""
    require(state.get("policy", {}).get("incrementalTestIntent") is True,
            "Incremental RED: адресная схема требует своей закреплённой политики")
    old = _legacy()
    task, attempt = old._attempt(state, task_id)
    _journal_view, events = _journal(store, state, task_id)
    _epochs(store, state, task_id, attempt, current=not historical)
    contracts, _sequences = _history(store, state, task_id, task, attempt, events)
    latest = contracts[-1]
    require(latest["taskContract"] == task_contract(state, task_id)
            and latest["report"]["criteria"] == task["criteria"],
            "Incremental RED: текущий контракт задачи изменён")
    if not historical:
        # A repaired schema-two contract also binds current hashes. The legacy
        # allow_source_changes option cannot silently waive these new bindings.
        _shape(store, state, task, attempt, latest["report"], current=True)
    if require_green:
        _green(store, state, task_id, task, attempt, latest, events)
    return True


def chain_matches(store, attempt, submitted, rounds, repair):
    """Original submission/critic1 and revised repair/critic2 retain their refs."""
    try:
        refs = attempt.get("testIntentHistory", [])
        latest = attempt.get("testIntent")
        original = submitted.get("testIntent") if submitted else None
        if not refs or latest != refs[-1] or original not in refs or not rounds:
            return False
        if rounds[0].get("testIntent") != original:
            return False
        original_contract, latest_contract = _legacy()._saved(store, original), _legacy()._saved(store, latest)
        if original_contract.get("schema") != SCHEMA or latest_contract.get("schema") != SCHEMA:
            return False
        if any(original_contract.get(key) != latest_contract.get(key) for key in ("run", "task", "attempt")):
            return False
        if original == latest:
            return all(record.get("testIntent") == latest
                       for record in [submitted, *rounds, *([repair] if repair else [])])
        return bool(rounds[0].get("passed") is False and repair
                    and repair.get("testIntent") == latest
                    and all(record.get("testIntent") == latest for record in rounds[1:]))
    except (Invalid, OSError, ValueError, TypeError, KeyError, IndexError):
        return False
