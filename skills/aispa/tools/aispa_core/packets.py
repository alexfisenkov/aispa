"""Bounded dispatch envelopes. Full evidence is referenced, never truncated."""
import hashlib
import json

from .contracts import (active, basis, definition, task_closure, require, artifact_valid)
from .store import common_input, confined, Invalid
from .portable import match_name, relative_name
from . import docmeta, telemetry, review_policy, task_tiers

MAX_PACKET_BYTES = 16000


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _context_artifact(store, value):
    """Stable references for derived context only; never replace damaged bytes.

    CLI callers hold the journal lock. Fresh actors still read independently;
    address reuse is not evidence that any actor has read these bytes.
    """
    data = encode(value)
    sha = hashlib.sha256(data).hexdigest()
    name = "contexts/by-sha/" + sha + ".json"
    path = confined(store.run, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = confined(store.run, name)
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        path = confined(store.run, name)
        require(path.is_file() and path.read_bytes() == data,
                "Повреждён сохранённый раздел контекста; автоматическая замена запрещена")
    return {"file": name, "sha256": sha}


def routing(state, task, role, attempt=None, store=None):
    """Recommend host-defined tiers, never invent a model ID or drop reviews."""
    foundation = any(task["id"] in other["dependsOn"] for other in state["tasks"])
    inherited_retry = False
    if store is not None and state.get("carryover"):
        from .carryover import view
        inherited_retry = any(item.get("attempts") and item.get("mapping", {}).get("run") == state["run"]
                              and item["mapping"]["requirement"] in task.get("requirements", [])
                              for item in view(store, state)["items"] if item.get("mapping"))
    strong = (role != "worker" or task.get("risk", "normal") != "low" or foundation
              or bool((attempt or {}).get("authorization")) or inherited_retry
              or bool((attempt or {}).get("repairPlan")))
    light = task_tiers.is_light(attempt if attempt is not None else task)
    if light:
        require(role == "worker", "У light нет роли критика или приёмщика")
        return {"tier": "standard", "advisory": True, "taskTier": "light",
                "tierReason": (attempt or task).get("tierReason"),
                "selection": "Use only host-exposed models permitted by project rules",
                "reason": "Explicit bounded low-risk change; semantic safety still requires final review",
                "reviewPolicy": "verified_completion", "criticRounds": 0,
                "minimumCriticRounds": 0, "maximumCriticRounds": 0, "actualCriticRounds": 0,
                "taskRoleSlots": {"minimum": 1, "maximum": 1},
                "perTaskReview": "skipped_light", "projectReviewSeparate": True,
                "reviewRule": "Current verification and worker evidence complete implementation; independent project review and final acceptance remain mandatory."}
    policy = review_policy.mode(attempt) if attempt is not None else review_policy.selected(state)
    return {"tier": "strong" if strong else "standard", "advisory": True,
            "reason": "Independent review, shared foundation, risk or retry" if strong else "Explicitly low-risk isolated implementation",
            "selection": "Use only models exposed by the host and permitted by project rules; otherwise keep the current model",
            "report": "Save the full structured report to a file; return its path and at most 25 lines of conclusions",
            "reviewPolicy": policy,
            "criticRounds": None if policy == review_policy.ADAPTIVE else 2,
            "minimumCriticRounds": 1 if policy == review_policy.ADAPTIVE else 2,
            "maximumCriticRounds": 2,
            "actualCriticRounds": len(attempt.get("rounds", [])) if attempt is not None else None,
            "taskRoleSlots": {"minimum": 3, "maximum": 4} if policy == review_policy.ADAPTIVE else {"minimum": 4, "maximum": 4},
            "roleSlotNote": "Base role slots for one attempt, not a native-start cap. Excludes preparation, actor replacements and newly authorized attempts; actual cost requires native usage.",
            "reviewRule": "First PASS goes to a separate acceptor; WARN/BLOCK/FAIL requires repair and one fresh critic2. No third round." if policy == review_policy.ADAPTIVE else "Two independent critics, then a separate acceptor."}


def _brief_packet(store, state):
    return [{**b, "absolutePath": str(store.run / b["file"])} for b in state["briefs"]]


def document_context(store, state, kind, selected, source_paths=None):
    """Navigation is optional; original sources and proof contracts are not."""
    from .discovery import _kind, _names, _safe_path
    zones = {match_name(zone) for task in selected for zone in task["zones"] + task.get("readZones", [])}
    registered = state.get("documents", {})
    paths = [name for name in registered if kind != "task" or common_input(name) or any(
        zone == "." or match_name(name) == zone or match_name(name).startswith(zone + "/") for zone in zones)]
    if kind == "coverage" and source_paths is None:
        return {"documentRouting": {"catalog": "omitted", "reason": "brief-and-spec"}}

    known = None
    if source_paths is not None:
        require(isinstance(source_paths, list) and source_paths, "Нужен непустой список известных файлов")
        names = set(_names(store))
        known = []
        for raw in source_paths:
            try:
                name = relative_name(raw)
            except (ValueError, TypeError) as exc:
                raise Invalid("Небезопасный путь известного источника") from exc
            require(name in names and (_kind(name, explicit=True) is not None or common_input(name)),
                    "Источник отсутствует, исключён или не поддержан: " + name)
            require(_safe_path(store.root, name).is_file(), "Нужен точный файл, не каталог: " + name)
            require(kind != "task" or common_input(name) or any(zone == "." or match_name(name) == zone
                    or match_name(name).startswith(zone + "/") for zone in zones),
                    "Источник вне readZones/zones задачи: " + name)
            known.append(name)
        known = sorted(set(known))
    elif selected:
        selectors = sorted({zone for task in selected for zone in task["zones"] + task.get("readZones", [])})
        # Missing/future paths and directory selectors do not establish a full read list.
        try:
            names = set(_names(store))
            if selectors and all(name in names and (_kind(name, explicit=True) is not None or common_input(name))
                                 and _safe_path(store.root, name).is_file() for name in selectors):
                known = selectors
        except (OSError, ValueError):
            pass
    if known is not None:
        checked = sorted(set(known) | {name for name in paths if common_input(name)})
        route = {"catalog": "omitted", "reason": "explicit-files" if source_paths is not None else "task-files"}
        if source_paths is not None:
            route["sources"] = known
        result = {"documentRouting": route}
        warnings = docmeta.source_warnings(store, state, checked)
        if warnings:
            result["documentWarnings"] = warnings
        return result
    return {"documentCatalog": docmeta.catalog(store, state, paths=paths),
            "documentRouting": {"catalog": "compact", "reason": "navigation-needed"}}


def build(store, state, pipeline, kind, task_id=None, role=None, *, source_paths=None):
    if kind == "blind":
        require(source_paths is None, "Blind-пакет не принимает каталог или список источников проекта")
        # Deliberately omit spec, plan, registry, reviews, discovery and memory.
        from . import blind, carryover
        clone = state.get("cleanroom") or {}
        return {"kind": "blind", "basis": blind.basis(store, state),
                "briefs": _brief_packet(store, state),
                "inheritedBriefs": carryover.brief_sources(store, state),
                "clone": {k: clone[k] for k in ("path", "head", "tree") if k in clone},
                "instructions": "Read original briefs and run the cloned product first. Do not open the source project's spec, registry, tasks or reviews. Save independent observations before requesting the reconciliation packet."}
    if kind == "knowledge":
        from . import executions
        from .discovery import _names, _kind, _safe_path
        from .store import digest
        require(source_paths is None, "Для автора знаний используйте список projectFiles и актуальный код")
        names = [name for name in _names(store) if _kind(name, explicit=True) is not None
                 and _safe_path(store.root, name).is_file()]
        check, clean = state.get("verification"), state.get("cleanrun")
        return {"kind": kind, "basis": store.code(), "writer": state.get("knowledge", {}).get("writer", {}).get("id"),
                "projectFiles": names[:128], "omittedFiles": max(0, len(names) - 128),
                "verification": check, "verificationDigest": digest(check) if check else None,
                "cleanrun": clean, "cleanrunDigest": digest(clean) if clean else None,
                "commandHistory": executions.view(store, state, limit=8),
                "handoff": state.get("handoff"),
                "runDirectory": str(store.run),
                "instructions": "Read actual source files and execute or inspect genuine verification evidence. Do not use the plan, spec, brief or task reports as proof of implemented behavior. Record command, trap, decision and glossary knowledge or explicit category uncertainties. A historical command check is not a current PASS."}
    selected = task_closure(state, task_id) if kind == "task" else state["tasks"]
    req_ids = {rid for task in selected for rid in task["requirements"]}
    reqs = [r for r in active(state) if kind != "task" or r["id"] in req_ids or r["kind"] == "constraint"]
    packet = {"kind": kind, "basis": basis(store, state, kind, task_id), "runDirectory": str(store.run),
              "briefs": _brief_packet(store, state), "requirements": reqs, "spec": state["spec"],
              "tasks": [dict(definition(t), status=t["status"]) for t in selected],
              "verification": state["verification"], "projectContext": state.get("projectContext"),
              "discovery": state.get("discovery"), "carryover": state.get("carryover")}
    packet.update(document_context(store, state, kind, selected, source_paths))
    if state.get("ideaReview") and kind == "coverage":
        packet["ideaStudy"] = state["ideaReview"]
        if state.get("policy", {}).get("productChallenge"):
            from .product import QUESTIONS
            packet["requiredIdeaChecks"] = list(QUESTIONS)
    if state.get("planReview") and kind == "task":
        packet["deliveryPlan"] = state["planReview"]["report"]
    if kind == "task":
        for key in ("briefMap", "featureScenarios", "interfaceContract"):
            if state.get(key):
                require(artifact_valid(store, state[key]), "Повреждён источник контекста задачи: " + key)
                packet[key] = state[key]
        if state.get("interfaceContract"):
            from . import design_contract
            packet["interfaceReferences"] = design_contract.reference_snapshots(store, state)
    if kind == "task" and state.get("knowledge"):
        from . import knowledge
        paths = sorted({z for task in selected for z in task["zones"] + task.get("readZones", [])})
        hints = knowledge.query(store, state, paths=paths, limit=4, max_chars=3000)
        if hints.get("entries") or hints.get("omitted", {}).get("stale"):
            packet["knowledgeHints"] = hints
    packet["documentReadingRule"] = "Read original briefs, constraints and every required check. Known paths need no catalog; warnings require source inspection. Metadata never replaces evidence."
    if kind == "task":
        current = state["taskHistory"][task_id][-1]
        from . import test_intent
        task = next(t for t in selected if t["id"] == task_id)
        packet.update(verification=current.get("verification"), attempt=current["id"],
                      reviewPolicy=review_policy.mode(current),
                      taskTier=current.get("tier", "heavy"), tierReason=current.get("tierReason", ""),
                      verifiedCompletion=current.get("verifiedCompletion"),
                      maximumCriticRounds=0 if task_tiers.is_light(current) else 2, actualCriticRounds=len(current["rounds"]),
                      perTaskReview="skipped_light" if task_tiers.is_light(current) else "required",
                      testIntent=current.get("testIntent"),
                      testSourceChanges=test_intent.source_changes(store, state, task_id),
                      repairPlan=current.get("repairPlan"),
                      roles={key: value for key, value in current["roles"].items() if role is None or key == role},
                      actors=current["actors"],
                      rounds=[{k: r[k] for k in ("number", "passed", "verdict", "report") if k in r} for r in current["rounds"]],
                      repair={k: current["repair"][k] for k in ("report", "previousReport", "addresses") if k in current["repair"]} if current.get("repair") else None,
                      traceRequirements=pipeline.trace_requirements(task_id),
                      inheritedProofs=pipeline.inherited_proofs(task_id),
                      neighbors=[{"id": t["id"], "zones": t["zones"], "status": t["status"]}
                                 for t in state["tasks"] if t["id"] != task_id])
        if role:
            packet["routing"] = routing(state, task, role, current, store)
            packet["expectedChecks"] = [{"requirement": rid, "criterion": criterion}
                                        for rid, criterion in sorted(pipeline.expected_checks(task_id, role))]
    if kind == "acceptance":
        packet.update(cleanroom=state.get("cleanroom"), cleanrun=state.get("cleanrun"),
                      blindReview=state.get("blindReview"), handoff=state.get("handoff"),
                      projectIntegrationReview=state.get("projectIntegrationReview"))
        if state.get("policy", {}).get("executionObligations"):
            from . import execution_obligations
            packet["executionObligations"] = execution_obligations.view(store, state)
            packet["executionRule"] = "Map only exact presealed execution quotes/record SHA. Do not rewrite blind status or move product requirements. Future host handoff stays pending, never a product PASS."
    if kind == "task" and task_id == state.get("integrationTask") and state.get("policy", {}).get("integrationReview"):
        from . import integration
        packet["branchReview"] = integration.context(store, state, task_id)
        packet["branchReviewAxes"] = list(integration.AXES)
        if task_tiers.is_light(state["taskHistory"][task_id][-1]):
            packet["projectReviewRole"] = "integration-reviewer"
            packet["projectReviewSeparate"] = True
    if kind == "task" and state.get("ownerReferences"):
        ids = {rid for task in selected for rid in task["requirements"]}
        packet["ownerReferences"] = [ref for ref in state["ownerReferences"] if set(ref["requirements"]) & ids]
    return packet


def write(store, packet, *, maximum=MAX_PACKET_BYTES):
    require(type(maximum) is int and maximum >= 2000, "Слишком малый размер пакета")
    envelope = dict(packet)
    original_bytes = len(encode(packet))
    sections = {}
    # Each reference is smaller than its payload. Never discard even one check.
    movable = sorted((key for key in packet if key not in ("kind", "basis")),
                     key=lambda key: len(encode(packet[key])), reverse=True)
    for key in movable:
        if len(encode(envelope)) <= maximum:
            break
        if len(encode(packet[key])) < 500:
            continue
        reference = _context_artifact(store, packet[key])
        sections[key] = {**reference, "absolutePath": str(store.run / reference["file"])}
        envelope.pop(key)
        envelope["sections"] = sections
        envelope["readingRule"] = "Open every referenced section needed for the role. All expectedChecks, original briefs and constraints remain mandatory; no summary replaces them."
    require(len(encode(envelope)) <= maximum, "Не удалось вместить указатели контекста; пакет не усечён")
    artifact = _context_artifact(store, envelope)
    return {**artifact, "basis": packet["basis"], "absolutePath": str(store.run / artifact["file"]),
            "envelopeBytes": len(encode(envelope)), "inlineBytesBefore": original_bytes,
            "sectionCount": len(sections), "tokenCount": None}


def write_task_bundle(store, state, pipeline, task_id, role, *, source_paths=None):
    """Prepare addressed inputs only; assignment and report submission stay separate."""
    from . import task_reports
    from .contracts import task_verified
    # Validate the actual assignment and lifecycle before creating any files.
    report = task_reports.template(pipeline, task_id, role)
    if role == "worker":
        require(task_verified(store, state, task_id),
                "Форма worker в пакете требует успешной адресной проверки текущей задачи")
    packet = build(store, state, pipeline, "task", task_id, role, source_paths=source_paths)
    require(packet["basis"] == report["basis"], "Кандидат изменился во время подготовки пакета")
    card = packet["roles"][role]
    return {"kind": "task-handoff", "task": task_id, "role": role, "actor": report["reviewer"],
            "card": {"file": card["file"], "sha256": card["sha256"],
                     "absolutePath": str(confined(store.run, card["file"]))},
            "context": write(store, packet),
            "template": task_reports.write(store, report, task_id, role)}


def record_usage(store, state, value):
    return telemetry.record(store, state, value)


def usage_summary(store, state):
    return telemetry.summary(store, state)
