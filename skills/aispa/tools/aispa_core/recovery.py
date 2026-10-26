"""Paused journal recovery: fresh code check and independent reconciliation.

The caller holds the Journal lock and saves the returned receipt/state mutation
as explicit journal events. Neither command makes a recovered run active.
"""
import json
import math
import os
import re
import time

from . import cleanroom, discovery, executions, participants, process_runner, resources, timing
from .contracts import artifact_valid, nonempty
from .portable import relative_name
from .store import Invalid, confined, digest, file_hash, timestamp


MAX_OUTPUT = 1024 * 1024
MAX_REPORT_BYTES = 128 * 1024
SHA = re.compile(r"[0-9a-f]{64}\Z")
CORE_FIELDS = ("sourceSha256", "backupFile", "tailSha256", "validSequence")


def _need(ok, message):
    if not ok:
        raise Invalid("AISpa recovery: " + message)


def _core(marker):
    _need(isinstance(marker, dict) and marker.get("pending") is True,
          "нет ожидающей сверки восстановления")
    core = {key: marker.get(key) for key in CORE_FIELDS}
    _need(all(isinstance(core[key], str) and SHA.fullmatch(core[key]) for key in
              ("sourceSha256", "tailSha256")) and type(core["validSequence"]) is int
          and core["validSequence"] >= 1 and isinstance(core["backupFile"], str)
          and core["backupFile"].startswith("recovery/original-")
          and core["backupFile"].endswith(".jsonl"), "повреждены границы восстановления")
    return core


def _relative_path(value):
    if not isinstance(value, str) or len(value) > 300:
        return False
    try:
        return relative_name(value, root=True) == value
    except ValueError:
        return False


def _anchor(store, state):
    _need(store.run is not None and state.get("run") == store.run.name
          and state.get("status") == "paused", "сверка возможна только в приостановленном прогоне")
    marker = state.get("journalRecovery")
    core = _core(marker)
    _need(core["backupFile"] == "recovery/original-" + core["sourceSha256"] + ".jsonl",
          "резервная копия не относится к исходному журналу")
    backup = confined(store.run, core["backupFile"])
    try:
        backup_ok = backup.is_file() and file_hash(backup) == core["sourceSha256"]
    except OSError:
        backup_ok = False
    _need(backup_ok, "точная резервная копия журнала отсутствует или изменена")
    journal = store.read_journal(include_events=True)
    rows = journal["events"]
    index = core["validSequence"]
    recovered = rows[index]["state"].get("journalRecovery") if len(rows) > index else None
    _need(len(rows) > index and isinstance(recovered, dict) and rows[index]["sequence"] == index + 1
          and rows[index]["event"] == "journal-recovered"
          and {key: recovered.get(key) for key in CORE_FIELDS} == core,
          "маркер не соответствует событию восстановления")
    _need(digest(journal["last"]["state"]) == digest(state),
          "состояние изменилось без записи в журнал")
    return core, journal


