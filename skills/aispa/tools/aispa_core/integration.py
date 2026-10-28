"""Integrated diff review bound to the final task's frozen review route.

The host chooses a final task before native roles are prepared and keeps the
ordinary critic budget (and, outside lean, separate acceptor) for a heavy final
task. A light or standard final task needs a separate independent project review
after all task proofs exist.
This module validates complete code-tree coverage, immutable source baseline,
and candidate-specific review.
It does not run a model or assert that a critic's judgment is true.
"""
from . import scale
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
    payload = store.json(ref)
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
    from .scale.graph import DependencyIndex
    dependency_index = DependencyIndex(tasks, budget=scale.Budget() if scale.enabled(state) else None)
    files = {name for name, _ in store.code_records()}
    candidates = []
    for task in tasks:
        closure = {item["id"] for item in dependency_index.closure(task["id"])}
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


def zone_changes(store, state, zones, *, baseline=None):
    """Changed rows inside the given zones, never bytes.

    ``baseline`` — a snapshot of an attempt or a repair (a team-records style
    reference with ``records``, or the records list itself); without it the
    diff is taken from the run's ``integrationBaseline`` as before.
    """
    if baseline is None:
        records = _baseline(store, state)["records"]
    elif isinstance(baseline, list):
        records = baseline
    else:
        _need(isinstance(baseline, dict) and artifact_valid(store, baseline), "снимок для diff отсутствует или изменён")
        payload = store.json(baseline)
        _need(isinstance(payload, dict) and isinstance(payload.get("records"), list), "повреждён снимок для diff")
        records = payload["records"]
    rows = _changes(records, store.code_records())
    return [row for row in rows if any(_covers(zone, row["path"]) for zone in zones)]


def focused(state):
    return state.get("policy", {}).get("focusedReview") is True


def lenses(state, store=None):
    """Lenses of the branch review: 1, 2 or 3 reviewers.

    The mode is the owner's current one until the first lens of the reviewed
    candidate is recorded; from then on that candidate keeps the lens set of
    the mode its first lens was recorded in, so a later check-mode switch
    neither reopens a closed branch review nor leaves it incomplete. The
    candidate is the chain base after a repair, otherwise the current one
    (with ``store``) or the latest reviewed one.
    """
    from . import check_modes, focused_review
    mode = (_frozen_mode(state, store) if focused(state) else None) or check_modes.current(state)
    return [list(lens) for lens in focused_review.LENSES[mode]]


def _lens_mode(row):
    """Mode a lens record was written in: its own field, or the only mode that has this lens."""
    from . import focused_review
    if row.get("checkMode") in focused_review.LENSES:
        return row["checkMode"]
    value = sorted(row.get("lens") or [])
    return next((mode for mode, rows in focused_review.LENSES.items()
                 if any(sorted(lens) == value for lens in rows)), None)


def _frozen_mode(state, store=None):
    rows = [row for row in state.get("projectIntegrationReviewHistory") or [] if isinstance(row, dict) and row.get("lens")]
    if not rows:
        return None
    chain = state.get("candidateChain") or []
    candidate = rows[-1].get("integrationBasis")
    if chain and isinstance(chain[0], dict):
        candidate = chain[0].get("integrationBasis")
    elif store is not None:
        try:
            candidate = context(store, state)["basis"]
        except (Invalid, OSError, ValueError, KeyError, TypeError):
            pass
    first = next((row for row in rows if row.get("integrationBasis") == candidate), None)
    return _lens_mode(first) if first else None


def _lens(state, value, *, historical=False, store=None):
    """The canonical lens of the candidate's mode; a sealed record may carry a lens of an earlier mode."""
    from . import focused_review
    _need(isinstance(value, list) and value and all(isinstance(axis, str) for axis in value),
          "отчёту проверки ветки нужна линза lens: список направлений")
    allowed = lenses(state, store)
    choices = [list(lens) for rows in focused_review.LENSES.values() for lens in rows] if historical else allowed
    for lens in choices:
        if sorted(lens) == sorted(value):
            return lens
    raise Invalid("AISpa integration: линза не из режима; допустимы: " + "; ".join(
        "+".join(lens) for lens in allowed))


