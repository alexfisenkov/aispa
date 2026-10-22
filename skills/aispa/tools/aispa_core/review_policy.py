"""Frozen task review policy and required findings, without runtime decisions."""
from .contracts import require

LEGACY = "two_rounds"
ADAPTIVE = "adaptive"
# The lean contract keeps the adaptive critic rounds; a passing critic then
# closes the task itself, carrying the former acceptor's trace and closure.
LEAN = "lean"
POLICIES = (LEGACY, ADAPTIVE, LEAN)
VERDICTS = ("PASS", "WARN", "BLOCK", "FAIL")


def selected(state):
    policy = state.get("policy", {})
    if policy.get("adaptiveCritique") is not True:
        return LEGACY
    return LEAN if policy.get("leanControl") is True else ADAPTIVE


def mode(attempt):
    # Historical attempts without this field retain the original contract.
    value = attempt.get("reviewPolicy", LEGACY)
    require(value in POLICIES, "Неизвестная политика критики попытки")
    return value


def adaptive(attempt):
    """Conditional second round: true for both adaptive and lean attempts."""
    return mode(attempt) in (ADAPTIVE, LEAN)


def lean(attempt):
    return mode(attempt) == LEAN


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
