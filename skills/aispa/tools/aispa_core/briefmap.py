"""Account for original brief text before seal; the host owns journal events.

Spans are Python Unicode code-point offsets into the saved UTF-8 source. Exact
coverage and two declared participants are checkable; semantic interpretation
still requires independent human or model judgment.
"""
import json

from .contracts import active, artifact_valid, nonempty
from .store import Invalid, confined, digest


SCHEMA = 1
MAX_SOURCE_CHARS = 200000
MAX_UNITS = 2048
MAX_REPORT_CHARS = 750000
PUNCTUATION = frozenset(".!?。！？;；")


def _need(ok, message):
    if not ok:
        raise Invalid("AISpa briefmap: " + message)


def source_basis(state):
    return digest({"run": state["run"], "briefs": state["briefs"]})


def _sources(store, state):
    result = {}
    for ref in state["briefs"]:
        _need(artifact_valid(store, ref), "изменён исходный бриф")
        bid = ref.get("id")
        _need(isinstance(bid, str) and bid not in result, "повторный или пустой ID брифа")
        text = confined(store.run, ref["file"]).read_bytes().decode("utf-8")
        _need(text.strip() and len(text) <= MAX_SOURCE_CHARS, "пустой или слишком большой бриф")
        result[bid] = (ref, text)
    _need(result, "сначала сохраните бриф")
    return result


def _units(text):
    """Stable navigation only: line ends and sentence punctuation + whitespace."""
    rows = []
    start = 0

    def emit(left, right):
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            rows.append((left, right, text[left:right]))

    for index, char in enumerate(text):
        boundary = char in "\r\n" or (char in PUNCTUATION and
                                       (index + 1 == len(text) or text[index + 1].isspace()))
        if boundary:
            emit(start, index if char in "\r\n" else index + 1)
            start = index + 1
    emit(start, len(text))
    _need(0 < len(rows) <= MAX_UNITS, "слишком много исходных фрагментов")
    # This also guards edits to the navigation splitter that drop characters.
    marked = {i for left, right, _ in rows for i in range(left, right)}
    _need(all(i in marked for i, char in enumerate(text) if not char.isspace()),
          "исходный текст покрыт не полностью")
    return rows


def source_context(store, state):
    """The reviewer receives this original-only view, not analyst conclusions."""
    return {"basis": source_basis(state), "briefs": [
        {"id": bid, "sha256": ref["sha256"], "text": text,
         "units": [{"brief": bid, "start": start, "end": end, "text": chunk}
                   for start, end, chunk in _units(text)]}
        for bid, (ref, text) in _sources(store, state).items()]}


def _source_rows(store, state, report, role, actor_check):
    _need(isinstance(report, dict) and set(report) == {"basis", "actor", "units"},
          "отчёт требует basis, actor, units")
    _need(report["basis"] == source_basis(state), "отчёт устарел после изменения брифа")
    actor = report["actor"]
    _need(isinstance(actor, str) and actor_check(state, actor, role),
          "участник не подтверждён реестром: " + role)
    rows = report["units"]
    _need(isinstance(rows, list) and 0 < len(rows) <= MAX_UNITS * max(1, len(state["briefs"])),
          "нужен ограниченный список исходных фрагментов")
    expected = {(bid, start, end): chunk for bid, (_, text) in _sources(store, state).items()
                for start, end, chunk in _units(text)}
    seen = {}
    for row in rows:
        _need(isinstance(row, dict) and set(row) ==
              {"brief", "start", "end", "text", "kind", "summary", "reason"},
              "нужны точный диапазон, текст, вид, смысл и причина")
        key = (row["brief"], row["start"], row["end"])
        _need(isinstance(row["brief"], str) and type(row["start"]) is int and type(row["end"]) is int and
              key in expected and key not in seen and row["text"] == expected[key],
              "пропущен, повторён или изменён исходный диапазон")
        _need(row["kind"] in ("intent", "context") and nonempty(row["summary"])
              and len(row["summary"]) <= 1200, "каждому фрагменту нужны вид и смысл")
        _need(isinstance(row["reason"], str) and len(row["reason"]) <= 1200
              and (row["kind"] != "context" or nonempty(row["reason"])),
              "контекст требует отдельного объяснения")
        seen[key] = row
    _need(set(seen) == set(expected), "потеряны слова исходного брифа; учтите каждый фрагмент")
    _need(len(json.dumps(report, ensure_ascii=False, allow_nan=False)) <= MAX_REPORT_CHARS,
          "слишком большой отчёт")
    return seen


def _saved(store, state, key, history, group):
    ref = state.get(key)
    _need(isinstance(ref, dict) and isinstance(state.get(history), list)
          and state[history] and state[history][-1] == ref, "нет последнего отчёта: " + key)
    _need(isinstance(ref.get("file"), str) and ref["file"].startswith(group + "/")
          and artifact_valid(store, ref), "отчёт изменён: " + key)
    saved = json.loads(store.text(ref))
    _need(isinstance(saved, dict) and saved.get("schema") == SCHEMA
          and saved.get("run") == state["run"] and isinstance(saved.get("report"), dict),
          "чужая схема отчёта: " + key)
    return saved["report"]


