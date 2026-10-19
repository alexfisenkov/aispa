"""Validation and fingerprints for the brief, plan and review evidence."""
import re
from pathlib import Path

from .store import Invalid, confined, digest, file_hash


def require(condition, message):
    if not condition:
        raise Invalid(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def active(state):
    return [r for r in state["requirements"] if r.get("scope", "active") == "active"]


def definition(task):
    return {k: task[k] for k in ("id", "title", "requirements", "dependsOn", "zones", "criteria", "wave")}


def scope_basis(state):
    return digest({"briefs": state["briefs"], "requirements": state["requirements"],
                   "core": state.get("core", []), "sealed": state.get("sealed", False)})


def plan_basis(state):
    return digest({"scope": scope_basis(state), "spec": state["spec"],
                   "tasks": [definition(t) for t in state["tasks"]]})


def basis(store, state, kind, task_id=None):
    if kind == "coverage":
        return digest({"scope": scope_basis(state), "spec": state["spec"]})
    if kind == "plan":
        return plan_basis(state)
    body = {"plan": plan_basis(state), "code": store.code()}
    if kind == "task":
        task = next((t for t in state["tasks"] if t["id"] == task_id), None)
        require(task is not None, "Неизвестная задача")
        body["task"] = definition(task)
    elif kind in ("acceptance", "wave"):
        body["progress"] = [{"id": t["id"], "status": t["status"], "proof": t.get("proof")} for t in state["tasks"]]
    else:
        raise Invalid("Неизвестный вид проверки")
    body["verification"] = state["verification"]
    return digest(body)


def artifact_valid(store, artifact):
    try:
        p = confined(store.run, artifact["file"])
        return p.is_file() and file_hash(p) == artifact["sha256"]
    except (Invalid, KeyError, OSError):
        return False


def gate_valid(store, state, name):
    gate = state["gates"].get(name)
    return bool(gate and gate["passed"] and gate["basis"] == basis(store, state, name)
                and artifact_valid(store, gate["report"])
                and all(artifact_valid(store, a) for a in gate.get("artifacts", [])))


def verified(store, state):
    check = state["verification"]
    return bool(check and check["exitCode"] == 0 and check["code"] == store.code()
                and not check.get("changedCode") and artifact_valid(store, check["output"]))


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
        for values, label in ((reqs, "requirements"), (deps, "dependsOn"), (zones, "zones"), (criteria, "criteria")):
            require(isinstance(values, list) and all(nonempty(x) for x in values)
                    and len(values) == len(set(values)), "Некорректный список: " + label)
        require(reqs and set(reqs) <= available, "Задача ссылается на отсутствующее или снятое требование")
        require(zones and criteria, "Задаче нужны зоны и критерии")
        for zone in zones:
            require(not Path(zone).is_absolute() and ".." not in Path(zone).parts
                    and not zone.startswith((".git", ".aispa")) and "\\" not in zone,
                    "Некорректная зона задачи")
        tasks[tid] = {"id": tid, "title": raw["title"], "requirements": reqs,
                      "dependsOn": deps, "zones": [z.rstrip("/") for z in zones],
                      "criteria": criteria, "status": "pending"}
    covered = {r for t in tasks.values() for r in t["requirements"]}
    require(available <= covered, "В плане потеряны требования: " + ", ".join(sorted(available - covered)))
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
