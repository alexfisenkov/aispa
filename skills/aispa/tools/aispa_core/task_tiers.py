"""Explicit task classification; declarations never prove semantic safety.

Absent classifications stay heavy. Light changes are narrowly bounded to named
non-executable documentation or presentation files. Lean-control runs also allow
standard: ordinary non-foundation code with a worker proof and no per-task critic.
For both, the independent whole-version review and final brief acceptance remain
separate gates.
"""
import re
from pathlib import PurePosixPath

from . import review_policy
from .contracts import require, nonempty
from .store import canonical_zone, common_input

FIELDS = ("tier", "tierReason", "changeKind")
TIERS = ("light", "standard", "heavy")
WORKER_ONLY = frozenset({"light", "standard"})
KINDS = frozenset({"documentation", "formatting", "presentation"})
FORBIDDEN = re.compile(r"(?:^|[./_-])(?:agents?|claude|gemini|openclaw|crab|skills?|prompts?|instructions?|system|developer|cursor|copilot|codex|windsurf|aider|rules?|polic(?:y|ies)|config|settings|secrets?|auth|billing|payments?|migrations?|schema|contracts?|api|dependencies|lock|foundation|security|workflow|scripts?|tools?|src|tests?)(?:[./_-]|$)", re.I)
INSTRUCTION_NAMES = frozenset({"soul.md", "identity.md", "user.md", "heartbeat.md", "bootstrap.md", "boot.md", "memory.md"})
# Standard may write code, never agent rules, secrets, access, money, data shape
# or shared contracts: those carry risk a per-task critic is meant to catch.
SENSITIVE = re.compile(r"(?:^|[./_-])(?:agents?|claude|gemini|openclaw|crab|skills?|prompts?|instructions?|system|developer|cursor|copilot|codex|windsurf|aider|cline|rules?|conventions|mcp|polic(?:y|ies)|secrets?|credentials?|tokens?|keys?|login|billing|payments?|migrations?|schema|contracts?|foundation|security|github|gitlab|husky|vscode|devcontainer|dockerfile|docker|deploy|env|envrc|npmrc|pypirc|netrc|pem|p12|pfx|keystore|package|pyproject|requirements|lock)(?:[./_-]|$)", re.I)
# Access and money words are matched inside identifiers too (userAuth, stripeCheckout).
SENSITIVE_PART = re.compile(r"auth(?!or)|passw|permission|secret|credential|payment|billing|checkout|stripe|invoice|(?:cursor|windsurf|cline|agent)rules|правил|секрет|пароль|доступ(?!н)|оплат|платеж|платёж", re.I)
EXTENSIONS = {"documentation": {".md", ".txt", ".rst"},
              "formatting": {".md", ".txt", ".rst", ".css"},
              "presentation": {".css", ".png", ".jpg", ".jpeg", ".webp"}}


def enabled(state):
    return state.get("policy", {}).get("taskTiers") is True


def standard_enabled(state):
    return enabled(state) and state.get("policy", {}).get("leanControl") is True


def is_light(value):
    return value.get("tier") == "light"


def is_standard(value):
    return value.get("tier") == "standard"


def worker_only(value):
    """Completion by a verified worker report, never by a task critic."""
    return value.get("tier") in WORKER_ONLY


def roles(attempt):
    if worker_only(attempt):
        return ("worker",)
    if review_policy.lean(attempt):
        return ("worker", "critic1", "critic2")
    return ("worker", "critic1", "critic2", "acceptor")


def validate(state, raw, zones, risk, risk_reason):
    supplied = any(key in raw for key in FIELDS)
    if not enabled(state):
        require(not supplied, "Классы задач доступны только в новом init с taskTiers")
        return {}
    tier = raw.get("tier", "heavy")
    reason = raw.get("tierReason", "Класс не указан; требуется полный маршрут проверки")
    kind = raw.get("changeKind", "unspecified")
    require(tier in TIERS and nonempty(reason) and len(reason) <= 2000,
            "Нужны tier=light|standard|heavy и объяснение tierReason")
    require(tier != "standard" or standard_enabled(state), "Класс standard доступен только в новом init с leanControl")
    require(nonempty(kind) and len(kind) <= 80, "Нужен ограниченный changeKind")
    if tier == "light":
        require(risk == "low" and nonempty(risk_reason), "Лёгкая задача требует явного low risk и riskReason")
        require(kind in KINDS, "Лёгкими могут быть только documentation, formatting или presentation")
        require(0 < len(zones) <= 12 and all(safe_zone(z, kind) for z in zones),
                "Лёгкой задаче нужны конкретные безопасные файлы; код, правила, конфигурация и контракты требуют heavy")
    if tier == "standard":
        require(risk in ("low", "normal") and (risk != "low" or nonempty(risk_reason)),
                "Standard требует риск low или normal; высокий риск требует heavy")
        require(0 < len(zones) <= 24 and all(standard_zone(z) for z in zones),
                "Standard не пишет корень, общие входы, правила агента, секреты, доступ, платежи, миграции, схемы и контракты")
    return {"tier": tier, "tierReason": reason, "changeKind": kind}


def validate_plan(tasks, closures):
    """A shared foundation needs its own critic: only the final whole-plan task,
    whose review covers the entire branch, may depend on standard work."""
    standard = {tid for tid, task in tasks.items() if is_standard(task)}
    for task in tasks.values():
        final = {t["id"] for t in closures[task["id"]]} == set(tasks)
        require(final or not set(task["dependsOn"]) & standard,
                "Задача %s зависит от standard; фундамент других задач должен быть heavy" % task["id"])


def sensitive_path(name):
    return (common_input(name) or PurePosixPath(name).name.casefold() in INSTRUCTION_NAMES
            or bool(SENSITIVE.search(name)) or bool(SENSITIVE_PART.search(name)))


def standard_zone(zone):
    name = canonical_zone(zone)
    return name not in ("", ".") and not any(c in name for c in "*?[]") and not sensitive_path(name)


def safe_zone(zone, kind):
    name = canonical_zone(zone)
    return (name not in ("", ".") and not common_input(name)
            and PurePosixPath(name).name.casefold() not in INSTRUCTION_NAMES
            and not any(c in name for c in "*?[]") and not FORBIDDEN.search(name)
            and PurePosixPath(name).suffix.lower() in EXTENSIONS.get(kind, set()))
