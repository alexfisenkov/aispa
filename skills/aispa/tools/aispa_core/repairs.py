"""Diagnosis and worker selection within the existing two-round budget."""
import json

from .contracts import artifact_valid, nonempty, require
from .store import digest
from .review_policy import required_ids


def _lean(state):
    """Lean runs use cheap models everywhere: a repair needs a fresh context, not a strong model."""
    return state.get("policy", {}).get("leanControl") is True


def _attempt(state, tid):
    history = state.get("taskHistory", {}).get(tid, [])
    require(history, "Неизвестная попытка задачи")
    return history[-1]


def basis(state, tid):
    attempt = _attempt(state, tid)
    rounds = attempt.get("rounds", [])
    require(rounds and not rounds[0]["passed"], "Диагностика нужна после замечаний первого критика")
    return digest({"attempt": attempt["id"], "contract": attempt["contract"],
                   "first": rounds[0]["report"], "worker": attempt["submitted"]["actor"]})


def _validate(state, tid, report):
    attempt = _attempt(state, tid)
    require(isinstance(report, dict) and set(report) == {
        "basis", "cause", "diagnosis", "route", "reason", "changes", "addresses", "context"},
        "repair-plan: нужны basis, cause, diagnosis, route, reason, changes, addresses, context")
    require(report["basis"] == basis(state, tid), "Диагностика относится к другой попытке или замечаниям")
    require(report["cause"] in ("implementation_gap", "missing_context", "wrong_approach", "external_blocker")
            and report["route"] in ("same", "fresh", "blocked"), "Неизвестная причина или маршрут ремонта")
    require(all(nonempty(report[f]) and len(report[f]) <= 2400 for f in ("diagnosis", "reason", "changes")),
            "Нужны диагноз, обоснование исполнителя и конкретное изменение подхода")
    if report["cause"] in ("missing_context", "wrong_approach"):
        require(report["route"] == "fresh", "Недостающий контекст или неверный подход требуют нового исполнителя с полным контекстом"
                if _lean(state) else "Недостающий контекст или неверный подход требуют свежего сильного исполнителя")
    if report["route"] == "same":
        require(report["cause"] == "implementation_gap", "Тот же исполнитель подходит для локального дефекта реализации")
    if report["cause"] == "external_blocker":
        require(report["route"] == "blocked", "Внешнее препятствие надо сохранить; ремонт не заменяет недостающий доступ")
    expected = required_ids(attempt["rounds"][0])
    rows = report["addresses"]
    require(isinstance(rows, list) and all(isinstance(i, str) for i in rows)
            and len(set(rows)) == len(rows) and set(rows) == expected, "Диагноз должен учитывать все обязательные замечания")
    context = report["context"]
    require(isinstance(context, list) and len(context) <= 32
            and all(nonempty(s) and len(s) <= 1200 for s in context), "Контекст ремонта — до 32 адресных указаний")
    if report["cause"] in ("missing_context", "wrong_approach"):
        require(context, "Укажите, что новый исполнитель должен перечитать или проверить")
    return report


def record(store, state, tid, report):
    attempt = _attempt(state, tid)
    require(not attempt["terminal"] and len(attempt["rounds"]) == 1 and not attempt.get("repair"),
            "Диагностика записывается между первым критиком и сдачей ремонта")
    require(attempt["actors"]["worker"] == attempt["submitted"]["actor"],
            "После назначения нового исполнителя диагностика заморожена")
    _validate(state, tid, report)
    payload = {"schema": 1, "run": state["run"], "task": tid, "report": report}
    ref = store.artifact("repair-plans", (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(), ".json")
    attempt.setdefault("repairPlans", []).append(ref)
    attempt["repairPlan"] = ref
    return ref


def read(store, state, tid):
    attempt = _attempt(state, tid)
    ref = attempt.get("repairPlan")
    require(isinstance(ref, dict) and attempt.get("repairPlans") and attempt["repairPlans"][-1] == ref,
            "До ремонта нужна записанная диагностика repair-plan")
    require(all(artifact_valid(store, r) for r in attempt["repairPlans"]), "История диагностики повреждена")
    value = json.loads(store.text(ref))
    require(value.get("schema") == 1 and value.get("run") == state["run"] and value.get("task") == tid,
            "Диагностика относится к другому прогону или задаче")
    return _validate(state, tid, value.get("report"))


def assignment_guard(store, state, tid, *, fresh, model):
    report = read(store, state, tid)
    attempt = _attempt(state, tid)
    if _lean(state):  # the model ID is optional; a strong one is advised only for sensitive zones
        require(report["route"] == "fresh" and fresh is True,
                "Замена требует маршрута fresh и нового исполнителя с полным контекстом")
    else:
        require(report["route"] == "fresh" and fresh is True and nonempty(model),
                "Замена требует маршрута fresh, независимого контекста и реального ID сильной модели среды")
    require(attempt["actors"]["worker"] == attempt["submitted"]["actor"],
            "Новый исполнитель ремонта уже назначен; это не новый круг попыток")


def worker_guard(store, state, tid, actor=None):
    report = read(store, state, tid)
    attempt = _attempt(state, tid)
    original = attempt["submitted"]["actor"]
    aid = actor or attempt["actors"]["worker"]
    require(report["route"] != "blocked", "Внешнее препятствие ещё не снято")
    if report["route"] == "same":
        require(aid == original, "Ремонт должен выполнять исходный исполнитель согласно диагнозу")
    else:
        agent = state.get("actors", {}).get(aid, {})
        if _lean(state):
            require(aid != original and agent.get("fresh") is True,
                    "Нужен новый исполнитель с полным контекстом")
        else:
            require(aid != original and agent.get("fresh") is True and nonempty(agent.get("model")),
                    "Нужен новый исполнитель с независимым контекстом и записанной моделью")
    return attempt["repairPlan"]


def view(store, state, tid):
    attempt = _attempt(state, tid)
    return {"basis": basis(state, tid), "task": tid, "attempt": attempt["id"],
            "firstReport": attempt["rounds"][0]["report"],
            "blockingFindings": [f for f in attempt["rounds"][0]["findings"] if f["severity"] == "blocking"],
            "requiredFindingIds": sorted(required_ids(attempt["rounds"][0])),
            "requiredFindings": [f for f in attempt["rounds"][0]["findings"] if f["id"] in required_ids(attempt["rounds"][0])],
            "recorded": attempt.get("repairPlan"),
            "rule": "implementation_gap: same or fresh; missing_context/wrong_approach: fresh worker (full context; a strong model only for sensitive zones in lean runs); external_blocker: blocked. No extra critic round."}
