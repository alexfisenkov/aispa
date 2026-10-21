"""Read-only projection. A view never decides whether a requirement is accepted."""
from .contracts import basis, gate_valid, task_basis, verified
from .store import Invalid, atomic_json, timestamp
from .progress import display_stage, estimate
from .timing import TASK_PHASES, elapsed, phase_view
from .team import _record_valid, task_proof_valid
from . import carryover, review_policy, execution_obligations, phase_receipts, task_tiers

PHASES = {"implementation": "Реализация", "critic_1": "Критик · круг 1", "repair": "Исправление",
          "critic_2": "Критик · круг 2", "task_acceptance": "Приёмщик задачи",
          "verified_completion": "Выполнение проверено"}

LABELS = {"running": "В работе", "paused": "Приостановлено", "blocked": "Есть препятствие",
          "cancelled": "Отменено", "accepted": "Все действующие требования приняты",
          "delivered_with_remaining": "Основная часть готова, есть остатки"}

EVENT_LABELS = {
    "task-verified-completed": "Выполнение лёгкой задачи проверено",
    "task-promoted": "Задача переведена в тяжёлый класс",
    "project-integration-reviewed": "Сохранено общее ревью проекта",
    "brief-added": "Бриф сохранён", "brief-sealed": "Бриф зафиксирован",
    "plan-validated": "План проверен", "task-started": "Началась задача",
    "task-submitted": "Задача передана критикам", "task-repaired": "Исправления переданы на проверку",
    "wave-reviewed": "Весь бриф сверен после волны", "acceptance-reviewed": "Проверена итоговая приёмка",
    "delivery-recorded": "Результат сдан", "owner-answer-awaited": "Ожидается ответ владельца",
    "owner-answer-received": "Ответ владельца получен", "run-paused": "Работа приостановлена",
    "run-resumed": "Работа продолжена", "recovery-reconciled": "Восстановление журнала проверено",
}


def _events(store):
    rows = store.read_journal().get("eventTail", [])
    result = []
    for row in reversed(rows):
        name, _, target = row["event"].partition(":")
        if name not in EVENT_LABELS:
            continue
        # Only identifiers, not arbitrary note/brief content, enter the feed.
        suffix = " · " + target if target and len(target) <= 32 and target.replace("-", "").isalnum() else ""
        result.append({"type": name, "at": row["at"], "text": EVENT_LABELS[name] + suffix})
        if len(result) == 12:
            break
    return list(reversed(result))

def _record_intact(store, record):
    try:
        return isinstance(record, dict) and _record_valid(store, record)
    except (Invalid, KeyError, OSError, TypeError, ValueError):
        return False


def _phase_steps(store, state, task, proof_current):
    """Show receipts from this attempt; the order alone never implies PASS."""
    attempt = (state.get("taskHistory", {}).get(task["id"]) or [None])[-1]
    phase = task.get("phase")
    if attempt and task_tiers.is_light(attempt):
        completed = attempt.get("verifiedCompletion")
        status = ("done" if proof_current else "stale") if completed else ("current" if task["status"] == "running" else "pending")
        if task["status"] == "blocked":
            status = "blocked"
        return [{"id": "implementation", "label": PHASES["implementation"], "status": status},
                {"id": "verified_completion", "label": PHASES["verified_completion"], "status": status if completed else "pending"},
                *[{"id": name, "label": PHASES[name], "status": "skipped"} for name in TASK_PHASES if name != "implementation"]]
    result = {name: "pending" for name in TASK_PHASES}
    if task["status"] != "pending" and attempt:
        submitted = attempt.get("submitted")
        rounds = attempt.get("rounds", [])
        first = rounds[0] if rounds else None
        second = rounds[1] if len(rounds) > 1 else None
        repair = attempt.get("repair")
        acceptance = attempt.get("acceptance")
        receipts = {"implementation": submitted, "critic_1": first,
                    "repair": repair, "critic_2": second, "task_acceptance": acceptance}
        for name, record in receipts.items():
            if record:
                result[name] = ("done" if record.get("passed") else "blocked") if _record_intact(store, record) else "stale"
        if first and first.get("passed") and _record_intact(store, first) and not repair:
            result["repair"] = "skipped"
            if review_policy.adaptive(attempt) and not second:
                result["critic_2"] = "skipped"
        latest = acceptance or second or repair or first or submitted
        # A newer repair legitimately has a different basis from critic 1.
        # Compare only the latest candidate with today's code.
        basis_changed = bool(latest and latest.get("basis") != task_basis(store, state, task["id"]))
        repairing = (task["status"] == "running" and phase == "repair" and first
                     and not first.get("passed") and not repair)
        stale_candidate = any(record and not _record_intact(store, record) for record in receipts.values())
        stale_candidate = stale_candidate or (basis_changed and not repairing)
        if stale_candidate:
            for name in TASK_PHASES:
                if result[name] in ("done", "skipped"):
                    result[name] = "stale"
        if task["status"] == "completed" and not proof_current:
            for name in TASK_PHASES:
                if result[name] in ("done", "skipped"):
                    result[name] = "stale"
        if task["status"] == "running" and phase in result:
            if stale_candidate:
                result[phase] = "stale"
            elif result[phase] == "pending":
                result[phase] = "current"
        elif task["status"] == "blocked" and phase in result and result[phase] in ("pending", "stale"):
            result[phase] = "blocked"
    elif task["status"] == "blocked" and phase in result:
        result[phase] = "blocked"
    return [{"id": name, "label": PHASES[name], "status": result[name]} for name in TASK_PHASES]


