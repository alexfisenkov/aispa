"""Explicit task classification; declarations never prove semantic safety.

Absent classifications stay heavy. Light changes are narrowly bounded to named
non-executable documentation or presentation files; the independent whole-version
review and final brief acceptance remain separate gates.
"""
import re
from pathlib import PurePosixPath

from .contracts import require, nonempty
from .store import canonical_zone, common_input

FIELDS = ("tier", "tierReason", "changeKind")
KINDS = frozenset({"documentation", "formatting", "presentation"})
FORBIDDEN = re.compile(r"(?:^|[./_-])(?:agents?|claude|gemini|openclaw|crab|skills?|prompts?|instructions?|system|developer|cursor|copilot|codex|windsurf|aider|rules?|polic(?:y|ies)|config|settings|secrets?|auth|billing|payments?|migrations?|schema|contracts?|api|dependencies|lock|foundation|security|workflow|scripts?|tools?|src|tests?)(?:[./_-]|$)", re.I)
INSTRUCTION_NAMES = frozenset({"soul.md", "identity.md", "user.md", "heartbeat.md", "bootstrap.md", "boot.md", "memory.md"})
EXTENSIONS = {"documentation": {".md", ".txt", ".rst"},
              "formatting": {".md", ".txt", ".rst", ".css"},
              "presentation": {".css", ".png", ".jpg", ".jpeg", ".webp"}}


def enabled(state):
    return state.get("policy", {}).get("taskTiers") is True


def is_light(value):
    return value.get("tier") == "light"


def roles(attempt):
    return ("worker",) if is_light(attempt) else ("worker", "critic1", "critic2", "acceptor")


def validate(state, raw, zones, risk, risk_reason):
    supplied = any(key in raw for key in FIELDS)
    if not enabled(state):
        require(not supplied, "Классы задач доступны только в новом init с taskTiers")
        return {}
    tier = raw.get("tier", "heavy")
    reason = raw.get("tierReason", "Класс не указан; требуется полный маршрут проверки")
    kind = raw.get("changeKind", "unspecified")
    require(tier in ("light", "heavy") and nonempty(reason) and len(reason) <= 2000,
            "Нужны tier=light|heavy и объяснение tierReason")
    require(nonempty(kind) and len(kind) <= 80, "Нужен ограниченный changeKind")
    if tier == "light":
        require(risk == "low" and nonempty(risk_reason), "Лёгкая задача требует явного low risk и riskReason")
        require(kind in KINDS, "Лёгкими могут быть только documentation, formatting или presentation")
        require(0 < len(zones) <= 12 and all(safe_zone(z, kind) for z in zones),
                "Лёгкой задаче нужны конкретные безопасные файлы; код, правила, конфигурация и контракты требуют heavy")
    return {"tier": tier, "tierReason": reason, "changeKind": kind}


def safe_zone(zone, kind):
    name = canonical_zone(zone)
    return (name not in ("", ".") and not common_input(name)
            and PurePosixPath(name).name.casefold() not in INSTRUCTION_NAMES
            and not any(c in name for c in "*?[]") and not FORBIDDEN.search(name)
            and PurePosixPath(name).suffix.lower() in EXTENSIONS.get(kind, set()))
