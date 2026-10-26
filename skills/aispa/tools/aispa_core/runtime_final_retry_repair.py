"""One exact final-retry start bridge from the selected owner-attempt 0.4.0 pin.

The candidate is used only as a hash-bound source for three reviewed workflow
seams. No candidate code is imported into the maintenance process.
"""
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
from . import runtime_zone_repair as zone
from . import runtime_owner_attempt_repair as owner
from .store import Invalid, Journal, confined, digest, file_hash

OLD_DIGEST = "e7018aecae8a61f9b766b676013e01a2b2ef6f23090007b1691b2f433bf77211"
EVENT = "runtime-repaired:final-owner-retry-0.4.0"
STEM = "final-owner-retry-0.4.0-"
FILE = "tools/aispa_core/workflow.py"
SOURCE_HASHES = {FILE: "0d7606ce9273b1f79933489aad98818d11badd705461320051c6a0ae833dfe03"}
OLD_HASHES = {FILE: "0ac372f9676893419a954a98bd61be3cdf7fd570cf14be7154416ce2111e3392"}
NEW_HASHES = {FILE: "a271ee1701bfa2440f13e417dad9e22895300e564089f168d7219c3dca5b6d1c"}
CHANGED = frozenset({FILE})
MAX_APPROVAL_BYTES = 131072


def _need(ok, message):
    if not ok:
        raise Invalid("Final-retry runtime repair: " + message)


