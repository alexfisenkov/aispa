"""Derived dashboard feed: history rows, current work and attempt summaries.

Everything here is computed from the journal tail and state only; nothing is invented.
"""
from .work_progress import LABELS as WORK_PHASE_LABELS

EVENT_LABELS = {
    "work-progress-began": "Начат этап работы", "work-progress-ended": "Завершён интервал работы",
    "init": "Начало прогона", "runtime-pinned": "Среда выполнения зафиксирована",
    "brief-added": "Бриф сохранён", "brief-sealed": "Бриф зафиксирован",
    "brief-extract-recorded": "Записана выжимка брифа", "brief-review-recorded": "Записана независимая сверка брифа",
    "brief-map-recorded": "Записана карта брифа", "requirements-added": "Требования добавлены",
    "agent-decision-recorded": "Записано решение агента", "owner-scope-decision": "Решение владельца по объёму",
    "owner-task-attempt": "Новая попытка задачи по слову владельца",
    "owner-answer-awaited": "Ожидается ответ владельца", "owner-answer-received": "Ответ владельца получен",
    "spec-recorded": "Спецификация записана", "scenarios-recorded": "Сценарии записаны",
    "interfaces-recorded": "Интерфейсы записаны", "coverage-reviewed": "Проверено покрытие требований",
    "plan-validated": "План проверен", "team-prepared": "Команда подготовлена",
    "team-specialized": "Роли команды уточнены", "task-cards-refreshed": "Карточки задач обновлены",
    "participant-assigned": "Назначен участник", "actor-assigned": "Назначен участник задачи",
    "knowledge-writer-assigned": "Назначен хранитель знаний",
    "task-started": "Началась задача", "task-submitted": "Задача передана критикам",
    "task-critic-1": "Критик · круг 1", "task-critic-2": "Критик · круг 2",
    "task-critic-accepted": "Критик принял задачу", "task-repaired": "Исправления переданы на проверку",
    "repair-diagnosed": "Диагностика исправления", "task-accepted": "Задача принята",
    "task-blocked": "Задача заблокирована", "task-deferred": "Задача отложена",
    "task-extra-wave": "Дополнительная волна задачи", "task-promoted": "Задача переведена в тяжёлый класс",
    "task-verified-completed": "Выполнение лёгкой задачи проверено", "tasks-reset": "Задачи сброшены",
    "check-mode-changed": "Режим проверок изменён",
    "wave-reviewed": "Весь бриф сверен после волны", "acceptance-reviewed": "Проверена итоговая приёмка",
    "project-integration-reviewed": "Сохранено общее ревью проекта",
    "project-integration-lens-reviewed": "Проверена одна из линз общего ревью ветки", "delivery-recorded": "Результат сдан",
    "verification-started": "Запущена проверка кода", "verification-executed": "Проверка кода выполнена",
    "verification-could-not-start": "Проверку кода не удалось запустить", "execution-recorded": "Записан прогон",
    "execution-obligation-recorded": "Записано обязательство прогона",
    "clean-clone-prepared": "Подготовлена чистая копия", "clean-verification-started": "Запущена проверка в чистой копии",
    "clean-verification-recorded": "Результат проверки в чистой копии записан",
    "test-intent-recorded": "Записан замысел тестов", "blind-observations-recorded": "Записаны слепые наблюдения",
    "idea-reviewed": "Идея рассмотрена", "run-paused": "Работа приостановлена", "run-resumed": "Работа продолжена",
    "run-blocked": "Работа заблокирована", "run-cancelled": "Прогон отменён",
    "recovery-blocked": "Восстановление журнала требует исправления",
    "project-document-bound": "Сопоставлен исходный документ проекта",
    "inherited-obligation-keep": "Сохранено обязательство прошлого прогона",
    "inherited-obligation-map": "Обязательство прошлого прогона связано с требованием",
    "inherited-obligation-resolve": "Закрыто обязательство прошлого прогона",
    "inherited-obligation-drop": "Обязательство прошлого прогона отменено по решению владельца",
    "inherited-obligation-defer": "Обязательство прошлого прогона отложено по решению владельца",
    "recovery-reconciled": "Восстановление журнала проверено",
    "recovery-verification-executed": "Проверка восстановления выполнена",
    "project-context-initialized": "Контекст проекта создан", "project-context-adapted": "Контекст проекта обновлён",
    "project-discovery-collected": "Собраны сведения о проекте", "project-discovery-recorded": "Сведения о проекте записаны",
    "project-knowledge-recorded": "Знания о проекте записаны", "project-knowledge-checkpointed": "Знания о проекте сохранены",
    "project-handoff-recorded": "Передача проекта записана", "document-metadata-registered": "Документ зарегистрирован",
    "document-metadata-refreshed": "Метаданные документа обновлены",
    "native-usage-recorded": "Записано использование среды", "native-usage-imported": "Импортировано использование среды",
}
# Frequent bookkeeping events: kept in the feed, but never counted as "what the agent is doing".
NOISE = {"native-usage-recorded", "native-usage-imported", "execution-recorded", "verification-started",
         "verification-executed", "document-metadata-registered",
         "document-metadata-refreshed"}