def _branch_findings(report, lens, failures):
    from . import focused_review
    rows = report.get("findings", [])
    _need(isinstance(rows, list), "findings ревьюера ветки должен быть списком")
    seen = set()
    for row in rows:
        _need(isinstance(row, dict) and nonempty(row.get("id")) and row["id"] not in seen
              and row.get("severity") in ("blocking", "warning") and nonempty(row.get("criterion"))
              and nonempty(row.get("text")), "находке ветки нужны id, severity blocking|warning, criterion и text")
        seen.add(row["id"])
        if row["severity"] == "blocking":
            _need(row.get("blockingClass") in focused_review.BLOCKING_CLASSES,
                  "Блокирует только список: " + ", ".join(focused_review.BLOCKING_CLASSES) + "; остальное отметь warning")
    blocking = [row for row in rows if row["severity"] == "blocking"]
    _need(all(any(row["criterion"] == "Вся ветка: " + failed["axis"] for row in blocking) for failed in failures),
          "каждое проваленное направление требует блокирующую находку «Вся ветка: <направление>»")
    _need((report["verdict"] == "PASS") == (not failures and not blocking),
          "verdict PASS только без проваленных направлений и блокирующих находок")
    return rows


def _triage(state, report, lens):
    from . import findings
    rows = report.get("triage")
    if "behavior" not in lens:
        _need(not rows, "разбор накопленных предупреждений делает ревьюер линзы behavior")
        return []
    open_ids = sorted(row["id"] for row in findings.open_items(state))
    _need(isinstance(rows, list) and sorted(row.get("id") for row in rows if isinstance(row, dict)) == open_ids
          and all(row.get("status") in ("confirmed", "dismissed") and nonempty(row.get("note")) for row in rows),
          "ревьюер линзы behavior подтверждает или снимает с причиной каждое открытое предупреждение: "
          + (", ".join(open_ids) or "открытых нет, triage пуст"))
    return rows


def branch_records(store, state):
    """Lens records of the branch-reviewed candidate: the chain base, or the current candidate."""
    history = _project_history(store, state)
    chain = state.get("candidateChain") or []
    candidate = chain[0]["integrationBasis"] if chain else context(store, state)["basis"]
    return [row for row in history if row.get("integrationBasis") == candidate and row.get("lens")]


def lenses_complete(store, state):
    """Old runs: one review is the whole stage. Focused: every lens of the mode is recorded for the candidate."""
    if not focused(state):
        return True
    recorded = {tuple(row["lens"]) for row in branch_records(store, state)}
    return {tuple(lens) for lens in lenses(state, store)} <= recorded


def branch_reviewed(store, state):
    """Gate of the branch review: every lens of the mode recorded for one candidate, no open warning left."""
    from . import findings
    try:
        recorded = {tuple(row["lens"]) for row in branch_records(store, state)}
        return {tuple(lens) for lens in lenses(state, store)} <= recorded and not findings.open_items(state)
    except (Invalid, OSError, ValueError, KeyError, TypeError, RecursionError):
        return False


def _focused_proof(store, state):
    from . import final_repairs
    if not (branch_reviewed(store, state) and final_repairs.findings_settled(state)
            and final_repairs.chain_current(store, state)):
        return False
    if final_repairs.repaired(state):
        return final_repairs.tasks_settled(store, state)
    _project_ready(store, state)
    return True


def _evidence(store, name):
    _need(isinstance(name, str) and name.startswith("evidence/"),
          "доказательство должно лежать в evidence/ прогона")
    path = confined(store.run, name)
    _need(path.is_file() and path.stat().st_size > 0, "доказательство отсутствует или пусто")
    return {"file": name, "sha256": file_hash(path)}


