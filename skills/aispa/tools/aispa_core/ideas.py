"""Source-bound idea examination before sealing a brief.

The host owns locking and journal writes. This module checks structure, quoted
sources and freshness, not whether a product idea is useful or commercially
successful. It never adds, removes or weakens requirements on an agent's behalf.
"""
import json

from . import discovery, product
from .contracts import active, artifact_valid
from .store import Invalid, confined, digest, file_hash


SCHEMA = 1
AREAS = ("problem", "user", "outcome", "alternatives", "small_test",
         "contradictions", "onboarding", "empty", "failure", "data",
         "safety_access", "growth", "return_visit")
CORE_AREAS = ("problem", "user", "outcome", "alternatives", "small_test")
MAX_REPORT_CHARS = 48000
MAX_VIEW_CHARS = 8000
MAX_ROWS = 256
LIMITS = {"compact": 480, "deep": 1600}
_FIELDS = {"basis", "depth", "depthReason", "summary", "anchors", "coverage",
           "areas", "assumptions", "additions", "sourceObservations"}


def _require(condition, message):
    if not condition:
        raise Invalid("AISpa ideas: " + message)


def _object(value, fields, optional=()):
    _require(isinstance(value, dict), "нужен объект")
    _require(set(fields) <= set(value) <= set(fields) | set(optional),
             "пропущено обязательное или добавлено неизвестное поле")


def _text(value, label, limit):
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= limit,
             label + ": нужен непустой текст в пределах " + str(limit) + " символов")
    return value


def _rows(value, label, limit=MAX_ROWS):
    _require(isinstance(value, list) and len(value) <= limit,
             label + ": нужен список в пределах " + str(limit) + " записей")
    return value


def _size(value):
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as error:
        raise Invalid("AISpa ideas: нужен корректный ограниченный JSON") from error


def basis(state):
    """Sealing updates derived core/sealed fields, not the examined owner input."""
    return digest({"run": state["run"], "briefs": state["briefs"],
                   "requirements": state["requirements"]})


def _briefs(store, state):
    result = {}
    for source in state["briefs"]:
        _require(artifact_valid(store, source), "исходный бриф изменён или недоступен")
        _require(source["id"] not in result, "повторный ID исходного брифа")
        result[source["id"]] = (source, store.text(source))
    _require(result, "сначала сохраните слова владельца как бриф")
    return result


def _observations(store, state, report, *, check_sources):
    observations = _rows(report["sourceObservations"], "sourceObservations", 32)
    if not observations:
        _require("collection" not in report, "collection без sourceObservations не нужен")
        return
    collection = report.get("collection")
    _object(collection, ("file", "sha256", "basis"))
    saved = discovery._collection(store, dict(state, discoveryCollection=collection))
    known = {source["path"]: source for source in saved["sources"]}
    names = set(discovery._names(store)) if check_sources else None
    checked = set()
    for row in observations:
        _object(row, ("text", "sources"))
        _text(row["text"], "наблюдение", 1200)
        refs = _rows(row["sources"], "sources", 8)
        _require(refs, "наблюдению нужны источники из сохранённого discovery")
        seen = set()
        for ref in refs:
            _object(ref, ("path", "sha256", "quote"))
            name = _text(ref["path"], "путь источника", 512)
            source = known.get(name)
            _require(name not in seen and source is not None
                     and ref["sha256"] == source.get("sha256"),
                     "повторный, неизвестный или изменённый источник наблюдения")
            seen.add(name)
            quote = _text(ref["quote"], "цитата источника", 1200)
            _require(quote in source.get("excerpt", ""), "цитаты нет в собранном источнике")
            if check_sources and name not in checked:
                _require(name in names and discovery._kind(name, explicit=True),
                         "источник больше не входит в разрешённый состав проекта")
                path = discovery._safe_path(store.root, name)
                _require(path.is_file() and file_hash(path) == ref["sha256"],
                         "источник наблюдения изменён; изучите его заново")
                checked.add(name)


