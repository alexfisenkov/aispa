"""Review task size without trading away scope, dependency edges or two critics."""
import json
from collections import Counter

from .contracts import (active, artifact_valid, basis, definition, nonempty, overlap,
                        require, validate_plan)
from .store import digest
from . import check_modes, delivery, review_policy, task_tiers, scale

MAX_TASKS = 96
MAX_REPORT_CHARS = 131072
RISK = {"low": 0, "normal": 1, "high": 2}


def _role_estimate(state, tasks):
    if isinstance(tasks, int):
        count = tasks
    else:
        tasks = list(tasks)
        count = len(tasks)
        if task_tiers.enabled(state) or check_modes.current(state) != check_modes.DEFAULT:
            light = sum(_worker_only(state, t) for t in tasks)
            low, high = _heavy_slots(state)
            return {"minimum": light + (count - light) * low, "maximum": light + (count - light) * high}
    if review_policy.selected(state) == review_policy.LEGACY:
        return count * 4
    low, high = _heavy_slots(state)
    return {"minimum": count * low, "maximum": count * high}


def _worker_only(state, task):
    """Fast makes every task worker-only, max makes it heavy; otherwise the declared class."""
    tier = check_modes.effective_tier(state, task.get("tier"))
    return task_tiers.worker_only({"tier": tier})


def _heavy_slots(state):
    """Worker, critic1, conditional critic2, and a separate acceptor unless lean."""
    policy = review_policy.selected(state)
    return {review_policy.LEAN: (2, 3), review_policy.ADAPTIVE: (3, 4)}.get(policy, (4, 4))


def _critic_rounds(state):
    if task_tiers.enabled(state):
        rounds = {"light": 0, "heavy": {"minimum": 1, "maximum": 2}}
        if task_tiers.standard_enabled(state):
            rounds["standard"] = 0
        return rounds
    return {"minimum": 1, "maximum": 2} if review_policy.selected(state) != review_policy.LEGACY else 2


def _role_starts(state, tasks):
    slots = _role_estimate(state, tasks)
    return slots if isinstance(slots, int) else slots["minimum"] if slots["minimum"] == slots["maximum"] else None


ROLE_SLOT_NOTE = "Базовые роли одной попытки; не предел фактических запусков. Подготовка, замена исполнителя и новые разрешённые попытки не включены. Фактические запуски и токены подтверждает native usage."


def _tasks(state, raw):
    require(isinstance(raw, list) and raw and (scale.enabled(state) or len(raw) <= MAX_TASKS),
            "Разбор размера плана принимает от 1 до 96 задач")
    scale.check_value(raw, state)
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
            **({"taskTiers": {t["id"]: {"tier": t["tier"], "tierReason": t["tierReason"],
                 "effectiveTier": check_modes.effective_tier(state, t["tier"]),
                 "taskWorkers": 1, "taskCritics": {"minimum": 0 if _worker_only(state, t) else 1, "maximum": 0 if _worker_only(state, t) else 2},
                 "taskAcceptors": 0 if _worker_only(state, t) or review_policy.selected(state) == review_policy.LEAN else 1} for t in tasks},
                 "projectReviewSeparate": True,
                 "projectReviewRole": "integration-reviewer when the final frozen attempt is light or standard"} if task_tiers.enabled(state) else {}),
            "taskCount": len(tasks), "taskRoleStarts": _role_starts(state, tasks),
            "taskRoleSlots": _role_estimate(state, tasks), "roleSlotNote": ROLE_SLOT_NOTE,
            "criticRoundsPerTask": _critic_rounds(state),
            "maximumCriticRoundsPerTask": 2, "actualCriticRounds": None, "estimatedTokens": None,
            "duplicateCriteriaRule": "При совпадении текста критериев объединённых задач сохраните каждый как '<исходный ID>: <текст>'.",
            "note": "Эвристика размера, не измерение токенов или качества. Сохраняйте осмысленные границы: heavy требует критиков и (кроме lean) отдельного приёмщика, light и standard — проверенное выполнение без этих ролей. Общее ревью и итоговая приёмка сохраняются. Замена исполнителя ремонта добавляет actor сверх оценки."}


def review(store, state, final, report):
    require(isinstance(report, dict) and {"basis", "draft", "groups", "separate"} <= set(report)
            <= {"basis", "draft", "groups", "separate", "delivery"},
            "Разбор плана требует basis, draft, groups, separate и delivery для новых прогонов")
    scale.check_value(report, state)
    require(scale.enabled(state) or len(json.dumps(report, ensure_ascii=False)) <= MAX_REPORT_CHARS, "Слишком большой разбор плана")
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
        require(nonempty(group["reason"]) and (scale.enabled(state) or len(group["reason"]) <= 2000), "Объясните размер и границы итоговой задачи")
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
        if task_tiers.enabled(state) and task_tiers.worker_only(target):
            require(all(task_tiers.worker_only(t) for t in selected), "Укрупнение не может понизить heavy до light или standard")
            require(not task_tiers.is_light(target) or all(task_tiers.is_light(t) for t in selected),
                    "Укрупнение не может понизить standard до light")
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
                and (scale.enabled(state) or len(row["reason"]) <= 2000), "Объясните каждую сохранённую тонкую границу один раз")
        seen.add(key)
    require(seen == needed, "Остались мелкие связанные задачи без объяснения границ")
    if state.get("policy", {}).get("deliveryPlan") or "delivery" in report:
        delivery.validate(list(final.values()), report.get("delivery"), state=state)
    record = store.artifact("plan-reviews", (json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n").encode(), ".json")
    return {"report": record, "scopeBasis": basis(store, state, "coverage"),
            "planDigest": digest([definition(t) for t in final.values()]),
            "tasksBefore": len(draft), "tasksAfter": len(final),
            "taskRoleStartsBefore": _role_starts(state, draft.values()),
            "taskRoleStartsAfter": _role_starts(state, final.values()),
            "taskRoleSlotsBefore": _role_estimate(state, draft.values()), "taskRoleSlotsAfter": _role_estimate(state, final.values()),
            "roleSlotNote": ROLE_SLOT_NOTE,
            "criticRoundsPerTask": _critic_rounds(state),
            "maximumCriticRoundsPerTask": 2, "actualCriticRounds": None, "measuredTokenSavings": None,
            **({"deliveryDigest": digest(report["delivery"])} if "delivery" in report else {})}


def valid(store, state):
    receipt = state.get("planReview")
    if state.get("policy", {}).get("deliveryPlan"):
        try:
            report = store.json(receipt["report"])
            delivery.validate(state["tasks"], report["delivery"], state=state)
            if receipt.get("deliveryDigest") != digest(report["delivery"]):
                return False
        except (OSError, ValueError, TypeError, KeyError):
            return False
    return bool(isinstance(receipt, dict) and artifact_valid(store, receipt.get("report", {}))
                and receipt.get("scopeBasis") == basis(store, state, "coverage")
                and receipt.get("planDigest") == digest([definition(t) for t in state["tasks"]]))