def _review_fields(store, branch, state=None, axes=AXES):
    _need(isinstance(branch, dict) and set(branch) ==
          {"basis", "code", "files", "changes", "axes", "note", "evidence"},
          "branchReview требует basis, code, files, changes, axes, note, evidence")
    _need(nonempty(branch["note"]) and (state is not None and scale.enabled(state) or len(branch["note"]) <= 3000),
          "нужно заключение по всей ветке")
    _need(isinstance(branch["axes"], list) and len(branch["axes"]) == len(axes),
          "нужны пять направлений общего ревью" if len(axes) == len(AXES) else "нужны направления своей линзы")
    artifacts, failures, seen = [_evidence(store, branch["evidence"])], [], set()
    for row in branch["axes"]:
        _need(isinstance(row, dict) and set(row) == {"axis", "status", "note", "evidence"},
              "направлению ревью нужны status, note и evidence")
        axis = row["axis"]
        _need(isinstance(axis, str) and axis in axes and axis not in seen,
              "неизвестное или повторное направление ревью")
        seen.add(axis)
        _need(row["status"] in ("passed", "failed") and nonempty(row["note"])
              and (state is not None and scale.enabled(state) or len(row["note"]) <= 2000), "результат направления ревью не объяснён")
        artifacts.append(_evidence(store, row["evidence"]))
        if row["status"] == "failed":
            failures.append({"axis": axis, "status": "failed", "note": row["note"]})
    _need(seen == set(axes), "не все направления общего ревью рассмотрены")
    return list({ref["file"]: ref for ref in artifacts}.values()), failures


