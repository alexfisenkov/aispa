"""Offline, consent-bound repair of the exact released 0.3.5 source-quote bug.

This module never imports or executes a pinned runtime. It stages a same-version
copy and appends one journal event; it is deliberately not a version migration.
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
from .store import Invalid, Journal, confined, digest, file_hash

LEGACY_VERSION = "0.3.5"
# Computed from every skills/aispa file in the immutable v0.3.5 Git tag.
OFFICIAL_DIGEST = "731a9f14499fadd6e0e8e3630830cab7d33a22c274d060d4641e0ea248a3619f"
REPAIRED_DIGEST = "4d1505637aee31af605ff753af7927971c8037f255ae210dea6686a9fd364a41"
DESIGN_PATH = "tools/aispa_core/design_contract.py"
ORIGINAL_DESIGN_SHA = "8923008daa21ce945090c8293730d8afd54075cb15dde4cc9c0ce8cd0b8732e5"
REPAIRED_DESIGN_SHA = "65e8431f85defc5810dcbdceb77922e906d2eabe0196782faea35975f1a59484"
OLD_TEXT = '_text(row["sourceQuote"], "цитата исходного брифа")'.encode("utf-8")
NEW_TEXT = '_text(row["sourceQuote"], "цитата исходного брифа", max(1600, len(anchor.get("quote", ""))))'.encode("utf-8")
EVENT = "runtime-repaired:source-quotes-0.3.5"


def _need(condition, message):
    if not condition:
        raise Invalid("Runtime repair: " + message)


class RepairJournal(Journal):
    """The sole version exception, scoped to this one standalone repair tool."""
    def _require_version(self, state, *, handoff_only=False):
        _need(state.get("version") == LEGACY_VERSION, "нужен активный прогон 0.3.5")


def _tree(package):
    """Hash code like the pinned CLI; links are forbidden even in cache paths."""
    _need(package.is_dir() and not is_link(package), "runtime отсутствует или является ссылкой")
    files = {}
    directories = []
    for current, dirs, names in os.walk(package, followlinks=False):
        for name in dirs + names:
            path = Path(current) / name
            _need(not is_link(path), "ссылка/junction в runtime: " + path.relative_to(package).as_posix())
            mode = path.stat().st_mode
            _need(stat.S_ISDIR(mode) or stat.S_ISREG(mode), "специальный файл в runtime")
            relative = path.relative_to(package)
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            if path.is_dir():
                directories.append(relative.as_posix())
            else:
                files[relative.as_posix()] = file_hash(path)
    # The digest already binds all file names; reject extra empty directories too.
    for directory in directories:
        _need(any(name.startswith(directory + "/") for name in files), "лишний пустой каталог runtime")
    return files


def _verified_runtime(package, *, repaired=False):
    files = _tree(package)
    design_sha = files.get(DESIGN_PATH)
    allowed = {REPAIRED_DESIGN_SHA} if repaired else {ORIGINAL_DESIGN_SHA, REPAIRED_DESIGN_SHA}
    _need(design_sha in allowed, "неизвестное изменение design_contract.py")
    normalized = dict(files, **{DESIGN_PATH: ORIGINAL_DESIGN_SHA})
    _need(digest(normalized) == OFFICIAL_DIGEST, "дерево runtime не совпадает с официальным 0.3.5")
    return files


def _local_artifacts(store, value, checked, external):
    """Verify run-scoped refs, including refs nested in immutable JSON artifacts.

    Project code/reference files and other runs have different freshness rules;
    those references are counted, never treated as current-run snapshots.
    """
    if isinstance(value, list):
        for child in value:
            _local_artifacts(store, child, checked, external)
    elif isinstance(value, dict):
        if "file" in value and "sha256" in value:
            name, sha = value["file"], value["sha256"]
            _need(isinstance(name, str) and isinstance(sha, str), "некорректная ссылка материала")
            # Artifact filenames are generated UUIDs; existing run directories also
            # identify missing/tampered snapshots without mistaking project refs.
            parts = Path(name).parts
            run_ref = bool(parts and not Path(name).is_absolute() and parts[0] not in (".aispa", "..") and (
                (store.run / parts[0]).exists() or re.fullmatch(r"[0-9a-f]{32}(?:\..*)?", Path(name).name)))
            if run_ref:
                path = confined(store.run, name)
                key = (name, sha)
                if key not in checked:
                    _need(path.is_file() and file_hash(path) == sha, "изменён материал прогона: " + name)
                    checked.add(key)
                    if path.suffix == ".json":
                        try:
                            nested = json.loads(path.read_text(encoding="utf-8"))
                        except (ValueError, UnicodeError) as exc:
                            raise Invalid("Runtime repair: повреждён JSON материала: " + name) from exc
                        _local_artifacts(store, nested, checked, external)
            else:
                external.add((name, sha))
        for child in value.values():
            _local_artifacts(store, child, checked, external)


def _load(root):
    _need(not is_link(Path(root)), "корень проекта не должен быть ссылкой")
    store = RepairJournal(root)
    _need(store.home.is_dir(), "нет существующего .aispa")
    state = store.load()
    _need(state.get("status") in {"running", "paused", "blocked"}, "нужен незавершённый активный прогон")
    _need(not state.get("journalRecovery", {}).get("pending"), "сначала завершите сверку восстановления журнала")
    journal = store.read_journal(include_events=True)
    checked, external = set(), set()
    for row in journal["events"]:
        old = row["state"]
        store._require_version(old)
        store._verify_sources(old)
        _local_artifacts(store, old, checked, external)
    original = confined(store.run, "runtime")
    files = _verified_runtime(original)
    repairs = state.get("runtimeRepairs", [])
    _need(isinstance(repairs, list) and len(repairs) <= 1, "неподдерживаемая история ремонтов")
    if repairs:
        repair = repairs[0]
        _need(isinstance(repair, dict) and repair.get("kind") == EVENT, "неизвестный ремонт")
        _need(isinstance(repair.get("id"), str) and re.fullmatch(r"source-quotes-0\.3\.5-[0-9a-f]{32}", repair["id"]),
              "неверный идентификатор ремонта")
        relative = "runtime-repairs/" + repair["id"]
        _need(repair.get("runtimePath") == relative + "/runtime", "неверный путь runtime ремонта")
        receipt = confined(store.run, relative + "/receipt.json")
        _need(receipt.is_file() and json.loads(receipt.read_text(encoding="utf-8")) == repair,
              "изменена квитанция ремонта")
        target = confined(store.run, repair["runtimePath"])
        _verified_runtime(target, repaired=True)
        _need(state.get("runtimeDigest") == REPAIRED_DIGEST, "изменён digest отремонтированного прогона")
        _need(repair.get("oldRuntimeDigest") == OFFICIAL_DIGEST and repair.get("newRuntimeDigest") == REPAIRED_DIGEST,
              "неверная квитанция ремонта")
        # Validate saved original history and the exact append-only transition.
        before = confined(store.run, repair["beforeJournal"]["file"])
        prefix = before.read_bytes()
        _need(file_hash(before) == repair["beforeJournal"]["sha256"], "изменён исходный журнал ремонта")
        raw = confined(store.run, "events.jsonl").read_bytes()
        _need(raw.startswith(prefix), "история до ремонта переписана")
        sequence = repair["beforeSequence"]
        _need(type(sequence) is int and 0 < sequence < len(journal["events"]), "неверная позиция ремонта")
        previous = journal["events"][sequence - 1]["state"]
        _need(previous.get("runtimeDigest") == OFFICIAL_DIGEST and not previous.get("runtimeRepairs"),
              "ремонт не основан на официальном закреплённом runtime")
        event = journal["events"][sequence]
        expected = copy.deepcopy(previous)
        expected.update(runtimeDigest=REPAIRED_DIGEST, runtimeRepairs=repairs, updatedAt=event["state"]["updatedAt"])
        _need(event["event"] == EVENT and event["state"] == expected, "ремонт изменил защищённое состояние")
        _need(len(prefix.splitlines()) == sequence, "неверный префикс журнала ремонта")
        _need(sum(row["event"] == EVENT for row in journal["events"]) == 1, "повтор события ремонта")
        _need(digest(files) == repair["sourceRuntimeDigest"] and files[DESIGN_PATH] == repair["sourceDesignSha256"],
              "исходный runtime изменён после ремонта")
    else:
        _need(not any(row["event"] == EVENT for row in journal["events"]), "потеряна квитанция ремонта")
        _need(state.get("runtimeDigest") == OFFICIAL_DIGEST, "записан неофициальный digest 0.3.5")
        target = original
    return store, state, journal, files, target, checked, external


def _result(loaded):
    store, state, journal, files, target, checked, external = loaded
    repaired = bool(state.get("runtimeRepairs"))
    return {"run": state["run"], "version": LEGACY_VERSION, "journalSha256": journal["sha256"],
            "alreadyRepaired": repaired, "oldRuntimeDigest": OFFICIAL_DIGEST,
            "sourceRuntimeDigest": digest(files), "newRuntimeDigest": REPAIRED_DIGEST,
            "sourceDesignSha256": files[DESIGN_PATH], "originalRuntime": str(store.run / "runtime"),
            "runtime": str(target), "cli": str(target / "tools/aispa.py"),
            "diff": {"path": DESIGN_PATH, "oldSha256": ORIGINAL_DESIGN_SHA, "newSha256": REPAIRED_DESIGN_SHA,
                     "old": OLD_TEXT.decode(), "new": NEW_TEXT.decode()},
            "verifiedRunArtifacts": len(checked), "externalReferencesNotRevalidated": len(external),
            "approvalSemanticsVerified": False,
            "approvalNotice": "Approval must contain the owner's real consent; this tool verifies bytes, not authority or meaning."}


def inspect(root):
    """Read-only: no lock file, staging, journal append, views, or dashboard writes."""
    return _result(_load(root))


def _write(path, raw):
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def apply(root, *, confirm=False, expected_journal_sha256=None, approval_file=None):
    _need(confirm, "требуется --confirm и реальное согласие владельца")
    _need(isinstance(expected_journal_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_journal_sha256),
          "нужен --expected-journal-sha256 из свежего осмотра")
    _need(approval_file is not None, "нужен --approval-file с реальным согласием владельца")
    approval = Path(approval_file).absolute()
    _need(approval.is_file() and not is_link(approval), "approval-file должен быть обычным файлом")
    approval_raw = approval.read_bytes()
    try:
        _need(bool(approval_raw.decode("utf-8").strip()), "согласие владельца пусто")
    except UnicodeError as exc:
        raise Invalid("Runtime repair: согласие владельца должно быть UTF-8") from exc
    approval_sha = hashlib.sha256(approval_raw).hexdigest()
    # Preliminary load also prevents lock() from creating a missing project.
    initial = _load(root)
    store = initial[0]
    with store.lock():
        loaded = _load(root)  # Fresh scan, source checks and runtime checks under lock.
        store, state, journal, files, _target, _checked, _external = loaded
        repairs = state.get("runtimeRepairs", [])
        if repairs:
            repair = repairs[0]
            _need(expected_journal_sha256 in (journal["sha256"], repair["beforeJournal"]["sha256"]),
                  "журнал изменился: повторите осмотр")
            # Old CAS is idempotent only immediately after that exact append.
            _need(expected_journal_sha256 == journal["sha256"] or journal["count"] == repair["beforeSequence"] + 1,
                  "журнал изменился после ремонта: повторите осмотр")
            _need(repair["approval"]["sha256"] == approval_sha, "повтор требует тот же файл согласия")
            return _result(loaded)
        _need(journal["sha256"] == expected_journal_sha256, "журнал изменился: повторите осмотр")
        repair_id = "source-quotes-0.3.5-" + uuid.uuid4().hex
        relative = "runtime-repairs/" + repair_id
        stage = confined(store.run, relative)
        stage.mkdir(parents=True, exist_ok=False)
        target = stage / "runtime"
        target.mkdir()
        # Copy only the verified release file inventory, never caches or links.
        for name in sorted(files):
            source = confined(store.run, "runtime/" + name)
            raw = source.read_bytes()
            _need(hashlib.sha256(raw).hexdigest() == files[name], "runtime изменился при копировании")
            if name == DESIGN_PATH and files[name] == ORIGINAL_DESIGN_SHA:
                _need(raw.count(OLD_TEXT) == 1, "неоднозначная строка ремонта")
                raw = raw.replace(OLD_TEXT, NEW_TEXT)
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write(destination, raw)
            shutil.copymode(source, destination)
        _verified_runtime(target, repaired=True)
        before_raw = confined(store.run, "events.jsonl").read_bytes()
        _need(hashlib.sha256(before_raw).hexdigest() == expected_journal_sha256, "журнал изменился перед записью")
        _write(stage / "before-events.jsonl", before_raw)
        _write(stage / "owner-approval.txt", approval_raw)
        repair = {"kind": EVENT, "id": repair_id, "runtimePath": relative + "/runtime",
                  "oldRuntimeDigest": OFFICIAL_DIGEST, "newRuntimeDigest": REPAIRED_DIGEST,
                  "sourceRuntimeDigest": digest(files), "sourceDesignSha256": files[DESIGN_PATH],
                  "beforeSequence": journal["count"],
                  "beforeJournal": {"file": relative + "/before-events.jsonl", "sha256": expected_journal_sha256},
                  "approval": {"file": relative + "/owner-approval.txt", "sha256": approval_sha}}
        _write(stage / "receipt.json", (json.dumps(repair, ensure_ascii=False, indent=2) + "\n").encode())
        # Persist staged directory entries before the journal selects this runtime.
        for directory in sorted((p for p in target.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            store._sync_directory(directory)
        for directory in (target, stage, stage.parent, store.run):
            store._sync_directory(directory)
        # Last validation leaves any failed staging orphaned, never selected.
        fresh = _load(root)
        _need(fresh[2]["sha256"] == expected_journal_sha256 and fresh[3] == files,
              "источники изменились перед добавлением события")
        store, state, journal = fresh[:3]
        state["runtimeDigest"] = REPAIRED_DIGEST
        state["runtimeRepairs"] = [repair]
        store.append(state, EVENT)
        store._sync_directory(stage)
        return _result(_load(root))