def verify(store, state, command, timeout=600, *, on_admitted=None):
    """Execute a fresh bounded check while paused; caller journals the receipt."""
    core, journal = _anchor(store, state)
    executions.validate_command(command)
    _need(isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
          and math.isfinite(timeout) and 0 < timeout <= 86400, "нужен конечный положительный timeout")
    joined = " ".join(command)
    secrets = [value for key, value in os.environ.items()
               if cleanroom.SENSITIVE_ENV.search(key) and len(value) >= 8]
    _need(not cleanroom.SECRET.search(joined) and not any(value in joined for value in secrets),
          "секреты нельзя передавать аргументами проверки")
    code = store.code()
    started = time.monotonic()
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    def admitted(kind):
        nonlocal core, journal
        if on_admitted is None:
            # The direct API leaves persistence to its caller. Its receipt and
            # timing mutation are saved together after this unchanged anchor.
            timing.resource_admitted(state, kind, timestamp())
        else:
            on_admitted(kind)
            # A journal-saving hook is now the immediate predecessor of the
            # verification receipt; bind that saved tip before starting a child.
            core, journal = _anchor(store, state)

    raw, rc, timed_out, truncated, redacted = process_runner.run(
        command, store.root, environment, timeout, max_output=MAX_OUTPUT,
        secret_pattern=cleanroom.SECRET, secrets=secrets,
        resource_context=resources.context(store, state, allow_paused=True), on_admitted=admitted)
    output = raw.decode("utf-8", "replace")
    redacted = redacted or bool(cleanroom.SECRET.search(output) or any(v in output for v in secrets))
    if redacted:
        output, rc = "AISpa withheld the entire output because it may contain a secret.\n", 125
    output_ref = store.artifact("evidence", output.encode() or b"(no output)\n", ".recovery.log")
    try:
        changed = store.code() != code
    except (Invalid, OSError, ValueError):
        changed = True
    receipt = {"purpose": "recovery", "run": state["run"], "sourceSha256": core["sourceSha256"],
               "recoveryAnchor": digest(core), "journalAtStart": journal["last"]["hash"],
               "command": list(command), "timeout": timeout, "code": code,
               "exitCode": rc, "timedOut": timed_out, "truncated": truncated,
               "redacted": redacted, "changedCode": changed, "output": output_ref,
               "durationSeconds": round(time.monotonic() - started, 3), "at": timestamp()}
    receipt["artifact"] = store.artifact("recovery", (json.dumps(receipt, ensure_ascii=False,
        sort_keys=True, indent=2) + "\n").encode(), ".verification.json")
    return receipt


def _verification_valid(store, state, core, journal):
    receipt = state["journalRecovery"].get("verification")
    _need(isinstance(receipt, dict) and receipt.get("purpose") == "recovery"
          and receipt.get("run") == state["run"]
          and receipt.get("sourceSha256") == core["sourceSha256"]
          and receipt.get("recoveryAnchor") == digest(core)
          and type(receipt.get("exitCode")) is int and receipt["exitCode"] == 0
          and all(receipt.get(key) is False for key in
              ("timedOut", "truncated", "redacted", "changedCode"))
          and receipt.get("code") == store.code()
          and artifact_valid(store, receipt.get("output", {}))
          and artifact_valid(store, receipt.get("artifact", {})),
          "нужна свежая успешная проверка без усечения и изменения кода")
    try:
        saved = store.json(receipt["artifact"])
    except (OSError, ValueError, TypeError) as exc:
        raise Invalid("AISpa recovery: повреждена квитанция проверки") from exc
    _need(saved == {key: value for key, value in receipt.items() if key != "artifact"},
          "квитанция проверки изменена")
    rows = journal["events"]
    verification_index = max((i for i, row in enumerate(rows)
                              if row["event"] == "recovery-verification-executed"), default=-1)
    blocked_index = max((i for i, row in enumerate(rows)
                         if row["event"] == "recovery-blocked"), default=-1)
    saved_marker = (rows[verification_index]["state"].get("journalRecovery")
                    if verification_index >= 0 else None)
    _need(verification_index > state["journalRecovery"]["validSequence"]
          and verification_index > blocked_index
          and rows[verification_index - 1]["hash"] == receipt.get("journalAtStart")
          and isinstance(saved_marker, dict) and saved_marker.get("verification") == receipt,
          "проверка не записана после последней блокировки восстановления")
    return receipt


def context(store, state):
    """Report basis from saved recovery, journal tip, current code and proof."""
    core, journal = _anchor(store, state)
    receipt = _verification_valid(store, state, core, journal)
    code = store.code()
    basis = digest({"run": state["run"], "recovery": core,
                    "journal": journal["last"]["hash"], "journalSha256": journal["sha256"],
                    "code": code, "verification": receipt["artifact"]["sha256"]})
    return {"basis": basis, "run": state["run"], "code": code,
            "journalSequence": journal["count"], "journalHash": journal["last"]["hash"],
            "sourceSha256": core["sourceSha256"], "verification": receipt["artifact"],
            "requiredRole": "recovery-reviewer"}