def check(store, state, tid, role, report):
    """Return artifact refs and failed axes to the normal critic budget."""
    if focused(state):
        # Focused: the whole branch is read by the lens reviewers; the final critic reads its attempt.
        return [], []
    selected = state.get("integrationTask")
    if tid != selected:
        _need("branchReview" not in report, "branchReview допустим только для итоговой задачи")
        return [], []
    _need(role in ("critic1", "critic2"), "общее ревью делает штатный критик выполняемого раунда")
    _need(choose_task(store, state, state["tasks"]) == tid, "итоговая задача больше не охватывает план")
    view = context(store, state, tid)
    branch = report.get("branchReview")
    artifacts, failures = _review_fields(store, branch, state)
    _need(report.get("basis") == view["candidateBasis"]
          and branch["basis"] == view["basis"]
          and branch["code"] == view["code"]
          and branch["files"] == view["files"]
          and branch["changes"] == view["changes"],
          "общее ревью не соответствует текущему полному коду и списку изменений")
    scale.check_value(report, state)
    _need(scale.enabled(state) or len(json.dumps(report, ensure_ascii=False)) <= MAX_REPORT_BYTES,
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
    if focused(state) or tid != state.get("integrationTask") or role not in ("critic1", "critic2"):
        return True
    try:
        baseline = _baseline(store, state)
        _need(isinstance(record, dict) and artifact_valid(store, record.get("report", {})),
              "критический отчёт изменён")
        report = store.json(record["report"])
        _need(isinstance(report, dict) and report.get("basis") == record.get("basis"),
              "исторический отчёт относится к другому кандидату")
        branch = report.get("branchReview")
        artifacts, _failures = _review_fields(store, branch, state)
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
        reviewed = {row.get("actor") for row in state.get("projectIntegrationReviewHistory", [])} if focused(state) else set()
        choices = [row["id"] for row in participants.list_participants(store, state)
                   if row.get("role") == "integration-reviewer" and row["id"] not in reviewed]
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
    if focused(state):
        from . import final_repairs
        used |= final_repairs.used_identities(state)
    acceptance = state.get("gates", {}).get("acceptance") or {}
    if acceptance.get("report"):
        _need(artifact_valid(store, acceptance["report"]), "итоговый отчёт изменён")
        final_report = store.json(acceptance["report"])
        _need(isinstance(final_report, dict), "повреждён итоговый отчёт")
        used.update(final_report.get(key) for key in ("reviewer", "actor"))
    _need(actor not in used and who["nativeId"] not in used,
          "integration-reviewer должен отличаться от исполнителей и итогового приёмщика")
    return who


def _project_ready(store, state):
    from .check_modes import is_deferred
    from .task_tiers import worker_only
    from .team import direct_proof_current, task_proof_valid
    tid = state.get("integrationTask")
    _need(choose_task(store, state, state["tasks"]) == tid, "итоговая задача изменена")
    attempt = (state.get("taskHistory", {}).get(tid) or [{}])[-1]
    # Focused: the lens reviewers read the whole branch whatever the weight of the final task.
    _need(focused(state) or worker_only(attempt), "отдельное общее ревью предназначено для итоговой задачи light или standard")
    _need(direct_proof_current(store, state, tid), "нет текущего доказательства итоговой задачи")
    _need(all(is_deferred(state, task, store)
              or task.get("status") == "completed" and task_proof_valid(store, state, task["id"])
              for task in state["tasks"]), "до общего ревью нужны текущие доказательства всех задач")
    return tid, attempt


def project_context(store, state, actor=None):
    """Prepare an unassessed review; this neither runs a model nor awards PASS."""
    tid, _attempt = _project_ready(store, state)
    who = _project_actor(store, state, actor, optional=True)
    view = context(store, state, tid)
    branch = {key: copy.deepcopy(view[key]) for key in ("basis", "code", "files", "changes")}
    axes, extra, template_extra = AXES, {}, {}
    if focused(state):
        from . import findings
        done = [row["lens"] for row in _project_history(store, state) if row.get("integrationBasis") == view["basis"]
                and row.get("lens")]
        allowed = lenses(state, store)
        pending = [lens for lens in allowed if lens not in done]
        lens = pending[0] if pending else None
        axes = lens or []
        extra = {"lenses": allowed, "recordedLenses": done, "pendingLenses": pending,
                 "openFindings": findings.open_items(state)}
        template_extra = {"lens": lens, "findings": []}
        if lens and "behavior" in lens:
            template_extra["triage"] = [{"id": row["id"], "status": "", "note": ""} for row in extra["openFindings"]]
    branch.update(note="", evidence="", axes=[
        {"axis": axis, "status": "unassessed", "note": "", "evidence": ""} for axis in axes])
    return {**view, **extra, "actor": who["id"] if who else None,
            "routing": {"tier": "strong", "advisory": True,
                        "reason": "Independent review of the complete integrated project",
                        "selection": "Use only models exposed by the host and permitted by project rules; otherwise keep the current model",
                        "identityEvidence": "host-declared; model and fresh context are not authenticated"},
            "template": {"basis": view["candidateBasis"], "reviewer": who["id"] if who else None,
                         "independent": True, "verdict": "UNASSESSED", "branchReview": branch, **template_extra}}


BASE_FIELDS = {"basis", "reviewer", "independent", "verdict", "branchReview"}


def _project_report(store, report, state=None, *, historical=False):
    lens_mode = state is not None and focused(state)
    _need(isinstance(report, dict) and (set(report) == BASE_FIELDS if not lens_mode else
          BASE_FIELDS | {"lens"} <= set(report) <= BASE_FIELDS | {"lens", "findings", "triage"}),
          "общему отчёту нужны basis, reviewer, independent, verdict, branchReview"
          + (", lens (findings и triage по необходимости)" if lens_mode else ""))
    _need(report["independent"] is True and report["verdict"] in ("PASS", "FAIL"),
          "нужны независимое ревью и явный verdict PASS или FAIL")
    scale.check_value(report, state or {})
    _need(state is not None and scale.enabled(state) or len(json.dumps(report, ensure_ascii=False).encode("utf-8")) <= MAX_REPORT_BYTES,
          "слишком большой отчёт общего ревью")
    if lens_mode:
        lens = _lens(state, report["lens"], historical=historical, store=None if historical else store)
        artifacts, failures = _review_fields(store, report["branchReview"], state, axes=tuple(lens))
        _branch_findings(report, lens, failures)
        return artifacts, failures
    artifacts, failures = _review_fields(store, report["branchReview"], state)
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
        report = store.json(record["report"])
        artifacts, failures = _project_report(store, report, state, historical=True)
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
        # Focused: one record per candidate and lens; an old-policy record keeps one per candidate.
        key = (branch["basis"], tuple(report["lens"])) if "lens" in report else branch["basis"]
        _need(record.get("lens", None) == report.get("lens", None), "сохранённая линза расходится с отчётом")
        _need(key not in bases and record["actor"] not in actors,
              "повторный кандидат или использованный integration-reviewer")
        bases.add(key)
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
    artifacts, failures = _project_report(store, report, state)
    view = context(store, state, tid)
    branch = report["branchReview"]
    _need(report["basis"] == view["candidateBasis"]
          and all(branch[key] == view[key] for key in ("basis", "code", "files", "changes")),
          "общее ревью не соответствует текущему полному коду и списку изменений")
    history = _project_history(store, state)
    lens, decisions = None, []
    if focused(state):
        _need(not state.get("finalRepairs"), "проверка ветки закрыта: изменения после неё проверяет final-repair")
        lens = _lens(state, report["lens"], store=store)
        _need((view["basis"], tuple(lens)) not in {(row["integrationBasis"], tuple(row.get("lens") or ()))
                                                   for row in history},
              "линза этого кандидата уже записана; заменять её вердикт нельзя")
        decisions = _triage(state, report, lens)
    else:
        _need(view["basis"] not in {row["integrationBasis"] for row in history},
              "общее ревью этого кандидата уже записано; заменять его вердикт нельзя")
    _need(who["id"] not in {row["actor"] for row in history},
          "новому кандидату нужен новый integration-reviewer" if lens is None
          else "каждой линзе нужен свой свежий integration-reviewer")
    ref = store.artifact("project-integration-reviews", (json.dumps(report, ensure_ascii=False,
                         sort_keys=True, indent=2) + "\n").encode(), ".json")
    body = {"schema": SCHEMA, "run": state["run"], "task": tid,
        "attempt": attempt["id"], "actor": who["id"], "participant": copy.deepcopy(who["record"]),
        "basis": view["candidateBasis"], "integrationBasis": view["basis"], "code": view["code"],
        "baseline": copy.deepcopy(state["integrationBaseline"]), "report": ref,
        "artifacts": artifacts, "failures": failures, "verdict": report["verdict"],
        "passed": report["verdict"] == "PASS", "at": timestamp(),
        "previous": history[-1]["record"]["sha256"] if history else None}
    if lens is not None:
        from . import check_modes
        body["lens"] = lens
        # The lens set of this candidate stays the one of this mode (lenses).
        body["checkMode"] = _frozen_mode(state, store) or check_modes.current(state)
    record = _seal(store, body)
    state["projectIntegrationReviewHistory"] = copy.deepcopy([*history, record])
    state["projectIntegrationReview"] = record
    if lens is not None:
        from . import findings
        if decisions:
            findings.triage(state, [{"id": row["id"], "status": row["status"], "note": row["note"]} for row in decisions],
                            who["id"])
        findings.collect_branch(state, {"kind": "branch", "lens": lens,
                                        "report": {"file": ref["file"], "sha256": ref["sha256"]}},
                                report.get("findings", []), who["id"])
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
    """Final task must still prove the current full code and complete closure.

    Focused: every lens of the mode recorded for the reviewed candidate, every
    warning decided, every repair finished and checked up to the current code.
    """
    if focused(state):
        try:
            return _focused_proof(store, state)
        except (Invalid, OSError, ValueError, KeyError, TypeError, RecursionError):
            return False
    try:
        tid = state["integrationTask"]
        _need(choose_task(store, state, state["tasks"]) == tid, "итоговая задача изменена")
        from .team import direct_proof_current, acceptance_chain
        from .task_tiers import worker_only
        _need(direct_proof_current(store, state, tid), "нет текущего доказательства итоговой задачи")
        attempt = state["taskHistory"][tid][-1]
        if worker_only(attempt):
            return _project_proof(store, state, tid, attempt)
        final = acceptance_chain(store, state, tid, attempt)
        view = context(store, state, tid)
        branch = store.json(final["report"])["branchReview"]
        _need(branch["code"] == view["code"] and branch["basis"] == view["basis"],
              "после последнего обязательного общего ревью полный код изменён")
        return True
    except (Invalid, OSError, ValueError, KeyError, TypeError, RecursionError):
        return False
