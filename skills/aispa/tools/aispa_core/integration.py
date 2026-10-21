"""Integrated diff review bound to the final task's frozen review route.

The host chooses a final task before native roles are prepared and keeps the
ordinary critic / separate-acceptor budget for a heavy final task. A light final
task needs a separate independent project review after all task proofs exist.
This module validates complete code-tree coverage, immutable source baseline,
and candidate-specific review.
It does not run a model or assert that a critic's judgment is true.
"""
import copy
import json
import os
import re

from .contracts import artifact_valid, nonempty, task_basis, task_closure
from .portable import match_name
from .store import Invalid, canonical_zone, confined, digest, file_hash, timestamp


SCHEMA = 1
AXES = ("behavior", "integration", "maintainability", "data_security", "test_quality")
SHA = re.compile(r"[0-9a-f]{64}\Z")
MAX_REPORT_BYTES = 1024 * 1024


def _need(ok, message):
    if not ok:
        raise Invalid("AISpa integration: " + message)


def _code(records):
    return digest(records)


def _records_map(records):
    _need(isinstance(records, list), "нужен список файлов проекта")
    seen = {}
    for item in records:
        _need(isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)
              and item[0] not in seen and isinstance(item[1], dict), "повреждён снимок файлов проекта")
        value = item[1]
        file_value = (set(value) == {"sha256", "executable"}
                      and isinstance(value.get("sha256"), str) and SHA.fullmatch(value["sha256"])
                      and type(value.get("executable")) is bool)
        link_value = set(value) == {"link"} and isinstance(value.get("link"), str)
        missing_value = value == {"missing": True}
        _need(file_value or link_value or missing_value, "повреждён тип файла проекта")
        seen[item[0]] = value
    _need(records == [[name, seen[name]] for name in sorted(seen, key=os.fsencode)],
          "изменён порядок файлов снимка")
    return seen


def _mode(value):
    if value is None:
        return None
    if "sha256" in value:
        return "100755" if value["executable"] else "100644"
    return "120000" if "link" in value else "missing"


