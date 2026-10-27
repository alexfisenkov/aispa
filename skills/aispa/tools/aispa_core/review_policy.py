"""Frozen task review policy and required findings, without runtime decisions."""
from .contracts import require

LEGACY = "two_rounds"
ADAPTIVE = "adaptive"
# The lean contract keeps the adaptive critic rounds; a passing critic then
# closes the task itself, carrying the former acceptor's trace and closure.
LEAN = "lean"
# 0.5.0: lean rounds, but only the short list blocks; warnings are collected for the run.
FOCUSED = "focused"
POLICIES = (LEGACY, ADAPTIVE, LEAN, FOCUSED)
VERDICTS = ("PASS", "WARN", "BLOCK", "FAIL")


def selected(state):
    policy = state.get("policy", {})
    if policy.get("focusedReview") is True:
        return FOCUSED
    if policy.get("adaptiveCritique") is not True:
        return LEGACY
    return LEAN if policy.get("leanControl") is True else ADAPTIVE


def mode(attempt):
    # Historical attempts without this field retain the original contract.
    value = attempt.get("reviewPolicy", LEGACY)
    require(value in POLICIES, "Неизвестная политика критики попытки")
    return value


def adaptive(attempt):
    """Conditional second round: true for adaptive, lean and focused attempts."""
    return mode(attempt) in (ADAPTIVE, LEAN, FOCUSED)


def lean(attempt):
    """The passing critic closes the task (lean and focused)."""
    return mode(attempt) in (LEAN, FOCUSED)


def focused(attempt):
    return mode(attempt) == FOCUSED


def required_severities(policy=None):
    return ("blocking",) if policy == FOCUSED else ("blocking", "warning")


def required_ids(record, policy=None):
    """Findings that need a repair: under focused only blocking ones."""
    severities = required_severities(policy)
    return {f["id"] for f in record.get("findings") or [] if f["severity"] in severities}


def check_findings(findings, policy=None):
    """Focused: blocking only with a class from the short list; nothing is recorded otherwise."""
    if policy != FOCUSED:
        return findings
    from .focused_review import BLOCKING_CLASSES
    for finding in findings:
        if finding.get("severity") == "blocking":
            require(finding.get("blockingClass") in BLOCKING_CLASSES,
                    "Блокирует только список: " + ", ".join(BLOCKING_CLASSES) + "; остальное отметь warning")
        else:
            require("blockingClass" not in finding, "blockingClass допустим только у blocking; остальное отметь warning")
    return findings


def second_allowed(attempt):
    rounds = attempt.get("rounds", [])
    if len(rounds) != 1:
        return False
    # A frozen max-mode second review follows the first round whatever its verdict.
    return not adaptive(attempt) or not rounds[0]["passed"] or attempt.get("secondRound") is True


def verdict(report, *, clean, critic=False, policy=None):
    value = report.get("verdict")
    require(value in VERDICTS, "Нужен явный verdict: PASS, WARN, BLOCK или FAIL")
    require(value != "PASS" or clean, "PASS противоречит проверкам, замечаниям или нерешённым требованиям")
    if critic and value != "PASS":
        require(required_ids(report, policy), "WARN/BLOCK/FAIL требует обязательных замечаний с ID для исправления"
                if policy != FOCUSED else "WARN/BLOCK/FAIL требует блокирующего замечания из короткого списка; одни warning — это PASS")
    return value