ROLE_LABELS = {"worker": "Исполнитель", "critic1": "Критик · круг 1", "critic2": "Критик · круг 2",
               "acceptor": "Приёмщик задачи", "coordinator": "Координатор", "idea": "Разбор идеи",
               "source-reader": "Читатель источников", "brief-analyst": "Аналитик брифа",
               "brief-reviewer": "Проверяющий бриф", "branch-reviewer": "Проверяющий всю ветку",
               "integration-reviewer": "Проверяющий проект", "recovery-reviewer": "Проверяющий восстановление",
               "discovery": "Исследователь проекта", "coverage": "Проверяющий покрытие брифа",
               "planner": "Планировщик", "blind": "Итоговый независимый приёмщик",
               "knowledge-writer": "Хранитель знаний"}
DOCUMENT_ROLE_LABELS = {"interview": "Интервью", "intent": "Замысел проекта", "spec": "Спецификация",
                        "plan": "План", "index": "Карта проекта"}
FEED_ROWS = 16
BRIEF_WORK = {
    "brief-added": "Разбор нового сообщения владельца",
    "owner-task-attempt": "Новая попытка задачи по слову владельца: разбор брифа",
    "owner-answer-received": "Ответ владельца получен: разбор брифа",
    "brief-extract-recorded": "Независимая сверка брифа",
    "brief-review-recorded": "Карта брифа и требования",
    "brief-map-recorded": "Требования и ядро по карте брифа",
    "requirements-added": "Требования добавлены; ожидается фиксация брифа",
    "agent-decision-recorded": "Решение агента записано; подготовка брифа продолжается",
}
STAGE_WORK = {"spec": "Подготовка спецификации", "coverage": "Сверка покрытия требований",
              "plan": "Проверка плана задач", "acceptance": "Итоговая приёмка"}
REASONS = {"tier-escalation": "повышение класса", "check-mode": "смена режима проверок",
           "extra-wave": "дополнительная волна", None: "по словам владельца"}
OUTCOMES = {"accepted": "принята", "verified": "выполнение проверено", "blocked": "блок",
            "pass": "PASS критика", "stopped": "остановлена", "incomplete": "не завершена"}


def _label(name):
    return EVENT_LABELS.get(name) or "Событие: " + name


def _target(target):
    # Only identifiers, not arbitrary note/brief content, enter the feed.
    return " · " + target if target and len(target) <= 32 and target.replace("-", "").isalnum() else ""


