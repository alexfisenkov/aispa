"""Diagnosis and worker selection within the existing two-round budget."""
import json

from .contracts import artifact_valid, nonempty, require
from .store import digest
from .review_policy import mode as review_mode, required_ids


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
    expected = required_ids(attempt["rounds"][0], review_mode(attempt))
    rows = report["addresses"]
    require(isinstance(rows, list) and all(isinstance(i, str) for i in rows)
            and len(set(rows)) == len(rows) and set(rows) == expected, "Диагноз должен учитывать все обязательные замечания")
    context = report["context"]
    require(isinstance(context, list) and len(context) <= 32
            and all(nonempty(s) and len(s) <= 1200 for s in context), "Контекст ремонта — до 32 адресных указаний")
    if report["cause"] in ("missing_context", "wrong_approach"):
        require(context, "Укажите, что новый исполнитель должен перечитать или проверить")
    return report


def epoch_history(store, state, tid):
    """Validate diagnosis provenance without rebasing any old receipt.

    Schema 1 has no recorded author. Schema 2 records the assigned worker as
    context, never as an inferred author, and binds the new diagnosis to an
    exact journal append and zone epoch.
    """
    from .team import _actor_valid
    from . import task_zone_extension
    attempt = _attempt(state, tid)
    refs = attempt.get("repairPlans", [])
    require(isinstance(refs, list), "История диагностики повреждена")
    seen, values = [], []
    for marker in store.task_timeline(tid):
        if marker["event"] not in {"repair-diagnosed:" + tid, "repair-plan-recorded:" + tid}:
            continue
        event, before = store.event_pair(marker["sequence"])
        history = event["state"].get("taskHistory", {}).get(tid, [])
        if not history or history[-1].get("id") != attempt["id"]:
            continue
        saved = history[-1]
        event_refs = saved.get("repairPlans", [])
        require(isinstance(event_refs, list)
                and saved.get("repairPlan") == (event_refs[-1] if event_refs else None),
                "История диагностики расходится с журналом")
        require(event_refs[:-1] == seen and len(event_refs) == len(seen) + 1
                and before and _attempt(before["state"], tid).get("repairPlans", []) == seen,
                "История диагностики потеряла прежние квитанции")
        ref = event_refs[-1]
        require(artifact_valid(store, ref), "История диагностики повреждена")
        value = store.json(ref)
        common = {"schema", "run", "task", "report"}
        require(value.get("run") == state["run"] and value.get("task") == tid,
                "Диагностика относится к другому прогону или задаче")
        _validate(event["state"], tid, value.get("report"))
        if value.get("schema") == 1:
            require(set(value) == common and not saved.get("zoneExtensions")
                    and saved["actors"]["worker"] == saved["submitted"]["actor"],
                    "Прежняя диагностика не соответствует исходной эпохе")
        else:
            require(value.get("schema") == 2 and set(value) == common | {
                        "attempt", "taskContract", "epoch", "previous", "recordedAfterSequence", "provenance"}
                    and value["attempt"] == attempt["id"]
                    and value["taskContract"] == saved["contract"]
                    == task_zone_extension.contract_at(attempt, event["sequence"])
                    and value["epoch"] == len(saved.get("zoneExtensions", [])) > 0
                    and value["previous"] == (seen[-1] if seen else None)
                    and value["recordedAfterSequence"] == event["sequence"] - 1
                    and value["provenance"] == {"author": None, "assignedWorker": saved["actors"]["worker"]}
                    and _actor_valid(store, event["state"], saved, "worker"),
                    "Диагностика новой эпохи потеряла provenance")
        seen, values = event_refs, [*values, value]
    require(refs == seen and attempt.get("repairPlan") == (refs[-1] if refs else None),
            "История диагностики не сохранена или изменена")
    return values


def record(store, state, tid, report):
    attempt = _attempt(state, tid)
    require(not attempt["terminal"] and len(attempt["rounds"]) == 1 and not attempt.get("repair"),
            "Диагностика записывается между первым критиком и сдачей ремонта")
    _validate(state, tid, report)
    epoch = bool(attempt.get("zoneExtensions"))
    previous = []
    if epoch:
        from . import task_zone_extension
        from .team import _actor_valid
        task_zone_extension.history_valid(store, state, tid, attempt)
        task_zone_extension._stage(next(t for t in state["tasks"] if t["id"] == tid), attempt)
        previous = epoch_history(store, state, tid)
        require(_actor_valid(store, state, attempt, "worker"), "Исполнитель диагностики новой эпохи повреждён")
        journal = store.read_journal()
        require(digest(journal["last"]["state"]) == digest(state), "Сначала сохраните состояние перед диагностикой")
    if attempt["actors"]["worker"] != attempt["submitted"]["actor"]:
        require(epoch and previous and previous[-1]["report"]["basis"] != basis(state, tid)
                and previous[-1]["report"]["route"] == report.get("route") == "fresh",
                "После назначения нового исполнителя диагностика заморожена до новой эпохи")
    payload = {"schema": 1, "run": state["run"], "task": tid, "report": report}
    if epoch:
        payload.update(schema=2, attempt=attempt["id"], taskContract=attempt["contract"],
                       epoch=len(attempt["zoneExtensions"]), previous=attempt.get("repairPlan"),
                       recordedAfterSequence=journal["last"]["sequence"],
                       provenance={"author": None, "assignedWorker": attempt["actors"]["worker"]})
    ref = store.artifact("repair-plans", (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(), ".json")
    attempt.setdefault("repairPlans", []).append(ref)
    attempt["repairPlan"] = ref
    if review_mode(attempt) == "focused" and not attempt.get("repairBaseline"):
        # The repair is re-checked by its own diff: snapshot the tree at the first diagnosis.
        records = {"records": store.code_records()}
        attempt["repairBaseline"] = store.artifact(
            "team-records", (json.dumps(records, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(), ".json")
    return ref


def read(store, state, tid):
    attempt = _attempt(state, tid)
    ref = attempt.get("repairPlan")
    require(isinstance(ref, dict) and attempt.get("repairPlans") and attempt["repairPlans"][-1] == ref,
            "До ремонта нужна записанная диагностика repair-plan")
    require(all(artifact_valid(store, r) for r in attempt["repairPlans"]), "История диагностики повреждена")
    if attempt.get("zoneExtensions"):
        values = epoch_history(store, state, tid)
        value = values[-1]
    else:
        value = store.json(ref)
    require(value.get("schema") in (1, 2) and value.get("run") == state["run"] and value.get("task") == tid,
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
    if attempt.get("zoneExtensions"):
        from .team import _actor_valid
        require(aid == attempt["actors"]["worker"] and _actor_valid(store, state, attempt, "worker", aid),
                "Ремонт новой эпохи требует действительного назначенного исполнителя")
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
    required = required_ids(attempt["rounds"][0], review_mode(attempt))
    return {"basis": basis(state, tid), "task": tid, "attempt": attempt["id"],
            "firstReport": attempt["rounds"][0]["report"],
            "blockingFindings": [f for f in attempt["rounds"][0]["findings"] if f["severity"] == "blocking"],
            "requiredFindingIds": sorted(required),
            "requiredFindings": [f for f in attempt["rounds"][0]["findings"] if f["id"] in required],
            "recorded": attempt.get("repairPlan"),
            "rule": "implementation_gap: same or fresh; missing_context/wrong_approach: fresh worker (full context; a strong model only for sensitive zones in lean runs); external_blocker: blocked. No extra critic round."}
