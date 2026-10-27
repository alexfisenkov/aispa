"""Read-only projection. A view never decides whether a requirement is accepted."""
from datetime import datetime

from .contracts import basis, gate_valid, task_basis, task_contract, verified
from .store import Invalid, atomic_json, timestamp
from .progress import display_stage, estimate
from .timing import TASK_PHASES, elapsed, phase_view
from .team import _record_valid, _actor_valid, _role_data, task_proof_valid, proof_provenance
from . import display_details, display_feed, carryover, check_modes, review_policy, execution_obligations, phase_receipts, task_tiers, work_progress
from . import measured_estimate

PHASES = {"implementation": "Реализация", "critic_1": "Критик · круг 1", "repair": "Исправление",
          "critic_2": "Критик · круг 2", "task_acceptance": "Приёмщик задачи",
          "verified_completion": "Выполнение проверено"}

MODE_LABELS = {"fast": "Быстро", "balanced": "Сбалансированно", "max": "Максимум"}

LABELS = {"running": "В работе", "paused": "Приостановлено", "blocked": "Есть препятствие",
          "cancelled": "Отменено", "accepted": "Все действующие требования приняты",
          "delivered_with_remaining": "Основная часть готова, есть остатки"}


def _evidence_summary(store, state, tid):
    """Expose route types only; procedures, paths and observations stay private."""
    from . import test_intent
    try:
        summary = test_intent.route_summary(store, state, tid)
        if not isinstance(summary, dict) or summary.get("mode") != "mixed":
            return None
        routes = summary.get("routes")
        if not isinstance(routes, list) or not routes:
            return None
        public = []
        for row in routes:
            if (not isinstance(row, dict) or not isinstance(row.get("criterion"), str)
                    or not row["criterion"] or row.get("kind") not in ("automated", "browser", "process")):
                return None
            public.append({"criterion": row["criterion"], "kind": row["kind"]})
        return {"mode": "mixed", "counts": {kind: sum(r["kind"] == kind for r in public)
                for kind in ("automated", "browser", "process")}, "routes": public}
    except (Invalid, KeyError, OSError, TypeError, ValueError):
        return None


def _record_intact(store, record):
    try:
        return isinstance(record, dict) and _record_valid(store, record)
    except (Invalid, KeyError, OSError, TypeError, ValueError):
        return False


PHASE_ROLES = {"implementation": "worker", "critic_1": "critic1", "repair": "worker",
               "critic_2": "critic2", "task_acceptance": "acceptor"}


def _public_actor_text(value):
    return value if isinstance(value, str) and value and len(value) <= 160 and not any(ord(c) < 32 for c in value) else None


def _assignment(store, state, task, phase_stale):
    """Current-role assignment, not proof that a model is executing right now."""
    if task.get("status") == "completed":
        return None
    role = PHASE_ROLES.get(task.get("phase") or "implementation")
    if role is None:
        return None
    try:
        attempt = (state.get("taskHistory", {}).get(task["id"]) or [{}])[-1]
        if not _actor_valid(store, state, attempt, role):
            return None
        _role_data(store, attempt, role)
        if attempt.get("contract") != task_contract(state, task["id"]):
            return None
        actor = state["actors"][attempt["actors"][role]]
        if actor.get("task") != task["id"]:
            return None
        assigned_at = actor.get("at")
        try:
            datetime.fromisoformat(assigned_at)
        except (TypeError, ValueError):
            assigned_at = None
        waiting = state.get("waiting")
        if phase_stale:
            status = "stale"
        elif task.get("status") == "blocked" or state.get("status") == "blocked":
            status = "blocked"
        elif state.get("status") == "paused" or isinstance(waiting, dict) and waiting.get("kind") == "owner_question":
            status = "paused"
        elif state.get("status") != "running" or attempt.get("terminal"):
            status = "inactive"
        elif task.get("status") == "pending":
            status = "assigned"
        elif task.get("status") == "running":
            status = "running"
        else:
            status = "inactive"
        return {"role": role, "roleLabel": display_feed.ROLE_LABELS[role],
                "runtime": _public_actor_text(actor.get("runtime")), "model": _public_actor_text(actor.get("model")),
                **({"reasoning": reasoning} if (reasoning := _public_actor_text(actor.get("requestedReasoning"))) else {}),
                "assignedAt": assigned_at, "status": status}
    except (Invalid, KeyError, OSError, TypeError, ValueError, AttributeError):
        # Legacy records and malformed/missing actor proofs never gain a live badge.
        return None


def _phase_receipt(store, task, record, current_basis):
    if not record:
        return {}
    return {"basis": record.get("basis"), "recordedAt": store.receipt_time(record),
            "basisCurrent": record.get("basis") == current_basis,
            "historical": bool(task["status"] == "completed" and record.get("basis") != current_basis)}


