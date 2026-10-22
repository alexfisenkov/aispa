"""Editable task-report drafts derived from the existing pipeline contracts.

No report is submitted here. Assessments, evidence and independence must be
supplied by the assigned native actor; the pipeline remains the authority.
"""
import hashlib
import json
import os
import uuid

from . import integration, test_intent, review_policy, task_tiers
from .contracts import require, task_basis
from .store import Invalid, confined
from .team import ROLES, _actor_valid, acceptance_chain

PREPARATION_KINDS = ("brief-extract", "brief-review", "brief-map", "scenarios", "interfaces")


def template(pipeline, tid, role):
    """Populate mechanical references only, at the role's current stage."""
    require(role in ROLES, "Неизвестная роль шаблона")
    store, state = pipeline.store, pipeline.s
    _task, attempt = pipeline._live(tid, allow_blocked=role == "worker")
    require(role in task_tiers.roles(attempt), "Роль не предусмотрена классом задачи и политикой проверки")
    require(_actor_valid(store, state, attempt, role),
            "Сначала назначьте actor с действующей карточкой для " + role)
    rounds = attempt["rounds"]
    candidate = task_basis(store, state, tid)
    report = {
        "basis": candidate, "reviewer": attempt["actors"][role],
        "independent": False, "unmapped": None,
        "checks": [{"requirement": rid, "criterion": criterion,
                    "status": "not_tested", "note": "", "artifact": ""}
                   for rid, criterion in sorted(pipeline.expected_checks(tid, role))],
        "testSourceChanges": test_intent.source_changes(store, state, tid),
    }
    if role != "worker" and review_policy.adaptive(attempt):
        report["verdict"] = None
    if role == "worker":
        require(not rounds or len(rounds) == 1 and not rounds[0]["passed"],
                "Шаблон worker доступен до критики или для исправления первого FAIL")
        if rounds:
            report.update(previousReport=rounds[0]["report"]["sha256"],
                          addresses=sorted(review_policy.required_ids(rounds[0])))
    elif role == "critic1":
        require(not rounds and attempt["submitted"], "Для critic1 нужна сдача исполнителя до первого раунда")
        require(candidate == attempt["submitted"]["basis"], "Код после сдачи изменён; сначала сдайте исполнение заново")
    elif role == "critic2":
        require(len(rounds) == 1, "Для critic2 нужен ровно один завершённый раунд критики")
        require(review_policy.second_allowed(attempt), "После первого PASS второй критик не требуется")
        first, repair = rounds[0], attempt.get("repair")
        if first["passed"]:
            require(repair is None, "После первого PASS исправление не ожидается")
            linked = first
        else:
            require(repair, "После первого FAIL сначала нужен receipt исправления")
            linked = repair
        require(candidate == linked["basis"], "Кандидат изменился после первого раунда или исправления")
        report.update(previousReport=first["report"]["sha256"],
                      repairReceipt=repair["report"]["sha256"] if repair else None,
                      resolutions=[{"id": fid, "status": "not_tested", "note": "", "artifact": ""}
                                   for fid in sorted(review_policy.required_ids(first))])
    else:
        final = acceptance_chain(store, state, tid, attempt)
        require(candidate == final["basis"], "После последнего критика изменён результат")
        report.update(previousReport=final["report"]["sha256"],
                      inheritedProofs=pipeline.inherited_proofs(tid), trace=_trace(pipeline, tid))
    if role.startswith("critic"):
        # None is intentional: an empty list would pre-assess "no findings".
        report["findings"] = None
        if review_policy.lean(attempt):
            # The lean critic also closes the task: same trace as the former acceptor.
            report.update(inheritedProofs=pipeline.inherited_proofs(tid), trace=_trace(pipeline, tid))
        if state.get("policy", {}).get("integrationReview") and tid == state.get("integrationTask"):
            branch = integration.context(store, state, tid)
            report["branchReview"] = {
                **{key: branch[key] for key in ("basis", "code", "files", "changes")},
                "axes": [{"axis": axis, "status": "not_tested", "note": "", "evidence": ""}
                         for axis in integration.AXES],
                "note": "", "evidence": "",
            }
    return report


def _trace(pipeline, tid):
    return [{"requirement": req["id"], "brief": req["brief"], "quote": req["quote"],
             "result": "not_tested", "note": "", "artifact": ""}
            for req in pipeline.trace_requirements(tid)]


def _write_draft(store, report, kind):
    """Create a private editable draft for an allowlisted form, never evidence."""
    require(kind in (*ROLES, *PREPARATION_KINDS), "Неизвестный вид черновика")
    inbox = confined(store.root, ".aispa/inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    # Recheck after mkdir, including .aispa, to reject replaced/link directories.
    inbox = confined(store.root, ".aispa/inbox")
    require(inbox.is_dir(), "Для черновиков нужен обычный каталог .aispa/inbox")
    # Never interpolate a task ID or accept a caller-selected output path.
    relative = ".aispa/inbox/report-" + kind + "-" + uuid.uuid4().hex + ".json"
    destination = confined(store.root, relative)
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        raise Invalid("Файл черновика уже существует; перезапись запрещена") from exc
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    require(confined(store.root, relative).read_bytes() == raw, "Записанный черновик изменился при проверке")
    return {
        "status": "draft-not-evidence",
        "path": relative, "absolutePath": str(destination), "sha256": hashlib.sha256(raw).hexdigest(),
        "note": "Редактируемый черновик; не свидетельство и не принятый отчёт. Заполните оценки, доказательства и независимость.",
    }


def write(store, report, tid, role):
    """Create one task-report draft without journal mutations."""
    require(role in ROLES, "Неизвестная роль шаблона")
    return {
        **_write_draft(store, report, role), "task": tid, "role": role,
        "counts": {"checks": len(report["checks"]), "trace": len(report.get("trace", [])),
                   "resolutions": len(report.get("resolutions", [])),
                   "branchAxes": len(report.get("branchReview", {}).get("axes", []))},
    }


def write_preparation(store, report, kind):
    """Reuse the same file guards for the five existing preparation forms."""
    require(kind in PREPARATION_KINDS, "Неизвестный шаблон подготовки")
    return {
        **_write_draft(store, report, kind), "kind": kind, "basis": report["basis"],
        "counts": {key: len(report[key]) for key in ("units", "rows", "features", "interfaces") if key in report},
    }