def capture(store, state):
    """Capture once at init; the journal event remains the host's responsibility."""
    if state.get("integrationBaseline"):
        _baseline(store, state)
        return copy.deepcopy(state["integrationBaseline"])
    records = store.code_records()
    _records_map(records)
    try:
        head = store.git("rev-parse", "--verify", "HEAD").decode().strip()
        _need(re.fullmatch(r"[0-9a-f]{40,64}", head), "некорректный Git HEAD")
    except Invalid:
        head = None
    payload = {"schema": SCHEMA, "run": state["run"], "head": head,
               "records": records, "code": _code(records)}
    ref = store.artifact("integration-baselines",
                         (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(), ".json")
    state["integrationBaseline"] = ref
    return copy.deepcopy(ref)


def _baseline(store, state):
    ref = state.get("integrationBaseline")
    _need(isinstance(ref, dict) and isinstance(ref.get("file"), str)
          and ref["file"].startswith("integration-baselines/")
          and artifact_valid(store, ref), "исходный снимок проекта отсутствует или изменён")
    payload = json.loads(store.text(ref))
    _need(isinstance(payload, dict) and set(payload) == {"schema", "run", "head", "records", "code"}
          and payload["schema"] == SCHEMA and payload["run"] == state["run"],
          "исходный снимок относится к другому прогону")
    _records_map(payload["records"])
    _need(payload["code"] == _code(payload["records"]), "изменён digest исходного кода")
    _need(payload["head"] is None or isinstance(payload["head"], str)
          and re.fullmatch(r"[0-9a-f]{40,64}", payload["head"]), "некорректный Git HEAD снимка")
    return payload


def _covers(zone, path):
    zone, path = canonical_zone(zone), match_name(path)
    return zone in ("", ".") or path == zone or path.startswith(zone + "/")


def choose_task(store, state, tasks):
    """Return the sole final task that can review every dependency and file."""
    _baseline(store, state)
    _need(isinstance(tasks, list) and tasks, "для итогового ревью нужен непустой план")
    ids = {t["id"] for t in tasks}
    _need(len(ids) == len(tasks), "повторный ID задачи")
    graph = dict(state, tasks=tasks)
    files = {name for name, _ in store.code_records()}
    candidates = []
    for task in tasks:
        closure = {item["id"] for item in task_closure(graph, task["id"])}
        if closure != ids:
            continue
        zones = task["zones"] + task.get("readZones", [])
        if all(any(_covers(zone, path) for zone in zones) for path in files):
            candidates.append(task["id"])
    _need(len(candidates) == 1,
          "нужна одна финальная задача с зависимостями от всех задач и обзором всего дерева кода")
    return candidates[0]


def _changes(baseline_records, current_records):
    before = _records_map(baseline_records)
    after = _records_map(current_records)
    result = []
    for name in sorted(before.keys() | after.keys(), key=os.fsencode):
        prior, present = before.get(name), after.get(name)
        if prior != present:
            result.append({"path": name, "before": prior, "after": present,
                           "beforeMode": _mode(prior), "afterMode": _mode(present)})
    return result


def _review_basis(ref, tid, candidate_basis, code, changes):
    return digest({"baseline": ref, "task": tid, "candidate": candidate_basis,
                   "code": code, "changes": changes})


def context(store, state, tid=None):
    """Bounded changed rows, including staged and untracked files, never bytes."""
    baseline = _baseline(store, state)
    records = store.code_records()
    changes = _changes(baseline["records"], records)
    code = _code(records)
    ref = copy.deepcopy(state["integrationBaseline"])
    tid = tid or state.get("integrationTask")
    candidate = task_basis(store, state, tid) if tid else None
    return {"baseline": ref, "baselineHead": baseline["head"],
            "baselineCode": baseline["code"], "code": code,
            "changes": changes, "files": [row["path"] for row in changes],
            "task": tid, "candidateBasis": candidate,
            "basis": _review_basis(ref, tid, candidate, code, changes)}


def _evidence(store, name):
    _need(isinstance(name, str) and name.startswith("evidence/"),
          "доказательство должно лежать в evidence/ прогона")
    path = confined(store.run, name)
    _need(path.is_file() and path.stat().st_size > 0, "доказательство отсутствует или пусто")
    return {"file": name, "sha256": file_hash(path)}


def _review_fields(store, branch):
    _need(isinstance(branch, dict) and set(branch) ==
          {"basis", "code", "files", "changes", "axes", "note", "evidence"},
          "branchReview требует basis, code, files, changes, axes, note, evidence")
    _need(nonempty(branch["note"]) and len(branch["note"]) <= 3000,
          "нужно заключение по всей ветке")
    _need(isinstance(branch["axes"], list) and len(branch["axes"]) == len(AXES),
          "нужны пять направлений общего ревью")
    artifacts, failures, seen = [_evidence(store, branch["evidence"])], [], set()
    for row in branch["axes"]:
        _need(isinstance(row, dict) and set(row) == {"axis", "status", "note", "evidence"},
              "направлению ревью нужны status, note и evidence")
        axis = row["axis"]
        _need(isinstance(axis, str) and axis in AXES and axis not in seen,
              "неизвестное или повторное направление ревью")
        seen.add(axis)
        _need(row["status"] in ("passed", "failed") and nonempty(row["note"])
              and len(row["note"]) <= 2000, "результат направления ревью не объяснён")
        artifacts.append(_evidence(store, row["evidence"]))
        if row["status"] == "failed":
            failures.append({"axis": axis, "status": "failed", "note": row["note"]})
    _need(seen == set(AXES), "не все направления общего ревью рассмотрены")
    return list({ref["file"]: ref for ref in artifacts}.values()), failures


def check(store, state, tid, role, report):
    """Return artifact refs and failed axes to the normal critic budget."""
    selected = state.get("integrationTask")
    if tid != selected:
        _need("branchReview" not in report, "branchReview допустим только для итоговой задачи")
        return [], []
    _need(role in ("critic1", "critic2"), "общее ревью делает штатный критик выполняемого раунда")
    _need(choose_task(store, state, state["tasks"]) == tid, "итоговая задача больше не охватывает план")
    view = context(store, state, tid)
    branch = report.get("branchReview")
    artifacts, failures = _review_fields(store, branch)
    _need(report.get("basis") == view["candidateBasis"]
          and branch["basis"] == view["basis"]
          and branch["code"] == view["code"]
          and branch["files"] == view["files"]
          and branch["changes"] == view["changes"],
          "общее ревью не соответствует текущему полному коду и списку изменений")
    _need(len(json.dumps(report, ensure_ascii=False)) <= MAX_REPORT_BYTES,
          "слишком большой отчёт общего ревью")
    return artifacts, failures


def _reconstructed_code(records, changes):
    values = dict(_records_map(records))
    _need(isinstance(changes, list), "нет изменений проверенной ветки")
    seen = set()
    for row in changes:
        _need(isinstance(row, dict) and set(row) ==
              {"path", "before", "after", "beforeMode", "afterMode"},
              "повреждена строка исторического diff")
        name = row["path"]
        _need(isinstance(name, str) and name not in seen and row["before"] == values.get(name)
              and row["before"] != row["after"] and row["beforeMode"] == _mode(row["before"])
              and row["afterMode"] == _mode(row["after"]),
              "исторический diff расходится с исходным снимком")
        seen.add(name)
        if row["after"] is None:
            values.pop(name, None)
        else:
            _records_map([[name, row["after"]]])
            values[name] = row["after"]
    _need(changes == sorted(changes, key=lambda row: os.fsencode(row["path"])),
          "исторический diff переставлен")
    return _code([[name, values[name]] for name in sorted(values, key=os.fsencode)])


def record_valid(store, state, tid, role, record):
    """Validate a sealed critic candidate without demanding today's code."""
    if tid != state.get("integrationTask") or role not in ("critic1", "critic2"):
        return True
    try:
        baseline = _baseline(store, state)
        _need(isinstance(record, dict) and artifact_valid(store, record.get("report", {})),
              "критический отчёт изменён")
        report = json.loads(store.text(record["report"]))
        _need(isinstance(report, dict) and report.get("basis") == record.get("basis"),
              "исторический отчёт относится к другому кандидату")
        branch = report.get("branchReview")
        artifacts, _failures = _review_fields(store, branch)
        _need(all(artifact_valid(store, ref) for ref in artifacts), "доказательство общего ревью изменено")
        code = _reconstructed_code(baseline["records"], branch["changes"])
        _need(branch["code"] == code
              and branch["files"] == [row["path"] for row in branch["changes"]]
              and branch["basis"] == _review_basis(state["integrationBaseline"], tid,
                                                  record["basis"], code, branch["changes"]),
              "сохранённое общее ревью потеряло исходный кандидат")
        return True
    except (Invalid, OSError, ValueError, KeyError, TypeError, RecursionError):
        return False


def _project_actor(store, state, actor=None, *, optional=False):
    """Resolve a host-declared identity, never infer independence from a name."""
    from . import participants
    if actor is None:
        choices = [row["id"] for row in participants.list_participants(store, state)
                   if row.get("role") == "integration-reviewer"]
        if optional and not choices:
            return None
        _need(len(choices) == 1, "нужен actor единственного независимого integration-reviewer")
        actor = choices[0]
    who = participants.resolve(store, state, actor)
    _need(who and actor in state.get("participants", {})
          and who.get("role") == "integration-reviewer" and who.get("fresh") is True
          and who.get("external") is False and who.get("run") == state.get("run"),
          "нужен зарегистрированный свежий integration-reviewer")
    participants.require_unused_identity(state, who["runtime"], who["nativeId"], exclude_actor=actor)
    # Keep independence even when an invalid state dropped a historical actor
    # registry entry but retained its task binding, or later reused the reviewer.
    used = set(state.get("actors", {}))
    for history in state.get("taskHistory", {}).values():
        for attempt in history:
            used.update(attempt.get("actors", {}).values())
            used.update(attempt.get("writers", []))
            for replacement in attempt.get("actorHistory", []):
                used.update(replacement.get(key) for key in ("previous", "replacement"))
    blind_rows = [state.get("blindReview"), *state.get("blindHistory", [])]
    for row in blind_rows:
        if isinstance(row, dict):
            used.update(row.get(key) for key in ("reviewer", "actor"))
    for row in state.get("phaseReceipts", []):
        if isinstance(row, dict) and row.get("phase") in ("blind", "acceptance"):
            used.add(row.get("actor"))
    acceptance = state.get("gates", {}).get("acceptance") or {}
    if acceptance.get("report"):
        _need(artifact_valid(store, acceptance["report"]), "итоговый отчёт изменён")
        final_report = json.loads(store.text(acceptance["report"]))
        _need(isinstance(final_report, dict), "повреждён итоговый отчёт")
        used.update(final_report.get(key) for key in ("reviewer", "actor"))
    _need(actor not in used and who["nativeId"] not in used,
          "integration-reviewer должен отличаться от исполнителей и итогового приёмщика")
    return who


def _project_ready(store, state):
    from .task_tiers import is_light
    from .team import direct_proof_current, task_proof_valid
    tid = state.get("integrationTask")
    _need(choose_task(store, state, state["tasks"]) == tid, "итоговая задача изменена")
    attempt = (state.get("taskHistory", {}).get(tid) or [{}])[-1]
    _need(is_light(attempt), "отдельное общее ревью предназначено для лёгкой итоговой задачи")
    _need(direct_proof_current(store, state, tid), "нет текущего доказательства итоговой задачи")
    _need(all(task.get("status") == "completed" and task_proof_valid(store, state, task["id"])
              for task in state["tasks"]), "до общего ревью нужны текущие доказательства всех задач")
    return tid, attempt


def project_context(store, state, actor=None):
    """Prepare an unassessed review; this neither runs a model nor awards PASS."""
    tid, _attempt = _project_ready(store, state)
    who = _project_actor(store, state, actor, optional=True)
    view = context(store, state, tid)
    branch = {key: copy.deepcopy(view[key]) for key in ("basis", "code", "files", "changes")}
    branch.update(note="", evidence="", axes=[
        {"axis": axis, "status": "unassessed", "note": "", "evidence": ""} for axis in AXES])
    return {**view, "actor": who["id"] if who else None,
            "routing": {"tier": "strong", "advisory": True,
                        "reason": "Independent review of the complete integrated project",
                        "selection": "Use only models exposed by the host and permitted by project rules; otherwise keep the current model",
                        "identityEvidence": "host-declared; model and fresh context are not authenticated"},
            "template": {"basis": view["candidateBasis"], "reviewer": who["id"] if who else None,
                         "independent": True, "verdict": "UNASSESSED", "branchReview": branch}}


def _project_report(store, report):
    _need(isinstance(report, dict) and set(report) ==
          {"basis", "reviewer", "independent", "verdict", "branchReview"},
          "общему отчёту нужны basis, reviewer, independent, verdict, branchReview")
    _need(report["independent"] is True and report["verdict"] in ("PASS", "FAIL"),
          "нужны независимое ревью и явный verdict PASS или FAIL")
    _need(len(json.dumps(report, ensure_ascii=False).encode("utf-8")) <= MAX_REPORT_BYTES,
          "слишком большой отчёт общего ревью")
    artifacts, failures = _review_fields(store, report["branchReview"])
    _need((report["verdict"] == "PASS") == (not failures),
          "verdict должен соответствовать пяти направлениям общего ревью")
    return artifacts, failures


def _project_history(store, state):
    """Check historical candidates against their own baseline and sealed bytes."""
    from .team import _sealed_valid
    history = state.get("projectIntegrationReviewHistory", [])
    _need(isinstance(history, list), "повреждена история общего ревью")
    current = state.get("projectIntegrationReview")
    _need((not history and current is None) or (history and current == history[-1]),
          "история общего ревью потеряла последнюю запись")
    previous, bases, actors = None, set(), set()
    baseline = _baseline(store, state)
    for record in history:
        _need(_sealed_valid(store, record) and record.get("schema") == SCHEMA
              and record.get("run") == state["run"] and record.get("previous") == previous
              and artifact_valid(store, record.get("report", {})),
              "повреждена неизменная запись общего ревью")
        who = _project_actor(store, state, record.get("actor"))
        _need(record.get("participant") == who["record"], "изменена личность общего проверяющего")
        report = json.loads(store.text(record["report"]))
        artifacts, failures = _project_report(store, report)
        branch = report["branchReview"]
        code = _reconstructed_code(baseline["records"], branch["changes"])
        _need(report["basis"] == record.get("basis") and report["reviewer"] == record["actor"]
              and record.get("baseline") == state["integrationBaseline"]
              and branch["code"] == record.get("code") == code
              and branch["files"] == [row["path"] for row in branch["changes"]]
              and branch["basis"] == record.get("integrationBasis") == _review_basis(
                  state["integrationBaseline"], record.get("task"), report["basis"], code, branch["changes"])
              and artifacts == record.get("artifacts") and failures == record.get("failures")
              and all(artifact_valid(store, ref) for ref in record["artifacts"])
              and report["verdict"] == record.get("verdict")
              and record.get("passed") is (report["verdict"] == "PASS"),
              "сохранённое общее ревью потеряло кандидата или доказательства")
        _need(branch["basis"] not in bases and record["actor"] not in actors,
              "повторный кандидат или использованный integration-reviewer")
        bases.add(branch["basis"])
        actors.add(record["actor"])
        previous = record["record"]["sha256"]
    return history


def project_review(store, state, report, actor=None):
    """Seal PASS/FAIL after task completion; the caller journals the mutation."""
    from .team import _seal
    _need(state.get("status") == "running", "общее ревью записывается только в активном прогоне")
    tid, attempt = _project_ready(store, state)
    _need(isinstance(report, dict), "общий отчёт должен быть объектом JSON")
    who = _project_actor(store, state, actor if actor is not None else report.get("reviewer"))
    _need(report.get("reviewer") == who["id"], "автор отчёта отличается от integration-reviewer")
    artifacts, failures = _project_report(store, report)
    view = context(store, state, tid)
    branch = report["branchReview"]
    _need(report["basis"] == view["candidateBasis"]
          and all(branch[key] == view[key] for key in ("basis", "code", "files", "changes")),
          "общее ревью не соответствует текущему полному коду и списку изменений")
    history = _project_history(store, state)
    _need(view["basis"] not in {row["integrationBasis"] for row in history},
          "общее ревью этого кандидата уже записано; заменять его вердикт нельзя")
    _need(who["id"] not in {row["actor"] for row in history},
          "новому кандидату нужен новый integration-reviewer")
    ref = store.artifact("project-integration-reviews", (json.dumps(report, ensure_ascii=False,
                         sort_keys=True, indent=2) + "\n").encode(), ".json")
    record = _seal(store, {"schema": SCHEMA, "run": state["run"], "task": tid,
        "attempt": attempt["id"], "actor": who["id"], "participant": copy.deepcopy(who["record"]),
        "basis": view["candidateBasis"], "integrationBasis": view["basis"], "code": view["code"],
        "baseline": copy.deepcopy(state["integrationBaseline"]), "report": ref,
        "artifacts": artifacts, "failures": failures, "verdict": report["verdict"],
        "passed": report["verdict"] == "PASS", "at": timestamp(),
        "previous": history[-1]["record"]["sha256"] if history else None})
    state["projectIntegrationReviewHistory"] = copy.deepcopy([*history, record])
    state["projectIntegrationReview"] = record
    return copy.deepcopy(record)


def _project_proof(store, state, tid, attempt):
    _project_ready(store, state)
    history = _project_history(store, state)
    _need(history, "нет независимого общего ревью лёгкой итоговой задачи")
    record = history[-1]
    view = context(store, state, tid)
    _need(record["passed"] is True and record["verdict"] == "PASS"
          and record["task"] == tid and record["attempt"] == attempt["id"]
          and record["basis"] == view["candidateBasis"] and record["code"] == view["code"]
          and record["integrationBasis"] == view["basis"],
          "нет PASS общего ревью текущего полного кода")
    return True


def proof_valid(store, state):
    """Final task must still prove the current full code and complete closure."""
    try:
        tid = state["integrationTask"]
        _need(choose_task(store, state, state["tasks"]) == tid, "итоговая задача изменена")
        from .team import direct_proof_current, acceptance_chain
        from .task_tiers import is_light
        _need(direct_proof_current(store, state, tid), "нет текущего доказательства итоговой задачи")
        attempt = state["taskHistory"][tid][-1]
        if is_light(attempt):
            return _project_proof(store, state, tid, attempt)
        final = acceptance_chain(store, state, tid, attempt)
        view = context(store, state, tid)
        branch = json.loads(store.text(final["report"]))["branchReview"]
        _need(branch["code"] == view["code"] and branch["basis"] == view["basis"],
              "после последнего обязательного общего ревью полный код изменён")
        return True
    except (Invalid, OSError, ValueError, KeyError, TypeError, RecursionError):
        return False