def _approval_bytes(path, expected_sha256, owner_quote):
    """Read a regular approval file with a physical cap, before and under lock."""
    try:
        before = path.lstat()
        _need(not is_link(path) and stat.S_ISREG(before.st_mode)
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
        raise Invalid("Final-retry runtime repair: не удалось прочитать обычный approval-file") from exc
    _need(0 < len(raw) <= MAX_APPROVAL_BYTES
          and hashlib.sha256(raw).hexdigest() == expected_sha256
          and owner_quote in raw.decode("utf-8"),
          "approval-file не совпадает с просмотренным решением и точной цитатой")
    return raw


def _patch(old_runtime, source):
    old = _tree(old_runtime)
    _need(digest(old) == OLD_DIGEST and old.get(FILE) == OLD_HASHES[FILE],
          "нужен точный выбранный owner-attempt runtime")
    _need(not is_link(source) and source.is_dir()
          and source.resolve() == Path(__file__).resolve().parents[2],
          "candidate должен быть собственным проверенным maintenance-пакетом")
    _need(file_hash(confined(source, FILE)) == SOURCE_HASHES[FILE],
          "source изменился после проверки")
    before = confined(old_runtime, FILE).read_text(encoding="utf-8")
    fresh = confined(source, FILE).read_text(encoding="utf-8")
    before = owner._replace_once(
        before, "from .team import TaskPipeline, task_proof_valid\n",
        "from .team import TaskPipeline, _history_valid, historical_proof_valid, task_proof_valid\n")
    old_import = ("from . import check_modes, project_context, discovery, carryover, cleanroom, blind, "
                  "process_runner, ideas, planning, knowledge, product, handoff, executions, briefmap, "
                  "design_contract, participants, integration, test_intent\n")
    before = owner._replace_once(before, old_import, old_import[:-1] + ", owner_task_attempt\n")
    helper = owner._span(fresh, "    def _final_retry_revalidates_dependencies(self, task):\n",
                         "    def build_ready(self):\n")
    before = owner._replace_once(before, "    def build_ready(self):\n",
                                 helper + "    def build_ready(self):\n")
    old_start = (
        '        closure = {t["id"] for t in task_closure(self.s, tid)} - {tid}\n'
        "        deferred = self.pipeline._deferred_ids()\n"
        "        require(all(task_proof_valid(self.store, self.s, dep, allowed_task_ids=closure)\n"
        '                    for dep in task["dependsOn"] if dep not in deferred),\n')
    new_start = (
        "        deferred = self.pipeline._deferred_ids()\n"
        "        require(self._dependencies_ready(task, deferred),\n")
    _need(new_start in fresh, "source start guard не совпал")
    before = owner._replace_once(before, old_start, new_start)
    old_ready = (
        '            closure = {t["id"] for t in task_closure(self.s, task["id"])} - {task["id"]}\n'
        "            if all(task_proof_valid(self.store, self.s, dep, allowed_task_ids=closure)\n"
        '                   for dep in task["dependsOn"] if dep not in deferred):\n')
    new_ready = "            if self._dependencies_ready(task, deferred):\n"
    _need(new_ready in fresh, "source ready guard не совпал")
    before = owner._replace_once(before, old_ready, new_ready)
    raw = before.encode("utf-8")
    changed = {FILE: raw}
    files = {**old, FILE: hashlib.sha256(raw).hexdigest()}
    _need(NEW_HASHES[FILE] and files[FILE] == NEW_HASHES[FILE]
          and {name for name in files if files[name] != old.get(name)} == CHANGED
          and set(files) == set(old), "patch вышел за узкий seam")
    return files, changed


def _scan_run(root):
    """Verify the journal while retaining only four repair event pairs."""
    _need(not is_link(Path(root)), "корень проекта является ссылкой")
    store = Journal(root)
    _need(store.home.is_dir(), "нет существующего .aispa")
    state = store.load()
    _need(state.get("version") == zone.VERSION
          and state.get("status") in {"running", "paused", "blocked"}
          and not state.get("journalRecovery", {}).get("pending"),
          "нужен активный проверенный прогон 0.4.0")
    cursor = store._verified
    _need(cursor is not None and cursor["path"] == confined(store.run, "events.jsonl"),
          "журнал не загружен")
    checked, external, validated = set(), set(), set()
    zone_rows, owner_rows, final_rows = [], [], []
    previous_state = zone_selected = zone_digest = owner_selected = final_selected = None
    count, hasher = 0, hashlib.sha256()
    with cursor["path"].open("rb") as history:
        for raw in history:
            hasher.update(raw)
            row = json.loads(raw)
            count += 1
            current = row["state"]
            _need(current.get("version") == zone.VERSION, "журнал содержит другую версию")
            store._verify_sources(current)
            zone._zone_local_artifacts(store, current, checked, external)
            zone._mixed_record_closure(store, current, validated)
            event = row["event"]
            if event in (zone.EVENT, zone.DEFERRED_EVENT):
                expected_zone_event = zone.EVENT if not zone_rows else zone.DEFERRED_EVENT
                _need(len(zone_rows) < 2 and event == expected_zone_event,
                      "лишнее событие или неверный порядок прежнего ремонта")
                if zone_selected is not None:
                    _need(previous_state.get("runtimeZoneRepair") == zone_selected
                          and previous_state.get("runtimeDigest") == zone_digest,
                          "прежний zone runtime изменён до следующего ремонта")
                zone_rows.append((count - 1, row, previous_state))
                zone_selected = current.get("runtimeZoneRepair")
                zone_digest = current.get("runtimeDigest")
                _need(isinstance(zone_selected, dict) and isinstance(zone_digest, str),
                      "zone repair не выбрал runtime")
            elif zone_selected is None:
                _need(current.get("runtimeZoneRepair") is None
                      and current.get("runtimeDigest") == zone.OFFICIAL_DIGEST,
                      "официальный runtime до первого zone repair изменён")
            else:
                _need(current.get("runtimeZoneRepair") == zone_selected,
                      "zone runtime изменён после ремонта")
                if owner_selected is None and event != owner.EVENT:
                    _need(current.get("runtimeDigest") == zone_digest,
                          "zone runtime digest изменён до owner repair")
            if event == owner.EVENT:
                _need(not owner_rows and not final_rows, "повтор или неверный порядок owner repair")
                owner_rows.append((count - 1, row, previous_state))
                owner_selected = current.get("runtimeOwnerAttemptRepair")
            elif owner_selected is None:
                _need(current.get("runtimeOwnerAttemptRepair") is None,
                      "выбор owner runtime возник без события")
            else:
                _need(current.get("runtimeOwnerAttemptRepair") == owner_selected,
                      "выбор owner runtime изменён после ремонта")
                if final_selected is None and event != EVENT:
                    _need(current.get("runtimeDigest") == OLD_DIGEST,
                          "owner runtime изменён до final retry repair")
            if event == EVENT:
                _need(len(owner_rows) == 1 and not final_rows,
                      "повтор или неверный порядок final retry repair")
                final_rows.append((count - 1, row, previous_state))
                final_selected = current.get("runtimeFinalRetryRepair")
            elif final_selected is None:
                _need(current.get("runtimeFinalRetryRepair") is None,
                      "выбор final retry runtime возник без события")
            else:
                _need(current.get("runtimeFinalRetryRepair") == final_selected
                      and current.get("runtimeDigest") == final_rows[0][1]["state"].get("runtimeDigest"),
                      "выбор final retry runtime изменён после ремонта")
            previous_state = current
    _need(count == cursor["count"] and hasher.hexdigest() == cursor["sha256"]
          and previous_state == state, "журнал изменился во время потоковой проверки")
    _need(digest(_tree(confined(store.run, "runtime"))) == zone.OFFICIAL_DIGEST,
          "оригинальный runtime не является официальным 0.4.0")
    journal = {"sha256": cursor["sha256"], "count": count,
               "verifiedRunArtifacts": len(checked),
               "externalReferencesNotRevalidated": len(external)}
    return store, state, journal, zone_rows, owner_rows, final_rows


def _owner_chain(store, zone_rows, owner_rows, history_path):
    parent_receipt, zone_manifest = owner._zone_chain(store, zone_rows, history_path)
    _need(len(owner_rows) == 1, "нужен точный выбранный owner-attempt repair")
    index, event, previous = owner_rows[0]
    receipt = event["state"].get("runtimeOwnerAttemptRepair")
    _need(isinstance(previous, dict) and isinstance(receipt, dict)
          and set(receipt) == {"kind", "id", "manifest", "beforeSequence",
                               "oldRuntimeDigest", "newRuntimeDigest", "beforeJournal", "approval"}
          and type(receipt["beforeSequence"]) is int and receipt["beforeSequence"] == index,
          "неверная квитанция owner-attempt repair")
    manifest = owner._manifest(store, receipt["manifest"])
    _need(manifest["parentManifest"] == parent_receipt["manifest"]
          and previous.get("runtimeZoneRepair") == parent_receipt
          and previous.get("runtimeDigest") == owner.OLD_DIGEST
          and receipt["kind"] == owner.EVENT and receipt["id"] == manifest["id"]
          and receipt["oldRuntimeDigest"] == owner.OLD_DIGEST
          and receipt["newRuntimeDigest"] == manifest["newRuntimeDigest"] == OLD_DIGEST,
          "owner-attempt repair не продолжает прежний pin")
    stage = confined(store.run, "runtime-repairs/" + manifest["id"])
    saved = confined(store.run, "runtime-repairs/" + manifest["id"] + "/receipt.json")
    _need(saved.is_file() and not is_link(saved)
          and json.loads(saved.read_text(encoding="utf-8")) == receipt, "квитанция owner изменена")
    before_ref = receipt["beforeJournal"]
    _need(isinstance(before_ref, dict) and before_ref == {
        "file": (stage / "before-events.jsonl").relative_to(store.run).as_posix(),
        "sha256": manifest["beforeJournalSha256"]}, "неверный owner before-journal")
    owner._prefix_matches(confined(store.run, before_ref["file"]), history_path,
                          manifest["beforeJournalSha256"], index)
    expected = {**previous, "runtimeDigest": manifest["newRuntimeDigest"],
                "runtimeOwnerAttemptRepair": receipt, "updatedAt": event["state"]["updatedAt"]}
    _need(event["state"] == expected, "owner repair изменил защищённое состояние")
    approval = receipt["approval"]
    _need(isinstance(approval, dict) and approval.get("file")
          == (stage / "owner-authorization.txt").relative_to(store.run).as_posix()
          and approval.get("sha256") == manifest["approvalSha256"]
          == file_hash(confined(store.run, approval["file"]))
          and manifest["ownerQuote"] in confined(store.run, approval["file"]).read_text(encoding="utf-8"),
          "разрешение owner repair изменено")
    _need(zone_manifest["newRuntimeDigest"] == owner.OLD_DIGEST,
          "неверный parent owner chain")
    return manifest, receipt


def _manifest(store, ref):
    _need(isinstance(ref, dict) and set(ref) == {"file", "sha256"}, "неверная ссылка manifest")
    path = confined(store.run, ref["file"])
    _need(path.is_file() and not is_link(path) and file_hash(path) == ref["sha256"],
          "manifest изменён")
    value = json.loads(path.read_text(encoding="utf-8"))
    _need(isinstance(value, dict) and set(value) == {
        "schema", "kind", "id", "run", "version", "sourceBaseline", "oldRuntimeDigest",
        "newRuntimeDigest", "beforeJournalSha256", "beforeSequence", "files", "diff",
        "runtimePath", "parentManifest", "sourceHashes", "approvalSha256", "ownerQuote"},
        "неверная схема manifest")
    owner._approval_binding(value["approvalSha256"], value["ownerQuote"])
    _need(type(value["schema"]) is int and value["schema"] == 1
          and type(value["beforeSequence"]) is int and value["beforeSequence"] > 0
          and value["kind"] == EVENT and value["run"] == store.run.name
          and value["version"] == zone.VERSION and value["sourceBaseline"] == zone.SOURCE_BASELINE
          and value["oldRuntimeDigest"] == OLD_DIGEST and value["sourceHashes"] == SOURCE_HASHES
          and re.fullmatch(STEM + r"[0-9a-f]{32}", value["id"])
          and value["runtimePath"] == "runtime-repairs/" + value["id"] + "/runtime"
          and ref["file"] == "runtime-repairs/" + value["id"] + "/manifest.json",
          "неверная привязка manifest")
    parent = owner._manifest(store, value["parentManifest"])
    _need(parent["newRuntimeDigest"] == OLD_DIGEST, "неверный parent manifest")
    old_files = _tree(confined(store.run, parent["runtimePath"]))
    _need(digest(old_files) == OLD_DIGEST and old_files.get(FILE) == OLD_HASHES[FILE],
          "предыдущий runtime изменён")
    files = {**old_files, **NEW_HASHES}
    runtime = confined(store.run, value["runtimePath"])
    _need(_tree(runtime) == files == value["files"]
          and digest(files) == value["newRuntimeDigest"], "runtime не совпадает с manifest")
    differences = [{"path": FILE, "before": old_files[FILE], "after": files[FILE]}]
    _need(value["diff"] == differences, "diff manifest изменён")
    return value


def _selected(root):
    store, state, journal, zone_rows, owner_rows, final_rows = _scan_run(root)
    history_path = confined(store.run, "events.jsonl")
    parent, parent_receipt = _owner_chain(store, zone_rows, owner_rows, history_path)
    _need(state.get("runtimeZoneRepair") is not None
          and state["runtimeZoneRepair"]["manifest"] == parent["parentManifest"]
          and state.get("runtimeOwnerAttemptRepair") == parent_receipt,
          "утеряна выбранная цепочка прежних ремонтов")
    old_runtime = confined(store.run, parent["runtimePath"])
    _need(digest(_tree(old_runtime)) == OLD_DIGEST, "старый pin изменён")
    if not final_rows:
        _need(state.get("runtimeFinalRetryRepair") is None
              and state.get("runtimeDigest") == OLD_DIGEST,
              "выбор final retry runtime возник без события")
        _need(file_hash(history_path) == journal["sha256"],
              "журнал изменился после проверки")
        return store, state, journal, old_runtime, None, None

    index, event, previous = final_rows[0]
    receipt = event["state"].get("runtimeFinalRetryRepair")
    _need(isinstance(previous, dict) and isinstance(receipt, dict)
          and set(receipt) == {"kind", "id", "manifest", "beforeSequence",
                               "oldRuntimeDigest", "newRuntimeDigest", "beforeJournal", "approval"}
          and type(receipt["beforeSequence"]) is int and receipt["beforeSequence"] == index,
          "неверная квитанция или позиция final retry repair")
    manifest = _manifest(store, receipt["manifest"])
    _need(manifest["parentManifest"] == parent_receipt["manifest"]
          and previous.get("runtimeOwnerAttemptRepair") == parent_receipt
          and previous.get("runtimeDigest") == OLD_DIGEST
          and receipt["kind"] == EVENT and receipt["id"] == manifest["id"]
          and receipt["oldRuntimeDigest"] == OLD_DIGEST
          and receipt["newRuntimeDigest"] == manifest["newRuntimeDigest"],
          "final retry repair не продолжает прежний pin")
    stage = confined(store.run, "runtime-repairs/" + manifest["id"])
    saved = confined(store.run, "runtime-repairs/" + manifest["id"] + "/receipt.json")
    _need(saved.is_file() and not is_link(saved)
          and json.loads(saved.read_text(encoding="utf-8")) == receipt,
          "квитанция final retry изменена")
    before_ref = receipt["beforeJournal"]
    _need(isinstance(before_ref, dict) and before_ref == {
        "file": (stage / "before-events.jsonl").relative_to(store.run).as_posix(),
        "sha256": manifest["beforeJournalSha256"]}, "неверный before-journal")
    owner._prefix_matches(confined(store.run, before_ref["file"]), history_path,
                          manifest["beforeJournalSha256"], index)
    expected = {**previous, "runtimeDigest": manifest["newRuntimeDigest"],
                "runtimeFinalRetryRepair": receipt, "updatedAt": event["state"]["updatedAt"]}
    _need(event["state"] == expected
          and state.get("runtimeFinalRetryRepair") == receipt
          and state.get("runtimeDigest") == manifest["newRuntimeDigest"],
          "repair изменил защищённое состояние или выбор утерян")
    approval = receipt["approval"]
    _need(isinstance(approval, dict)
          and approval.get("file")
          == (stage / "owner-authorization.txt").relative_to(store.run).as_posix()
          and approval.get("sha256") == manifest["approvalSha256"]
          == file_hash(confined(store.run, approval["file"]))
          and manifest["ownerQuote"] in confined(store.run, approval["file"]).read_text(encoding="utf-8"),
          "разрешение final retry изменено")
    _need(file_hash(history_path) == journal["sha256"],
          "журнал изменился после проверки")
    return store, state, journal, confined(store.run, manifest["runtimePath"]), manifest, receipt


def _result(loaded):
    store, state, journal, runtime, manifest, _receipt = loaded
    return {"run": state["run"], "version": zone.VERSION,
            "journalSha256": journal["sha256"],
            "alreadyRepaired": manifest is not None,
            "oldRuntimeDigest": OLD_DIGEST,
            "newRuntimeDigest": manifest["newRuntimeDigest"] if manifest else None,
            "runtime": str(runtime), "cli": str(runtime / "tools/aispa.py"),
            "originalRuntime": str(store.run / "runtime"),
            "allowedDiff": sorted(CHANGED),
            "verifiedRunArtifacts": journal["verifiedRunArtifacts"],
            "externalReferencesNotRevalidated": journal["externalReferencesNotRevalidated"],
            "sourceHashes": SOURCE_HASHES, "approvalSemanticsVerified": False}


def inspect(root):
    """Read-only and bounded by one state/event row plus four repair pairs."""
    return _result(_selected(root))


def prepare(root, *, candidate_package, expected_journal_sha256,
            expected_approval_sha256, owner_quote):
    owner._approval_binding(expected_approval_sha256, owner_quote)
    initial = _selected(root)
    owner._cas(initial[2], expected_journal_sha256)
    _need(initial[4] is None, "final retry repair уже выбран")
    store = initial[0]
    with store.lock():
        loaded = _selected(root)
        owner._cas(loaded[2], expected_journal_sha256)
        _need(loaded[4] is None, "final retry repair уже выбран")
        store, state, journal, old_runtime = loaded[:4]
        source = Path(candidate_package).absolute()
        files, changed = _patch(old_runtime, source)
        parent_ref = state["runtimeOwnerAttemptRepair"]["manifest"]
        ident = STEM + uuid.uuid4().hex
        relative = "runtime-repairs/" + ident
        stage = confined(store.run, relative)
        created = False
        try:
            stage.mkdir(parents=True, exist_ok=False)
            created = True
            target = stage / "runtime"
            target.mkdir()
            old = _tree(old_runtime)
            for name in sorted(files):
                raw = changed.get(name)
                if raw is None:
                    path = confined(old_runtime, name)
                    raw = path.read_bytes()
                    _need(hashlib.sha256(raw).hexdigest() == old[name],
                          "старый runtime изменился при копировании")
                destination = confined(target, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _write(destination, raw)
                if name in old and (confined(old_runtime, name).stat().st_mode & 0o111):
                    os.chmod(destination, 0o755)
            _need(_tree(target) == files and digest(_tree(old_runtime)) == OLD_DIGEST,
                  "runtime изменился при подготовке")
            _need(file_hash(confined(source, FILE)) == SOURCE_HASHES[FILE],
                  "source изменился при подготовке")
            differences = [{"path": FILE, "before": old[FILE], "after": files[FILE]}]
            manifest = {"schema": 1, "kind": EVENT, "id": ident, "run": state["run"],
                        "version": zone.VERSION, "sourceBaseline": zone.SOURCE_BASELINE,
                        "oldRuntimeDigest": OLD_DIGEST, "newRuntimeDigest": digest(files),
                        "beforeJournalSha256": expected_journal_sha256,
                        "beforeSequence": journal["count"],
                        "files": files, "diff": differences,
                        "runtimePath": relative + "/runtime", "parentManifest": parent_ref,
                        "sourceHashes": SOURCE_HASHES,
                        "approvalSha256": expected_approval_sha256, "ownerQuote": owner_quote}
            path = stage / "manifest.json"
            _write(path, (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
            for path in target.rglob("*"):
                if path.is_file():
                    os.chmod(path, 0o555 if path.stat().st_mode & 0o111 else 0o444)
            for path in sorted((p for p in target.rglob("*") if p.is_dir()),
                               key=lambda p: len(p.parts), reverse=True):
                store._sync_directory(path)
                os.chmod(path, 0o555)
            store._sync_directory(target)
            os.chmod(target, 0o555)
            os.chmod(stage / "manifest.json", 0o444)
            for directory in (stage, stage.parent, store.run):
                store._sync_directory(directory)
            owner._cas(_selected(root)[2], expected_journal_sha256)
            ref = {"file": relative + "/manifest.json", "sha256": file_hash(stage / "manifest.json")}
            _manifest(store, ref)
        except Exception:
            if created and stage.exists():
                for directory in (p for p in stage.rglob("*") if p.is_dir()):
                    os.chmod(directory, 0o755)
                shutil.rmtree(stage)
            raise
        return {"manifest": ref, "manifestPath": str(stage / "manifest.json"),
                "runtime": str(target), "newRuntimeDigest": digest(files),
                "diff": differences, "journalSha256": expected_journal_sha256,
                "selected": False}


def apply(root, *, manifest_file, expected_manifest_sha256,
          expected_journal_sha256, approval_file, expected_approval_sha256,
          owner_quote, confirm=False):
    _need(confirm, "нужен --confirm для разрешённого владельцем ремонта")
    owner._approval_binding(expected_approval_sha256, owner_quote)
    initial = _selected(root)
    owner._cas(initial[2], expected_journal_sha256)
    _need(initial[4] is None, "final retry repair уже выбран")
    store = initial[0]
    path = Path(manifest_file).absolute()
    _need(not is_link(path), "manifest является ссылкой")
    try:
        name = path.relative_to(store.run).as_posix()
    except ValueError as exc:
        raise Invalid("Final-retry runtime repair: manifest вне этого run") from exc
    ref = {"file": name, "sha256": expected_manifest_sha256}
    approval = Path(approval_file).absolute()
    approval_raw = _approval_bytes(approval, expected_approval_sha256, owner_quote)
    with store.lock():
        loaded = _selected(root)
        owner._cas(loaded[2], expected_journal_sha256)
        _need(loaded[4] is None, "final retry repair уже выбран")
        store, state, journal = loaded[:3]
        manifest = _manifest(store, ref)
        _need(manifest["beforeJournalSha256"] == expected_journal_sha256
              and manifest["beforeSequence"] == journal["count"]
              and manifest["parentManifest"] == state["runtimeOwnerAttemptRepair"]["manifest"]
              and manifest["approvalSha256"] == expected_approval_sha256
              and manifest["ownerQuote"] == owner_quote
              and _approval_bytes(approval, expected_approval_sha256, owner_quote) == approval_raw,
              "подготовка устарела")
        stage = confined(store.run, "runtime-repairs/" + manifest["id"])
        history_path = confined(store.run, "events.jsonl")
        saved = []
        try:
            path = stage / "before-events.jsonl"
            owner._stage_file(path, owner._prefix_chunks(history_path), expected_journal_sha256)
            saved.append(path)
            path = stage / "owner-authorization.txt"
            owner._stage_file(path, [approval_raw], expected_approval_sha256)
            saved.append(path)
            receipt = {"kind": EVENT, "id": manifest["id"], "manifest": ref,
                       "beforeSequence": journal["count"],
                       "oldRuntimeDigest": OLD_DIGEST,
                       "newRuntimeDigest": manifest["newRuntimeDigest"],
                       "beforeJournal": {"file": (stage / "before-events.jsonl").relative_to(store.run).as_posix(),
                                         "sha256": expected_journal_sha256},
                       "approval": {"file": (stage / "owner-authorization.txt").relative_to(store.run).as_posix(),
                                    "sha256": expected_approval_sha256}}
            path = stage / "receipt.json"
            raw = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
            owner._stage_file(path, [raw], hashlib.sha256(raw).hexdigest())
            saved.append(path)
            store._sync_directory(stage)
            _need(file_hash(history_path) == expected_journal_sha256,
                  "журнал изменился до append")
            _manifest(store, ref)
        except Exception:
            for path in reversed(saved):
                path.unlink(missing_ok=True)
            raise
        # After append begins, retain receipts even if the outcome is uncertain.
        state.update(runtimeDigest=manifest["newRuntimeDigest"],
                     runtimeFinalRetryRepair=receipt)
        try:
            store.append(state, EVENT)
        except Exception as exc:
            try:
                selected = _selected(root)
            except Exception:
                try:
                    Journal(root).inspect_recovery()
                except Exception:
                    raise Invalid("Final-retry runtime repair: OUTCOME_UNKNOWN_AFTER_APPEND; "
                                  "свидетельства сохранены, не повторяйте apply до осмотра журнала") from exc
                raise Invalid("Final-retry runtime repair: PARTIAL_JOURNAL_AFTER_APPEND; "
                              "свидетельства сохранены, нужен штатный journal-recover") from exc
            if selected[4] is not None and selected[5]["manifest"] == ref:
                return {**_result(selected), "selectionOutcome": "readback-complete-after-append-error",
                        "durability": "not-proven"}
            raise Invalid("Final-retry runtime repair: NO_VERIFIED_APPEND; "
                          "свидетельства сохранены, не повторяйте apply без осмотра") from exc
        try:
            return {**_result(_selected(root)), "selectionOutcome": "selected-readback"}
        except Exception as exc:
            raise Invalid("Final-retry runtime repair: OUTCOME_UNKNOWN_AFTER_APPEND; "
                          "свидетельства сохранены, не повторяйте apply до осмотра") from exc
