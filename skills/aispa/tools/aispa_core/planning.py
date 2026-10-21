"""Review task size without trading away scope, dependency edges or two critics."""
import json
from collections import Counter

from .contracts import (active, artifact_valid, basis, definition, nonempty, overlap,
                        require, validate_plan)
from .store import digest
from . import delivery, review_policy

MAX_TASKS = 96
MAX_REPORT_CHARS = 131072
RISK = {"low": 0, "normal": 1, "high": 2}


def _role_estimate(state, count):
    return {"minimum": count * 3, "maximum": count * 4} if review_policy.selected(state) == review_policy.ADAPTIVE else count * 4


ROLE_SLOT_NOTE = "Базовые роли одной попытки; не предел фактических запусков. Подготовка, замена исполнителя и новые разрешённые попытки не включены. Фактические запуски и токены подтверждает native usage."


def _tasks(state, raw):
    require(isinstance(raw, list) and 0 < len(raw) <= MAX_TASKS,
            "Разбор размера плана принимает от 1 до 96 задач")
    return validate_plan(state, raw)


def _related(state, left, right):
    product = {r["id"] for r in active(state) if r.get("kind", "requested") != "constraint"}
    return (left["id"] in right["dependsOn"] or right["id"] in left["dependsOn"]
            or bool(set(left["requirements"]) & set(right["requirements"]) & product)
            or overlap(left["zones"], right["zones"] + right["readZones"])
            or overlap(right["zones"], left["readZones"]))


def analyze(store, state, raw):
    tasks = _tasks(state, raw)
    suggestions = []
    for left, right in zip(tasks, tasks[1:]):
        reviewed = any(any(a.get("rounds") for a in state.get("taskHistory", {}).get(t["id"], []))
                       for t in (left, right))
        if (not reviewed and min(len(left["criteria"]), len(right["criteria"])) < 3
                and left["risk"] != "high" and right["risk"] != "high"
                and _related(state, left, right)):
            suggestions.append({"id": left["id"] + "+" + right["id"],
                "tasks": [left["id"], right["id"]],
                "reason": "Связанные соседние задачи; хотя бы одна содержит менее трёх критериев. Рассмотрите объединение либо объясните границу."})
    definitions = [definition(t) for t in tasks]
    return {"basis": digest({"run": state["run"], "scope": basis(store, state, "coverage"), "draft": definitions}),
            "draft": definitions, "suggestions": suggestions,
            "taskCount": len(tasks), "taskRoleStarts": None if review_policy.selected(state) == review_policy.ADAPTIVE else len(tasks) * 4,
            "taskRoleSlots": _role_estimate(state, len(tasks)), "roleSlotNote": ROLE_SLOT_NOTE,
            "criticRoundsPerTask": {"minimum": 1, "maximum": 2} if review_policy.selected(state) == review_policy.ADAPTIVE else 2,
            "maximumCriticRoundsPerTask": 2, "actualCriticRounds": None, "estimatedTokens": None,
            "duplicateCriteriaRule": "При совпадении текста критериев объединённых задач сохраните каждый как '<исходный ID>: <текст>'.",
            "note": "Эвристика размера, не измерение токенов или качества. Сохраняйте осмысленные границы и отдельного приёмщика; число критиков определяется замороженной политикой попытки. Замена исполнителя ремонта добавляет actor сверх оценки."}


