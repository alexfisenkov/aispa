"""Additive dashboard detail, derived from recorded work and checked evidence.

These values explain progress. They never authorize workflow transitions.
Public rows contain labels and identifiers, not private evidence paths.
"""
import json

from . import check_modes
from .contracts import active, artifact_valid, basis, gate_current, plan_basis, task_basis
from .store import Invalid
from .team import _record_valid, proof_provenance


TASK_STATES = ("queued", "implementation", "review", "repair", "blocked", "completed", "stale")
GATE_LABELS = {"pending": "Ожидает проверки", "running": "Проверка выполняется",
               "passed": "Подтверждено", "failed": "Есть замечания", "stale": "Нужна повторная проверка"}


def _count(value):
    return type(value) is int and value >= 0


def usage_by_participant(state):
    """Measured tokens per participant, from `usage-import` rows only.

    A participant with no row, or with any malformed row, is absent: "unknown" is
    never turned into zero or into a guess from file sizes, roles or percentages.
    """
    totals, broken = {}, set()
    for row in state.get("usage") or []:
        actor = row.get("actor") if isinstance(row, dict) else None
        if not isinstance(actor, str) or not actor:
            continue
        if not _count(row.get("inputTokens")) or not _count(row.get("outputTokens")):
            broken.add(actor)
            continue
        entry = totals.setdefault(actor, {"inputTokens": 0, "outputTokens": 0, "measuredCalls": 0})
        entry["inputTokens"] += row["inputTokens"]
        entry["outputTokens"] += row["outputTokens"]
        entry["measuredCalls"] += 1
    return {actor: entry for actor, entry in totals.items() if actor not in broken}


def _read_report(store, ref):
    try:
        if not artifact_valid(store, ref):
            return None
        value = store.json(ref)
        return value if isinstance(value, (dict, list)) else None
    except (Invalid, OSError, ValueError, KeyError, TypeError):
        return None


def _working(state, phase):
    work = state.get("workProgress") or {}
    return bool(state.get("status") == "running" and work.get("phase") == phase
                and not work.get("finishedAt") and work.get("activeSince"))


def task_status(task):
    if task.get("status") == "completed":
        return "completed" if task.get("proofCurrent") else "stale"
    if task.get("phaseStale"):
        return "stale"
    if task.get("status") == "blocked":
        return "blocked"
    if task.get("status") == "pending":
        return "queued"
    if task.get("phase") == "repair":
        return "repair"
    if task.get("phase") in ("critic_1", "critic_2", "task_acceptance"):
        return "review"
    return "implementation"


def counts(tasks):
    result = dict.fromkeys(TASK_STATES, 0)
    for task in tasks:
        result[task_status(task)] += 1
    return result


def aggregate(values):
    total = sum(values.values())
    if not total:
        return "unplanned"
    if values["completed"] == total:
        return "completed"
    for name in ("stale", "blocked", "repair", "review", "implementation"):
        if values[name]:
            return name
    return "partial" if values["completed"] else "queued"