def _save(store, state, report, key, history, group):
    value = {"schema": SCHEMA, "run": state["run"], "report": report}
    if state.get(key):
        try:
            if _saved(store, state, key, history, group) == report:
                return state[key]
        except (Invalid, OSError, ValueError, KeyError, TypeError):
            pass
    old = state.get(history, [])
    _need(isinstance(old, list), "повреждена история: " + history)
    receipt = {**store.artifact(group, (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(), ".json"),
               "basis": report["basis"]}
    state[key] = receipt
    state[history] = [*old, dict(receipt)]
    return receipt


def record_extraction(store, state, report, actor_check):
    _source_rows(store, state, report, "brief-analyst", actor_check)
    return _save(store, state, report, "briefExtraction", "briefExtractionHistory", "brief-extractions")


def record_review(store, state, report, actor_check):
    analyst = _saved(store, state, "briefExtraction", "briefExtractionHistory", "brief-extractions")
    _source_rows(store, state, analyst, "brief-analyst", actor_check)
    _source_rows(store, state, report, "brief-reviewer", actor_check)
    _need(report["actor"] != analyst["actor"], "исходный текст должен читать другой участник")
    return _save(store, state, report, "briefReview", "briefReviewHistory", "brief-reviews")


def reconciliation_basis(state):
    return digest({"source": source_basis(state),
                   "extraction": state.get("briefExtraction"), "review": state.get("briefReview"),
                   "requirements": state["requirements"]})


def _reconciliation(store, state, report, actor_check):
    analyst = _saved(store, state, "briefExtraction", "briefExtractionHistory", "brief-extractions")
    reviewer = _saved(store, state, "briefReview", "briefReviewHistory", "brief-reviews")
    left = _source_rows(store, state, analyst, "brief-analyst", actor_check)
    right = _source_rows(store, state, reviewer, "brief-reviewer", actor_check)
    _need(analyst["actor"] != reviewer["actor"], "проверяющий должен быть независимым")
    _need(isinstance(report, dict) and set(report) == {"basis", "actor", "rows"}
          and report["basis"] == reconciliation_basis(state), "сверка устарела или неполна")
    _need(report["actor"] == reviewer["actor"]
          and actor_check(state, report["actor"], "brief-reviewer"),
          "сверку подписывает исходный независимый проверяющий")
    # A quoted owner decision may defer an original obligation; its source
    # still has to remain accounted for, rather than disappear from this map.
    reqs = {r["id"]: r for r in state["requirements"]
            if r.get("kind", "requested") != "addition"}
    rows = report["rows"]
    _need(isinstance(rows, list) and len(rows) == len(left), "сверка должна сохранить все исходные фрагменты")
    seen, mapped, unresolved = set(), set(), False
    for row in rows:
        _need(isinstance(row, dict) and set(row) ==
              {"brief", "start", "end", "disposition", "requirements", "reason"},
              "строка сверки неполна")
        _need(isinstance(row["brief"], str) and type(row["start"]) is int
              and type(row["end"]) is int, "некорректный диапазон сверки")
        key = (row["brief"], row["start"], row["end"])
        _need(key in left and key in right and key not in seen, "неизвестный или повторный диапазон сверки")
        seen.add(key)
        _need(isinstance(row["reason"], str) and nonempty(row["reason"])
              and len(row["reason"]) <= 1600, "объясните решение по каждому фрагменту")
        kinds = {left[key]["kind"], right[key]["kind"]}
        disposition = row["disposition"]
        if len(kinds) != 1:
            _need(disposition == "disagreement" and row["requirements"] == [],
                  "расхождение независимых разборов нельзя скрыть")
            unresolved = True
        elif kinds == {"context"}:
            _need(disposition == "context" and row["requirements"] == [],
                  "контекст нельзя выдать за дополнение или требование")
        else:
            ids = row["requirements"]
            _need(disposition == "requirement" and isinstance(ids, list) and ids
                  and all(isinstance(rid, str) and rid in reqs for rid in ids)
                  and len(ids) == len(set(ids)),
                  "каждая исходная просьба требует исходное требование или ограничение")
            for rid in ids:
                req = reqs[rid]
                decision = req.get("decision", {})
                original = req.get("brief") == key[0] and left[key]["text"] in req.get("quote", "")
                amended = decision.get("brief") == key[0] and left[key]["text"] in decision.get("quote", "")
                _need(original or amended,
                      "требование должно цитировать весь исходный фрагмент того же брифа")
                mapped.add(rid)
    _need(seen == set(left), "часть исходного текста не сверена")
    _need(len(json.dumps(report, ensure_ascii=False, allow_nan=False)) <= MAX_REPORT_CHARS,
          "слишком большой отчёт сверки")
    return not unresolved and mapped == set(reqs)


def record_reconciliation(store, state, report, actor_check):
    _reconciliation(store, state, report, actor_check)
    return _save(store, state, report, "briefMap", "briefMapHistory", "brief-maps")


def valid(store, state, actor_check):
    try:
        report = _saved(store, state, "briefMap", "briefMapHistory", "brief-maps")
        return _reconciliation(store, state, report, actor_check)
    except (Invalid, OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return False


def view(store, state, actor_check):
    """Only status and source units; never echo private extraction in UI."""
    try:
        context = source_context(store, state)
        count = sum(len(b["units"]) for b in context["briefs"])
    except (Invalid, OSError, ValueError, UnicodeError):
        count = 0
    return {"sourceBasis": source_basis(state), "sourceUnits": count,
            "extractionRecorded": bool(state.get("briefExtraction")),
            "reviewRecorded": bool(state.get("briefReview")),
            "reconciliationRecorded": bool(state.get("briefMap")),
            "valid": valid(store, state, actor_check),
            "assessment": "structural-source-coverage-not-semantic-proof"}