def _phase_steps(store, state, task, proof_current):
    """Show receipts from this attempt; the order alone never implies PASS."""
    attempt = (state.get("taskHistory", {}).get(task["id"]) or [None])[-1]
    phase = task.get("phase")
    if attempt and task_tiers.worker_only(attempt):
        completed = attempt.get("verifiedCompletion")
        status = ("done" if proof_current else "stale") if completed else ("current" if task["status"] == "running" else "pending")
        if task["status"] == "blocked":
            status = "blocked"
        current_basis = task_basis(store, state, task["id"]) if completed or attempt.get("submitted") else None
        return [{"id": "implementation", "label": PHASES["implementation"], "status": status,
                 **_phase_receipt(store, task, attempt.get("submitted"), current_basis)},
                {"id": "verified_completion", "label": PHASES["verified_completion"], "status": status if completed else "pending",
                 **_phase_receipt(store, task, completed, current_basis)},
                *[{"id": name, "label": PHASES[name], "status": "skipped"} for name in TASK_PHASES if name != "implementation"]]
    result = {name: "pending" for name in TASK_PHASES}
    receipts = {}
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
        if review_policy.lean(attempt):
            # The passing critic closes the task; no separate acceptor step exists.
            result["task_acceptance"] = "skipped"
            receipts["task_acceptance"] = None
        latest = acceptance or second or repair or first or submitted
        # A newer repair legitimately has a different basis from critic 1.
        # Compare only the latest candidate with today's code.
        basis_changed = bool(latest and latest.get("basis") != task_basis(store, state, task["id"]))
        repairing = (task["status"] == "running" and phase == "repair" and first
                     and not first.get("passed") and not repair)
        stale_candidate = any(record and not _record_intact(store, record) for record in receipts.values())
        # Intact historical phase bases do not revoke a valid current result
        # established by the exact domain descendant proof.
        historical_result = task["status"] == "completed" and proof_current
        stale_candidate = stale_candidate or (basis_changed and not repairing and not historical_result)
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
    current_basis = task_basis(store, state, task["id"]) if receipts else None
    return [{"id": name, "label": PHASES[name], "status": result[name],
             **_phase_receipt(store, task, receipts.get(name), current_basis)} for name in TASK_PHASES]


def _model_blocks(state):
    """Approved models the host does not offer (modelRoutes): who needs what, for which task, and the options.

    The internal message stays in the journal; the panel builds its own words in both languages.
    """
    rows = state.get("modelBlocks")
    if not isinstance(rows, list):
        return None
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        options = [{"id": o["id"], "text": o["text"]} for o in row.get("options") or []
                   if isinstance(o, dict) and isinstance(o.get("id"), str) and isinstance(o.get("text"), str)]
        result.append({"role": row.get("role"), "roleLabel": display_feed.ROLE_LABELS.get(row.get("role")),
                       "runtime": row.get("runtime"), "model": row.get("model"), "effort": row.get("effort"),
                       "task": row.get("task"), "options": options})
    return result


