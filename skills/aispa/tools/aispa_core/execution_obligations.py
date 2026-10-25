"""Presealed deadline obligations for engine finish or external host handoff.

These are never product waivers. External handoff remains pending: the engine
cannot attest a host message that has not happened yet.
"""
import copy
from datetime import datetime, timedelta, timezone
import json
import re

from .contracts import artifact_valid, require
from .store import Invalid, timestamp

EVENTS = frozenset({"engine-finish", "host-final-handoff"})
FIELDS = {"id", "kind", "event", "brief", "quote", "startedAt", "startEvidence", "deadline"}


def enabled(state):
    return bool(state.get("policy", {}).get("executionObligations"))


def instant(value):
    require(isinstance(value, str), "Нужно время ISO 8601 с часовым поясом")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Invalid("Нужно время ISO 8601 с часовым поясом") from error
    require(result.tzinfo is not None and result.utcoffset() is not None, "У времени нет часового пояса")
    return result



ISO_LITERAL = re.compile(r"(?<![0-9])\d{4}-\d{2}-\d{2}[Tt ]\d{2}(?::\d{2}(?::\d{2}(?:\.\d+)?)?)?(?:[Zz]|[+-]\d{2}(?::?\d{2})?)?(?![0-9])")


def literal_timing(quote, event, deadline=None, *, started_at=None):
    """Check explicit tokens only; this does not claim general language meaning."""
    require(isinstance(event, str) and event in EVENTS, "Неизвестное событие исполнения")
    require(isinstance(quote, str) and quote.strip(), "Нужна исходная цитата срока")
    tokens = {token for token in EVENTS if re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", quote)}
    require(not tokens or tokens == {event}, "Событие исполнения не совпадает с буквальным событием в цитате")
    literals = ISO_LITERAL.findall(quote)
    if deadline is None or not literals:
        return
    targets = [instant(deadline)]
    if started_at is not None:
        targets.append(instant(started_at))
    matches = []
    for text in literals:
        try:
            value = datetime.fromisoformat(text.upper().replace("Z", "+00:00"))
        except ValueError as error:
            raise Invalid("В исходной цитате некорректное ISO-время") from error
        matches.append(any(value == target if value.tzinfo is not None else value == target.replace(tzinfo=None)
                           for target in targets))
    require(any(matches), "Deadline не совпадает ни с одним буквальным ISO-временем исходной цитаты")


def annotation(value, quote):
    """Normalize an independent source reader's explicit timing interpretation."""
    require(isinstance(value, dict) and set(value) in ({"event", "deadline"}, {"event", "budgetSeconds"}),
            "execution требует event и ровно одно из deadline/budgetSeconds")
    event = value.get("event")
    require(isinstance(event, str) and event in EVENTS, "execution.event должен быть engine-finish или host-final-handoff")
    if "deadline" in value:
        deadline = instant(value["deadline"]).astimezone(timezone.utc).isoformat()
        literal_timing(quote, event, deadline)
        return {"event": event, "deadline": deadline}
    budget = value["budgetSeconds"]
    require(type(budget) is int and budget > 0, "execution.budgetSeconds должен быть положительным целым числом")
    literal_timing(quote, event)
    return {"event": event, "budgetSeconds": budget}


def bind_timing(obligation, analyst, reviewer):
    left = annotation(analyst, obligation["quote"])
    right = annotation(reviewer, obligation["quote"])
    require(left == right, "Независимые исходные разборы расходятся в событии или сроке исполнения")
    require(obligation["event"] == left["event"], "Событие ledger отличается от двух исходных разборов")
    deadline = instant(obligation["deadline"])
    if "deadline" in left:
        require(deadline == instant(left["deadline"]), "Deadline ledger отличается от двух исходных разборов")
    else:
        try:
            duration = timedelta(seconds=left["budgetSeconds"])
        except OverflowError as error:
            raise Invalid("Слишком большой budgetSeconds") from error
        require(deadline - instant(obligation["startedAt"]) == duration,
                "Deadline-startedAt не совпадает с независимо прочитанным budgetSeconds")
    literal_timing(obligation["quote"], obligation["event"], obligation["deadline"],
                   started_at=obligation["startedAt"] if "budgetSeconds" in left else None)
    return left


def report_timing(obligation, analyst, reviewer):
    """Bind one exact source unit from each previously saved independent report."""
    require(isinstance(analyst, dict) and isinstance(reviewer, dict)
            and isinstance(analyst.get("actor"), str) and isinstance(reviewer.get("actor"), str)
            and analyst["actor"] != reviewer["actor"], "Нужны два разных автора исходного разбора срока")
    annotations = []
    for report in (analyst, reviewer):
        require(isinstance(report.get("units"), list), "Нет исходных фрагментов срока")
        rows = [unit for unit in report["units"] if isinstance(unit, dict)
                and unit.get("brief") == obligation["brief"] and unit.get("text") == obligation["quote"]]
        require(len(rows) == 1 and rows[0].get("kind") == "intent", "Нужен единственный точный исходный фрагмент срока")
        annotations.append(rows[0].get("execution"))
    return bind_timing(obligation, *annotations)


def source_timing(store, state, rows, refs):
    from . import participants
    require(isinstance(refs, dict) and set(refs) == {"extraction", "review"}, "Нет двух исходных отчётов срока")
    reports = []
    for key, role in (("extraction", "brief-analyst"), ("review", "brief-reviewer")):
        require(artifact_valid(store, refs[key]), "Исходный отчёт срока повреждён")
        saved = store.json(refs[key])
        require(isinstance(saved, dict) and saved.get("schema") == 1 and saved.get("run") == state["run"]
                and isinstance(saved.get("report"), dict), "Чужой исходный отчёт срока")
        report = saved["report"]
        require(participants.actor_check(store, state, report.get("actor"), role), "Автор исходного отчёта срока не подтверждён")
        reports.append(report)
    for row in rows:
        report_timing(row, *reports)

def _validate(store, state, value):
    require(isinstance(value, dict) and set(value) == FIELDS, "Обязательству нужны только id, kind, event, brief, quote, startedAt, startEvidence, deadline")
    require(isinstance(value["id"], str) and re.fullmatch(r"E\d{2,}", value["id"]), "ID обязательства: E01")
    require(value["kind"] == "deadline" and isinstance(value["event"], str) and value["event"] in EVENTS,
            "Допустим только deadline для engine-finish или host-final-handoff")
    source = next((b for b in state["briefs"] if b["id"] == value["brief"]), None)
    require(source and artifact_valid(store, source) and isinstance(value["quote"], str)
            and value["quote"].strip() and value["quote"] in store.text(source),
            "Обязательству нужна точная цитата действующего брифа")
    # At record time the independent interpretation is not sealed yet. An ISO
    # literal may name the observed start; bind_timing later distinguishes an
    # absolute deadline from a relative budget before any acceptance is valid.
    literal_timing(value["quote"], value["event"], value["deadline"], started_at=value["startedAt"])
    evidence = value["startEvidence"]
    require(isinstance(evidence, dict) and isinstance(evidence.get("file"), str)
            and evidence["file"].startswith("evidence/") and artifact_valid(store, evidence),
            "Нужно неизменное свидетельство начала из evidence/")
    saved = store.json(evidence)
    require(isinstance(saved, dict) and saved.get("event") == "execution-start"
            and saved.get("at") == value["startedAt"] and isinstance(saved.get("note"), str)
            and saved["note"].strip(), "Свидетельство начала требует event=execution-start, at и note")
    require(instant(value["startedAt"]) <= instant(value["deadline"]), "Срок раньше начала исполнения")
    require(instant(value["startedAt"]) <= instant(timestamp()), "Начало исполнения находится в будущем")
    # Existing product classifications stay product requirements, including core.
    require(not any(r.get("brief") == value["brief"] and
                    (value["quote"] in r.get("quote", "") or r.get("quote", "") in value["quote"])
                    for r in state["requirements"] if r.get("kind") != "addition"),
            "Исходное продуктовое требование нельзя переклассифицировать в обязательство исполнения")


def record(store, state, value):
    require(enabled(state), "Execution obligations не включены для этого прогона")
    require(state.get("status") == "running" and not state.get("sealed")
            and not state.get("executionSeal") and not state.get("spec") and not state.get("tasks"),
            "Обязательства исполнения фиксируются только до первой фиксации брифа")
    _validate(store, state, value)
    rows = state.get("executionObligations", [])
    require(isinstance(rows, list) and value["id"] not in {r["id"] for r in rows},
            "Нельзя заменить существующее обязательство исполнения")
    result = copy.deepcopy(value)
    result.update(run=state["run"], recordedAt=timestamp())
    result["record"] = store.artifact("execution-obligations", (json.dumps(result, sort_keys=True) + "\n").encode(), ".json")
    state["executionObligations"] = [*rows, result]
    return copy.deepcopy(result)


def obligations(store, state, *, sealed=False):
    if not enabled(state):
        return []
    rows = state.get("executionObligations", [])
    require(isinstance(rows, list), "Повреждён список обязательств исполнения")
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and row.get("run") == state["run"]
                and artifact_valid(store, row.get("record", {})), "Повреждено обязательство исполнения")
        require(store.json(row["record"]) == {k: v for k, v in row.items() if k != "record"},
                "Обязательство исполнения изменено после записи")
        _validate(store, state, {k: row[k] for k in FIELDS})
        require(row["id"] not in seen, "Повторный ID обязательства исполнения")
        seen.add(row["id"])
    if sealed:
        seal = state.get("executionSeal")
        require(seal and artifact_valid(store, seal), "Обязательства исполнения не зафиксированы до разработки")
        value = store.json(seal)
        require(value.get("run") == state["run"] and value.get("obligations") == rows,
                "Состав обязательств изменён после фиксации")
        require(not rows or artifact_valid(store, value.get("briefMap", {})),
                "Исходная независимая сверка обязательств повреждена")
        if rows:
            source_timing(store, state, rows, value.get("sourceReports"))
    return rows


