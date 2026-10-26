"""Portable, source-bound owner decision for retrying one blocked task.

The decision ID is an agent-recorded local provenance key. It does not attest
to a host message identity or authenticate the sender.
"""
import hashlib
import os
from pathlib import Path
import re
import stat

from .contracts import artifact_valid, nonempty, require
from .portable import is_link
from .store import Invalid

MAX_SOURCE_BYTES = 65536
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_DECISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
KIND = "owner-task-attempt"


def read_source(path, expected_sha256, quote, decision_id):
    """Check physical bytes and exact quote before creating an artifact."""
    require(isinstance(expected_sha256, str) and _SHA.fullmatch(expected_sha256),
            "Нужен SHA-256 файла решения владельца")
    require(isinstance(decision_id, str) and _DECISION.fullmatch(decision_id),
            "Нужен локальный ID решения владельца")
    require(isinstance(quote, str) and nonempty(quote) and len(quote) <= MAX_SOURCE_BYTES,
            "Нужна точная непустая цитата решения владельца")
    try:
        source = Path(path).expanduser().absolute()
        if is_link(source):
            raise Invalid("Файл решения владельца не может быть ссылкой или junction")
        if not stat.S_ISREG(source.stat().st_mode) or source.stat().st_size > MAX_SOURCE_BYTES:
            raise Invalid("Нужен ограниченный обычный файл решения владельца")
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(source, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                raise Invalid("Нужен ограниченный обычный файл решения владельца")
            chunks, remaining = [], MAX_SOURCE_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError) as error:
        raise Invalid("Не удалось прочитать обычный файл решения владельца") from error
    require(0 < len(raw) <= MAX_SOURCE_BYTES and hashlib.sha256(raw).hexdigest() == expected_sha256,
            "SHA-256 решения владельца не совпадает")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise Invalid("Решение владельца должно быть UTF-8") from error
    require(quote in text, "Цитата отсутствует в файле решения владельца")
    from .workflow import SECRET  # Same guard as other owner input; delayed to avoid import cycle.
    require(not SECRET.search(text), "Обнаружен возможный секрет; подготовьте обезличенный входной файл")
    return raw


def authorization_valid(store, state, tid, previous, attempt, auth):
    """Strict history check, separate from the legacy brief authorization."""
    if not isinstance(auth, dict) or set(auth) != {"kind", "run", "task", "previousAttempt",
                                             "source", "quote", "decisionId", "at", "record"}:
        return False
    source = auth.get("source")
    if (auth.get("kind") != KIND or auth.get("run") != state.get("run")
            or auth.get("task") != tid or auth.get("previousAttempt") != previous.get("id")
            or not isinstance(auth.get("decisionId"), str)
            or not _DECISION.fullmatch(auth["decisionId"])
            or not isinstance(auth.get("quote"), str) or not nonempty(auth["quote"])
            or not isinstance(auth.get("at"), str) or not auth["at"]
            or not isinstance(source, dict) or set(source) != {"file", "sha256"}
            or not isinstance(source.get("file"), str)
            or not source["file"].startswith("owner-attempt-requests/")
            or not source["file"].endswith(".txt")
            or not isinstance(source.get("sha256"), str) or not _SHA.fullmatch(source["sha256"])
            or previous.get("briefCount") != attempt.get("briefCount")):
        return False
    try:
        return artifact_valid(store, source) and auth["quote"] in store.text(source)
    except (Invalid, OSError, UnicodeError, KeyError, TypeError):
        return False


def decision_uses(state, decision_id):
    """Return task and source binding for every prior use of this local key."""
    for tid, history in state.get("taskHistory", {}).items():
        for attempt in history:
            auth = attempt.get("authorization") or {}
            if isinstance(auth, dict) and auth.get("kind") == KIND and auth.get("decisionId") == decision_id:
                source = auth.get("source")
                yield tid, source.get("sha256") if isinstance(source, dict) else None, auth.get("quote")
