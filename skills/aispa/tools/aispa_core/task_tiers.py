"""Explicit task classification; declarations never prove semantic safety.

Absent classifications stay heavy. Light changes are narrowly bounded to named
non-executable documentation or presentation files. Lean-control runs also allow
standard: ordinary non-foundation code with a worker proof and no per-task critic.
For both, the independent whole-version review and final brief acceptance remain
separate gates.
"""
import re
import shlex
from pathlib import PurePosixPath

from . import review_policy
from .contracts import require, nonempty
from .store import canonical_zone, common_input

FIELDS = ("tier", "tierReason", "changeKind")
TIERS = ("light", "standard", "heavy")
# "fast" is never declared in a plan: the owner's fast mode assigns it to attempts.
WORKER_ONLY = frozenset({"light", "standard", "fast"})
KINDS = frozenset({"documentation", "formatting", "presentation"})
FORBIDDEN = re.compile(r"(?:^|[./_-])(?:agents?|claude|gemini|antigravity|openclaw|crab|skills?|prompts?|instructions?|system|developer|cursor|copilot|codex|windsurf|aider|rules?|polic(?:y|ies)|config|settings|secrets?|auth|billing|payments?|migrations?|schema|contracts?|api|dependencies|lock|foundation|security|workflow|scripts?|tools?|src|tests?)(?:[./_-]|$)", re.I)
INSTRUCTION_NAMES = frozenset({"soul.md", "identity.md", "user.md", "heartbeat.md", "bootstrap.md", "boot.md", "memory.md"})
# Standard may write code, never agent rules, secrets, access, money, data shape
# or shared contracts: those carry risk a per-task critic is meant to catch.
SENSITIVE = re.compile(r"(?:^|[./_-])(?:agents?|claude|gemini|antigravity|openclaw|crab|skills?|prompts?|instructions?|system|developer|cursor|copilot|codex|windsurf|aider|cline|rules?|conventions|mcp|polic(?:y|ies)|secrets?|credentials?|tokens?|keys?|login|billing|payments?|migrations?|schema|contracts?|foundation|security|github|gitlab|husky|vscode|devcontainer|dockerfile|docker|deploy|env|envrc|npmrc|pypirc|netrc|pem|p12|pfx|keystore|package|pyproject|requirements|lock)(?:[./_-]|$)", re.I)
# Access and money words are matched inside identifiers too (userAuth, stripeCheckout).
SENSITIVE_PART = re.compile(r"auth(?!or)|passw|permission|secret|credential|payment|billing|checkout|stripe|invoice|(?:cursor|windsurf|cline|agent)rules|правил|секрет|пароль|доступ(?!н)|оплат|платеж|платёж", re.I)
# The narrow list that earns a stronger model: security, secrets, access, money,
# personal data. Manifests, schemas, Docker, skills and prompts are not on it.
STRONG_WORD = re.compile(r"(?:^|[./_-])(?:security|secrets?|credentials?|tokens?|keys?|apikeys?|passwords?|passwd|auth|oauth|login|access|permissions?|rights|acl|rbac|payments?|billing|checkout|invoices?|stripe|personal|pii|gdpr|profiles?|passport|безопасность|секреты?|пароли?|токены?|ключи?|доступ|права|оплата|платежи?|платёж|личное|личные|персональные)(?:[./_-]|$)", re.I)
STRONG_PART = re.compile(r"auth(?!or)|passw|permission|secret|credential|payment|billing|checkout|stripe|invoice|gdpr|passport|персон|паспорт|личн|авториз|секрет|пароль|доступ(?!н)|оплат|платеж|платёж", re.I)
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


def is_fast(value):
    return value.get("tier") == "fast"


def worker_only(value):
    """Completion by a verified worker report, never by a task critic.

    A focused attempt carries the roles the engine froze for it (weight and
    mode, see focused_review.critic_roles); a plan task keeps its declared tier.
    """
    if isinstance(value.get("criticRoles"), list):
        return value["criticRoles"] == ["worker"]
    return value.get("tier") in WORKER_ONLY


def roles(attempt):
    if isinstance(attempt.get("criticRoles"), list):
        return tuple(attempt["criticRoles"])
    if worker_only(attempt):
        return ("worker",)
    if review_policy.lean(attempt):
        return ("worker", "critic1", "critic2")
    return ("worker", "critic1", "critic2", "acceptor")


def validate(state, raw, zones, risk, risk_reason):
    supplied = any(key in raw for key in FIELDS)
    require("scriptCheck" not in raw or enabled(state) and state.get("policy", {}).get("modelRoutes") is True,
            "scriptCheck доступен только в новом init с modelRoutes")
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
    if tier == "standard" and state.get("policy", {}).get("focusedReview") is True:
        # The engine computes the weight; a declaration below it is refused by
        # focused_review.validate_plan with the engine's reason.
        require(risk in ("low", "normal") and (risk != "low" or nonempty(risk_reason)),
                "Standard требует риск low или normal; высокий риск требует heavy")
    elif tier == "standard":
        require(risk in ("low", "normal") and (risk != "low" or nonempty(risk_reason)),
                "Standard требует риск low или normal; высокий риск требует heavy")
        require(0 < len(zones) <= 24 and all(standard_zone(z) for z in zones),
                "Standard не пишет корень, общие входы, правила агента, секреты, доступ, платежи, миграции, схемы и контракты")
    value = {"tier": tier, "tierReason": reason, "changeKind": kind}
    if "scriptCheck" in raw:
        value["scriptCheck"] = _script_check(state, raw, zones, risk, kind)
    return value


