"""One inspected, source-bound owner-attempt bridge from the exact selected 0.4.0 pin.

This tool never imports code from the candidate or writes to the original pin.
Only reviewed snippets are spliced into known old files, then an immutable copy
is selected by one append-only journal event.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from .portable import is_link
from .runtime_repair import _tree, _write
from . import runtime_zone_repair as zone
from .store import Invalid, Journal, confined, digest, file_hash

OLD_DIGEST = "062f4fb7d168422870c47bb63725617947aef29bb2518811b8e2c5fae2c5a471"
EVENT = "runtime-repaired:owner-task-attempt-0.4.0"
STEM = "owner-task-attempt-0.4.0-"
SOURCE_HASHES = {
    # 0.5.0 package: the new-attempt parser and dispatch seams are byte-identical to 7bcfa114….
    "tools/aispa.py": "6bbd46c00b824d4313218cff317f5822fe3b7519fae8c8c29cdc6e35e17ab9cf",
    "tools/aispa_core/team.py": "0788ad62d24aafa866a9472aa5e7bb819c7674af1ebf35736225b4a366c247bc",
    "tools/aispa_core/workflow.py": "0d7606ce9273b1f79933489aad98818d11badd705461320051c6a0ae833dfe03",
    "tools/aispa_core/owner_task_attempt.py": "27cf4c2c480b65fb233c7db5dac4296186344b8722faf8bd81558346adba0b9a",
}
# Earlier reviewed sources of the same seams; manifests prepared from them stay valid.
HISTORICAL_SOURCE_HASHES = (
    {**SOURCE_HASHES,
     "tools/aispa.py": "7bcfa1146363fcfc85c1c5a7afe684ea249919ac386a1724fb28075d0041a465"},
    {**SOURCE_HASHES,
     "tools/aispa.py": "7bcfa1146363fcfc85c1c5a7afe684ea249919ac386a1724fb28075d0041a465",
     "tools/aispa_core/workflow.py": "7c53f2f7ca48ccc80962d8123144dd9dd47344ac65142f4950c998982c3b678d"},
)
OLD_HASHES = {
    "tools/aispa.py": "e1a07f26e106c1e826bb829192749f6755efefaf1aa980cf0699518190c6e080",
    "tools/aispa_core/team.py": "a34803f27c7a02806b791f2831bb7a4a798c42853e08cc0131db41faa1f7ef7c",
    "tools/aispa_core/workflow.py": "a1de6709698f5b4862919d22e60abaf4df733315df3e3dfb88426997b37715d4",
}
NEW_HASHES = {
    "tools/aispa.py": "18373d83315de53a71a3c56b065831c6fe1e1fbe5ef4acaaa681cc7c679eceaf",
    "tools/aispa_core/team.py": "1763f4957292946e6b1381584f0d77ef88ef50bc02cfda3d1da3de65ab51b453",
    "tools/aispa_core/workflow.py": "0ac372f9676893419a954a98bd61be3cdf7fd570cf14be7154416ce2111e3392",
    "tools/aispa_core/owner_task_attempt.py": SOURCE_HASHES["tools/aispa_core/owner_task_attempt.py"],
}
CHANGED = frozenset(SOURCE_HASHES)


def _need(ok, message):
    if not ok:
        raise Invalid("Owner-attempt runtime repair: " + message)


def _approval_binding(expected_approval_sha256, owner_quote):
    _need(isinstance(expected_approval_sha256, str)
          and re.fullmatch(r"[0-9a-f]{64}", expected_approval_sha256),
          "нужен SHA-256 просмотренного разрешения")
    _need(isinstance(owner_quote, str) and 0 < len(owner_quote.strip()) <= 2000
          and owner_quote == owner_quote.strip(), "нужна точная непустая цитата владельца")


def _span(text, start, end):
    _need(text.count(start) == 1 and text.count(end) == 1, "неоднозначный источник patch")
    a = text.index(start)
    b = text.index(end, a + len(start))
    return text[a:b]


def _replace_once(text, old, new):
    _need(text.count(old) == 1, "не совпал точный anchor старого runtime")
    return text.replace(old, new, 1)


def _patch(old_runtime, source):
    """Extract only four reviewed seams from exact source hashes; no code execution."""
    old = _tree(old_runtime)
    _need(digest(old) == OLD_DIGEST and all(old.get(k) == v for k, v in OLD_HASHES.items()),
          "нужен точный выбранный task-zone-deferred runtime")
    _need(not is_link(source) and source.is_dir() and source.resolve() == Path(__file__).resolve().parents[2],
          "candidate должен быть собственным проверенным maintenance-пакетом")
    _need(all(file_hash(confined(source, k)) == v for k, v in SOURCE_HASHES.items()),
          "source изменился после проверки")
    changed = {}
    team = confined(old_runtime, "tools/aispa_core/team.py").read_text(encoding="utf-8")
    fresh_team = confined(source, "tools/aispa_core/team.py").read_text(encoding="utf-8")
    old_import = "from . import autonomy, timing, repairs, participants, integration, test_intent, mixed_evidence, review_policy, task_tiers, check_modes\n"
    new_import = old_import[:-1] + ", owner_task_attempt\n"
    _need(new_import in fresh_team, "source import не совпал")
    team = _replace_once(team, old_import, new_import)
    branch = _span(fresh_team, "        if auth.get(\"kind\") == owner_task_attempt.KIND:\n", "        index, source = briefs.get(")
    team = _replace_once(team, "        index, source = briefs.get(", branch + "        index, source = briefs.get(")
    method = _span(fresh_team, "    def owner_new_attempt(self, tid, *,", "    def switch_check_mode(self, mode, words):")
    team = _replace_once(team, "    def switch_check_mode(self, mode, words):", method + "    def switch_check_mode(self, mode, words):")
    changed["tools/aispa_core/team.py"] = team.encode("utf-8")

    workflow = confined(old_runtime, "tools/aispa_core/workflow.py").read_text(encoding="utf-8")
    fresh_workflow = confined(source, "tools/aispa_core/workflow.py").read_text(encoding="utf-8")
    method = _span(fresh_workflow, "    def owner_new_attempt(self, tid, *,", "    def brief(self, path):")
    workflow = _replace_once(workflow, "    def brief(self, path):", method + "    def brief(self, path):")
    changed["tools/aispa_core/workflow.py"] = workflow.encode("utf-8")

    cli = confined(old_runtime, "tools/aispa.py").read_text(encoding="utf-8")
    fresh_cli = confined(source, "tools/aispa.py").read_text(encoding="utf-8")
    old_parser = _span(cli, '    attempt = sub.add_parser("new-attempt");', '    switch = sub.add_parser("check-mode");')
    new_parser = _span(fresh_cli, '    attempt = sub.add_parser("new-attempt");', '    switch = sub.add_parser("check-mode");')
    cli = _replace_once(cli, old_parser, new_parser)
    old_dispatch = _span(cli, '        elif action == "new-attempt":\n', '        elif action == "check-mode":\n')
    new_dispatch = _span(fresh_cli, '        elif action == "new-attempt":\n', '        elif action == "check-mode":\n')
    cli = _replace_once(cli, old_dispatch, new_dispatch)
    changed["tools/aispa.py"] = cli.encode("utf-8")
    changed["tools/aispa_core/owner_task_attempt.py"] = confined(source, "tools/aispa_core/owner_task_attempt.py").read_bytes()
    _need(hashlib.sha256(changed["tools/aispa_core/owner_task_attempt.py"]).hexdigest()
          == SOURCE_HASHES["tools/aispa_core/owner_task_attempt.py"], "source helper изменился")
    files = dict(old)
    files.update({name: hashlib.sha256(raw).hexdigest() for name, raw in changed.items()})
    _need({name for name in files if old.get(name) != files[name]} == CHANGED
          and set(files) == set(old) | {"tools/aispa_core/owner_task_attempt.py"}
          and all(files[k] == v for k, v in NEW_HASHES.items()), "patch вышел за узкий seam")
    return files, changed


def _prefix_matches(prefix_path, journal_path, expected_sha, expected_count):
    """Verify a saved prefix against current journal in bounded chunks."""
    _need(prefix_path.is_file() and not is_link(prefix_path), "префикс ремонта отсутствует или является ссылкой")
    hasher, lines = hashlib.sha256(), 0
    with prefix_path.open("rb") as saved, journal_path.open("rb") as history:
        while block := saved.read(65536):
            _need(history.read(len(block)) == block, "префикс журнала изменён")
            hasher.update(block)
            lines += block.count(b"\n")
    _need(hasher.hexdigest() == expected_sha and lines == expected_count,
          "префикс журнала или его поколение изменены")


def _prefix_chunks(path):
    with path.open("rb") as source:
        while block := source.read(65536):
            yield block


def _stage_file(target, chunks, expected_sha):
    """Write owned temp, fsync, then publish without overwriting a collision."""
    _need(not target.exists() and not is_link(target), "путь свидетельства уже занят")
    temp = target.with_name(".owner-attempt-" + uuid.uuid4().hex + ".tmp")
    published = created_temp = False
    hasher = hashlib.sha256()
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created_temp = True
        try:
            stream = os.fdopen(fd, "wb")
        except Exception:
            os.close(fd)
            raise
        with stream:
            for block in chunks:
                stream.write(block)
                hasher.update(block)
            stream.flush()
            os.fsync(stream.fileno())
        _need(hasher.hexdigest() == expected_sha, "источник свидетельства изменился при копировании")
        os.link(temp, target)  # Atomic no-replace publication on the same filesystem.
        published = True
        temp.unlink()
    except Exception:
        if published:
            target.unlink(missing_ok=True)  # Only the link created above is ours.
        raise
    finally:
        if created_temp:
            temp.unlink(missing_ok=True)


def _scan_run(root):
    """The store validates the chain, then we retain only three addressed event pairs.

    No read_journal(include_events=True), events list, or full journal read_bytes.
    A second hash binds the streaming artifact scan to Journal.load's generation.
    """
    _need(not is_link(Path(root)), "корень проекта является ссылкой")
    store = zone.RepairJournal(root)
    _need(store.home.is_dir(), "нет существующего .aispa")
    state = store.load()
    _need(state.get("version") == zone.VERSION and state.get("status") in {"running", "paused", "blocked"}
          and not state.get("journalRecovery", {}).get("pending"), "нужен активный проверенный прогон 0.4.0")
    cursor = store._verified
    _need(cursor is not None and cursor["path"] == confined(store.run, "events.jsonl"), "журнал не загружен")
    checked, external, validated = set(), set(), set()
    zone_rows, owner_rows = [], []
    previous_state = None
    owner_selected = None
    expected_runtime_digest = zone.OFFICIAL_DIGEST
    count, hasher = 0, hashlib.sha256()
    with cursor["path"].open("rb") as history:
        for raw in history:
            hasher.update(raw)
            row = json.loads(raw)
            count += 1
            current = row["state"]
            _need(current.get("version") == zone.VERSION, "журнал содержит другую версию")
            if row["event"] in (zone.EVENT, zone.DEFERRED_EVENT, EVENT):
                # The addressed zone/owner receipts validate these transitions.
                expected_runtime_digest = current.get("runtimeDigest")
            else:
                _need(current.get("runtimeDigest") == expected_runtime_digest,
                      "runtimeDigest изменён вне выбранного ремонта")
            store._verify_sources(current)
            zone._zone_local_artifacts(store, current, checked, external)
            zone._mixed_record_closure(store, current, validated)
            if row["event"] in (zone.EVENT, zone.DEFERRED_EVENT):
                _need(len(zone_rows) < 2, "лишнее событие прежнего ремонта")
                zone_rows.append((count - 1, row, previous_state))
            if row["event"] == EVENT:
                _need(not owner_rows, "повтор события owner-attempt repair")
                owner_rows.append((count - 1, row, previous_state))
                owner_selected = current.get("runtimeOwnerAttemptRepair")
            elif owner_selected is None:
                _need(current.get("runtimeOwnerAttemptRepair") is None,
                      "выбор owner-attempt runtime возник без события")
            else:
                _need(current.get("runtimeOwnerAttemptRepair") == owner_selected
                      and current.get("runtimeDigest") == owner_rows[0][1]["state"].get("runtimeDigest"),
                      "выбор runtime изменён после ремонта")
            previous_state = current
    _need(count == cursor["count"] and hasher.hexdigest() == cursor["sha256"]
          and previous_state == state, "журнал изменился во время потоковой проверки")
    _need(digest(_tree(confined(store.run, "runtime"))) == zone.OFFICIAL_DIGEST,
          "оригинальный runtime не является официальным 0.4.0")
    journal = {"sha256": cursor["sha256"], "count": count,
               "verifiedRunArtifacts": len(checked),
               "externalReferencesNotRevalidated": len(external)}
    return store, state, journal, zone_rows, owner_rows


def _zone_chain(store, zone_rows, journal_path):
    _need(len(zone_rows) == 2 and [row["event"] for _, row, _ in zone_rows]
          == [zone.EVENT, zone.DEFERRED_EVENT], "нужна точная цепочка двух прежних ремонтов")
    previous_receipt = None
    for index, row, prior in zone_rows:
        _need(index > 0 and isinstance(prior, dict), "неверная позиция прежнего ремонта")
        receipt = row["state"].get("runtimeZoneRepair")
        _need(isinstance(receipt, dict) and receipt.get("beforeSequence") == index,
              "повреждена прежняя квитанция")
        manifest = zone._manifest(store, receipt["manifest"])
        prefix = confined(store.run, receipt["beforeJournal"]["file"])
        _prefix_matches(prefix, journal_path, manifest["beforeJournalSha256"], index)
        _need(receipt["beforeJournal"]["sha256"] == manifest["beforeJournalSha256"],
              "неверная SHA прежнего префикса")
        saved = confined(store.run, "runtime-repairs/" + manifest["id"] + "/receipt.json")
        _need(saved.is_file() and json.loads(saved.read_text(encoding="utf-8")) == receipt,
              "повреждена прежняя квитанция")
        expected = {**prior, "runtimeDigest": manifest["newRuntimeDigest"],
                    "runtimeZoneRepair": receipt, "updatedAt": row["state"]["updatedAt"]}
        _need(row["state"] == expected and row["event"] == manifest["kind"] == receipt["kind"]
              and prior.get("runtimeDigest") == manifest["oldRuntimeDigest"] == receipt["oldRuntimeDigest"]
              and receipt["newRuntimeDigest"] == manifest["newRuntimeDigest"]
              and prior.get("runtimeZoneRepair") == previous_receipt
              and (previous_receipt is None or manifest.get("parentManifest") == previous_receipt["manifest"]),
              "прежний repair изменил защищённое состояние")
        previous_receipt = receipt
    _need(manifest["schema"] == 2 and manifest["newRuntimeDigest"] == OLD_DIGEST,
          "нужен точный task-zone-deferred pin")
    return previous_receipt, manifest


def _cas(journal, expected):
    _need(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected)
          and journal["sha256"] == expected, "stale journal CAS; повторите inspect/prepare")


def _manifest(store, ref):
    _need(isinstance(ref, dict) and set(ref) == {"file", "sha256"}, "неверная ссылка manifest")
    path = confined(store.run, ref["file"])
    _need(path.is_file() and not is_link(path) and file_hash(path) == ref["sha256"], "manifest изменён")
    value = json.loads(path.read_text(encoding="utf-8"))
    _need(isinstance(value, dict) and set(value) == {"schema", "kind", "id", "run", "version", "sourceBaseline",
          "oldRuntimeDigest", "newRuntimeDigest", "beforeJournalSha256", "beforeSequence", "files", "diff",
          "runtimePath", "parentManifest", "sourceHashes", "approvalSha256", "ownerQuote"},
          "неверная схема manifest")
    _approval_binding(value["approvalSha256"], value["ownerQuote"])
    _need(type(value["schema"]) is int and value["schema"] == 1
          and type(value["beforeSequence"]) is int and value["beforeSequence"] > 0
          and value["kind"] == EVENT and value["run"] == store.run.name
          and value["version"] == zone.VERSION and value["sourceBaseline"] == zone.SOURCE_BASELINE
          and value["oldRuntimeDigest"] == OLD_DIGEST
          and value["sourceHashes"] in (SOURCE_HASHES, *HISTORICAL_SOURCE_HASHES)
          and re.fullmatch(STEM + r"[0-9a-f]{32}", value["id"])
          and value["runtimePath"] == "runtime-repairs/" + value["id"] + "/runtime"
          and ref["file"] == "runtime-repairs/" + value["id"] + "/manifest.json", "неверная привязка manifest")
    parent = zone._manifest(store, value["parentManifest"])
    _need(parent["schema"] == 2 and parent["newRuntimeDigest"] == OLD_DIGEST,
          "неверный parent manifest")
    runtime = confined(store.run, value["runtimePath"])
    old_files = _tree(confined(store.run, parent["runtimePath"]))
    _need(digest(old_files) == OLD_DIGEST and all(old_files.get(k) == v for k, v in OLD_HASHES.items()),
          "предыдущий runtime изменён")
    files = {**old_files, **NEW_HASHES}
    _need(_tree(runtime) == files == value["files"] and digest(files) == value["newRuntimeDigest"],
          "runtime не совпадает с manifest")
    differences = [{"path": p, "before": old_files.get(p),
                    "after": files[p]} for p in sorted(CHANGED)]
    _need(value["diff"] == differences, "diff manifest изменён")
    return value


def _selected(root):
    """Validate old selection or our event with bounded event retention."""
    store, state, journal, zone_rows, owner_rows = _scan_run(root)
    history_path = confined(store.run, "events.jsonl")
    parent_receipt, parent = _zone_chain(store, zone_rows, history_path)
    _need(state.get("runtimeZoneRepair") == parent_receipt,
          "утерян выбранный task-zone-deferred runtime")
    old_runtime = confined(store.run, parent["runtimePath"])
    _need(digest(_tree(old_runtime)) == OLD_DIGEST, "старый pin изменён")
    if not owner_rows:
        _need(state.get("runtimeOwnerAttemptRepair") is None and state.get("runtimeDigest") == OLD_DIGEST,
              "выбор owner-attempt runtime возник без события")
        _need(file_hash(history_path) == journal["sha256"], "журнал изменился после проверки")
        return store, state, journal, old_runtime, None, None

    index, event, previous = owner_rows[0]
    receipt = event["state"].get("runtimeOwnerAttemptRepair")
    _need(isinstance(previous, dict) and isinstance(receipt, dict)
          and set(receipt) == {"kind", "id", "manifest", "beforeSequence",
                                "oldRuntimeDigest", "newRuntimeDigest", "beforeJournal", "approval"}
          and type(receipt["beforeSequence"]) is int and receipt["beforeSequence"] == index,
          "неверная квитанция или позиция owner-attempt repair")
    manifest = _manifest(store, receipt["manifest"])
    _need(manifest["parentManifest"] == parent_receipt["manifest"]
          and previous.get("runtimeZoneRepair") == parent_receipt
          and previous.get("runtimeDigest") == OLD_DIGEST
          and receipt["kind"] == EVENT and receipt["id"] == manifest["id"]
          and receipt["oldRuntimeDigest"] == OLD_DIGEST
          and receipt["newRuntimeDigest"] == manifest["newRuntimeDigest"],
          "owner-attempt repair не продолжает прежний pin")
    stage = confined(store.run, "runtime-repairs/" + manifest["id"])
    saved = confined(store.run, "runtime-repairs/" + manifest["id"] + "/receipt.json")
    _need(saved.is_file() and not is_link(saved)
          and json.loads(saved.read_text(encoding="utf-8")) == receipt, "квитанция изменена")
    before_ref = receipt["beforeJournal"]
    _need(isinstance(before_ref, dict) and before_ref == {
        "file": (stage / "before-events.jsonl").relative_to(store.run).as_posix(),
        "sha256": manifest["beforeJournalSha256"]}, "неверный before-journal")
    _prefix_matches(confined(store.run, before_ref["file"]), history_path,
                    manifest["beforeJournalSha256"], index)
    expected = {**previous, "runtimeDigest": manifest["newRuntimeDigest"],
                "runtimeOwnerAttemptRepair": receipt, "updatedAt": event["state"]["updatedAt"]}
    _need(event["state"] == expected and state.get("runtimeOwnerAttemptRepair") == receipt
          and state.get("runtimeDigest") == manifest["newRuntimeDigest"],
          "repair изменил защищённое состояние или выбор утерян")
    approval = receipt["approval"]
    _need(isinstance(approval, dict) and approval.get("file") ==
          (stage / "owner-authorization.txt").relative_to(store.run).as_posix()
          and approval.get("sha256") == manifest["approvalSha256"]
          == file_hash(confined(store.run, approval["file"]))
          and manifest["ownerQuote"] in confined(store.run, approval["file"]).read_text(encoding="utf-8"),
          "разрешение изменено")
    _need(file_hash(history_path) == journal["sha256"], "журнал изменился после проверки")
    return store, state, journal, confined(store.run, manifest["runtimePath"]), manifest, receipt


def _result(loaded):
    store, state, journal, runtime, manifest, _receipt = loaded
    return {"run": state["run"], "version": zone.VERSION, "journalSha256": journal["sha256"],
            "alreadyRepaired": manifest is not None, "oldRuntimeDigest": OLD_DIGEST,
            "newRuntimeDigest": manifest["newRuntimeDigest"] if manifest else None,
            "runtime": str(runtime), "cli": str(runtime / "tools/aispa.py"),
            "originalRuntime": str(store.run / "runtime"), "allowedDiff": sorted(CHANGED),
            "verifiedRunArtifacts": journal["verifiedRunArtifacts"],
            "externalReferencesNotRevalidated": journal["externalReferencesNotRevalidated"],
            "sourceHashes": SOURCE_HASHES, "approvalSemanticsVerified": False}


def inspect(root):
    """Read-only; does not create a lock, stage, or journal event."""
    return _result(_selected(root))


def prepare(root, *, candidate_package, expected_journal_sha256, expected_approval_sha256, owner_quote):
    _approval_binding(expected_approval_sha256, owner_quote)
    initial = _selected(root)
    _cas(initial[2], expected_journal_sha256)
    _need(initial[4] is None, "owner-attempt repair уже выбран")
    store = initial[0]
    with store.lock():
        loaded = _selected(root)
        _cas(loaded[2], expected_journal_sha256)
        _need(loaded[4] is None, "owner-attempt repair уже выбран")
        store, state, journal, old_runtime = loaded[:4]
        source = Path(candidate_package).absolute()
        files, changed = _patch(old_runtime, source)
        parent_ref = state["runtimeZoneRepair"]["manifest"]
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
                    _need(hashlib.sha256(raw).hexdigest() == old[name], "старый runtime изменился при копировании")
                destination = confined(target, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _write(destination, raw)
                if name in old and (confined(old_runtime, name).stat().st_mode & 0o111):
                    os.chmod(destination, 0o755)
            _need(_tree(target) == files and digest(_tree(old_runtime)) == OLD_DIGEST,
                  "runtime изменился при подготовке")
            _need(all(file_hash(confined(source, k)) == v for k, v in SOURCE_HASHES.items()),
                  "source изменился при подготовке")
            differences = [{"path": p, "before": old.get(p), "after": files[p]} for p in sorted(CHANGED)]
            manifest = {"schema": 1, "kind": EVENT, "id": ident, "run": state["run"],
                        "version": zone.VERSION, "sourceBaseline": zone.SOURCE_BASELINE,
                        "oldRuntimeDigest": OLD_DIGEST, "newRuntimeDigest": digest(files),
                        "beforeJournalSha256": expected_journal_sha256, "beforeSequence": journal["count"],
                        "files": files, "diff": differences, "runtimePath": relative + "/runtime",
                        "parentManifest": parent_ref, "sourceHashes": SOURCE_HASHES,
                        "approvalSha256": expected_approval_sha256, "ownerQuote": owner_quote}
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
            _cas(_selected(root)[2], expected_journal_sha256)
            ref = {"file": relative + "/manifest.json", "sha256": file_hash(stage / "manifest.json")}
            _manifest(store, ref)
        except Exception:
            if created and stage.exists():
                for directory in (p for p in stage.rglob("*") if p.is_dir()):
                    os.chmod(directory, 0o755)
                shutil.rmtree(stage)
            raise
        return {"manifest": ref, "manifestPath": str(stage / "manifest.json"),
                "runtime": str(target), "newRuntimeDigest": digest(files), "diff": differences,
                "journalSha256": expected_journal_sha256, "selected": False}


def apply(root, *, manifest_file, expected_manifest_sha256, expected_journal_sha256,
          approval_file, expected_approval_sha256, owner_quote, confirm=False):
    _need(confirm, "нужен --confirm для разрешённого владельцем ремонта")
    _approval_binding(expected_approval_sha256, owner_quote)
    initial = _selected(root)
    _cas(initial[2], expected_journal_sha256)
    _need(initial[4] is None, "owner-attempt repair уже выбран")
    store = initial[0]
    path = Path(manifest_file).absolute()
    _need(not is_link(path), "manifest является ссылкой")
    try:
        name = path.relative_to(store.run).as_posix()
    except ValueError as exc:
        raise Invalid("Owner-attempt runtime repair: manifest вне этого run") from exc
    ref = {"file": name, "sha256": expected_manifest_sha256}
    approval = Path(approval_file).absolute()
    approval_raw = zone._approval_bytes(approval, link_check=is_link)
    _need(hashlib.sha256(approval_raw).hexdigest() == expected_approval_sha256
          and owner_quote in approval_raw.decode("utf-8"),
          "approval-file не совпадает с просмотренным решением и точной цитатой")
    with store.lock():
        loaded = _selected(root)
        _cas(loaded[2], expected_journal_sha256)
        _need(loaded[4] is None, "owner-attempt repair уже выбран")
        store, state, journal = loaded[:3]
        manifest = _manifest(store, ref)
        _need(manifest["beforeJournalSha256"] == expected_journal_sha256
              and manifest["beforeSequence"] == journal["count"]
              and manifest["parentManifest"] == state["runtimeZoneRepair"]["manifest"]
              and manifest["approvalSha256"] == expected_approval_sha256
              and manifest["ownerQuote"] == owner_quote
              and zone._approval_bytes(approval, link_check=is_link) == approval_raw,
              "подготовка устарела")
        stage = confined(store.run, "runtime-repairs/" + manifest["id"])
        history_path = confined(store.run, "events.jsonl")
        saved = []
        try:
            path = stage / "before-events.jsonl"
            _stage_file(path, _prefix_chunks(history_path), expected_journal_sha256)
            saved.append(path)
            path = stage / "owner-authorization.txt"
            _stage_file(path, [approval_raw], expected_approval_sha256)
            saved.append(path)
            receipt = {"kind": EVENT, "id": manifest["id"], "manifest": ref,
                       "beforeSequence": journal["count"], "oldRuntimeDigest": OLD_DIGEST,
                       "newRuntimeDigest": manifest["newRuntimeDigest"],
                       "beforeJournal": {"file": (stage / "before-events.jsonl").relative_to(store.run).as_posix(),
                                         "sha256": expected_journal_sha256},
                       "approval": {"file": (stage / "owner-authorization.txt").relative_to(store.run).as_posix(),
                                    "sha256": expected_approval_sha256}}
            path = stage / "receipt.json"
            raw = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
            _stage_file(path, [raw], hashlib.sha256(raw).hexdigest())
            saved.append(path)
            store._sync_directory(stage)
            _need(file_hash(history_path) == expected_journal_sha256, "журнал изменился до append")
            _manifest(store, ref)
        except Exception:
            for path in reversed(saved):
                path.unlink(missing_ok=True)
            raise
        # From this point an exception may mean that append wrote a complete or
        # partial event. Never remove its supporting files or append again.
        state.update(runtimeDigest=manifest["newRuntimeDigest"], runtimeOwnerAttemptRepair=receipt)
        try:
            store.append(state, EVENT)
        except Exception as exc:
            try:
                selected = _selected(root)
            except Exception:
                try:
                    zone.RepairJournal(root).inspect_recovery()
                except Exception:
                    raise Invalid("Owner-attempt runtime repair: OUTCOME_UNKNOWN_AFTER_APPEND; "
                                  "свидетельства сохранены, не повторяйте apply до осмотра журнала") from exc
                raise Invalid("Owner-attempt runtime repair: PARTIAL_JOURNAL_AFTER_APPEND; "
                              "свидетельства сохранены, нужен штатный journal-recover") from exc
            if selected[4] is not None and selected[5]["manifest"] == ref:
                return {**_result(selected), "selectionOutcome": "readback-complete-after-append-error",
                        "durability": "not-proven"}
            raise Invalid("Owner-attempt runtime repair: NO_VERIFIED_APPEND; "
                          "свидетельства сохранены, не повторяйте apply без осмотра") from exc
        try:
            return {**_result(_selected(root)), "selectionOutcome": "selected-readback"}
        except Exception as exc:
            raise Invalid("Owner-attempt runtime repair: OUTCOME_UNKNOWN_AFTER_APPEND; "
                          "свидетельства сохранены, не повторяйте apply до осмотра") from exc
