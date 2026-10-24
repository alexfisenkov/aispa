"""Owner-chosen checking modes: fast, balanced, max.

The owner picks the mode at start and may change it at any time by giving
words that are recorded verbatim (the brief and its seal stay untouched).
Finished work keeps the mode it was done under; the mode only decides the
class of tasks not yet submitted and which project-wide gates apply.
"""
from .contracts import plan_basis, require
from .store import timestamp

MODES = ("fast", "balanced", "max")
DEFAULT = "balanced"


def enabled(state):
    return state.get("policy", {}).get("leanControl") is True


def current(state):
    return state.get("checkMode", DEFAULT) if enabled(state) else DEFAULT


def is_fast(state):
    return current(state) == "fast"


def is_max(state):
    return current(state) == "max"


def initialize(state, mode):
    require(mode in MODES, "Режим проверки: fast, balanced или max")
    state["checkMode"] = mode
    state["checkModeHistory"] = [{"mode": mode, "at": timestamp(), "source": "init"}]


def effective_tier(state, declared):
    """Fast and max override the plan; balanced keeps the declared class."""
    mode = current(state)
    if mode == "fast":
        return "fast"
    if mode == "max":
        return "heavy"
    return declared or "heavy"


def is_deferred(state, task, store):
    """Deferred = blocked after a refusal that followed the one extra wave; derived from sealed history."""
    history = (state.get("taskHistory", {}).get(task["id"]) or [])
    if not (enabled(state) and task.get("status") == "blocked" and history and history[-1].get("terminal")):
        return False
    last = history[-1]
    refused = (last.get("acceptance") or {}).get("passed") is False or bool(last.get("rounds") and not last["rounds"][-1]["passed"])
    if not refused:
        return False
    from .team import _history_valid, _sealed_valid  # team imports this module: resolved at call time
    return bool(_history_valid(store, state, task["id"]) and any(
        (a.get("authorization") or {}).get("kind") == "extra-wave" and _sealed_valid(store, a["authorization"])
        for a in history))


def deferred_ids(state, store):
    return {t["id"] for t in state["tasks"] if is_deferred(state, t, store)}


def record_switch(store, state, mode, text):
    """Keep the owner's own words verbatim as a separate record; callers re-card affected tasks."""
    require(enabled(state), "Режимы проверки доступны только в новом init с leanControl")
    require(mode in MODES, "Режим проверки: fast, balanced или max")
    require(mode != current(state), "Этот режим уже действует")
    # The owner may repeat the same words at another moment: a lawful request is never refused for its wording.
    require(isinstance(text, str) and text.strip(), "Смена режима требует слов владельца: файл с его сообщением")
    data = text.encode("utf-8")
    request = store.artifact("mode-requests", data, ".md")
    row = {"mode": mode, "previous": current(state), "request": request, "at": timestamp()}
    state["checkMode"] = mode
    state.setdefault("checkModeHistory", []).append(row)
    return row


def wave_unchecked(state, number):
    """Fast mode skips the whole-brief check of a wave opened in it, or any wave while fast is on."""
    entry = state.get("waveEntries", {}).get(str(number)) or {}
    marked = {"wave": number, "plan": plan_basis(state)} in state.get("fastWaves", [])
    return is_fast(state) or entry.get("checkMode") == "fast" or marked


def skipped_checks(state):
    """Names of the checks the owner's fast mode left out, for the final report."""
    if not enabled(state):
        return []
    from . import test_intent  # resolved at call time: team imports check_modes
    fast_attempts = [attempt for t in state["tasks"]
                     if (attempt := (state.get("taskHistory", {}).get(t["id"]) or [{}])[-1]).get("tier") == "fast"]
    policy, rows = state.get("policy", {}), []
    if fast_attempts:
        rows.append("Проверка задач критиками")
        if policy.get("testIntent") and any(not test_intent.required(state, attempt) for attempt in fast_attempts):
            rows.append("Тестовый контракт RED")
    if any(wave_unchecked(state, n) for n in {t["wave"] for t in state["tasks"]}):
        rows.append("Сверка волн с полным брифом")
    if policy.get("integrationReview") and is_fast(state):
        rows.append("Обзор версии целиком")
    if policy.get("designContract") and is_fast(state):
        rows.append("Подробные сценарии (только main и error)")
    return rows


def history_view(store, state):
    """The owner's switches as they were said: mode, from what, when and the words (first 300 characters)."""
    if not enabled(state):
        return []
    rows = []
    for row in state.get("checkModeHistory", []):
        if not isinstance(row.get("request"), dict):
            continue
        try:
            text = store.text(row["request"]).strip()[:300]
        except (OSError, KeyError, ValueError):
            text = ""
        rows.append({"mode": row["mode"], "previous": row.get("previous"), "at": row.get("at"), "text": text})
    return rows