def gates(store, state, proven, current):
    """Separate a missing report, a failed current report, and stale evidence.

    gate_current checks report integrity and freshness while preserving its
    negative outcome. It never authorizes a workflow transition.
    """
    details, reports = {}, {}
    requirements = active(state)
    for name in ("coverage", "plan", "acceptance"):
        gate = state["gates"].get(name)
        total = (sum(len(r.get("acceptance", [])) for r in requirements)
                 if name == "acceptance" else len(requirements))
        valid_basis, fresh, report = None, False, None
        if gate:
            try:
                valid_basis = gate.get("basis") == basis(store, state, name)
                fresh = bool(valid_basis and (proven[name] or gate_current(store, state, name)))
                if name == "acceptance" and not all(current.values()):
                    fresh = False
                report = _read_report(store, gate.get("report", {})) if fresh else None
                fresh = fresh and report is not None
                if fresh and name == 'acceptance' and state.get('policy', {}).get('criticalProductFlows') is True:
                    from . import product_flows
                    # This detached presentation reflects the domain outcome;
                    # the immutable reviewer's original claim stays untouched.
                    vetoes = {(f.get('requirement'), f.get('criterion')): f['status']
                              for f in product_flows.saved_check(store, state)}
                    report['checks'] = [dict(row, status=vetoes.get(
                        (row.get('requirement'), row.get('criterion')), row.get('status')))
                        for row in report.get('checks', [])]
            except (Invalid, OSError, ValueError, KeyError, TypeError):
                fresh = False
            status = ("passed" if gate.get("passed") else "failed") if fresh else "stale"
            reason = "checked_report" if fresh else "basis_changed" if valid_basis is False else "evidence_not_current"
        else:
            status = "running" if _working(state, name) else "pending"
            reason = "recorded_work" if status == "running" else "no_report"
        passed, failed = 0, 0
        if fresh:
            reports[name] = report
            if name == "plan":
                mapped = {rid for task in state["tasks"] for rid in task["requirements"]}
                passed = sum(r["id"] in mapped for r in requirements)
            else:
                rows = report.get("checks", []) if isinstance(report, dict) else []
                expected = ({(r["id"], c) for r in requirements for c in r.get("acceptance", [])}
                            if name == "acceptance" else {(r["id"], None) for r in requirements})
                seen = set()
                for row in rows:
                    key = (row.get("requirement"), row.get("criterion") if name == "acceptance" else None)
                    if key not in expected or key in seen:
                        continue
                    seen.add(key)
                    if row.get("status") == ("covered" if name == "coverage" else "passed"):
                        passed += 1
                    elif row.get("status") != "not_tested":
                        failed += 1
        details[name] = {"status": status, "label": GATE_LABELS[status], "reason": reason,
                         "basisCurrent": valid_basis, "evidenceCurrent": bool(fresh),
                         "completed": passed, "total": total,
                         "counts": {"passed": passed, "failed": failed,
                                    "pending": max(0, total - passed - failed)}}
    return details, reports


def requirements(state, tasks, gate_details, reports):
    result = {}
    acceptance = reports.get("acceptance") or {}
    checks = {(r.get("requirement"), r.get("criterion")): r.get("status")
              for r in acceptance.get("checks", [])} if isinstance(acceptance, dict) else {}
    for req in state["requirements"]:
        holders = [t for t in tasks if req["id"] in t["requirements"]]
        values = counts(holders)
        total = len(req.get("acceptance", []))
        passed = sum(checks.get((req["id"], c)) == "passed" for c in req.get("acceptance", []))
        if req.get("scope", "active") != "active":
            status, accepted = "excluded", "excluded"
        else:
            status = aggregate(values)
            if gate_details["acceptance"]["status"] == "stale":
                accepted = "stale"
            elif gate_details["acceptance"]["evidenceCurrent"]:
                accepted = "passed" if total and passed == total else "failed"
            else:
                accepted = "pending"
        result[req["id"]] = {
            "status": status, "completed": values["completed"], "total": len(holders), "counts": values,
            "taskIds": [t["id"] for t in holders],
            "currentPhases": [{"task": t["id"], "phase": t.get("phase", "implementation"),
                               "label": t.get("phaseLabel", "Реализация")}
                              for t in holders if t["status"] == "running"],
            "acceptance": {"status": accepted, "passed": passed, "total": total},
        }
    return result


