"""Offline inspect/prepare/apply for the exact official 0.4.0 zone-epoch fix.

The original runtime is never edited or executed. A reviewed bounded diff is
copied into an independently hash-bound runtime; one append selects its digest.
This is not an updater, policy migration, task reset or general runtime patcher.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from .portable import is_link
from .runtime_repair import _local_artifacts, _tree, _write
from .store import Invalid, Journal, confined, digest, file_hash

VERSION = "0.4.0"
SOURCE_BASELINE = "0977f893106312a87068fb6533e03f1ca9ad8633"
OFFICIAL_DIGEST = "18da50b582274c2010d5e8607800053dab0134ca8f234674b653d83657ba33e9"
EVENT = "runtime-repaired:task-zone-epochs-0.4.0"
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


def _need(value, message):
    if not value:
        raise Invalid("Zone runtime repair: " + message)


def _bounded(original, candidate):
    _need(digest(original) == OFFICIAL_DIGEST, "исходное дерево не является официальным 0.4.0")
    changed = {p for p in original.keys() | candidate.keys() if original.get(p) != candidate.get(p)}
    _need(REQUIRED_DIFF <= changed <= ALLOWED_DIFF and set(original) <= set(candidate),
          "изменение выходит за узкий zone-epoch maintenance fix или удаляет файл")
    return [{"path": p, "before": original.get(p), "after": candidate[p]} for p in sorted(changed)]


def _manifest(store, ref):
    path = confined(store.run, ref["file"])
    _need(path.is_file() and not is_link(path) and file_hash(path) == ref["sha256"], "изменён manifest")
    value = json.loads(path.read_text(encoding="utf-8"))
    _need(set(value) == {"schema", "kind", "id", "run", "version", "sourceBaseline", "oldRuntimeDigest",
            "newRuntimeDigest", "beforeJournalSha256", "beforeSequence", "files", "diff", "runtimePath"}
          and value["schema"] == 1 and value["kind"] == EVENT and value["run"] == store.run.name
          and value["version"] == VERSION and value["sourceBaseline"] == SOURCE_BASELINE
          and value["oldRuntimeDigest"] == OFFICIAL_DIGEST,
          "неверная схема или baseline manifest")
    _need(re.fullmatch(r"task-zone-epochs-0\.4\.0-[0-9a-f]{32}", value["id"])
          and value["runtimePath"] == "runtime-repairs/" + value["id"] + "/runtime"
          and ref["file"] == "runtime-repairs/" + value["id"] + "/manifest.json", "неверный путь manifest/runtime")
    original = _tree(confined(store.run, "runtime"))
    candidate = _tree(confined(store.run, value["runtimePath"]))
    _need(_bounded(original, candidate) == value["diff"] and candidate == value["files"]
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
    checked, external = set(), set()
    for event in journal["events"]:
        _need(event["state"].get("version") == VERSION, "журнал содержит другую версию")
        store._verify_sources(event["state"])
        _local_artifacts(store, event["state"], checked, external)
    original = _tree(confined(store.run, "runtime"))
    _need(digest(original) == OFFICIAL_DIGEST, "оригинальный runtime не совпадает с официальным 0.4.0")
    selected = state.get("runtimeZoneRepair")
    events = [e for e in journal["events"] if e.get("event") == EVENT]
    if selected:
        _need(len(events) == 1 and isinstance(selected, dict), "повреждена история выбора runtime")
        manifest = _manifest(store, selected["manifest"])
        sequence = selected["beforeSequence"]
        _need(type(sequence) is int and 0 < sequence < len(journal["events"]), "неверная позиция runtime repair")
        event = journal["events"][sequence]
        previous = journal["events"][sequence - 1]["state"]
        expected = {**previous, "runtimeDigest": manifest["newRuntimeDigest"], "runtimeZoneRepair": selected,
                    "updatedAt": event["state"]["updatedAt"]}
        receipt_path = confined(store.run, "runtime-repairs/" + manifest["id"] + "/receipt.json")
        _need(receipt_path.is_file() and json.loads(receipt_path.read_text(encoding="utf-8")) == selected,
              "изменена квитанция выбора runtime")
        before = confined(store.run, selected["beforeJournal"]["file"])
        prefix = before.read_bytes()
        _need(file_hash(before) == selected["beforeJournal"]["sha256"] == manifest["beforeJournalSha256"]
              and len(prefix.splitlines()) == sequence == manifest["beforeSequence"]
              and confined(store.run, "events.jsonl").read_bytes().startswith(prefix), "изменён префикс журнала")
        _need(event["event"] == EVENT and event["state"] == expected
              and not previous.get("runtimeZoneRepair") and previous.get("runtimeDigest") == OFFICIAL_DIGEST
              and state.get("runtimeDigest") == manifest["newRuntimeDigest"], "repair изменил защищённое состояние")
        runtime = confined(store.run, manifest["runtimePath"])
    else:
        _need(not events and state.get("runtimeDigest") == OFFICIAL_DIGEST, "потеряна квитанция или неофициальный digest")
        manifest, runtime = None, confined(store.run, "runtime")
    return store, state, journal, original, manifest, runtime, checked, external


def _result(loaded):
    store, state, journal, original, manifest, runtime, checked, external = loaded
    return {"run": state["run"], "version": VERSION, "sourceBaseline": SOURCE_BASELINE,
            "journalSha256": journal["sha256"], "alreadyRepaired": manifest is not None,
            "oldRuntimeDigest": OFFICIAL_DIGEST, "newRuntimeDigest": manifest["newRuntimeDigest"] if manifest else None,
            "runtime": str(runtime), "cli": str(runtime / "tools/aispa.py"),
            "originalRuntime": str(store.run / "runtime"), "allowedDiff": sorted(ALLOWED_DIFF),
            "verifiedRunArtifacts": len(checked), "externalReferencesNotRevalidated": len(external),
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
        _need(not state.get("runtimeZoneRepair"), "runtime уже выбран; повторное применение не требуется")
        source = Path(candidate_package).absolute()
        _need(not is_link(source), "candidate является ссылкой")
        files = _tree(source)
        _need(files == _tree(Path(__file__).resolve().parents[2]),
              "candidate должен совпадать с проверяемым maintenance-пакетом самого инструмента")
        differences = _bounded(original, files)
        ident = "task-zone-epochs-0.4.0-" + uuid.uuid4().hex
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
        manifest = {"schema": 1, "kind": EVENT, "id": ident, "run": state["run"], "version": VERSION,
                    "sourceBaseline": SOURCE_BASELINE, "oldRuntimeDigest": OFFICIAL_DIGEST,
                    "newRuntimeDigest": digest(files), "beforeJournalSha256": expected_journal_sha256,
                    "beforeSequence": journal["count"], "files": files, "diff": differences,
                    "runtimePath": relative + "/runtime"}
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
    _need(approval.is_file() and not is_link(approval), "нужен файл исходного разрешения владельца")
    approval_raw = approval.read_bytes()
    _need(bool(approval_raw.decode("utf-8").strip()), "файл разрешения пуст")
    with store.lock():
        loaded = _load(root)
        _cas(loaded, expected_journal_sha256)
        store, state, journal = loaded[:3]
        manifest = _manifest(store, ref)
        if state.get("runtimeZoneRepair"):
            selected = state["runtimeZoneRepair"]
            _need(selected["manifest"] == ref and selected["approval"]["sha256"] == hashlib.sha256(approval_raw).hexdigest(),
                  "повтор отличается от выбранного ремонта")
            return _result(loaded)
        _need(manifest["beforeJournalSha256"] == expected_journal_sha256
              and manifest["beforeSequence"] == journal["count"], "подготовка устарела; повторите prepare")
        stage = confined(store.run, "runtime-repairs/" + manifest["id"])
        prefix = confined(store.run, "events.jsonl").read_bytes()
        _need(hashlib.sha256(prefix).hexdigest() == expected_journal_sha256, "журнал изменился перед записью")
        _write(stage / "before-events.jsonl", prefix)
        _write(stage / "owner-authorization.txt", approval_raw)
        receipt = {"kind": EVENT, "id": manifest["id"], "manifest": ref,
                   "beforeSequence": journal["count"],
                   "oldRuntimeDigest": OFFICIAL_DIGEST, "newRuntimeDigest": manifest["newRuntimeDigest"],
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
        store.append(state, EVENT)
        store._sync_directory(stage)
        return _result(_load(root))
