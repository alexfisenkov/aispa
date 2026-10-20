"""Bounded dispatch envelopes. Full evidence is referenced, never truncated."""
import json

from .contracts import (active, basis, definition, task_closure, require, artifact_valid)
from .store import timestamp, common_input, Invalid
from .portable import match_name, relative_name
from . import docmeta

MAX_PACKET_BYTES = 16000


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


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
    return {"tier": "strong" if strong else "standard", "advisory": True,
            "reason": "Independent review, shared foundation, risk or retry" if strong else "Explicitly low-risk isolated implementation",
            "selection": "Use only models exposed by the host and permitted by project rules; otherwise keep the current model",
            "report": "Save the full structured report to a file; return its path and at most 25 lines of conclusions",
            "criticRounds": 2}


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
    if kind == "task" and state.get("knowledge"):
        from . import knowledge
        paths = sorted({z for task in selected for z in task["zones"] + task.get("readZones", [])})
        hints = knowledge.query(store, state, paths=paths, limit=4, max_chars=3000)
        if hints.get("entries") or hints.get("omitted", {}).get("stale"):
            packet["knowledgeHints"] = hints
    packet["documentReadingRule"] = "Read original briefs, constraints and every required check. Known paths need no catalog; warnings require source inspection. Metadata never replaces evidence."
    if kind == "task":
        current = state["taskHistory"][task_id][-1]
        task = next(t for t in selected if t["id"] == task_id)
        packet.update(verification=current.get("verification"), attempt=current["id"],
                      repairPlan=current.get("repairPlan"),
                      roles={key: value for key, value in current["roles"].items() if role is None or key == role},
                      actors=current["actors"],
                      rounds=[{k: r[k] for k in ("number", "passed", "report") if k in r} for r in current["rounds"]],
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
                      blindReview=state.get("blindReview"), handoff=state.get("handoff"))
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
        reference = store.artifact("contexts", encode(packet[key]), ".json")
        sections[key] = {**reference, "absolutePath": str(store.run / reference["file"])}
        envelope.pop(key)
        envelope["sections"] = sections
        envelope["readingRule"] = "Open every referenced section needed for the role. All expectedChecks, original briefs and constraints remain mandatory; no summary replaces them."
    require(len(encode(envelope)) <= maximum, "Не удалось вместить указатели контекста; пакет не усечён")
    artifact = store.artifact("contexts", encode(envelope), ".json")
    return {**artifact, "basis": packet["basis"], "absolutePath": str(store.run / artifact["file"]),
            "envelopeBytes": len(encode(envelope)), "inlineBytesBefore": original_bytes,
            "sectionCount": len(sections), "tokenCount": None}


def record_usage(store, state, value):
    require(isinstance(value, dict), "Usage должен быть объектом")
    require(value.get("actor") in state.get("actors", {}), "Usage требует известного нативного участника")
    for key in ("callId", "source"):
        require(isinstance(value.get(key), str) and value[key].strip(), "Нет источника метрики или ID вызова")
    for key in ("inputTokens", "outputTokens", "cachedInputTokens"):
        require(type(value.get(key)) is int and value[key] >= 0, "Usage: нужны реальные неотрицательные целые tokens")
    require(value["cachedInputTokens"] <= value["inputTokens"], "Cached input превышает весь input")
    prior = state.setdefault("usage", [])
    identity = (value["actor"], value["callId"])
    require(identity not in {(r["actor"], r["callId"]) for r in prior}, "Метрика вызова уже записана")
    row = {k: value[k] for k in ("actor", "callId", "source", "inputTokens", "outputTokens", "cachedInputTokens")}
    row["at"] = timestamp()
    row["record"] = store.artifact("usage", encode(row), ".json")
    prior.append(row)
    return usage_summary(store, state)


def usage_summary(store, state):
    rows = state.get("usage", [])
    for row in rows:
        require(artifact_valid(store, row["record"]), "Повреждён источник метрики")
        require(json.loads(store.text(row["record"])) == {k: v for k, v in row.items() if k != "record"}, "Метрика отличается от источника")
    return {"measuredCalls": len(rows), "complete": False, "unit": "tokens",
            "inputTokens": sum(r["inputTokens"] for r in rows) if rows else None,
            "outputTokens": sum(r["outputTokens"] for r in rows) if rows else None,
            "cachedInputTokens": sum(r["cachedInputTokens"] for r in rows) if rows else None,
            "note": "Only recorded native measurements; missing calls and token savings are not inferred"}
