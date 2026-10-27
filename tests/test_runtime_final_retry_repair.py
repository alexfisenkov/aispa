"""Exact final-owner-retry bridge on disposable 0.4.0 runtime chains."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))

from aispa_core import runtime_final_retry_repair as repair
from aispa_core.runtime_zone_repair import RepairJournal
from aispa_core.store import Invalid, Journal, file_hash
import test_runtime_owner_attempt_repair as owner_tests


class FinalRetryRuntimeRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        owner_tests.OwnerAttemptRuntimeRepairTests.setUpClass()
        old = owner_tests.OwnerAttemptRuntimeRepairTests(methodName="test_new_selected_copy_preserves_old_history_and_runtime")
        old.setUp()
        try:
            old.apply(old.prepare())
            cls.seed = tempfile.TemporaryDirectory(prefix="aispa-final-retry-seed-")
            cls.template = Path(cls.seed.name) / "project"
            shutil.copytree(old.root, cls.template)
        finally:
            old.tearDown()

    @classmethod
    def tearDownClass(cls):
        cls.seed.cleanup()
        owner_tests.OwnerAttemptRuntimeRepairTests.tearDownClass()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-final-retry-fixture-")
        self.root = Path(self.temp.name) / "project"
        shutil.copytree(self.template, self.root)
        self.store = RepairJournal(self.root)  # Real 0.4.0 run, read by the 0.5.0 package.
        self.state = self.store.load()
        self.run = self.store.run
        self.journal = self.run / "events.jsonl"
        self.prefix = self.journal.read_bytes()
        self.sha = file_hash(self.journal)
        self.approval = Path(self.temp.name) / "owner-decision.txt"
        self.quote = "Да давай"
        self.approval.write_text("Question: Retry T01 and T06 with history?\nOwner: Да давай\n", encoding="utf-8")
        self.approval_sha = file_hash(self.approval)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **overrides):
        options = dict(candidate_package=ROOT / "skills/aispa", expected_journal_sha256=self.sha,
                       expected_approval_sha256=self.approval_sha, owner_quote=self.quote)
        options.update(overrides)
        return repair.prepare(self.root, **options)

    def apply(self, prepared, **overrides):
        options = dict(manifest_file=prepared["manifestPath"],
                       expected_manifest_sha256=prepared["manifest"]["sha256"],
                       expected_journal_sha256=self.sha, approval_file=self.approval,
                       expected_approval_sha256=self.approval_sha, owner_quote=self.quote, confirm=True)
        options.update(overrides)
        return repair.apply(self.root, **options)

    def test_exact_one_file_selection_preserves_history_and_pin(self):
        before = {p.relative_to(self.run).as_posix(): p.read_bytes() for p in self.run.rglob("*")
                  if p.is_file() and p.name != "events.jsonl"}
        inspected = repair.inspect(self.root)
        self.assertEqual(inspected["oldRuntimeDigest"], repair.OLD_DIGEST)
        prepared = self.prepare()
        self.assertEqual(prepared["diff"], [{"path": "tools/aispa_core/workflow.py",
                                                "before": repair.OLD_HASHES["tools/aispa_core/workflow.py"],
                                                "after": repair.NEW_HASHES["tools/aispa_core/workflow.py"]}])
        self.assertEqual(self.journal.read_bytes(), self.prefix)
        selected = self.apply(prepared)
        self.assertEqual(selected["selectionOutcome"], "selected-readback")
        self.assertTrue(self.journal.read_bytes().startswith(self.prefix))
        self.assertEqual(len(self.journal.read_bytes().splitlines()), len(self.prefix.splitlines()) + 1)
        for name, raw in before.items():
            self.assertEqual((self.run / name).read_bytes(), raw, name)
        self.assertEqual(self.store.load()["runtimeDigest"], selected["newRuntimeDigest"])
        self.assertEqual(repair.inspect(self.root)["newRuntimeDigest"], selected["newRuntimeDigest"])
        cli = Path(selected["cli"])
        called = subprocess.run([sys.executable, "-B", str(cli), "--root", str(self.root),
                                 "start", "--help"], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(called.returncode, 0, called.stderr)
        self.assertIn("context", called.stdout)
        write = subprocess.run([sys.executable, "-B", str(cli), "--root", str(self.root),
                                "participant", "--role", "coordinator", "--runtime", "codex",
                                "--native-id", "synthetic-after-final-bridge", "--session", "synthetic"],
                               capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(write.returncode, 0, write.stdout + write.stderr)
        self.assertEqual(repair.inspect(self.root)["newRuntimeDigest"], selected["newRuntimeDigest"])
        with self.assertRaises(Invalid):
            self.apply(prepared)

    def test_apply_requires_matching_cas_manifest_and_approval(self):
        prepared = self.prepare()
        for override in ({"expected_journal_sha256": "0" * 64},
                         {"expected_manifest_sha256": "0" * 64},
                         {"expected_approval_sha256": "0" * 64},
                         {"owner_quote": "Нет"},
                         {"approval_file": Path(self.temp.name) / "missing"},
                         {"confirm": False}):
            with self.subTest(override=override), self.assertRaises(Exception):
                self.apply(prepared, **override)
            self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_wrong_pin_source_hash_and_package_path_refuse_without_stage(self):
        old = self.run / "runtime-repairs" / self.state["runtimeOwnerAttemptRepair"]["id"] / "runtime"
        target = old / repair.FILE
        target.chmod(0o644)
        target.write_bytes(target.read_bytes() + b"\n# drift\n")
        with self.assertRaises(Invalid):
            self.prepare()
        template = (self.template / ".aispa/runs" / self.state["run"] / "runtime-repairs"
                    / self.state["runtimeOwnerAttemptRepair"]["id"] / "runtime" / repair.FILE)
        target.write_bytes(template.read_bytes())
        with patch.dict(repair.SOURCE_HASHES, {repair.FILE: "0" * 64}):
            with self.assertRaises(Invalid):
                self.prepare()
        with self.assertRaises(Invalid):
            self.prepare(candidate_package=Path(self.temp.name))
        self.assertEqual(self.journal.read_bytes(), self.prefix)
        self.assertFalse(any(p.name.startswith(repair.STEM)
                             for p in (self.run / "runtime-repairs").iterdir()))

    def test_manifest_tamper_stale_journal_and_links_refuse(self):
        prepared = self.prepare()
        manifest = Path(prepared["manifestPath"])
        raw = manifest.read_bytes()
        manifest.chmod(0o644)
        value = json.loads(raw)
        value["parentManifest"] = {"file": "../../outside", "sha256": "0" * 64}
        manifest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(Invalid):
            self.apply(prepared, expected_manifest_sha256=file_hash(manifest))
        manifest.write_bytes(raw)
        other = Path(self.temp.name) / "manifest-copy.json"
        other.write_bytes(raw)
        manifest.unlink()
        manifest.symlink_to(other)
        with self.assertRaises(Invalid):
            self.apply(prepared)
        manifest.unlink()
        manifest.write_bytes(raw)
        linked = Path(self.temp.name) / "approval-link.txt"
        linked.symlink_to(self.approval)
        with self.assertRaises(Invalid):
            self.apply(prepared, approval_file=linked)
        self.store.append(self.state, "synthetic-intervening-event")
        changed = self.journal.read_bytes()
        with self.assertRaises(Invalid):
            self.apply(prepared)
        self.assertEqual(self.journal.read_bytes(), changed)

    def test_zone_selector_mutate_then_restore_after_final_selection_is_rejected(self):
        prepared = self.prepare()
        self.apply(prepared)
        store = RepairJournal(self.root)
        state = store.load()
        original = state["runtimeZoneRepair"]
        state["runtimeZoneRepair"] = None
        store.append(state, "synthetic-zone-selector-mutation")
        state["runtimeZoneRepair"] = original
        store.append(state, "synthetic-zone-selector-restore")
        with self.assertRaises(Invalid):
            repair.inspect(self.root)

    def test_runtime_digest_mutate_then_restore_before_first_zone_event_is_rejected(self):
        import test_runtime_zone_repair as zone_tests
        zone_fixture = zone_tests.ZoneRuntimeRepairTests(
            methodName="test_second_immutable_repair_preserves_first_manifest_prefix_runtime_and_attempts")
        zone_fixture.setUp()
        try:
            original = zone_fixture.before_state["runtimeDigest"]
            self.assertEqual(original, zone_tests.repair.OFFICIAL_DIGEST)
            zone_fixture.before_state["runtimeDigest"] = "0" * 64
            zone_fixture.store.append(zone_fixture.before_state, "synthetic-pre-zone-digest-mutation")
            zone_fixture.before_state["runtimeDigest"] = original
            zone_fixture.store.append(zone_fixture.before_state, "synthetic-pre-zone-digest-restore")
            zone_fixture.sha = file_hash(zone_fixture.history)
            frozen = zone_fixture.history.read_bytes()
            with self.assertRaisesRegex(Invalid, "runtimeDigest"):
                zone_tests.repair.inspect(zone_fixture.root)
            self.assertEqual(zone_fixture.history.read_bytes(), frozen)
        finally:
            zone_fixture.tearDown()

    def test_runtime_digest_mutate_then_restore_before_owner_event_is_rejected(self):
        old = owner_tests.OwnerAttemptRuntimeRepairTests(
            methodName="test_new_selected_copy_preserves_old_history_and_runtime")
        old.setUp()
        try:
            original = old.state["runtimeDigest"]
            old.state["runtimeDigest"] = "0" * 64
            old.store.append(old.state, "synthetic-pre-owner-digest-mutation")
            old.state["runtimeDigest"] = original
            old.store.append(old.state, "synthetic-pre-owner-digest-restore")
            old.sha = file_hash(old.journal)
            frozen = old.journal.read_bytes()
            with self.assertRaisesRegex(Invalid, "runtimeDigest"):
                owner_tests.repair.inspect(old.root)
            with self.assertRaisesRegex(Invalid, "runtimeDigest"):
                old.prepare()
            self.assertEqual(old.journal.read_bytes(), frozen)
        finally:
            old.tearDown()

    def test_prepare_failure_or_id_collision_preserves_existing_bytes(self):
        real_write = repair._write
        count = 0
        def fail_mid_copy(path, raw):
            nonlocal count
            count += 1
            if count == 5:
                raise OSError("synthetic disk failure")
            return real_write(path, raw)
        with patch.object(repair, "_write", side_effect=fail_mid_copy):
            with self.assertRaises(OSError):
                self.prepare()
        stage = self.run / "runtime-repairs" / (repair.STEM + "f" * 32)
        stage.mkdir()
        marker = stage / "existing.txt"
        marker.write_text("keep", encoding="utf-8")
        with patch.object(repair.uuid, "uuid4", return_value=SimpleNamespace(hex="f" * 32)):
            with self.assertRaises(FileExistsError):
                self.prepare()
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_bounded_scan_and_prefix_never_read_whole(self):
        for number in range(20):
            self.store.append(self.state, "synthetic-scale-" + str(number))
        self.sha = file_hash(self.journal)
        refs = []
        peak = 0
        original = json.loads
        class Row(dict):
            __slots__ = ("__weakref__",)
        def tracked(raw, *args, **kwargs):
            nonlocal peak
            result = original(raw, *args, **kwargs)
            if isinstance(result, dict) and {"sequence", "state", "event"} <= set(result):
                result = Row(result)
                refs.append(weakref.ref(result))
                peak = max(peak, sum(ref() is not None for ref in refs))
            return result
        original_read = Path.read_bytes
        def guarded(path):
            if path.name in ("events.jsonl", "before-events.jsonl"):
                raise AssertionError("full journal/prefix read_bytes")
            return original_read(path)
        with patch.object(repair.json, "loads", side_effect=tracked), patch.object(Path, "read_bytes", guarded):
            repair.inspect(self.root)
            prepared = self.prepare()
            self.apply(prepared)
            repair.inspect(self.root)
            selected_store = RepairJournal(self.root)
            selected_state = selected_store.load()
            for number in range(40):
                selected_store.append(selected_state, "synthetic-later-" + str(number))
            repair.inspect(self.root)
        # Two zone rows, one owner row, one final row, their state references,
        # the current row/previous state and Journal.load's state remain bounded.
        self.assertLessEqual(peak, 12, "journal rows retained instead of streamed")

    def test_support_file_collision_and_preappend_write_fault(self):
        prepared = self.prepare()
        stage = Path(prepared["manifestPath"]).parent
        occupied = stage / "before-events.jsonl"
        occupied.write_bytes(b"unowned-collision")
        with self.assertRaises(Invalid):
            self.apply(prepared)
        self.assertEqual(occupied.read_bytes(), b"unowned-collision")
        occupied.unlink()
        temp = stage / (".owner-attempt-" + "e" * 32 + ".tmp")
        temp.write_bytes(b"unowned-temp")
        with patch.object(repair.owner.uuid, "uuid4", return_value=SimpleNamespace(hex="e" * 32)):
            with self.assertRaises(FileExistsError):
                self.apply(prepared)
        self.assertEqual(temp.read_bytes(), b"unowned-temp")
        temp.unlink()
        with patch.object(repair.owner, "_stage_file", side_effect=OSError("synthetic support write fault")):
            with self.assertRaises(OSError):
                self.apply(prepared)
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_partial_support_write_is_removed_before_append(self):
        prepared = self.prepare()
        stage = Path(prepared["manifestPath"]).parent
        def interrupted_source(_path):
            yield b"partial-prefix"
            raise OSError("synthetic partial support write")
        with patch.object(repair.owner, "_prefix_chunks", side_effect=interrupted_source):
            with self.assertRaises(OSError):
                self.apply(prepared)
        for name in ("before-events.jsonl", "owner-authorization.txt", "receipt.json"):
            self.assertFalse((stage / name).exists(), name)
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_oversized_approval_is_refused_without_whole_file_read(self):
        self.approval.write_bytes(("Owner: " + self.quote + "\n").encode() + b"x" * 131073)
        self.approval_sha = file_hash(self.approval)
        prepared = self.prepare()
        original = Path.read_bytes
        def guarded(path):
            if path == self.approval:
                raise AssertionError("approval must never be read whole")
            return original(path)
        with patch.object(Path, "read_bytes", guarded):
            with self.assertRaises(Invalid):
                self.apply(prepared)
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_growing_approval_is_refused_with_bounded_physical_read(self):
        prepared = self.prepare()
        original = Path.read_bytes
        real_read = repair.os.read
        calls = []
        grown = False
        def guarded(path):
            if path == self.approval:
                raise AssertionError("approval must never be read whole")
            return original(path)
        def growing_read(fd, size):
            nonlocal grown
            calls.append(size)
            if not grown:
                grown = True
                with self.approval.open("ab") as output:
                    output.write(b"x" * 131073)
            return real_read(fd, size)
        with patch.object(Path, "read_bytes", guarded), patch.object(repair.os, "read", side_effect=growing_read):
            with self.assertRaises(Invalid):
                self.apply(prepared)
        self.assertTrue(calls)
        self.assertLessEqual(max(calls), 131073)
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_approval_growth_between_preflight_and_locked_recheck_is_refused(self):
        prepared = self.prepare()
        real_read = repair._approval_bytes
        calls = 0
        def grow_after_first(path, sha, quote):
            nonlocal calls
            calls += 1
            raw = real_read(path, sha, quote)
            if calls == 1:
                with self.approval.open("ab") as output:
                    output.write(b"x" * 131073)
            return raw
        with patch.object(repair, "_approval_bytes", side_effect=grow_after_first):
            with self.assertRaises(Invalid):
                self.apply(prepared)
        self.assertEqual(calls, 2)
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO needs POSIX")
    def test_approval_replaced_by_fifo_after_lstat_does_not_block(self):
        prepared = self.prepare()
        real_open = repair.os.open
        swapped = False
        def swap_then_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if Path(path) == self.approval and not swapped:
                swapped = True
                self.approval.unlink()
                os.mkfifo(self.approval)
            return real_open(path, flags, *args, **kwargs)
        with patch.object(repair.os, "open", side_effect=swap_then_open):
            with self.assertRaises(Invalid):
                self.apply(prepared)
        self.assertTrue(swapped)
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_append_error_before_write_keeps_receipts_without_retry(self):
        prepared = self.prepare()
        stage = Path(prepared["manifestPath"]).parent
        with patch.object(Journal, "append", side_effect=OSError("synthetic pre-append fault")):
            with self.assertRaisesRegex(Invalid, "NO_VERIFIED_APPEND"):
                self.apply(prepared)
        self.assertEqual(self.journal.read_bytes(), self.prefix)
        for name in ("before-events.jsonl", "owner-authorization.txt", "receipt.json"):
            self.assertTrue((stage / name).is_file(), name)
        with self.assertRaises(Invalid):
            self.apply(prepared)
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_complete_append_error_keeps_supporting_receipts(self):
        prepared = self.prepare()
        stage = Path(prepared["manifestPath"]).parent
        original = Journal.append
        def full_then_error(store, state, event):
            original(store, state, event)
            raise OSError("synthetic fsync report after complete append")
        with patch.object(Journal, "append", full_then_error):
            result = self.apply(prepared)
        self.assertEqual(result["selectionOutcome"], "readback-complete-after-append-error")
        self.assertEqual(result["durability"], "not-proven")
        for name in ("before-events.jsonl", "owner-authorization.txt", "receipt.json"):
            self.assertTrue((stage / name).is_file(), name)

    def test_partial_append_error_keeps_recovery_evidence(self):
        prepared = self.prepare()
        stage = Path(prepared["manifestPath"]).parent
        original = Journal.append
        def partial_then_error(store, state, event):
            start = (store.run / "events.jsonl").stat().st_size
            original(store, state, event)
            with (store.run / "events.jsonl").open("r+b") as stream:
                stream.truncate(start + 100)
            raise OSError("synthetic interrupted append")
        with patch.object(Journal, "append", partial_then_error):
            with self.assertRaisesRegex(Invalid, "PARTIAL_JOURNAL_AFTER_APPEND"):
                self.apply(prepared)
        for name in ("before-events.jsonl", "owner-authorization.txt", "receipt.json"):
            self.assertTrue((stage / name).is_file(), name)
        self.assertEqual(RepairJournal(self.root).inspect_recovery()["validSequence"],
                         len(self.prefix.splitlines()))

    def test_selected_cli_starts_only_fresh_final_owner_retry(self):
        """Run the actual selected CLI on a disposable accepted-ancestor chain."""
        import test_final_revalidation as final_tests
        import test_runtime_zone_repair as zone_tests
        from aispa_core.contracts import basis

        fixture = final_tests.FinalRetryStartTests(methodName="test_owner_retry_of_started_final_heavy_task_is_ready_and_starts")
        zone_tests.official_line(self)
        old = zone_tests.ZoneRuntimeRepairTests(
            methodName="test_second_immutable_repair_preserves_first_manifest_prefix_runtime_and_attempts")
        real_create = Journal.create
        def official_create(store, slug, title, initializer=None):
            def seeded(state):
                state["runtimeDigest"] = zone_tests.repair.OFFICIAL_DIGEST
                if initializer is not None:
                    initializer(state)
            result = real_create(store, slug, title, initializer=seeded)
            shutil.copytree(zone_tests.ZoneRuntimeRepairTests.package, store.run / "runtime")
            return result
        with patch.object(Journal, "create", official_create):
            f, flow = fixture.fixture()
        old.setUp()
        try:
            fixture.owner_retry(f, flow)
            # The unit fixture skips coverage/plan authoring; record valid
            # synthetic gates so the actual CLI can reach its dependency guard.
            for gate in ("coverage", "plan"):
                f.state["gates"][gate] = {
                    "passed": True, "basis": basis(f.store, f.state, gate),
                    "report": f.store.artifact("synthetic-gates", gate.encode(), ".txt")}
            f.host.save("synthetic-gates-recorded")
            old.first_maintenance()
            first_source = Path(old.temp.name) / "first-maintenance"

            def historical(cli, action, candidate=None, prepared=None):
                sha = file_hash(f.store.run / "events.jsonl")
                if action == "prepare":
                    args = ["--candidate-package", str(candidate), "--expected-journal-sha256", sha]
                else:
                    args = ["--manifest-file", prepared["manifestPath"],
                            "--expected-manifest-sha256", prepared["manifest"]["sha256"],
                            "--expected-journal-sha256", sha, "--approval-file", str(old.approval), "--confirm"]
                result = subprocess.run([sys.executable, "-B", str(cli), "--root", str(f.root), action, *args],
                                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)

            first_cli = first_source / "tools/repair_task_zones.py"
            historical(first_cli, "apply", prepared=historical(first_cli, "prepare", candidate=first_source))
            second_cli = zone_tests.ZoneRuntimeRepairTests.maintenance / "tools/repair_task_zones.py"
            historical(second_cli, "apply", prepared=historical(
                second_cli, "prepare", candidate=zone_tests.ZoneRuntimeRepairTests.maintenance))
            current_sha = file_hash(f.store.run / "events.jsonl")
            decision = f.home / "owner-retry.txt"
            quoted = "Yes, retry T06."
            owner_prepared = owner_tests.repair.prepare(
                f.root, candidate_package=ROOT / "skills/aispa",
                expected_journal_sha256=current_sha,
                expected_approval_sha256=file_hash(decision), owner_quote=quoted)
            owner_selected = owner_tests.repair.apply(
                f.root, manifest_file=owner_prepared["manifestPath"],
                expected_manifest_sha256=owner_prepared["manifest"]["sha256"],
                expected_journal_sha256=current_sha, approval_file=decision,
                expected_approval_sha256=file_hash(decision),
                owner_quote=quoted, confirm=True)
            old_state = Journal(f.root).load()
            old_context = basis(Journal(f.root), old_state, "task", "T06")
            old_call = subprocess.run(
                [sys.executable, "-B", owner_selected["cli"], "--root", str(f.root),
                 "start", "T06", "--context", old_context],
                capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertNotEqual(old_call.returncode, 0)
            self.assertIn("Зависимости задачи", (old_call.stderr or "") + (old_call.stdout or ""))
            current_sha = file_hash(f.store.run / "events.jsonl")
            fresh = repair.prepare(
                f.root, candidate_package=ROOT / "skills/aispa",
                expected_journal_sha256=current_sha,
                expected_approval_sha256=file_hash(decision), owner_quote=quoted)
            selected = repair.apply(
                f.root, manifest_file=fresh["manifestPath"],
                expected_manifest_sha256=fresh["manifest"]["sha256"],
                expected_journal_sha256=current_sha, approval_file=decision,
                expected_approval_sha256=file_hash(decision),
                owner_quote=quoted, confirm=True)
            state = Journal(f.root).load()
            context = basis(Journal(f.root), state, "task", "T06")
            new_call = subprocess.run(
                [sys.executable, "-B", selected["cli"], "--root", str(f.root),
                 "start", "T06", "--context", context],
                capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(new_call.returncode, 0, (new_call.stdout or "") + (new_call.stderr or ""))
            check_store = Journal(f.root)
            final_state = check_store.load()
            self.assertEqual(next(t for t in final_state["tasks"] if t["id"] == "T06")["status"], "running")
            self.assertEqual(final_state["taskHistory"]["T06"][-1]["authorization"]["kind"], "owner-task-attempt")
            from aispa_core.team import direct_proof_current
            self.assertFalse(direct_proof_current(check_store, final_state, "T05"))
            self.assertEqual(repair.inspect(f.root)["newRuntimeDigest"], selected["newRuntimeDigest"])
        finally:
            old.tearDown()
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