def project(store, state):
    observed = timestamp()
    gates = {name: gate_valid(store, state, name) for name in ("coverage", "plan", "acceptance")}
    provenance = {t["id"]: proof_provenance(store, state, t["id"])
                  for t in state["tasks"] if t["status"] == "completed"}
    current = {tid: confirmation is not None for tid, confirmation in provenance.items()}
    if not all(current.values()):
        gates["acceptance"] = False
    status = state["status"]
    settled = gates["acceptance"] or (all(current.values()) and gate_valid(store, state, "acceptance", settled=True))
    if status in ("accepted", "delivered_with_remaining") and not settled:
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
        "deferredTasks": sorted(check_modes.deferred_ids(state, store)),
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
    if check_modes.enabled(state):
        remaining["skippedChecks"] = check_modes.skipped_checks(state)
        remaining["checkModeChanges"] = check_modes.history_view(store, state)
    remaining["execution"] = [row for row in results if row["status"] != "passed"]
    remaining["inheritedExecution"] = carryover.unresolved_execution(store, state)
    tasks = []
    for task in state["tasks"]:
        steps = _phase_steps(store, state, task, current.get(task["id"], False))
        phase_stale = any(step["status"] == "stale" for step in steps)
        attempt = (state.get("taskHistory", {}).get(task["id"]) or [{}])[-1]
        light = task_tiers.worker_only(attempt)
        accepted = attempt.get("acceptance") or {}
        tasks.append(dict(task, tier=attempt.get("tier", "heavy"), tierReason=attempt.get("tierReason", ""),
                          plannedTier=attempt.get("plannedTier", task.get("tier", "heavy")),
                          deferred=check_modes.is_deferred(state, task, store),
                          completionKind="verified_completion" if attempt.get("verifiedCompletion") else (accepted.get("completionKind", "task_acceptance") if accepted else None),
                          perTaskReview=("skipped_" + attempt["tier"]) if light else "required",
                          elapsedSeconds=elapsed(task, observed),
                          phaseLabel=PHASES.get(task.get("phase"), "Реализация"),
                          phaseTiming=None if phase_stale else phase_view(task, observed),
                          phaseSteps=steps, phaseStale=phase_stale,
                          assignment=_assignment(store, state, task, phase_stale),
                          proofCurrent=current.get(task["id"], False),
                          proofProvenance=provenance.get(task["id"]),
                          maximumCriticRounds=0 if light else 2,
                          reviewPolicy=review_policy.mode((state.get("taskHistory", {}).get(task["id"]) or [{}])[-1]),
                          criticRounds=len((state.get("taskHistory", {}).get(task["id"]) or [{}])[-1].get("rounds", [])),
                          **display_feed.attempts(state.get("taskHistory", {}).get(task["id"]))))
    estimate_tasks = [dict(t, status="pending") if t["status"] == "completed" and not current.get(t["id"]) else t
                      for t in tasks]
    gate_details, gate_reports = display_details.gates(store, state, gates, current)
    current_assessment = gate_details["acceptance"]["evidenceCurrent"]
    if not current_assessment:
        remaining["checks"], remaining["unmapped"] = [], []
    requirement_progress = display_details.requirements(state, tasks, gate_details, gate_reports)
    for row in rows:
        row["progress"] = requirement_progress[row["id"]]
    for task in tasks:
        task["requirementsDetails"] = [dict(row) for row in rows if row["id"] in task["requirements"]]
        task["criteriaDetails"] = display_details.criteria(store, state, task)
        summary = _evidence_summary(store, state, task["id"])
        if summary is not None:
            task["evidenceSummary"] = summary
    mode = check_modes.current(state)
    stage_now = display_stage(state, gates)
    feed = display_feed.tail(store)
    view = {"version": state["version"], "checkMode": mode, "checkModeLabel": MODE_LABELS[mode], "title": state["title"], "run": state["run"],
            "status": status, "label": LABELS.get(status, "Проект изменён; приёмка устарела"),
            "stage": state["stage"], "displayStage": stage_now,
            "updatedAt": state["updatedAt"], "observedAt": observed, "gates": gates,
            "gateDetails": gate_details, "waveDetails": display_details.waves(store, state, tasks),
            "verified": verified(store, state), "checkModeHistory": check_modes.history_view(store, state), "requirements": rows, "tasks": tasks,
            "remaining": remaining, "remainingAssessed": current_assessment, "decisions": state["notes"],
            "executionPolicy": execution_obligations.enabled(state),
            "phaseAuthorship": phase_receipts.summary(store, state),
            "elapsedSeconds": elapsed(state, observed), "finishedAt": state.get("finishedAt"),
            "estimate": (measured_estimate.for_state(store, state, observed) if measured_estimate.enabled(state)
                         else estimate(estimate_tasks, status, observed, projected=True)),
            "activity": state.get("activity"),
            "waiting": state.get("waiting"), "events": display_feed.events(feed),
            "currentWork": display_feed.current_work(state, stage_now, feed),
            "workProgress": work_progress.view(state, observed),
            "runDirectory": "runs/" + state["run"]}
    from . import autonomy
    autonomy_view = autonomy.view(state)
    if autonomy_view is not None:
        view["autonomy"] = autonomy_view
    if state.get("policy", {}).get("managedExecution"):
        from . import timing, participants
        view["timing"] = timing.summary(state, observed)
        view["interview"] = state.get("interview", autonomy.interview_setting())
        view["hostContext"] = {"sessionKnown": bool(state.get("hostSession")),
            "adoptedPurpose": (state.get("hostPreflight") or {}).get("purpose"),
            "authority": "source-bound host report; purpose-scoped adoption is not full capability approval"}
        view["resourceScope"] = {"scope": "project", "profile": state.get("resourceProfile"),
            "unmanagedLaunches": "outside guarantee", "hostWideLimit": "unknown"}
        usage = display_details.usage_by_participant(state)
        view["models"] = [{**{k: row.get(k) for k in ("id", "role", "requestedModel", "requestedReasoning",
            "actualModel", "actualReasoning", "actualModelStatus", "selection", "routeClass")},
            "roleLabel": display_feed.ROLE_LABELS.get(row.get("role")),
            "reasoningSupport": (row.get("selection") or {}).get("reasoningSupport"),
            "usage": usage.get(row.get("id"))}
            for row in participants.list_participants(store, state)]
    blocks = _model_blocks(state)
    if blocks is not None:
        view["modelBlocks"] = blocks
    from . import findings, focused_review
    if "collectedFindings" in state or focused_review.enabled(state):
        view["findings"] = findings.view(state)
    from . import product_flows
    flow_view = product_flows.view(store, state)
    if flow_view is not None:
        view['productFlows'] = flow_view
    return view


def publish(store, state):
    from .dashboard_reader import SnapshotJournal
    from .store import Invalid, digest
    reader = SnapshotJournal(store.root)
    committed = reader.load()
    if committed["run"] != state["run"] or digest(committed) != digest(state):
        raise Invalid("Dashboard publication state differs from the committed journal")
    with reader._reads.scope():
        view = project(reader, committed)
        reader.finish_snapshot()
    atomic_json(store.home / "status.json", view)
    return view
