"""Bounded dispatch envelopes. Full evidence is referenced, never truncated."""
import json

from .contracts import (active, basis, definition, task_closure, require, artifact_valid)
from .store import timestamp, common_input
from .portable import match_name
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
              or bool((attempt or {}).get("authorization")) or inherited_retry)
    return {"tier": "strong" if strong else "standard", "advisory": True,
            "reason": "Independent review, shared foundation, risk or retry" if strong else "Explicitly low-risk isolated implementation",
            "selection": "Use only models exposed by the host and permitted by project rules; otherwise keep the current model",
            "report": "Save the full structured report to a file; return its path and at most 25 lines of conclusions",
            "criticRounds": 2}


def _brief_packet(store, state):
    return [{**b, "absolutePath": str(store.run / b["file"])} for b in state["briefs"]]


def build(store, state, pipeline, kind, task_id=None, role=None):
    if kind == "blind":
        # Deliberately omit spec, plan, registry, reviews, discovery and memory.
        from . import blind, carryover
        clone = state.get("cleanroom") or {}
        return {"kind": "blind", "basis": blind.basis(store, state),
                "briefs": _brief_packet(store, state),
                "inheritedBriefs": carryover.brief_sources(store, state),
                "clone": {k: clone[k] for k in ("path", "head", "tree") if k in clone},
                "instructions": "Read original briefs and run the cloned product first. Do not open the source project's spec, registry, tasks or reviews. Save independent observations before requesting the reconciliation packet."}
    selected = task_closure(state, task_id) if kind == "task" else state["tasks"]
    req_ids = {rid for task in selected for rid in task["requirements"]}
    reqs = [r for r in active(state) if kind != "task" or r["id"] in req_ids or r["kind"] == "constraint"]
    packet = {"kind": kind, "basis": basis(store, state, kind, task_id), "runDirectory": str(store.run),
              "briefs": _brief_packet(store, state), "requirements": reqs, "spec": state["spec"],
              "tasks": [dict(definition(t), status=t["status"]) for t in selected],
              "verification": state["verification"], "projectContext": state.get("projectContext"),
              "discovery": state.get("discovery"), "carryover": state.get("carryover")}
    document_paths = None
    if kind == "task":
        zones = {match_name(zone) for task in selected for zone in task["zones"] + task.get("readZones", [])}
        document_paths = [name for name in state.get("documents", {})
                          if common_input(name) or any(zone == "." or match_name(name) == zone
                             or match_name(name).startswith(zone + "/") for zone in zones)]
    packet["documentCatalog"] = docmeta.catalog(store, state, paths=document_paths)
    packet["documentReadingRule"] = "Catalog summaries select relevant files; stale summaries are withheld. Original briefs, constraints and every required check remain mandatory. Read source bodies for decisions and evidence."
    if kind == "task":
        current = state["taskHistory"][task_id][-1]
        task = next(t for t in selected if t["id"] == task_id)
        packet.update(verification=current.get("verification"), attempt=current["id"],
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
        packet.update(cleanroom=state.get("cleanroom"), cleanrun=state.get("cleanrun"), blindReview=state.get("blindReview"))
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
