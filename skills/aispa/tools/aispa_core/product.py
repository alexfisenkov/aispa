"""Explicit product challenges; structural checks never certify market facts."""
from .contracts import active, nonempty, require

QUESTIONS = {
    "adoption": "Почему человек начнёт пользоваться этим, что он делает сейчас и что проверим, если пользователей не окажется?",
    "value_cost": "Какую измеримую пользу получит пользователь; оправдывает ли она создание, поддержку и регулярные расходы?",
    "operations": "Кто поставляет данные, обслуживает результат и исправляет сбои; что происходит без этого человека или доступа?",
    "lifecycle": "Что произойдёт при повторном использовании и на второй неделе: пустые, старые, ошибочные данные и возвращение пользователя?",
    "actors": "Кто ещё участвует или зависит от результата: оператор, администратор, получатель; какие у них права и конфликтующие интересы?",
}


def validate(state, rows):
    require(isinstance(rows, list) and len(rows) == len(QUESTIONS),
            "Разбор идеи требует пять явных продуктовых проверок challenge")
    ids = {r["id"] for r in active(state)}
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == {
            "probe", "status", "finding", "decision", "validation", "requirements"},
            "challenge: нужны probe, status, finding, decision, validation, requirements")
        key = row["probe"]
        require(isinstance(key, str) and key in QUESTIONS and key not in seen,
                "Неизвестная или повторная продуктовая проверка")
        seen.add(key)
        require(row["status"] in ("resolved", "assumption", "blocker", "not_applicable"),
                "challenge: неизвестный статус; предположение не является фактом")
        require(all(nonempty(row[f]) and len(row[f]) <= 1600 for f in ("finding", "decision", "validation")),
                "Каждой проверке нужны наблюдение, решение и способ проверки либо конкретная причина неприменимости")
        refs = row["requirements"]
        require(isinstance(refs, list) and refs and all(isinstance(i, str) and i in ids for i in refs)
                and len(set(refs)) == len(refs),
                "Свяжите продуктовую проверку с действующими требованиями; их отмена здесь невозможна")


def coverage(state, report, spec, challenge):
    """Independent spec reviewer must locate every chosen decision/uncertainty."""
    validate(state, challenge)
    choices = {row["probe"]: row for row in challenge}
    rows = report.get("ideaChecks")
    require(isinstance(rows, list) and len(rows) == len(QUESTIONS),
            "Сверка спецификации требует ideaChecks по пяти продуктовым проверкам")
    seen, passed = set(), True
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"probe", "status", "specQuote", "note"},
                "ideaChecks: нужны probe, status, specQuote, note")
        key = row["probe"]
        require(isinstance(key, str) and key in QUESTIONS and key not in seen,
                "Неизвестная или повторная проверка идеи в спецификации")
        seen.add(key)
        require(row["status"] in ("covered", "missing", "partial") and nonempty(row["note"]),
                "Укажите результат и смысловую связь решения с разбором идеи")
        if row["status"] == "covered":
            require(nonempty(row["specQuote"]) and row["specQuote"] in spec,
                    "Нет точной цитаты решения, гипотезы или препятствия в спецификации")
            require(all(choices[key][field] in row["specQuote"] for field in ("decision", "validation")),
                    "Цитата должна сохранять выбранное решение и способ проверки из challenge; смысл проверяет независимый рецензент")
        else:
            passed = False
    return passed
