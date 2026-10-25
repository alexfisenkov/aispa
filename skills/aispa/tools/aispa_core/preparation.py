"""Addressed preparation commands, owner references and honest templates."""
import json

from . import briefmap, design_contract, participants, scale
from .contracts import active, artifact_valid, nonempty, require
from .store import confined, digest, file_hash


def actor_check(store):
    return lambda state, actor, role: participants.actor_check(store, state, actor, role)


def source_context(store, state):
    """Deliberately contains no extracted registry, specification or plan."""
    return briefmap.source_context(store, state)


def map_context(store, state):
    result = source_context(store, state)
    result.update(basis=briefmap.reconciliation_basis(state),
                  requirements=state["requirements"], executionObligations=state.get("executionObligations", []))
    for key in ("briefExtraction", "briefReview"):
        ref = state.get(key)
        require(isinstance(ref, dict) and artifact_valid(store, ref), "Сначала два независимых исходных разбора")
        result[key] = store.json(ref)["report"]
    return result


def record(store, state, kind, report):
    require(state["status"] == "running", "Подготовка требует действующий прогон")
    methods = {
        "brief-extract": briefmap.record_extraction, "brief-review": briefmap.record_review,
        "brief-map": briefmap.record_reconciliation,
    }
    if kind in methods:
        return methods[kind](store, state, report, actor_check(store))
    require(state.get("sealed") and state.get("spec"), "Сначала зафиксируйте бриф и спецификацию")
    return {"scenarios": design_contract.record_scenarios,
            "interfaces": design_contract.record_interfaces}[kind](store, state, report)


def owner_reference(store, state, report):
    """Freeze a specifically supplied local reference; never fetch a URL."""
    require(not state.get("tasks"), "Материалы владельца фиксируются до плана")
    require(isinstance(report, dict) and set(report) ==
            {"path", "sha256", "brief", "quote", "requirements", "use"},
            "Материалу нужны path, sha256, brief, quote, requirements, use")
    source = next((b for b in state["briefs"] if b["id"] == report["brief"]), None)
    require(source and artifact_valid(store, source) and nonempty(report["quote"])
            and report["quote"] in store.text(source), "Нужна точная цитата поручения владельца о материале")
    ids = report["requirements"]
    require(isinstance(ids, list) and ids and all(isinstance(x, str) for x in ids)
            and len(set(ids)) == len(ids) and set(ids) <= {r["id"] for r in active(state)},
            "Материал должен быть связан с действующими требованиями")
    require(nonempty(report["use"]) and (scale.enabled(state) or len(report["use"]) <= 1600), "Объясните назначение материала")
    design_contract.safe_reference_name(report["path"])
    path = confined(store.root, report["path"])
    require(path.is_file() and (scale.enabled(state) or path.stat().st_size <= 5 * 1024 * 1024), "Материал должен быть локальным файлом до 5 MiB")
    raw = scale.read_file(path, allow_manifest=False) if scale.enabled(state) else path.read_bytes()
    from .discovery import SECRET_CONTENT
    require(not SECRET_CONTENT.search(raw.decode("utf-8", "replace")), "Материал содержит возможный секрет")
    import hashlib
    require(hashlib.sha256(raw).hexdigest() == report["sha256"], "Прочитанный материал изменился")
    # Reference content is private, immutable and never automatically published.
    ref = {**report, "snapshot": store.artifact("owner-references", raw, path.suffix or ".bin")}
    state.setdefault("ownerReferences", []).append(ref)
    return ref


def template(store, state, kind, *, actor=None):
    """TODO fields intentionally fail validation until someone actually checks."""
    if kind in ("brief-extract", "brief-review"):
        ctx = source_context(store, state)
        return {"basis": ctx["basis"], "actor": actor or "TODO-native-actor",
                "units": [{**unit, "kind": "TODO-intent-or-context", "summary": "", "reason": ""}
                          for b in ctx["briefs"] for unit in b["units"]]}
    if kind == "brief-map":
        ctx = map_context(store, state)
        return {"basis": ctx["basis"], "actor": actor or "TODO-native-reviewer",
                "rows": [{k: unit[k] for k in ("brief", "start", "end")} |
                         {"disposition": "TODO", "requirements": [], "reason": ""}
                         for b in ctx["briefs"] for unit in b["units"]]}
    if kind == "scenarios":
        indexed = {r["id"]: r for r in active(state)}
        return {"basis": design_contract.scenario_basis(state), "depth": state.get("depth", "balanced"),
                "depthReason": "", "features": [
                    {"requirement": r["id"], "changeKind": r["kind"],
                     "sourceQuote": (indexed[r["parent"]] if r["kind"] == "addition" else r).get("quote", ""),
                     "specQuote": "", "dimensions": [
                         {"name": name, "status": "not_tested", "scenario": "", "verification": "", "counterexample": ""}
                         for name in design_contract.required_dimensions(state)]}
                    for r in indexed.values() if r["kind"] in ("requested", "addition")]}
    require(kind == "interfaces", "Неизвестный шаблон")
    return {"basis": design_contract.interface_basis(state), "interfaces": [],
            "noInterfaceReason": ""}