def _validate(store, state, report, *, check_sources=True):
    _object(report, _FIELDS, ("collection", "challenge"))
    _require(_size(report) <= MAX_REPORT_CHARS, "отчёт слишком большой; сократите изложение, сохранив все требования")
    _require(report["basis"] == basis(state), "бриф или требования изменились; обновите разбор идеи")
    _require(isinstance(report["depth"], str) and report["depth"] in LIMITS,
             "depth должен быть compact или deep")
    limit = LIMITS[report["depth"]]
    _text(report["depthReason"], "причина глубины разбора", 600)
    _text(report["summary"], "резюме", 1600)
    briefs = _briefs(store, state)
    seen = set()
    for anchor in _rows(report["anchors"], "anchors"):
        _object(anchor, ("brief", "sha256", "quote"))
        bid = anchor["brief"]
        _require(isinstance(bid, str) and bid in briefs and bid not in seen,
                 "неизвестная или повторная ссылка на бриф")
        source, text = briefs[bid]
        _require(anchor["sha256"] == source["sha256"], "неверная контрольная сумма брифа")
        _require(_text(anchor["quote"], "цитата брифа", 1600) in text,
                 "цитата отсутствует в исходных словах владельца")
        seen.add(bid)
    _require(seen == set(briefs), "разбор должен ссылаться на каждый текущий бриф владельца")

    requirements = {row["id"]: row for row in active(state)}
    _require(requirements, "сначала выделите действующие требования")
    seen = set()
    for row in _rows(report["coverage"], "coverage"):
        _object(row, ("requirement", "role", "proposal"))
        rid = row["requirement"]
        _require(isinstance(rid, str) and rid in requirements and rid not in seen,
                 "повторное или неизвестное требование в разборе")
        role = {"requested": "outcome", "constraint": "constraint", "addition": "addition"}[requirements[rid]["kind"]]
        _require(row["role"] == role, "нельзя выдать исходное требование за дополнение или сменить его роль")
        _text(row["proposal"], "сохранение требования в выбранном решении", 1200)
        seen.add(rid)
    _require(seen == set(requirements), "в разборе потеряны действующие требования: " + ", ".join(sorted(set(requirements) - seen)))

    seen = set()
    for row in _rows(report["areas"], "areas", len(AREAS)):
        _object(row, ("area", "status", "text"))
        area = row["area"]
        _require(isinstance(area, str) and area in AREAS and area not in seen,
                 "неизвестная или повторная область разбора")
        _require(row["status"] in ("assessed", "unknown", "not_applicable"), "неизвестный статус области")
        _text(row["text"], "наблюдение, причина неприменимости или неопределённости", limit)
        if area in CORE_AREAS:
            _require(row["status"] == "assessed", "любой разбор требует рассмотренные проблему, пользователя, результат, альтернативы и малый эксперимент")
        seen.add(area)
    _require(seen == set(AREAS), "разобраны не все области: " + ", ".join(sorted(set(AREAS) - seen)))

    for row in _rows(report["assumptions"], "assumptions", 48):
        _object(row, ("text", "validation", "consequence"))
        for key in row:
            _text(row[key], "предположение: " + key, 1200)
    for row in _rows(report["additions"], "additions", 32):
        _object(row, ("parent", "text", "rationale"))
        parent = row["parent"]
        _require(isinstance(parent, str) and parent in requirements
                 and requirements[parent]["kind"] != "addition",
                 "предложенному дополнению нужно действующее исходное требование")
        _text(row["text"], "предложенное дополнение", 1200)
        _text(row["rationale"], "обоснование дополнения", 1200)
    _observations(store, state, report, check_sources=check_sources)
    if state.get("policy", {}).get("productChallenge") or "challenge" in report:
        product.validate(state, report.get("challenge"))
    return report


def _read(store, ref):
    _require(isinstance(ref, dict) and isinstance(ref.get("file"), str)
             and ref["file"].startswith("ideas/") and ref["file"].endswith(".json"),
             "нет корректной ссылки на разбор идеи")
    path = confined(store.run, ref["file"])
    _require(path.stat().st_size <= MAX_REPORT_CHARS * 4 + 4096, "артефакт разбора слишком большой")
    _require(artifact_valid(store, ref), "артефакт разбора изменён")
    return json.loads(path.read_text(encoding="utf-8"))


def _saved_report(store, state, *, check_sources=True):
    ref = state.get("ideaReview")
    saved = _read(store, ref)
    _object(saved, ("schema", "run", "report"))
    _require(type(saved["schema"]) is int and saved["schema"] == SCHEMA
             and saved["run"] == state["run"], "чужой или неподдерживаемый разбор идеи")
    _object(saved["report"], _FIELDS, ("collection", "challenge"))
    _require(ref.get("basis") == saved["report"].get("basis"), "связь отчёта с основанием нарушена")
    history = state.get("ideaHistory", [])
    _require(isinstance(history, list) and history and history[-1] == ref,
             "текущий разбор не соответствует последней записи истории")
    _validate(store, state, saved["report"], check_sources=check_sources)
    return saved["report"]


def record(store, state, report):
    """Write immutable evidence and update in-memory refs; host appends the event."""
    _validate(store, state, report)
    value = {"schema": SCHEMA, "run": state["run"], "report": report}
    previous = state.get("ideaReview")
    if previous:
        try:
            if _saved_report(store, state) == report:
                return previous
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            pass
    history = state.get("ideaHistory", [])
    _require(isinstance(history, list), "история разбора повреждена")
    receipt = {**store.artifact("ideas", (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode(), ".json"),
               "basis": report["basis"]}
    state["ideaReview"] = receipt
    state["ideaHistory"] = [*history, dict(receipt)]
    return receipt


def valid(store, state, *, check_sources=True):
    try:
        _saved_report(store, state, check_sources=check_sources)
        return True
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return False


def view(store, state, *, compact=True):
    """Read-only routing view. Any omitted prose is explicit and stays in the artifact."""
    try:
        report = _saved_report(store, state)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        report = None
    fresh = report is not None
    result = {"basis": basis(state), "recorded": bool(state.get("ideaReview")), "fresh": fresh,
              "assessment": "structure-and-source-checks-only", "requiredAreas": list(AREAS)}
    if state.get("policy", {}).get("productChallenge"):
        result["challengeQuestions"] = product.QUESTIONS
    if not fresh:
        return result
    result.update(artifact=dict(state["ideaReview"]), depth=report["depth"], summary=report["summary"])
    if not compact:
        result["report"] = report
        return result
    result["omitted"] = {}
    fields = ("challenge", "coverage", "areas", "assumptions", "additions", "sourceObservations")
    for field in fields:
        result[field] = []
    # A stable budget is shared across lists. Important coverage is visible first;
    # a large list cannot silently exclude later observations from the artifact.
    for field in fields:
        rows = report.get(field, [])
        result["omitted"][field] = len(rows)
        for row in rows:
            result[field].append(row)
            result["omitted"][field] -= 1
            if _size(result) > MAX_VIEW_CHARS - 256:
                result[field].pop()
                result["omitted"][field] += 1
                break
    _require(_size(result) <= MAX_VIEW_CHARS, "краткое представление превысило лимит")
    return result