def _evidence(store, refs, field):
    _need(isinstance(refs, list) and 0 < len(refs) <= 16, field + ": нужны неизменные свидетельства")
    seen = set()
    for ref in refs:
        _need(isinstance(ref, dict) and set(ref) == {"file", "sha256"}
              and isinstance(ref["file"], str) and ref["file"].startswith("evidence/")
              and isinstance(ref["sha256"], str) and SHA.fullmatch(ref["sha256"])
              and ref["sha256"] not in seen and artifact_valid(store, ref),
              field + ": неверное или изменённое свидетельство")
        path = confined(store.run, ref["file"])
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        _need(0 < size <= 4 * 1024 * 1024, field + ": пустое или слишком большое свидетельство")
        seen.add(ref["sha256"])


def reconcile(store, state, report):
    """Record an independent finding; only a resolved finding clears pending."""
    review = context(store, state)
    _need(isinstance(report, dict) and set(report) == {
        "basis", "reviewer", "independent", "decision", "filesystem", "externalEffects", "note"},
        "отчёт требует basis, reviewer, independent, decision, filesystem, externalEffects, note")
    _need(report["basis"] == review["basis"], "отчёт относится к другой версии журнала или кода")
    _need(report["independent"] is True and isinstance(report["reviewer"], str)
          and participants.actor_check(store, state, report["reviewer"], "recovery-reviewer"),
          "нужен свежий независимый recovery-reviewer")
    actor = participants.resolve(store, state, report["reviewer"])
    _need(actor.get("fresh") is True and actor.get("external") is False,
          "проверяющий должен иметь свежую штатную роль")
    _need(report["decision"] in ("resume", "blocked") and nonempty(report["note"])
          and len(report["note"]) <= 2400, "нужны решение и конкретный вывод")
    filesystem = report["filesystem"]
    _need(isinstance(filesystem, dict) and set(filesystem) == {"inspected", "paths", "note", "evidence"}
          and filesystem["inspected"] is True and nonempty(filesystem["note"])
          and len(filesystem["note"]) <= 2400, "нужен явный осмотр файловой системы")
    paths = filesystem["paths"]
    _need(isinstance(paths, list) and 0 < len(paths) <= 64 and
          all(_relative_path(p) for p in paths)
          and len(paths) == len(set(paths)), "нужны точные относительные пути осмотра")
    _evidence(store, filesystem["evidence"], "filesystem")
    effects = report["externalEffects"]
    _need(isinstance(effects, dict) and set(effects) == {"status", "note", "evidence"}
          and effects["status"] in ("none", "reconciled", "unknown")
          and nonempty(effects["note"]) and len(effects["note"]) <= 2400,
          "нужно явно классифицировать внешние эффекты")
    _evidence(store, effects["evidence"], "externalEffects")
    _need(report["decision"] != "resume" or effects["status"] != "unknown",
          "неизвестные внешние эффекты запрещают возобновление")
    try:
        raw = (json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()
    except (TypeError, ValueError) as exc:
        raise Invalid("AISpa recovery: отчёт не является конечным JSON") from exc
    _need(len(raw) <= MAX_REPORT_BYTES and not cleanroom.SECRET.search(raw.decode("utf-8"))
          and not discovery.SECRET_CONTENT.search(raw.decode("utf-8")),
          "отчёт слишком велик или содержит возможный секрет")
    artifact = store.artifact("recovery-reviews", raw, ".json")
    marker = state["journalRecovery"]
    record = {"basis": review["basis"], "reviewer": report["reviewer"],
              "decision": report["decision"], "report": artifact,
              "verification": review["verification"], "at": timestamp()}
    marker.setdefault("reviewHistory", []).append(record)
    marker["lastReview"] = record
    if report["decision"] == "resume":
        marker["pending"] = False
        marker["reconciledAt"] = record["at"]
        state["waiting"] = "Сверка восстановления завершена; явно возобновите прогон"
    else:
        state["waiting"] = report["note"]
    return record