def seal(store, state):
    if not enabled(state):
        return None
    rows = obligations(store, state)
    from . import briefmap, participants
    require(not rows or state.get("policy", {}).get("sourceAccounting") and
            briefmap.valid(store, state, lambda s, a, r: participants.actor_check(store, s, a, r)),
            "Обязательства требуют полного исходного учёта и независимого brief-map до seal")
    if state.get("executionSeal"):
        obligations(store, state, sealed=True)
        return state["executionSeal"]
    refs = {"extraction": state.get("briefExtraction"), "review": state.get("briefReview")}
    if rows:
        source_timing(store, state, rows, refs)
    value = {"run": state["run"], "obligations": rows, "briefMap": state.get("briefMap"), "at": timestamp()}
    if rows:
        value["sourceReports"] = refs
    return store.artifact("execution-seals", (json.dumps(value, sort_keys=True) + "\n").encode(), ".json")


def evaluate(store, state, at):
    """One actual finish timestamp; no prediction and no reinterpretation of event."""
    result = []
    for row in obligations(store, state, sealed=bool(state.get("executionObligations"))):
        deadline = instant(row["deadline"])
        reached = instant(at)
        status = ("passed" if reached <= deadline else "failed") if row["event"] == "engine-finish" else "pending"
        result.append({"id": row["id"], "event": row["event"], "deadline": row["deadline"],
                       "status": status, "at": at if row["event"] == "engine-finish" else None,
                       "deadlineElapsed": reached > deadline,
                       "origin": row["record"]["sha256"],
                       "note": "Engine finish timestamp checked" if row["event"] == "engine-finish"
                       else "External host final handoff has not been attested"})
    return result


def view(store, state):
    rows = obligations(store, state, sealed=bool(state.get("executionSeal")))
    return {"obligations": rows, "sealed": bool(state.get("executionSeal")),
            "externalReceiptSupported": False, "results": state.get("executionResults", [])}