def review(store, state, final, report):
    require(isinstance(report, dict) and {"basis", "draft", "groups", "separate"} <= set(report)
            <= {"basis", "draft", "groups", "separate", "delivery"},
            "Разбор плана требует basis, draft, groups, separate и delivery для новых прогонов")
    require(len(json.dumps(report, ensure_ascii=False)) <= MAX_REPORT_CHARS, "Слишком большой разбор плана")
    advice = analyze(store, state, report["draft"])
    require(report["basis"] == advice["basis"], "Разбор размера плана устарел")
    draft = {t["id"]: t for t in advice["draft"]}
    final = {t["id"]: t for t in _tasks(state, final)}
    groups = report["groups"]
    require(isinstance(groups, list) and len(groups) == len(final), "Каждая итоговая задача требует группу исходных задач")
    mapping, seen_final = {}, set()
    for group in groups:
        require(isinstance(group, dict) and set(group) == {"task", "from", "reason"}, "Некорректная группа плана")
        tid, ids = group["task"], group["from"]
        require(isinstance(tid, str) and tid in final and tid not in seen_final, "Неизвестная или повторная итоговая задача")
        require(isinstance(ids, list) and ids and all(isinstance(i, str) and i in draft and i not in mapping for i in ids)
                and len(ids) == len(set(ids)), "Исходная задача потеряна или включена дважды")
        require(nonempty(group["reason"]) and len(group["reason"]) <= 2000, "Объясните размер и границы итоговой задачи")
        seen_final.add(tid)
        selected = [draft[i] for i in ids]
        connected = {ids[0]}
        while True:
            more = connected | {i for i in ids if any(_related(state, draft[i], draft[j]) for j in connected)}
            if more == connected:
                break
            connected = more
        require(connected == set(ids), "Нельзя объединить несвязанные задачи только ради числа запусков")
        target = final[tid]
        for field in ("requirements", "zones", "readZones"):
            expected = {value for task in selected for value in task[field]}
            require(set(target[field]) == expected, "При укрупнении изменён или потерян набор: " + field)
        counts = Counter(c for task in selected for c in task["criteria"])
        expected_checks = {task["id"] + ": " + c if counts[c] > 1 else c
                           for task in selected for c in task["criteria"]}
        require(len(expected_checks) == sum(len(t["criteria"]) for t in selected)
                and set(target["criteria"]) == expected_checks,
                "Сохраните каждый исходный критерий; совпадающие тексты подпишите исходным ID задачи")
        require(RISK[target["risk"]] >= max(RISK[t["risk"]] for t in selected), "Укрупнение не может понизить риск")
        require(all(not t["riskReason"].strip() or t["riskReason"] in target["riskReason"] for t in selected),
                "Сохраните исходные объяснения рисков в итоговом riskReason")
        mapping.update({i: tid for i in ids})
    require(set(mapping) == set(draft), "Не все исходные задачи учтены в итоговом плане")
    for group in groups:
        tid = group["task"]
        expected = {mapping[d] for i in group["from"] for d in draft[i]["dependsOn"] if mapping[d] != tid}
        require(set(final[tid]["dependsOn"]) == expected, "После объединения потеряны или добавлены внешние зависимости")
    separate = report["separate"]
    require(isinstance(separate, list), "Нужен список объяснений сохранённых границ")
    needed = {s["id"] for s in advice["suggestions"] if len({mapping[t] for t in s["tasks"]}) > 1}
    seen = set()
    for row in separate:
        require(isinstance(row, dict) and set(row) == {"suggestion", "reason"}, "Некорректное объяснение границы")
        key = row["suggestion"]
        require(isinstance(key, str) and key in needed and key not in seen and nonempty(row["reason"])
                and len(row["reason"]) <= 2000, "Объясните каждую сохранённую тонкую границу один раз")
        seen.add(key)
    require(seen == needed, "Остались мелкие связанные задачи без объяснения границ")
    if state.get("policy", {}).get("deliveryPlan") or "delivery" in report:
        delivery.validate(list(final.values()), report.get("delivery"))
    record = store.artifact("plan-reviews", (json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n").encode(), ".json")
    return {"report": record, "scopeBasis": basis(store, state, "coverage"),
            "planDigest": digest([definition(t) for t in final.values()]),
            "tasksBefore": len(draft), "tasksAfter": len(final),
            "taskRoleStartsBefore": None if review_policy.selected(state) == review_policy.ADAPTIVE else 4 * len(draft),
            "taskRoleStartsAfter": None if review_policy.selected(state) == review_policy.ADAPTIVE else 4 * len(final),
            "taskRoleSlotsBefore": _role_estimate(state, len(draft)), "taskRoleSlotsAfter": _role_estimate(state, len(final)),
            "roleSlotNote": ROLE_SLOT_NOTE,
            "criticRoundsPerTask": {"minimum": 1, "maximum": 2} if review_policy.selected(state) == review_policy.ADAPTIVE else 2,
            "maximumCriticRoundsPerTask": 2, "actualCriticRounds": None, "measuredTokenSavings": None,
            **({"deliveryDigest": digest(report["delivery"])} if "delivery" in report else {})}


def valid(store, state):
    receipt = state.get("planReview")
    if state.get("policy", {}).get("deliveryPlan"):
        try:
            report = json.loads(store.text(receipt["report"]))
            delivery.validate(state["tasks"], report["delivery"])
            if receipt.get("deliveryDigest") != digest(report["delivery"]):
                return False
        except (OSError, ValueError, TypeError, KeyError):
            return False
    return bool(isinstance(receipt, dict) and artifact_valid(store, receipt.get("report", {}))
                and receipt.get("scopeBasis") == basis(store, state, "coverage")
                and receipt.get("planDigest") == digest([definition(t) for t in state["tasks"]]))
