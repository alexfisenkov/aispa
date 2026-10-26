"""Validation and fingerprints for the brief, plan and review evidence."""
import re
from pathlib import Path

from .store import Invalid, confined, digest, file_hash, COMMON_INPUTS, common_input, canonical_zone
from .verified_reads import ResourceLimit


def require(condition, message):
    if not condition:
        raise Invalid(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def active(state):
    return [r for r in state["requirements"] if r.get("scope", "active") == "active"]


def definition(task):
    return {**{k: task[k] for k in ("id", "title", "requirements", "dependsOn", "zones", "criteria", "wave")},
            "readZones": task.get("readZones", []), "risk": task.get("risk", "normal"),
            "riskReason": task.get("riskReason", ""),
            **{key: task[key] for key in ("tier", "tierReason", "changeKind") if key in task}}


def task_closure(state, task_id):
    from .scale.graph import DependencyIndex
    from . import scale
    return DependencyIndex(state["tasks"], budget=scale.Budget() if scale.enabled(state) else None).closure(task_id)


def task_contract(state, task_id):
    return digest({"scope": scope_basis(state), "spec": state["spec"],
                   **design_basis(state),
                   "closure": [{k: v for k, v in definition(t).items() if k != "wave"}
                               for t in task_closure(state, task_id)],
                   **({"delivery": state.get("planReview", {}).get("deliveryDigest")}
                      if state.get("policy", {}).get("deliveryPlan") else {})})


def task_basis(store, state, task_id):
    closure = task_closure(state, task_id)
    attempt = (state.get("taskHistory", {}).get(task_id) or [{}])[-1]
    skipped = set(attempt.get("deferredDependencies", []))  # a deferred task's later retry cannot break its dependants
    # An epoch's additional zones are exact files even when initially absent.
    # Candidate calculation runs before/after verification and at live proof
    # consumers; saved historical receipts deliberately do not use this guard.
    from . import task_zone_extension
    for task in closure:
        if task["id"] not in skipped:
            current = (state.get("taskHistory", {}).get(task["id"]) or [{}])[-1]
            task_zone_extension.require_exact_files(store, current)
    # Its code is skipped with it: a retry writing its own zone must not stale the dependants' proofs.
    zones = sorted({z for task in closure if task["id"] not in skipped for z in task["zones"] + task.get("readZones", [])})
    if state.get("policy", {}).get("projectDocuments"):
        from . import project_documents
        zones = sorted(set(zones) | set(project_documents.required_paths(state)))
    return digest({"run": state["run"], "contract": task_contract(state, task_id),
                   "attempt": attempt.get("id"), "roles": attempt.get("roles", {}),
                   "code": store.code(zones, include_common=True), "readSet": zones,
                   "dependencies": [{"id": t["id"], "status": t["status"], "proof": t.get("proof")}
                                    for t in closure if t["id"] != task_id and t["id"] not in skipped]})


def scope_basis(state):
    value = {"briefs": state["briefs"], "requirements": state["requirements"],
             "core": state.get("core", []), "sealed": state.get("sealed", False)}
    if state.get("policy", {}).get("productChallenge"):
        value["ideaReview"] = state.get("ideaReview")
    if state.get("policy", {}).get("sourceAccounting"):
        value["briefMap"] = state.get("briefMap")
    if state.get("policy", {}).get("executionObligations"):
        value["executionObligations"] = state.get("executionObligations", [])
        value["executionSeal"] = state.get("executionSeal")
    if state.get("policy", {}).get("projectDocuments"):
        from . import project_documents
        value["projectIntent"] = project_documents.accepted_basis(state)
    return digest(value)


def design_basis(state):
    if not state.get("policy", {}).get("designContract"):
        return {}
    return {"scenarios": state.get("featureScenarios"), "interfaces": state.get("interfaceContract"),
            "references": state.get("ownerReferences", []), "depth": state.get("depth", "balanced"),
            "integrationTask": state.get("integrationTask")}


def plan_basis(state):
    return digest({"scope": scope_basis(state), "spec": state["spec"],
                   **design_basis(state),
                   "tasks": [definition(t) for t in state["tasks"]],
                   **({"delivery": state.get("planReview", {}).get("deliveryDigest")}
                      if state.get("policy", {}).get("deliveryPlan") else {})})


def basis(store, state, kind, task_id=None):
    if kind == "coverage":
        return digest({"scope": scope_basis(state), "spec": state["spec"]})
    if kind == "plan":
        return plan_basis(state)
    if kind == "task":
        return task_basis(store, state, task_id)
    body = {"plan": plan_basis(state), "code": store.code()}
    if kind in ("acceptance", "wave"):
        body["progress"] = [{"id": t["id"], "status": t["status"], "proof": t.get("proof")} for t in state["tasks"]]
    else:
        raise Invalid("Неизвестный вид проверки")
    body["verification"] = state["verification"]
    if kind == "acceptance":
        body.update(cleanroom=state.get("cleanroom"), cleanrun=state.get("cleanrun"),
                    carryover=state.get("carryover"), blindReview=state.get("blindReview"))
        if state.get("policy", {}).get("deliveryDocs"):
            body["handoff"] = state.get("handoff")
    return digest(body)


def artifact_valid(store, artifact):
    try:
        p = confined(store.run, artifact["file"])
        if not p.is_file():
            return False
        # The outer SHA binds ordered chunk identities; verify their bytes too.
        if p.suffix in (".json", ".md"):
            store.artifact_bytes(artifact)
            return True
        return store.artifact_hash_valid(artifact)
    except Invalid as error:
        from .verified_reads import ResourceLimit
        if isinstance(error, ResourceLimit):
            raise
        return False
    except (KeyError, OSError):
        return False


def gate_valid(store, state, name, settled=False):
    """Readiness predicate: mandatory product-flow failures always veto it."""
    return _gate_check(store, state, name, settled=settled)


def gate_current(store, state, name):
    """Report freshness only, including a current negative result; never readiness."""
    return _gate_check(store, state, name, outcome=False)


def _gate_check(store, state, name, settled=False, *, outcome=True):
    """settled: the owner's fast mode accepts a recorded, current acceptance even when it found failures;
    max does so only when every failure belongs to a deferred core task (deferredSettled)."""
    gate = state["gates"].get(name)
    if settled and name == "acceptance" and gate:
        from . import check_modes
        settled = check_modes.is_fast(state) or (check_modes.is_max(state) and bool(gate.get("deferredSettled")))
    else:
        settled = False
    if not gate or outcome and not (gate.get("passed") or settled):
        return False
    if state.get("policy", {}).get("projectDocuments"):
        from . import project_documents
        try:
            project_documents.require_fresh_intent(store, state)
        except ResourceLimit:
            raise
        except (Invalid, OSError, ValueError, TypeError, KeyError):
            return False
    from . import phase_receipts, execution_obligations
    if execution_obligations.enabled(state):
        try:
            execution_obligations.obligations(store, state, sealed=True)
        except ResourceLimit:
            raise
        except (Invalid, OSError, ValueError, TypeError, KeyError):
            return False
    if not phase_receipts.valid(store, state, {"plan": "planner"}.get(name, name)):
        return False
    if state.get("policy", {}).get("sourceAccounting"):
        from . import briefmap, participants
        if not briefmap.valid(store, state, lambda s, a, r: participants.actor_check(store, s, a, r)):
            return False
    if name in ("plan", "acceptance") and state.get("policy", {}).get("designContract"):
        from . import design_contract
        if (not design_contract.valid_scenarios(store, state)
                or not design_contract.valid_interfaces(store, state, check_sources=False)
                or not all(artifact_valid(store, ref.get("snapshot", {})) for ref in state.get("ownerReferences", []))):
            return False
    if name == "acceptance" and state.get("policy", {}).get("integrationReview"):
        from . import check_modes, integration
        # The owner's fast mode skips the whole-project review, as handoff_ready does.
        if not check_modes.is_fast(state) and not integration.proof_valid(store, state):
            return False
    if name == "acceptance" and state.get("policy", {}).get("cleanClone"):
        from .cleanroom import valid
        from .blind import valid as blind_valid
        if not valid(store, state) or not blind_valid(store, state):
            return False
    if name == "acceptance" and state.get("policy", {}).get("deliveryDocs"):
        from . import handoff, executions
        if not handoff.valid(store, state, command_receipt=lambda ref: executions.resolve(store, state, ref)):
            return False
    if name == "acceptance" and state.get("policy", {}).get("finalWaveReuse"):
        from . import blind
        if not blind.derived_waves_valid(store, state):
            return False
    if name == 'acceptance' and state.get('policy', {}).get('criticalProductFlows') is True:
        from . import product_flows
        try:
            flow_failures = product_flows.saved_check(store, state)
        except ResourceLimit:
            raise
        except (Invalid, OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return False
        if outcome and flow_failures:
            return False
    return bool(gate and (not outcome or gate["passed"] or settled) and gate["basis"] == basis(store, state, name)
                and artifact_valid(store, gate["report"])
                and all(artifact_valid(store, a) for a in gate.get("artifacts", [])))


def verified(store, state):
    check = state["verification"]
    return bool(check and check["exitCode"] == 0 and check["code"] == store.code()
                and not any(check.get(k) for k in ("changedCode", "timedOut", "truncated", "redacted"))
                and artifact_valid(store, check["output"]))


def task_verified(store, state, task_id):
    attempt = (state.get("taskHistory", {}).get(task_id) or [{}])[-1]
    check = attempt.get("verification")
    return bool(check and check["exitCode"] == 0 and not check.get("changedCode")
                and not any(check.get(k) for k in ("timedOut", "truncated", "redacted"))
                and check["candidate"] == task_basis(store, state, task_id)
                and artifact_valid(store, check["output"]))


def add_requirements(store, state, items):
    from . import scale
    scale.check_value(items, state)
    require(isinstance(items, list) and items, "Нужен непустой список требований")
    known = {r["id"]: r for r in state["requirements"]}
    briefs = {b["id"]: b for b in state["briefs"]}
    result = []
    for raw in items:
        require(isinstance(raw, dict), "Требование должно быть объектом")
        rid = raw.get("id")
        require(isinstance(rid, str) and re.fullmatch(r"[RGAX]\d{2,}", rid), "ID требования: R01, G01, A01 или X01")
        require(rid not in known, "Нельзя повторно использовать или переписать ID: " + rid)
        require(nonempty(raw.get("text")), "У требования нет текста")
        criteria = raw.get("acceptance")
        require(isinstance(criteria, list) and criteria and all(nonempty(c) for c in criteria)
                and len(set(criteria)) == len(criteria), "Нужны уникальные проверяемые условия приёмки")
        kind = raw.get("kind", "requested")
        require(kind in ("requested", "addition", "constraint"), "Неизвестный вид требования")
        require(isinstance(raw.get("core", False), bool), "core должен быть true или false")
        item = {"id": rid, "text": raw["text"], "acceptance": criteria,
                "kind": kind, "core": bool(raw.get("core", False)), "scope": "active"}
        if kind == "addition":
            parent = raw.get("parent")
            require(isinstance(parent, str) and parent in known and known[parent]["kind"] != "addition", "Дополнение должно иметь исходное родительское требование")
            require(not item["core"], "Инициатива агента не входит в обязательное ядро")
            item["parent"] = parent
        else:
            bid, quote = raw.get("brief"), raw.get("quote")
            require(isinstance(bid, str) and bid in briefs and nonempty(quote), "Нужны ID брифа и дословная цитата")
            require(quote in store.text(briefs[bid]), "Цитата отсутствует в указанном брифе")
            item.update(brief=bid, quote=quote)
        known[rid] = item
        result.append(item)
    return result


def validate_plan(state, items):
    from . import scale
    budget = scale.check_value(items, state)
    require(isinstance(items, list) and items, "Пустой план недопустим")
    available = {r["id"] for r in active(state)}
    tasks = {}
    for raw in items:
        require(isinstance(raw, dict), "Задача должна быть объектом")
        tid = raw.get("id")
        require(isinstance(tid, str) and re.fullmatch(r"T\d{2,}", tid) and tid not in tasks,
                "Нужен уникальный ID задачи вида T01")
        require(nonempty(raw.get("title")), "У задачи нет названия")
        reqs, deps, zones, criteria = (raw.get(k) for k in ("requirements", "dependsOn", "zones", "criteria"))
        reads = raw.get("readZones", [])
        for values, label in ((reqs, "requirements"), (deps, "dependsOn"), (zones, "zones"), (criteria, "criteria")):
            require(isinstance(values, list) and all(nonempty(x) for x in values)
                    and len(values) == len(set(values)), "Некорректный список: " + label)
        require(reqs and set(reqs) <= available, "Задача ссылается на отсутствующее или снятое требование")
        require(zones and criteria, "Задаче нужны зоны и критерии")
        require(isinstance(reads, list) and all(nonempty(x) for x in reads), "readZones должен быть списком путей")
        zones = list(dict.fromkeys(canonical_zone(z) for z in zones))
        reads = list(dict.fromkeys(canonical_zone(z) for z in reads))
        risk, reason = raw.get("risk", "normal"), raw.get("riskReason", "")
        require(risk in ("low", "normal", "high") and isinstance(reason, str), "Некорректный уровень риска")
        require(risk != "low" or nonempty(reason), "Для low risk укажите riskReason: почему задача допускает экономную модель")
        from . import task_tiers
        classification = task_tiers.validate(state, raw, zones, risk, reason)
        tasks[tid] = {**classification, "id": tid, "title": raw["title"], "requirements": reqs,
                      "dependsOn": deps, "zones": zones,
                      "readZones": reads,
                      "criteria": criteria, "status": "pending", "risk": risk, "riskReason": reason}
    covered = {r for t in tasks.values() for r in t["requirements"]}
    require(available <= covered, "В плане потеряны требования: " + ", ".join(sorted(available - covered)))
    graph = dict(state, tasks=list(tasks.values()))
    from .scale.graph import DependencyIndex
    index = DependencyIndex(list(tasks.values()), budget=budget)
    closures = {}
    import sys
    accounted_memory = sum(sys.getsizeof(bits) for bits in index.bits.values())
    for tid in tasks:
        closures[tid] = index.closure(tid)
        accounted_memory += sys.getsizeof(closures[tid])
        if budget:
            budget.check_memory(accounted_memory, path="plan dependency index", progress=len(closures))
    from . import task_tiers
    task_tiers.validate_plan(tasks, closures)
    common = {canonical_zone(z) for z in COMMON_INPUTS} | {z for t in tasks.values() for z in t["zones"] if common_input(z)}
    if state.get("policy", {}).get("projectDocuments"):
        from . import project_documents
        common |= {canonical_zone(z) for z in project_documents.required_paths(state)}
    readsets = {}
    for tid, closure in closures.items():
        readsets[tid] = sorted(common | {z for t in closure for z in t["zones"] + t.get("readZones", [])})
        accounted_memory += sys.getsizeof(readsets[tid])
        if budget:
            budget.check_memory(accounted_memory, path="plan read/conflict index", progress=len(readsets))
    values = list(tasks.values())
    for i, left in enumerate(values):
        if budget:
            budget.check(0, path="plan dependency/conflict validation", progress=i)
        for right in values[i + 1:]:
            ordered = index.contains(right["id"], left["id"]) or index.contains(left["id"], right["id"])
            conflict = overlap(left["zones"], readsets[right["id"]]) or overlap(right["zones"], readsets[left["id"]])
            require(not conflict or ordered, "Запись/чтение задач %s и %s пересекаются: добавьте явную dependsOn" % (left["id"], right["id"]))
    by_wave = {}
    for tid in index.order:
        task = tasks[tid]
        value = 1 + max((tasks[dep]["wave"] for dep in task["dependsOn"]), default=0)
        while any(overlap(task["zones"], other["zones"]) for other in by_wave.get(value, [])):
            value += 1
        task["wave"] = value
        by_wave.setdefault(value, []).append(task)
    return list(tasks.values())


def overlap(left, right):
    def parent(a, b):
        return a in ("", ".") or a == b or b.startswith(a + "/")
    return any(parent(a, b) or parent(b, a) for a in left for b in right)


def report_header(store, state, report, kind, task_id=None):
    require(isinstance(report, dict), "Отчёт должен быть объектом JSON")
    require(report.get("basis") == basis(store, state, kind, task_id), "Отчёт устарел или относится к другому состоянию проекта")
    require(nonempty(report.get("reviewer")), "Нужно указать проверяющего")
    if kind in ("coverage", "acceptance", "wave"):
        require(report.get("independent") is True, "Нужна независимая проверка с отдельным контекстом")
    require(isinstance(report.get("unmapped"), list), "Нужен список требований брифа, не попавших в учёт: unmapped")


def evidence_rows(store, report, expected):
    rows = report.get("checks")
    require(isinstance(rows, list), "Отсутствует checks")
    seen, artifacts, failures = set(), [], []
    for row in rows:
        require(isinstance(row, dict), "Некорректная строка проверки")
        key = (row.get("requirement"), row.get("criterion"))
        require(all(nonempty(x) for x in key), "Нужны строковые requirement и criterion")
        require(key in expected and key not in seen, "Неизвестное или повторное условие приёмки")
        seen.add(key)
        status = row.get("status")
        require(status in ("passed", "failed", "not_tested", "blocked"), "Неизвестный статус проверки")
        require(nonempty(row.get("note")), "Нужно объяснение результата проверки")
        if status == "passed":
            require(isinstance(row.get("artifact"), str) and row["artifact"].startswith("evidence/"),
                    "Доказательства должны лежать в evidence/ прогона")
            p = confined(store.run, row.get("artifact"))
            require(p.is_file() and p.stat().st_size > 0, "Нет непустого файла доказательства")
            artifacts.append({"file": row["artifact"], "sha256": file_hash(p)})
        else:
            failures.append({"requirement": key[0], "criterion": key[1], "status": status, "note": row["note"]})
    require(seen == expected, "Проверены не все условия приёмки")
    return artifacts, failures
