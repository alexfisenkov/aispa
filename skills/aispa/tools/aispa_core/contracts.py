"""Validation and fingerprints for the brief, plan and review evidence."""
import re
from pathlib import Path

from .store import Invalid, confined, digest, file_hash, COMMON_INPUTS, common_input, canonical_zone


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
    tasks = {t["id"]: t for t in state["tasks"]}
    found, visiting = {}, set()
    def visit(tid):
        require(tid in tasks and tid not in visiting, "Неизвестная или циклическая зависимость задачи")
        if tid in found:
            return
        visiting.add(tid)
        for dep in tasks[tid]["dependsOn"]:
            visit(dep)
        visiting.remove(tid)
        found[tid] = tasks[tid]
    visit(task_id)
    return list(found.values())


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
    zones = sorted({z for task in closure for z in task["zones"] + task.get("readZones", [])})
    return digest({"run": state["run"], "contract": task_contract(state, task_id),
                   "attempt": attempt.get("id"), "roles": attempt.get("roles", {}),
                   "code": store.code(zones, include_common=True), "readSet": zones,
                   "dependencies": [{"id": t["id"], "status": t["status"], "proof": t.get("proof")}
                                    for t in closure if t["id"] != task_id]})


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
        return p.is_file() and file_hash(p) == artifact["sha256"]
    except (Invalid, KeyError, OSError):
        return False


def gate_valid(store, state, name):
    gate = state["gates"].get(name)
    if not gate or not gate.get("passed"):
        return False
    from . import phase_receipts, execution_obligations
    if execution_obligations.enabled(state):
        try:
            execution_obligations.obligations(store, state, sealed=True)
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
        from . import integration
        if not integration.proof_valid(store, state):
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
    return bool(gate and gate["passed"] and gate["basis"] == basis(store, state, name)
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
    closures = {tid: task_closure(graph, tid) for tid in tasks}
    from . import task_tiers
    task_tiers.validate_plan(tasks, closures)
    common = {canonical_zone(z) for z in COMMON_INPUTS} | {z for t in tasks.values() for z in t["zones"] if common_input(z)}
    readsets = {tid: sorted(common | {z for t in closure for z in t["zones"] + t.get("readZones", [])})
                for tid, closure in closures.items()}
    values = list(tasks.values())
    for i, left in enumerate(values):
        for right in values[i + 1:]:
            ordered = left["id"] in {t["id"] for t in closures[right["id"]]} or right["id"] in {t["id"] for t in closures[left["id"]]}
            conflict = overlap(left["zones"], readsets[right["id"]]) or overlap(right["zones"], readsets[left["id"]])
            require(not conflict or ordered, "Запись/чтение задач %s и %s пересекаются: добавьте явную dependsOn" % (left["id"], right["id"]))
    visiting = set()

    def wave(tid):
        require(tid in tasks, "Неизвестная зависимость: " + tid)
        task = tasks[tid]
        if "wave" in task:
            return task["wave"]
        require(tid not in visiting, "Цикл зависимостей")
        visiting.add(tid)
        value = 1 + max([wave(dep) for dep in task["dependsOn"]] or [0])
        visiting.remove(tid)
        # Conflicting zones are serialised deterministically.
        used = [t for t in tasks.values() if t is not task and t.get("wave") == value]
        while any(overlap(task["zones"], t["zones"]) for t in used):
            value += 1
            used = [t for t in tasks.values() if t is not task and t.get("wave") == value]
        task["wave"] = value
        return value

    for tid in tasks:
        wave(tid)
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