def _script_check(state, raw, zones, risk, kind):
    """Optional plan command that alone proves mechanical work (model_routes: mechanical_scripted)."""
    script = raw["scriptCheck"]
    try:
        words = shlex.split(script) if isinstance(script, str) else []
    except ValueError:
        words = []
    require(words and len(script) <= 2000, "scriptCheck — непустая строка команды проверки (до 2000 знаков)")
    # Dependents are unknown here; validate_plan refuses a scriptCheck task that others depend on.
    candidate = {"id": raw.get("id"), "title": raw.get("title", ""), "requirements": raw.get("requirements", []),
                 "zones": zones, "risk": risk, "changeKind": kind}
    require(model_need(dict(state, tasks=[]), candidate) == "mechanical",
            "scriptCheck допустим только у механической задачи: risk low, changeKind documentation, formatting "
            "или presentation и только безопасные файлы")
    return script


def validate_plan(tasks, closures):
    """A shared foundation needs its own critic: only the final whole-plan task,
    whose review covers the entire branch, may depend on standard work."""
    standard = {tid for tid, task in tasks.items() if is_standard(task)}
    scripted = {tid for tid, task in tasks.items() if "scriptCheck" in task}
    for task in tasks.values():
        require(not set(task["dependsOn"]) & scripted,
                "scriptCheck допустим только у механической задачи; от неё зависит %s" % task["id"])
        final = {t["id"] for t in closures[task["id"]]} == set(tasks)
        require(final or not set(task["dependsOn"]) & standard,
                "Задача %s зависит от standard; фундамент других задач должен быть heavy" % task["id"])


def sensitive_path(name):
    return (common_input(name) or PurePosixPath(name).name.casefold() in INSTRUCTION_NAMES
            or bool(SENSITIVE.search(name)) or bool(SENSITIVE_PART.search(name)))


# Key and credential files, whatever their directory: the owner named keys and sensitive data.
STRONG_FILE = re.compile(r"(?:^|/)(?:\.env[^/]*|[^/]*\.(?:pem|key|p12|pfx)|id_rsa[^/]*|\.npmrc|\.pypirc|\.netrc|credentials[^/]*)$", re.I)
STRONG_DATA = re.compile(r"user[_-]?data|customer|client[_-]?data|персональн", re.I)


def strong_model_path(name):
    return bool(STRONG_WORD.search(name) or STRONG_PART.search(name)
                or STRONG_FILE.search(name) or STRONG_DATA.search(name))


SENSITIVE_MEANING = re.compile(r"financial|finance|payment|invoice|billing|account permissions|"
    r"authentication|authorization|access control|credentials|secrets|personal data|PII|"
    r"финанс|платеж|платёж|оплат|авторизац|аутентификац|права доступа|персональн|секрет", re.I)


def sensitive_task(state, task=None):
    """Use task/requirement meaning and known zones, never incidental source quotes."""
    if task is not None and any(strong_model_path(path) for path in task.get("zones", [])):
        return True
    selected = set(task.get("requirements", [])) if task is not None else None
    reqs = [r for r in state.get("requirements", []) if r.get("scope", "active") == "active"
            and (selected is None or r.get("id") in selected)]
    texts = ([task.get("title", "")] if task else [])
    for req in reqs:
        texts.extend([req.get("text", ""), *req.get("acceptance", [])])
        if req.get("sensitive") is True or req.get("sensitiveAreas"):
            return True
    return any(SENSITIVE_MEANING.search(text) for text in texts if isinstance(text, str))


def model_need(state, task, attempt=None):
    """Model advice follows the work; review tiers and check modes stay separate."""
    shared = any(task["id"] in other.get("dependsOn", []) for other in state.get("tasks", []))
    if (sensitive_task(state, task) or task.get("risk") == "high" or shared
            or (attempt or {}).get("authorization") or (attempt or {}).get("repairPlan")):
        return "complex"
    kind, zones = task.get("changeKind"), task.get("zones", [])
    if (task.get("risk") == "low" and kind in KINDS and zones
            and all(safe_zone(zone, kind) for zone in zones)):
        return "mechanical"
    return "ordinary"


def standard_zone(zone):
    name = canonical_zone(zone)
    return name not in ("", ".") and not any(c in name for c in "*?[]") and not sensitive_path(name)


def safe_zone(zone, kind):
    name = canonical_zone(zone)
    return (name not in ("", ".") and not common_input(name)
            and PurePosixPath(name).name.casefold() not in INSTRUCTION_NAMES
            and not any(c in name for c in "*?[]") and not FORBIDDEN.search(name)
            and PurePosixPath(name).suffix.lower() in EXTENSIONS.get(kind, set()))
