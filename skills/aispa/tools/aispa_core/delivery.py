"""Review complete outcomes and ordered, reversible migration steps."""
from .contracts import nonempty, overlap, require, task_closure
from .store import canonical_zone


def _text(value):
    return nonempty(value) and len(value) <= 2400


def _ids(value, known, *, empty=False):
    require(isinstance(value, list) and (value or empty)
            and all(isinstance(i, str) and i in known for i in value)
            and len(value) == len(set(value)), "Нужны известные неповторяющиеся ID")
    return set(value)


def _checks(rows, tasks, needed):
    require(isinstance(rows, list) and rows, "Нужны проверки совместимости и восстановления для каждого шага миграции")
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"task", "criterion"}, "Проверка должна ссылаться на критерий задачи")
        tid = row["task"]
        require(isinstance(tid, str) and tid in needed and row["criterion"] in tasks[tid]["criteria"],
                "Критерий миграции отсутствует в соответствующей задаче")
        require((tid, row["criterion"]) not in seen, "Повторная проверка миграции")
        seen.add((tid, row["criterion"]))
    require({tid for tid, _ in seen} == needed, "Не каждый шаг миграции проверяет совместимость и восстановление")
    return seen


def validate(tasks, report):
    require(isinstance(report, dict) and set(report) == {
        "profile", "reason", "slices", "migrations", "migrationReason"},
        "delivery: нужны profile, reason, slices, migrations, migrationReason")
    require(report["profile"] in ("small", "feature", "system", "migration") and _text(report["reason"]),
            "Объясните глубину планирования по размеру, связанности и риску проекта")
    tasks = {t["id"]: t for t in tasks}
    slices = report["slices"]
    require(isinstance(slices, list) and 0 < len(slices) <= len(tasks), "План требует проверяемых сквозных результатов")
    known, covered, supports = {}, set(), []
    for row in slices:
        require(isinstance(row, dict) and set(row) == {
            "id", "kind", "outcome", "tasks", "demonstration", "supports"},
            "Срезу нужны id, kind, outcome, tasks, demonstration, supports")
        sid = row["id"]
        require(_text(sid) and sid not in known and row["kind"] in ("user", "enabler") and _text(row["outcome"]),
                "Объясните наблюдаемый результат среза или необходимой инфраструктуры")
        tids = _ids(row["tasks"], tasks)
        require(not covered & tids, "Задача включена в несколько срезов")
        demo = row["demonstration"]
        require(isinstance(demo, dict) and set(demo) == {"task", "criterion"}
                and isinstance(demo["task"], str) and demo["task"] in tids
                and demo["criterion"] in tasks[demo["task"]]["criteria"],
                "Демонстрация результата должна быть критерием задачи этого среза")
        known[sid] = row
        covered |= tids
        supports.append((row, row["supports"]))
    require(covered == set(tasks), "Не все задачи входят в проверяемые результаты")
    require(any(s["kind"] == "user" for s in slices), "Нужен хотя бы один результат для пользователя проекта")
    for row, ids in supports:
        selected = _ids(ids, known, empty=row["kind"] == "user")
        require((not selected if row["kind"] == "user" else all(known[i]["kind"] == "user" for i in selected)),
                "Вспомогательный срез должен объяснять, какие пользовательские результаты обеспечивает")

    migrations = report["migrations"]
    require(isinstance(migrations, list) and len(migrations) <= 32 and _text(report["migrationReason"]),
            "Укажите миграции либо конкретную причину их отсутствия")
    require(report["profile"] != "migration" or migrations, "Профиль migration требует последовательности миграции")
    seen = set()
    for row in migrations:
        require(isinstance(row, dict) and set(row) == {
            "id", "reason", "expand", "migrate", "contract", "consumers", "compatibility", "rollback"},
            "Миграции нужны шаги expand/migrate/contract, потребители, проверки совместимости и восстановления")
        require(_text(row["id"]) and row["id"] not in seen and _text(row["reason"]), "Повторная или необъяснённая миграция")
        seen.add(row["id"])
        expand, migrate, contract = (_ids(row[k], tasks) for k in ("expand", "migrate", "contract"))
        require(not (expand & migrate or expand & contract or migrate & contract),
                "Добавление нового, перенос потребителей и удаление старого должны иметь отдельные границы проверки")
        for later, earlier in ((migrate, expand), (contract, migrate)):
            for tid in later:
                ancestors = {t["id"] for t in task_closure({"tasks": list(tasks.values())}, tid)} - {tid}
                require(earlier <= ancestors, "Нельзя переносить потребителей до добавления нового или удалять старое до переноса всех")
        consumers = row["consumers"]
        require(isinstance(consumers, list) and consumers and len(consumers) <= 256, "Перечислите затронутых потребителей")
        paths, moved = set(), set()
        for consumer in consumers:
            require(isinstance(consumer, dict) and set(consumer) == {"path", "task"}, "Потребителю нужны path и задача переноса")
            path, tid = consumer["path"], consumer["task"]
            require(isinstance(path, str) and path and path == canonical_zone(path) and path != "."
                    and path not in paths and isinstance(tid, str) and tid in migrate,
                    "Потребитель должен иметь безопасный путь и задачу migrate")
            require(overlap([path], tasks[tid]["zones"]), "Перенос потребителя отсутствует в зонах записи задачи")
            paths.add(path); moved.add(tid)
        require(moved == migrate, "Каждая задача migrate должна переносить известного потребителя")
        compatible = _checks(row["compatibility"], tasks, expand | migrate | contract)
        recoverable = _checks(row["rollback"], tasks, expand | migrate | contract)
        require(not compatible & recoverable, "Проверка совместимости не заменяет отдельный критерий восстановления")
    return report