def _event_text(row):
    name, target = row["name"], row["target"]
    if name == "actor-assigned":
        task, _separator, role = target.partition(":")
        label = "Назначение: " + ROLE_LABELS[role] if role in ("worker", "critic1", "critic2", "acceptor") else _label(name)
        return label + _target(task)
    if name == "participant-assigned":
        return "Назначение: " + ROLE_LABELS[target] if target in ROLE_LABELS else _label(name)
    if name in ("work-progress-began", "work-progress-ended"):
        return _label(name) + (" · " + WORK_PHASE_LABELS[target] if target in WORK_PHASE_LABELS else "")
    if name == "project-document-bound":
        return _label(name) + (" · " + DOCUMENT_ROLE_LABELS[target] if target in DOCUMENT_ROLE_LABELS else "")
    if name == "tasks-reset" and target:
        targets = [item for item in target.split(",") if _target(item)]
        return _label(name) + (" · " + ", ".join(targets[:8]) if targets else "") + (" …" if len(targets) > 8 else "")
    return _label(name) + _target(target)


def tail(store):
    return [{"name": row["event"].partition(":")[0], "target": row["event"].partition(":")[2], "at": row["at"]}
            for row in store.read_journal().get("eventTail", [])]


def events(rows):
    """Newest rows last. Equal neighbouring events collapse into one row with a counter; none is dropped."""
    grouped = []
    for row in rows:
        key = (row["name"], row["target"])
        if grouped and grouped[-1]["key"] == key:
            grouped[-1]["count"] += 1
            grouped[-1]["at"] = row["at"]
            continue
        grouped.append({"key": key, "type": row["name"], "at": row["at"], "count": 1,
                        "text": _event_text(row)})
    result = []
    for item in grouped[-FEED_ROWS:]:
        text = item["text"] + (" ×%d" % item["count"] if item["count"] > 1 else "")
        result.append({"type": item["type"], "at": item["at"], "text": text, "count": item["count"]})
    return result


def current_work(state, stage, rows):
    """What the agent is doing now, derived from the stage and the last meaningful event."""
    significant = [row for row in rows if row["name"] not in NOISE]
    last = significant[-1] if significant else None
    title, note = None, None
    pending_tasks = [t for t in state["tasks"] if t["status"] != "completed"]
    if stage in ("brief", "requirements"):
        match = next((row["name"] for row in reversed(significant) if row["name"] in BRIEF_WORK), None)
        title = BRIEF_WORK.get(match) or "Подготовка брифа и требований"
        if state["tasks"]:
            note = "Уточнение брифа: план сохранён, задачи продолжатся после пересогласования"
    elif stage in STAGE_WORK:
        title = STAGE_WORK[stage]
        if pending_tasks and stage != "acceptance":
            note = "Задачи ждут завершения подготовки"
    elif stage == "build":
        if not pending_tasks and state["tasks"]:
            title = "Задачи реализованы; подготовка итоговой проверки"
    return {"title": title, "note": note,
            "lastActionAt": last["at"] if last else None,
            "lastAction": _event_text(last) if last else None}


def _outcome(attempt):
    rounds = attempt.get("rounds") or []
    if attempt.get("acceptance"):
        return "accepted"
    if attempt.get("verifiedCompletion"):
        return "verified"
    if rounds and not rounds[-1].get("passed"):
        return "blocked"
    if rounds:
        return "pass"
    return "stopped" if attempt.get("terminal") else "incomplete"


def attempts(history):
    """Attempt number and a short summary of earlier attempts; old states without history give 1/[]."""
    history = [a for a in (history or []) if isinstance(a, dict)]
    previous = []
    for index, attempt in enumerate(history[:-1]):
        following = (history[index + 1].get("authorization") or {}).get("kind")
        previous.append({"number": index + 1, "criticRounds": len(attempt.get("rounds") or []),
                         "outcome": _outcome(attempt), "followedBy": following or "owner",
                         "reason": REASONS.get(following, REASONS[None])})
    note = None
    if previous:
        last = previous[-1]
        critic = "критик ×%d" % last["criticRounds"] if last["criticRounds"] else "без критика"
        note = "Попытка %d · прошлая: %s, %s → новая %s" % (len(history), critic, OUTCOMES[last["outcome"]], last["reason"])
    return {"attemptNumber": max(1, len(history)), "previousAttempts": previous, "attemptNote": note}
