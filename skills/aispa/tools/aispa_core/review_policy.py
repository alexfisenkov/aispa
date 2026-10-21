"""Frozen task review policy and required findings, without runtime decisions."""
from .contracts import require

LEGACY = "two_rounds"
ADAPTIVE = "adaptive"
VERDICTS = ("PASS", "WARN", "BLOCK", "FAIL")


def selected(state):
    return ADAPTIVE if state.get("policy", {}).get("adaptiveCritique") is True else LEGACY


def mode(attempt):
    # Historical attempts without this field retain the original contract.
    value = attempt.get("reviewPolicy", LEGACY)
    require(value in (LEGACY, ADAPTIVE), "Неизвестная политика критики попытки")
    return value


def adaptive(attempt):
    return mode(attempt) == ADAPTIVE


def required_ids(record):
    return {f["id"] for f in record.get("findings", []) if f["severity"] in ("blocking", "warning")}


def second_allowed(attempt):
    rounds = attempt.get("rounds", [])
    return len(rounds) == 1 and (not adaptive(attempt) or not rounds[0]["passed"])


def verdict(report, *, clean, critic=False):
    value = report.get("verdict")
    require(value in VERDICTS, "Нужен явный verdict: PASS, WARN, BLOCK или FAIL")
    require(value != "PASS" or clean, "PASS противоречит проверкам, замечаниям или нерешённым требованиям")
    if critic and value != "PASS":
        require(required_ids(report), "WARN/BLOCK/FAIL требует обязательных замечаний с ID для исправления")
    return value