def project(store, state):
    observed = timestamp()
    gates = {name: gate_valid(store, state, name) for name in ("coverage", "plan", "acceptance")}
    current = {t["id"]: task_proof_valid(store, state, t["id"]) for t in state["tasks"] if t["status"] == "completed"}
    if not all(current.values()):
        gates["acceptance"] = False
    status = state["status"]
    if status in ("accepted", "delivered_with_remaining") and not gates["acceptance"]:
        status = "stale"
    rows = []
    for req in state["requirements"]:
        holders = [t for t in state["tasks"] if req["id"] in t["requirements"]]
        implementation = "unplanned" if not holders else (
            "implemented" if all(t["status"] == "completed" for t in holders) else "unfinished")
        if implementation == "implemented" and not all(current.get(t["id"]) for t in holders):
            implementation = "needs_check"
        rows.append({"id": req["id"], "text": req["text"], "core": req["id"] in state.get("core", []),
                     "kind": req["kind"], "scope": req["scope"], "implementation": implementation,
                     **{k: req[k] for k in ("parent", "decision") if k in req}})
    assessment = state["gates"].get("acceptance", {})
    current_assessment = bool(assessment and assessment.get("basis") == basis(store, state, "acceptance"))
    remaining = {
        "checks": assessment.get("failures", []) if current_assessment else [],
        "blockedTasks": [t["id"] for t in state["tasks"] if t["status"] == "blocked"],
        "excluded": [r["id"] for r in state["requirements"] if r["scope"] != "active"],
        "unmapped": assessment.get("unmapped", []) if current_assessment else [],
        "inherited": [{"id": item["id"], "status": item["status"], "text": item["requirement"]["text"]}
                      for item in carryover.unresolved(store, state)],
    }
    execution = execution_obligations.view(store, state)
    results = execution["results"] if state.get("finishedAt") else [
        {"id": row["id"], "event": row["event"], "deadline": row["deadline"],
         "status": "pending", "note": "Execution event has not been attested"}
        for row in execution["obligations"]]
    remaining["execution"] = [row for row in results if row["status"] != "passed"]
    remaining["inheritedExecution"] = carryover.unresolved_execution(store, state)
    tasks = []
    for task in state["tasks"]:
        steps = _phase_steps(store, state, task, current.get(task["id"], False))
        phase_stale = any(step["status"] == "stale" for step in steps)
        attempt = (state.get("taskHistory", {}).get(task["id"]) or [{}])[-1]
        light = task_tiers.is_light(attempt)
        tasks.append(dict(task, tier=attempt.get("tier", "heavy"), tierReason=attempt.get("tierReason", ""),
                          plannedTier=task.get("tier", "heavy"),
                          completionKind="verified_completion" if attempt.get("verifiedCompletion") else ("task_acceptance" if attempt.get("acceptance") else None),
                          perTaskReview="skipped_light" if light else "required",
                          elapsedSeconds=elapsed(task, observed),
                          phaseLabel=PHASES.get(task.get("phase"), "Реализация"),
                          phaseTiming=None if phase_stale else phase_view(task, observed),
                          phaseSteps=steps, phaseStale=phase_stale,
                          proofCurrent=current.get(task["id"], False),
                          maximumCriticRounds=0 if light else 2,
                          reviewPolicy=review_policy.mode((state.get("taskHistory", {}).get(task["id"]) or [{}])[-1]),
                          criticRounds=len((state.get("taskHistory", {}).get(task["id"]) or [{}])[-1].get("rounds", []))))
    estimate_tasks = [dict(t, status="pending") if t["status"] == "completed" and not current.get(t["id"]) else t
                      for t in tasks]
    return {"version": state["version"], "title": state["title"], "run": state["run"],
            "status": status, "label": LABELS.get(status, "Проект изменён; приёмка устарела"),
            "stage": state["stage"], "displayStage": display_stage(state, gates),
            "updatedAt": state["updatedAt"], "observedAt": observed, "gates": gates,
            "verified": verified(store, state), "requirements": rows, "tasks": tasks,
            "remaining": remaining, "remainingAssessed": current_assessment, "decisions": state["notes"],
            "executionPolicy": execution_obligations.enabled(state),
            "phaseAuthorship": phase_receipts.summary(store, state),
            "elapsedSeconds": elapsed(state, observed), "finishedAt": state.get("finishedAt"),
            "estimate": estimate(estimate_tasks, status, observed), "activity": state.get("activity"),
            "waiting": state.get("waiting"), "events": _events(store),
            "runDirectory": "runs/" + state["run"]}


def publish(store, state):
    view = project(store, state)
    atomic_json(store.home / "status.json", view)
    return view
