"""Offline inspect/prepare/apply for bounded 0.4.0 zone-epoch maintenance.

The original runtime is never edited or executed. A reviewed bounded diff is
copied into an independently hash-bound runtime; one append selects its digest.
This is not an updater, policy migration, task reset or general runtime patcher.
One follow-up from the exact first maintenance digest repairs declared deferral
read-back. Both immutable copies/manifests and every journal prefix survive.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from .portable import is_link
from .runtime_repair import _tree, _write
from .store import Invalid, Journal, confined, digest, file_hash

VERSION = "0.4.0"
SOURCE_BASELINE = "0977f893106312a87068fb6533e03f1ca9ad8633"
OFFICIAL_DIGEST = "18da50b582274c2010d5e8607800053dab0134ca8f234674b653d83657ba33e9"
EVENT = "runtime-repaired:task-zone-epochs-0.4.0"
DEFERRED_EVENT = "runtime-repaired:task-zone-deferred-0.4.0"
FIRST_REPAIR_DIGEST = "3e41d77d07a803ac6c193be9a28759ea17574e418d5c891fd8d3f0167c8c4bf1"
FOLLOWUP_DIFF = frozenset({"tools/aispa_core/team.py", "tools/aispa_core/task_zone_extension.py",
                          "tools/aispa_core/runtime_zone_repair.py", "tools/repair_task_zones.py"})
ALLOWED_DIFF = frozenset({
    "tools/aispa.py", "tools/aispa_core/team.py", "tools/aispa_core/test_intent.py", "tools/aispa_core/repairs.py",
    "tools/aispa_core/planning.py", "tools/aispa_core/contracts.py", "tools/aispa_core/task_zone_extension.py",
    "tools/aispa_core/runtime_zone_repair.py", "tools/repair_task_zones.py",
    "references/task-zone-extension.md",
})
# Preserve the original required paths when reading an earlier immutable
# maintenance manifest. New preparations must still equal this tool's entire
# own package, including its repairs.py, before any copy is selected.
REQUIRED_DIFF = ALLOWED_DIFF - {"references/task-zone-extension.md", "tools/aispa_core/repairs.py"}
MAX_APPROVAL_BYTES = 131072


def _need(value, message):
    if not value:
        raise Invalid("Zone runtime repair: " + message)


def _approval_bytes(path, *, link_check=None):
    """Read one ordinary approval file with a physical cap and no link/FIFO wait."""
    if link_check is None:
        link_check = is_link
    try:
        before = path.lstat()
        _need(not link_check(path) and stat.S_ISREG(before.st_mode)
              and 0 < before.st_size <= MAX_APPROVAL_BYTES,
              "нужен ограниченный обычный approval-file")
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            _need(stat.S_ISREG(opened.st_mode)
                  and (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino)
                  and 0 < opened.st_size <= MAX_APPROVAL_BYTES,
                  "approval-file изменён при открытии")
            chunks, remaining = [], MAX_APPROVAL_BYTES + 1
            while remaining:
                block = os.read(fd, remaining)
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            raw = b"".join(chunks)
            _need(os.fstat(fd).st_size == len(raw),
                  "approval-file изменён при чтении")
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError) as exc:
        raise Invalid("Zone runtime repair: не удалось прочитать обычный approval-file") from exc
    _need(0 < len(raw) <= MAX_APPROVAL_BYTES, "approval-file превышает предел")
    try:
        _need(bool(raw.decode("utf-8").strip()), "файл разрешения пуст")
    except UnicodeError as exc:
        raise Invalid("Zone runtime repair: approval-file не является UTF-8") from exc
    return raw


def _zone_local_artifacts(store, value, checked, external):
    """Follow structural refs; hash evidence bytes without interpreting data.

    Observation attachments are authoritative only through a saved mixed
    report/intent and are checked separately by _mixed_record_closure.
    """
    if isinstance(value, list):
        for child in value:
            _zone_local_artifacts(store, child, checked, external)
    elif isinstance(value, dict):
        if "file" in value and "sha256" in value:
            name, sha = value["file"], value["sha256"]
            _need(isinstance(name, str) and isinstance(sha, str), "некорректная ссылка материала")
            parts = Path(name).parts
            run_ref = bool(parts and not Path(name).is_absolute() and parts[0] not in (".aispa", "..") and (
                (store.run / parts[0]).exists() or re.fullmatch(r"[0-9a-f]{32}(?:\..*)?", Path(name).name)))
            if run_ref:
                path = confined(store.run, name)
                key = name, sha
                if key not in checked:
                    _need(path.is_file() and file_hash(path) == sha, "изменён материал прогона: " + name)
                    checked.add(key)
                    if path.suffix == ".json" and parts[0] != "evidence":
                        try:
                            nested = json.loads(path.read_text(encoding="utf-8"))
                        except (ValueError, UnicodeError) as exc:
                            raise Invalid("Zone runtime repair: повреждён JSON материала: " + name) from exc
                        _zone_local_artifacts(store, nested, checked, external)
            else:
                external.add((name, sha))
        for child in value.values():
            _zone_local_artifacts(store, child, checked, external)


def _mixed_record_closure(store, state, validated):
    """Check typed observation edges and flattened membership on saved records.

    Use only engine-declared record slots, never keys guessed inside a blob.
    The native validator uses each record's immutable intent/basis, so later
    candidates and today's task status cannot rejudge historical observations.
    """
    from . import mixed_evidence
    from .team import _sealed_valid
    for history in state.get("taskHistory", {}).values():
        for attempt in history:
            records = [attempt.get(name) for name in ("submitted", "repair", "acceptance", "verifiedCompletion")]
            records += [record for name in ("rounds", "submissionHistory", "repairHistory") for record in attempt.get(name, [])]
            for record in records:
                if record is not None and record.get("testIntent"):
                    key = digest(record)
                    if key not in validated:
                        _need(_sealed_valid(store, record) and mixed_evidence.record_valid(store, record),
                              "Mixed evidence: повреждён sealed record или объявленные приложения отсутствуют в artifacts")
                        validated.add(key)


def _bounded(original, candidate):
    _need(digest(original) == OFFICIAL_DIGEST, "исходное дерево не является официальным 0.4.0")
    changed = {p for p in original.keys() | candidate.keys() if original.get(p) != candidate.get(p)}
    _need(REQUIRED_DIFF <= changed <= ALLOWED_DIFF and set(original) <= set(candidate),
          "изменение выходит за узкий zone-epoch maintenance fix или удаляет файл")
    return [{"path": p, "before": original.get(p), "after": candidate[p]} for p in sorted(changed)]


def _bounded_followup(previous, candidate):
    _need(digest(previous) == FIRST_REPAIR_DIGEST, "повторный repair требует точный проверенный первый maintenance runtime")
    changed = {p for p in previous.keys() | candidate.keys() if previous.get(p) != candidate.get(p)}
    _need(FOLLOWUP_DIFF - {"tools/repair_task_zones.py"} <= changed <= FOLLOWUP_DIFF
          and set(previous) == set(candidate), "повторный repair выходит за deferred-epoch fix")
    return [{"path": p, "before": previous.get(p), "after": candidate[p]} for p in sorted(changed)]


def _manifest(store, ref, depth=0):
    _need(depth <= 1, "слишком длинная цепочка runtime repair")
    _need(isinstance(ref, dict) and set(ref) == {"file", "sha256"}, "неверная ссылка manifest")
    path = confined(store.run, ref["file"])
    _need(path.is_file() and not is_link(path) and file_hash(path) == ref["sha256"], "изменён manifest")
    value = json.loads(path.read_text(encoding="utf-8"))
    _need(isinstance(value, dict), "manifest должен быть объектом")
    followup = value.get("schema") == 2
    _need(set(value) == {"schema", "kind", "id", "run", "version", "sourceBaseline", "oldRuntimeDigest",
            "newRuntimeDigest", "beforeJournalSha256", "beforeSequence", "files", "diff", "runtimePath"}
            | ({"parentManifest"} if followup else set())
          and type(value["schema"]) is int and value["schema"] in (1, 2)
          and value["kind"] == (DEFERRED_EVENT if followup else EVENT)
          and value["run"] == store.run.name
          and value["version"] == VERSION and value["sourceBaseline"] == SOURCE_BASELINE
          and value["oldRuntimeDigest"] == (FIRST_REPAIR_DIGEST if followup else OFFICIAL_DIGEST),
          "неверная схема или baseline manifest")
    stem = "task-zone-deferred" if followup else "task-zone-epochs"
    _need(re.fullmatch(stem + r"-0\.4\.0-[0-9a-f]{32}", value["id"])
          and value["runtimePath"] == "runtime-repairs/" + value["id"] + "/runtime"
          and ref["file"] == "runtime-repairs/" + value["id"] + "/manifest.json", "неверный путь manifest/runtime")
    original = _tree(confined(store.run, "runtime"))
    candidate = _tree(confined(store.run, value["runtimePath"]))
    _bounded(original, candidate)  # Every selected copy remains bounded against official 0.4.0.
    if followup:
        parent = _manifest(store, value["parentManifest"], depth + 1)
        _need(parent["schema"] == 1 and parent["newRuntimeDigest"] == FIRST_REPAIR_DIGEST,
              "неверный предшественник повторного repair")
        differences = _bounded_followup(_tree(confined(store.run, parent["runtimePath"])), candidate)
    else:
        differences = _bounded(original, candidate)
    _need(differences == value["diff"] and candidate == value["files"]
          and digest(candidate) == value["newRuntimeDigest"], "runtime не совпадает с проверяемым manifest")
    return value


def _load(root):
    _need(not is_link(Path(root)), "корень проекта является ссылкой")
    store = Journal(root)
    _need(store.home.is_dir(), "нет существующего .aispa")
    state = store.load()
    _need(state.get("version") == VERSION and state.get("status") in {"running", "paused", "blocked"},
          "нужен незавершённый активный прогон официальной 0.4.0")
    _need(not state.get("journalRecovery", {}).get("pending"), "сначала согласуйте восстановленный журнал")
    journal = store.read_journal(include_events=True)
    checked, external, validated_records = set(), set(), set()
    expected_runtime_digest = OFFICIAL_DIGEST
    for event in journal["events"]:
        current = event["state"]
        _need(current.get("version") == VERSION, "журнал содержит другую версию")
        if event["event"] in (EVENT, DEFERRED_EVENT):
            # The manifest/receipt chain below validates this exact transition.
            expected_runtime_digest = current.get("runtimeDigest")
        else:
            _need(current.get("runtimeDigest") == expected_runtime_digest,
                  "runtimeDigest изменён вне выбранного ремонта")
        store._verify_sources(current)
        _zone_local_artifacts(store, current, checked, external)
        _mixed_record_closure(store, current, validated_records)
    original = _tree(confined(store.run, "runtime"))
    _need(digest(original) == OFFICIAL_DIGEST, "оригинальный runtime не совпадает с официальным 0.4.0")
    selected = state.get("runtimeZoneRepair")
    events = [e for e in journal["events"] if e.get("event") in (EVENT, DEFERRED_EVENT)]
    if selected:
        _need(1 <= len(events) <= 2 and isinstance(selected, dict), "повреждена история выбора runtime")
        parent_receipt = None
        for index, event in enumerate(events):
            receipt = event["state"].get("runtimeZoneRepair")
            _need(isinstance(receipt, dict), "нет квитанции runtime repair")
            manifest = _manifest(store, receipt["manifest"])
            sequence = receipt["beforeSequence"]
            _need(type(sequence) is int and 0 < sequence < len(journal["events"])
                  and journal["events"][sequence] == event, "неверная позиция runtime repair")
            previous = journal["events"][sequence - 1]["state"]
            expected = {**previous, "runtimeDigest": manifest["newRuntimeDigest"], "runtimeZoneRepair": receipt,
                        "updatedAt": event["state"]["updatedAt"]}
            receipt_path = confined(store.run, "runtime-repairs/" + manifest["id"] + "/receipt.json")
            _need(receipt_path.is_file() and json.loads(receipt_path.read_text(encoding="utf-8")) == receipt,
                  "изменена квитанция выбора runtime")
            before = confined(store.run, receipt["beforeJournal"]["file"])
            prefix = before.read_bytes()
            _need(file_hash(before) == receipt["beforeJournal"]["sha256"] == manifest["beforeJournalSha256"]
                  and len(prefix.splitlines()) == sequence == manifest["beforeSequence"]
                  and confined(store.run, "events.jsonl").read_bytes().startswith(prefix), "изменён префикс журнала")
            _need(event["event"] == (EVENT if index == 0 else DEFERRED_EVENT)
                  and event["event"] == manifest["kind"] == receipt["kind"] and event["state"] == expected
                  and previous.get("runtimeZoneRepair") == parent_receipt
                  and previous.get("runtimeDigest") == manifest["oldRuntimeDigest"] == receipt["oldRuntimeDigest"]
                  and receipt["newRuntimeDigest"] == manifest["newRuntimeDigest"]
                  and (index == 0 or manifest.get("parentManifest") == parent_receipt["manifest"]),
                  "repair изменил защищённое состояние или цепочку")
            parent_receipt = receipt
        _need(selected == parent_receipt and state.get("runtimeDigest") == manifest["newRuntimeDigest"],
              "утерян последний выбор runtime")
        runtime = confined(store.run, manifest["runtimePath"])
    else:
        _need(not events and state.get("runtimeDigest") == OFFICIAL_DIGEST, "потеряна квитанция или неофициальный digest")
        manifest, runtime = None, confined(store.run, "runtime")
    return store, state, journal, original, manifest, runtime, checked, external


def _result(loaded):
    store, state, journal, original, manifest, runtime, checked, external = loaded
    return {"run": state["run"], "version": VERSION, "sourceBaseline": SOURCE_BASELINE,
            "journalSha256": journal["sha256"], "alreadyRepaired": manifest is not None,
            "oldRuntimeDigest": manifest["oldRuntimeDigest"] if manifest else OFFICIAL_DIGEST,
            "originalRuntimeDigest": OFFICIAL_DIGEST, "newRuntimeDigest": manifest["newRuntimeDigest"] if manifest else None,
            "runtime": str(runtime), "cli": str(runtime / "tools/aispa.py"),
            "originalRuntime": str(store.run / "runtime"), "allowedDiff": sorted(ALLOWED_DIFF),
            "verifiedRunArtifacts": len(checked), "externalReferencesNotRevalidated": len(external),
            "followupEligible": bool(manifest and manifest["schema"] == 1 and manifest["newRuntimeDigest"] == FIRST_REPAIR_DIGEST),
            "approvalSemanticsVerified": False,
            "approvalNotice": "Use the existing real owner authorization. Bytes are verified; authority and semantics require independent review."}


def inspect(root):
    """Strict read-only: no lock, stage, derived view or journal event."""
    return _result(_load(root))


def _cas(loaded, expected):
    _need(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected)
          and loaded[2]["sha256"] == expected, "stale CAS: повторите inspect/prepare")


def prepare(root, *, candidate_package, expected_journal_sha256):
    initial = _load(root)
    _cas(initial, expected_journal_sha256)
    store = initial[0]
    with store.lock():
        loaded = _load(root)
        _cas(loaded, expected_journal_sha256)
        store, state, journal, original = loaded[:4]
        parent = loaded[4]
        _need(parent is None or parent["schema"] == 1 and parent["newRuntimeDigest"] == FIRST_REPAIR_DIGEST,
              "runtime уже выбран; дополнительный repair для этого digest не разрешён")
        source = Path(candidate_package).absolute()
        _need(not is_link(source), "candidate является ссылкой")
        files = _tree(source)
        _need(files == _tree(Path(__file__).resolve().parents[2]),
              "candidate должен совпадать с проверяемым maintenance-пакетом самого инструмента")
        _bounded(original, files)
        differences = _bounded_followup(_tree(loaded[5]), files) if parent else _bounded(original, files)
        kind = DEFERRED_EVENT if parent else EVENT
        ident = ("task-zone-deferred-0.4.0-" if parent else "task-zone-epochs-0.4.0-") + uuid.uuid4().hex
        relative = "runtime-repairs/" + ident
        stage = confined(store.run, relative)
        stage.mkdir(parents=True, exist_ok=False)
        target = stage / "runtime"
        target.mkdir()
        for name, sha in sorted(files.items()):
            path = confined(source, name)
            raw = path.read_bytes()
            _need(hashlib.sha256(raw).hexdigest() == sha, "candidate изменился при копировании")
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write(destination, raw)
            shutil.copymode(path, destination)
        _need(_tree(target) == files and _tree(source) == files, "candidate изменился перед подготовкой manifest")
        manifest = {"schema": 2 if parent else 1, "kind": kind, "id": ident, "run": state["run"], "version": VERSION,
                    "sourceBaseline": SOURCE_BASELINE, "oldRuntimeDigest": state["runtimeDigest"],
                    "newRuntimeDigest": digest(files), "beforeJournalSha256": expected_journal_sha256,
                    "beforeSequence": journal["count"], "files": files, "diff": differences,
                    "runtimePath": relative + "/runtime"}
        if parent:
            manifest["parentManifest"] = copy.deepcopy(state["runtimeZoneRepair"]["manifest"])
        path = stage / "manifest.json"
        _write(path, (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
        for path in target.rglob("*"):
            if path.is_file():
                os.chmod(path, 0o555 if path.stat().st_mode & 0o111 else 0o444)
        for path in sorted((p for p in target.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            store._sync_directory(path)
            os.chmod(path, 0o555)
        store._sync_directory(target)
        os.chmod(target, 0o555)
        os.chmod(stage / "manifest.json", 0o444)
        for directory in (stage, stage.parent, store.run):
            store._sync_directory(directory)
        _cas(_load(root), expected_journal_sha256)
        ref = {"file": relative + "/manifest.json", "sha256": file_hash(stage / "manifest.json")}
        _manifest(store, ref)
        return {"manifest": ref, "manifestPath": str(stage / "manifest.json"),
                "runtime": str(target), "newRuntimeDigest": digest(files), "diff": differences,
                "journalSha256": expected_journal_sha256, "selected": False}


def apply(root, *, manifest_file, expected_manifest_sha256, expected_journal_sha256,
          approval_file, confirm=False):
    _need(confirm, "нужен --confirm для уже разрешённого ремонта")
    initial = _load(root)
    _cas(initial, expected_journal_sha256)
    store = initial[0]
    path = Path(manifest_file).absolute()
    _need(not is_link(path), "manifest является ссылкой")
    try:
        name = path.relative_to(store.run).as_posix()
    except ValueError as exc:
        raise Invalid("Zone runtime repair: manifest должен быть подготовлен внутри этого run") from exc
    ref = {"file": name, "sha256": expected_manifest_sha256}
    approval = Path(approval_file).absolute()
    approval_raw = _approval_bytes(approval)
    with store.lock():
        loaded = _load(root)
        _cas(loaded, expected_journal_sha256)
        store, state, journal = loaded[:3]
        manifest = _manifest(store, ref)
        _need(_approval_bytes(approval) == approval_raw,
              "файл разрешения изменился после проверки")
        if (state.get("runtimeZoneRepair") or {}).get("manifest") == ref:
            selected = state["runtimeZoneRepair"]
            _need(selected["manifest"] == ref and selected["approval"]["sha256"] == hashlib.sha256(approval_raw).hexdigest(),
                  "повтор отличается от выбранного ремонта")
            return _result(loaded)
        _need((manifest["schema"] == 1 and not state.get("runtimeZoneRepair"))
              or (manifest["schema"] == 2 and manifest.get("parentManifest") == (state.get("runtimeZoneRepair") or {}).get("manifest")
                  and state.get("runtimeDigest") == FIRST_REPAIR_DIGEST), "repair не продолжает текущий runtime")
        _need(manifest["beforeJournalSha256"] == expected_journal_sha256
              and manifest["beforeSequence"] == journal["count"], "подготовка устарела; повторите prepare")
        stage = confined(store.run, "runtime-repairs/" + manifest["id"])
        prefix = confined(store.run, "events.jsonl").read_bytes()
        _need(hashlib.sha256(prefix).hexdigest() == expected_journal_sha256, "журнал изменился перед записью")
        _write(stage / "before-events.jsonl", prefix)
        _write(stage / "owner-authorization.txt", approval_raw)
        receipt = {"kind": manifest["kind"], "id": manifest["id"], "manifest": ref,
                   "beforeSequence": journal["count"],
                   "oldRuntimeDigest": manifest["oldRuntimeDigest"], "newRuntimeDigest": manifest["newRuntimeDigest"],
                   "beforeJournal": {"file": (stage / "before-events.jsonl").relative_to(store.run).as_posix(),
                                     "sha256": expected_journal_sha256},
                   "approval": {"file": (stage / "owner-authorization.txt").relative_to(store.run).as_posix(),
                                "sha256": hashlib.sha256(approval_raw).hexdigest()}}
        _write(stage / "receipt.json", (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
        store._sync_directory(stage)
        fresh = _load(root)
        _cas(fresh, expected_journal_sha256)
        _manifest(store, ref)
        store, state = fresh[:2]
        state.update(runtimeDigest=manifest["newRuntimeDigest"], runtimeZoneRepair=receipt)
        store.append(state, manifest["kind"])
        store._sync_directory(stage)
        return _result(_load(root))
