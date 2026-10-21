"""Source-bound failure scenarios; declarations never imply a passing test."""
from .contracts import require
from .portable import relative_name
from .store import Invalid, canonical_zone


BOUNDARIES = frozenset({"input", "output", "storage", "protocol", "dependency"})
MAX_CASES = 24


def _text(value, label):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= 1600,
            "Failure checks: неверное поле " + label)


def _source(value, source_check):
    from .design_contract import safe_reference_name
    import re
    require(isinstance(value, dict) and set(value) == {"path", "sha256"}
            and isinstance(value["path"], str) and isinstance(value["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]),
            "Failure checks: нужен точный путь и SHA-256 источника")
    try:
        name = relative_name(value["path"])
        canonical_zone(name)
        safe_reference_name(name)
    except (ValueError, TypeError) as exc:
        raise Invalid("Failure checks: небезопасный путь источника") from exc
    require(name == value["path"], "Failure checks: неканонический путь")
    if source_check is not None:
        source_check(value)


def sources(value):
    return [row["test"] for row in value.get("cases", [])] + [
        row["source"] for row in value.get("notApplicable", [])]


def validate(task, report, source_check=None):
    value = report.get("failureChecks")
    require(isinstance(value, dict) and set(value) == {"cases", "notApplicable"},
            "Failure checks: нужен разбор пяти отказных границ")
    cases, excluded = value["cases"], value["notApplicable"]
    require(isinstance(cases, list) and len(cases) <= MAX_CASES
            and isinstance(excluded, list) and len(excluded) <= len(BOUNDARIES),
            "Failure checks: неверный список сценариев")
    expected = set(task["criteria"])
    tests = {(row["path"], row["sha256"]) for row in report.get("newTestFiles", [])}
    ids, covered, omitted = set(), set(), set()
    for row in cases:
        require(isinstance(row, dict) and set(row) == {
            "id", "boundary", "scenario", "expected", "criterion", "test"},
            "Failure checks: неверная схема сценария")
        for key in ("id", "scenario", "expected", "criterion"):
            _text(row[key], key)
        require(row["id"] not in ids and isinstance(row["boundary"], str)
                and row["boundary"] in BOUNDARIES and row["criterion"] in expected,
                "Failure checks: повторный сценарий или чужой критерий")
        _source(row["test"], source_check)
        if report["mode"] == "behavior_change":
            require((row["test"]["path"], row["test"]["sha256"]) in tests,
                    "Failure checks: сценарий не связан с RED-тестовым источником")
        ids.add(row["id"])
        covered.add(row["boundary"])
    for row in excluded:
        require(isinstance(row, dict) and set(row) == {"boundaries", "reason", "source"}
                and isinstance(row["boundaries"], list) and row["boundaries"]
                and all(isinstance(v, str) and v in BOUNDARIES for v in row["boundaries"])
                and len(set(row["boundaries"])) == len(row["boundaries"])
                and not (set(row["boundaries"]) & (covered | omitted)),
                "Failure checks: повторная, противоречивая или неизвестная граница")
        _text(row["reason"], "reason")
        _source(row["source"], source_check)
        omitted.update(row["boundaries"])
    require(covered | omitted == BOUNDARIES,
            "Failure checks: пропущена отказная граница")
    return {"cases": len(cases), "testedBoundaries": sorted(covered),
            "notApplicableBoundaries": sorted(omitted),
            "interpretation": "Test intent only; execution and semantic coverage require independent review"}


def template():
    return {"requiredBoundaries": sorted(BOUNDARIES),
            "failureChecks": {"cases": [], "notApplicable": []},
            "caseShape": {"id": "E01", "boundary": None, "scenario": None,
                          "expected": None, "criterion": None,
                          "test": {"path": None, "sha256": None}},
            "notApplicableShape": {"boundaries": [], "reason": None,
                                   "source": {"path": None, "sha256": None}},
            "status": "draft-not-evidence"}