def criteria(store, state, task):
    """Only a current report can mark an individual criterion passed/failed."""
    phase = task_status(task)
    default = "stale" if phase == "stale" else "in_progress" if task["status"] == "running" else "pending"
    result = [{"text": text, "status": default, "source": "task_state", "taskStatus": phase}
              for text in task.get("criteria", [])]
    if phase == "stale":
        return result
    if task.get("status") == "completed" and task.get("proofCurrent"):
        confirmation = proof_provenance(store, state, task["id"])
        if confirmation and confirmation["kind"] == "descendant":
            # The shared domain predicate already checked exact ancestor
            # criterion coverage, inherited identity and the complete DAG.
            for row in result:
                row.update(status="passed", source="descendant_proof", checkedBy="critic",
                           provisional=False, confirmedBy=confirmation["task"])
            return result
    attempt = (state.get("taskHistory", {}).get(task["id"]) or [{}])[-1]
    rounds = attempt.get("rounds", [])
    candidates = ((attempt.get("verifiedCompletion"), "worker", False),
                  (attempt.get("acceptance"), "critic" if task.get("completionKind") == "critic_acceptance" else "acceptor", False),
                  (rounds[1] if len(rounds) > 1 else None, "critic", False),
                  (attempt.get("repair"), "worker", True),
                  (rounds[0] if rounds else None, "critic", False),
                  (attempt.get("submitted"), "worker", True))
    record, checked_by, provisional = next((item for item in candidates if item[0]), (None, None, False))
    try:
        if not record or not _record_valid(store, record) or record.get("basis") != task_basis(store, state, task["id"]):
            return result
        report = _read_report(store, record["report"])
        if not isinstance(report, dict):
            return result
        checked = {row.get("criterion"): row.get("status") for row in report.get("checks", [])
                   if row.get("requirement") == task["id"]}
        issues = {finding.get("criterion") for finding in report.get("findings", [])
                  if finding.get("severity") in ("blocking", "warning")}
        unresolved = {row.get("id") for row in report.get("resolutions", []) if row.get("status") == "unresolved"}
        if unresolved and rounds:
            issues.update(f.get("criterion") for f in rounds[0].get("findings", []) if f.get("id") in unresolved)
        for row in result:
            outcome = checked.get(row["text"])
            if row["text"] in issues:
                outcome = "failed"
            if outcome in ("passed", "failed", "blocked", "not_tested"):
                status = ("reported" if provisional else "passed") if outcome == "passed" else "pending" if outcome == "not_tested" else "failed"
                row.update(status=status, source="checked_report", checkedBy=checked_by, provisional=provisional)
    except (Invalid, OSError, ValueError, KeyError, TypeError):
        pass
    return result


def waves(store, state, tasks):
    result = []
    numbers = sorted({t["wave"] for t in tasks})
    plan = plan_basis(state)
    for number in numbers:
        members = [t for t in tasks if t["wave"] == number]
        values = counts(members)
        checkpoint = state.get("waves", {}).get(str(number))
        review = {"status": "pending", "evidenceCurrent": False, "basisCurrent": None, "reason": "no_report"}
        if check_modes.wave_unchecked(state, number):
            review.update(status="skipped", reason="check_mode")
        elif checkpoint:
            try:
                report = _read_report(store, checkpoint.get("report", {}))
                checks = report.get("checks", []) if isinstance(report, dict) else []
                if isinstance(checks, list):
                    review["counts"] = {name: sum(isinstance(row, dict) and row.get("status") == name for row in checks)
                                        for name in ("passed", "failed", "blocked", "not_tested")}
                same_basis = checkpoint.get("basis") == basis(store, state, "wave")
                # The engine preserves an earlier checked checkpoint after a
                # later wave was opened under this exact plan. It is historical
                # evidence for that boundary, not a review of today's whole code.
                crossed = any(int(wave) > number and entry.get("plan") == plan
                              for wave, entry in state.get("waveEntries", {}).items())
                fresh = bool(checkpoint.get("plan") == plan and isinstance(report, dict)
                             and report.get("basis") == checkpoint.get("basis")
                             and all(artifact_valid(store, ref) for ref in checkpoint.get("artifacts", []))
                             and (same_basis or crossed) and not values["stale"])
                review.update(status=("passed" if checkpoint.get("passed") else "failed") if fresh else "stale",
                              evidenceCurrent=fresh, basisCurrent=same_basis,
                              reason="checked_checkpoint" if fresh and same_basis else "accepted_checkpoint" if fresh else "evidence_not_current")
            except (Invalid, OSError, ValueError, KeyError, TypeError):
                review.update(status="stale", reason="evidence_not_current")
        elif _working(state, "wave") and all(t["status"] in ("completed", "blocked") for t in members):
            # A non-addressed work interval only identifies the latest finished
            # wave; do not claim concurrent reviews of every pending checkpoint.
            finished = [n for n in numbers if all(t["status"] in ("completed", "blocked") for t in tasks if t["wave"] == n)]
            if finished and number == max(finished):
                review.update(status="running", reason="recorded_work")
        result.append({"wave": number, "status": aggregate(values), "completed": values["completed"],
                       "total": len(members), "counts": values, "review": review})
    return result
