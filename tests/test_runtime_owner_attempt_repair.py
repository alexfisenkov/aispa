"""Isolated exact-0.4.0 owner-attempt runtime repair; never uses the owner's run."""
import copy
import hashlib
import json
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

from aispa_core import runtime_owner_attempt_repair as repair
from aispa_core.store import Invalid, Journal, file_hash
import test_runtime_zone_repair as zone_tests
import test_team as team_tests


class OwnerAttemptRuntimeRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        zone_tests.ZoneRuntimeRepairTests.setUpClass()
        old = zone_tests.ZoneRuntimeRepairTests(methodName="test_second_immutable_repair_preserves_first_manifest_prefix_runtime_and_attempts")
        old.setUp()
        try:
            old.first_maintenance()
            old.apply(old.prepare())
            cls.seed = tempfile.TemporaryDirectory(prefix="aispa-owner-attempt-seed-")
            cls.template = Path(cls.seed.name) / "project"
            shutil.copytree(old.root, cls.template)
        finally:
            old.tearDown()

    @classmethod
    def tearDownClass(cls):
        cls.seed.cleanup()
        zone_tests.ZoneRuntimeRepairTests.tearDownClass()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-owner-attempt-fixture-")
        self.root = Path(self.temp.name) / "project"
        shutil.copytree(self.template, self.root)
        self.store = Journal(self.root)
        self.state = self.store.load()
        self.run = self.store.run
        self.journal = self.run / "events.jsonl"
        self.prefix = self.journal.read_bytes()
        self.sha = file_hash(self.journal)
        self.approval = Path(self.temp.name) / "owner-decision.txt"
        self.quote = "Да давай"
        self.approval.write_text("Question: Retry T01 and T06 under the old scope?\nOwner: Да давай\n", encoding="utf-8")
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
                       expected_journal_sha256=self.sha,
                       approval_file=self.approval, expected_approval_sha256=self.approval_sha,
                       owner_quote=self.quote, confirm=True)
        options.update(overrides)
        return repair.apply(self.root, **options)

    def test_new_selected_copy_preserves_old_history_and_runtime(self):
        before = {p.relative_to(self.run).as_posix(): p.read_bytes() for p in self.run.rglob("*")
                  if p.is_file() and p.name != "events.jsonl"}
        inspected = repair.inspect(self.root)
        self.assertEqual(inspected["oldRuntimeDigest"], repair.OLD_DIGEST)
        self.assertEqual(self.prefix, self.journal.read_bytes())
        prepared = self.prepare()
        self.assertEqual(self.prefix, self.journal.read_bytes())
        self.assertNotEqual(prepared["runtime"], inspected["runtime"])
        self.assertEqual(prepared["selected"], False)
        result = self.apply(prepared)
        self.assertTrue(self.journal.read_bytes().startswith(self.prefix))
        self.assertEqual(len(self.journal.read_bytes().splitlines()), len(self.prefix.splitlines()) + 1)
        for name, raw in before.items():
            self.assertEqual((self.run / name).read_bytes(), raw, name)
        self.assertEqual(self.store.load()["runtimeDigest"], result["newRuntimeDigest"])
        selected = self.store.load()
        expected = copy.deepcopy(self.state)
        expected.update(runtimeDigest=result["newRuntimeDigest"],
                        runtimeOwnerAttemptRepair=selected["runtimeOwnerAttemptRepair"],
                        updatedAt=selected["updatedAt"])
        self.assertEqual(selected, expected)
        self.assertEqual(repair.inspect(self.root)["newRuntimeDigest"], result["newRuntimeDigest"])
        self.assertEqual(result["newRuntimeDigest"], prepared["newRuntimeDigest"])
        selected_raw = self.journal.read_bytes()
        with self.assertRaises(Invalid):
            self.apply(prepared)
        self.assertEqual(self.journal.read_bytes(), selected_raw)
        cli = Path(result["cli"])
        called = subprocess.run([sys.executable, "-B", str(cli), "--root", str(self.root),
                                 "new-attempt", "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(called.returncode, 0, called.stderr)
        self.assertIn("--owner-source-file", called.stdout)
        self.assertIn("--owner-decision-id", called.stdout)
        write = subprocess.run([sys.executable, "-B", str(cli), "--root", str(self.root), "participant",
                                "--role", "coordinator", "--runtime", "codex",
                                "--native-id", "synthetic-after-owner-bridge", "--session", "synthetic"],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(write.returncode, 0, write.stdout + write.stderr)
        self.assertEqual(repair.inspect(self.root)["newRuntimeDigest"], result["newRuntimeDigest"])

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

    def test_manifest_tamper_and_candidate_drift_refuse_before_selection(self):
        prepared = self.prepare()
        manifest = Path(prepared["manifestPath"])
        manifest.chmod(0o644)
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["runtimePath"] = "../../outside"
        manifest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(Exception):
            self.apply(prepared, expected_manifest_sha256=file_hash(manifest))
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_wrong_pin_source_hash_and_package_path_refuse_without_stage(self):
        old = self.run / "runtime-repairs" / self.state["runtimeZoneRepair"]["id"] / "runtime"
        target = old / "tools/aispa_core/team.py"
        target.chmod(0o644)
        target.write_bytes(target.read_bytes() + b"\n# drift\n")
        with self.assertRaises(Invalid):
            self.prepare()
        self.assertEqual(self.journal.read_bytes(), self.prefix)
        # Restore only this disposable fixture before testing source and path gates.
        template = self.template / ".aispa/runs" / self.state["run"] / "runtime-repairs" / self.state["runtimeZoneRepair"]["id"] / "runtime/tools/aispa_core/team.py"
        target.write_bytes(template.read_bytes())
        with patch.dict(repair.SOURCE_HASHES, {"tools/aispa_core/owner_task_attempt.py": "0" * 64}):
            with self.assertRaises(Invalid):
                self.prepare()
        with self.assertRaises(Invalid):
            self.prepare(candidate_package=Path(self.temp.name))
        self.assertEqual(self.journal.read_bytes(), self.prefix)
        self.assertFalse(any(p.name.startswith("owner-task-attempt-")
                             for p in (self.run / "runtime-repairs").iterdir()))

    def test_stale_journal_and_manifest_symlink_refuse(self):
        prepared = self.prepare()
        manifest = Path(prepared["manifestPath"])
        other = Path(self.temp.name) / "manifest-copy.json"
        other.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(other)
        with self.assertRaises(Invalid):
            self.apply(prepared)
        manifest.unlink()
        manifest.write_bytes(other.read_bytes())
        state = self.store.load()
        self.store.append(state, "synthetic-intervening-event")
        changed = self.journal.read_bytes()
        with self.assertRaises(Invalid):
            self.apply(prepared)
        self.assertEqual(self.journal.read_bytes(), changed)

    def test_prepare_copy_error_cleans_unselected_partial_stage(self):
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
        self.assertEqual(self.journal.read_bytes(), self.prefix)
        self.assertFalse(any(p.name.startswith("owner-task-attempt-")
                             for p in (self.run / "runtime-repairs").iterdir()))

    def test_prepare_id_collision_keeps_existing_directory(self):
        stage = self.run / "runtime-repairs" / (repair.STEM + "f" * 32)
        stage.mkdir()
        marker = stage / "existing.txt"
        marker.write_text("keep", encoding="utf-8")
        with patch.object(repair.uuid, "uuid4", return_value=SimpleNamespace(hex="f" * 32)):
            with self.assertRaises(FileExistsError):
                self.prepare()
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_apply_write_error_removes_partial_receipt_without_selection(self):
        prepared = self.prepare()
        stage = Path(prepared["manifestPath"]).parent
        real_write = repair._stage_file
        count = 0

        def fail_receipt(path, chunks, expected_sha):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("synthetic receipt write failure")
            return real_write(path, chunks, expected_sha)

        with patch.object(repair, "_stage_file", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.apply(prepared)
        self.assertEqual(self.journal.read_bytes(), self.prefix)
        for name in ("before-events.jsonl", "owner-authorization.txt", "receipt.json"):
            self.assertFalse((stage / name).exists(), name)

    def test_manifest_path_escape_and_approval_link_refuse(self):
        prepared = self.prepare()
        outside = Path(self.temp.name) / "outside-manifest.json"
        outside.write_bytes(Path(prepared["manifestPath"]).read_bytes())
        with self.assertRaises(Invalid):
            self.apply(prepared, manifest_file=outside)
        linked = Path(self.temp.name) / "approval-link.txt"
        linked.symlink_to(self.approval)
        with self.assertRaises(Invalid):
            self.apply(prepared, approval_file=linked)
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_read_only_scan_never_materializes_all_events(self):
        with patch.object(Journal, "read_journal", side_effect=AssertionError("full events materialized")):
            self.assertEqual(repair.inspect(self.root)["oldRuntimeDigest"], repair.OLD_DIGEST)

    def test_streaming_scan_retains_at_most_bounded_event_rows(self):
        for number in range(40):
            self.store.append(self.state, "synthetic-scale-" + str(number))
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

        with patch.object(repair.json, "loads", side_effect=tracked):
            self.assertEqual(repair.inspect(self.root)["oldRuntimeDigest"], repair.OLD_DIGEST)
        self.assertLessEqual(peak, 8, "journal rows retained instead of streamed")

    def test_journal_and_saved_prefix_are_never_read_whole(self):
        original = Path.read_bytes

        def guarded(path):
            if path.name in ("events.jsonl", "before-events.jsonl"):
                raise AssertionError("full journal/prefix read_bytes")
            return original(path)

        with patch.object(Path, "read_bytes", guarded):
            self.assertEqual(repair.inspect(self.root)["oldRuntimeDigest"], repair.OLD_DIGEST)
            prepared = self.prepare()
            self.assertEqual(self.apply(prepared)["newRuntimeDigest"], prepared["newRuntimeDigest"])

    def test_completed_append_error_keeps_supporting_receipts(self):
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
        self.assertEqual(repair.inspect(self.root)["newRuntimeDigest"], prepared["newRuntimeDigest"])
        for name in ("before-events.jsonl", "owner-authorization.txt", "receipt.json"):
            self.assertTrue((stage / name).is_file(), name)

    def test_partial_append_error_keeps_recovery_evidence(self):
        prepared = self.prepare()
        stage = Path(prepared["manifestPath"]).parent
        original = Journal.append

        def partial_then_error(store, state, event):
            start = (store.run / "events.jsonl").stat().st_size
            original(store, state, event)
            path = store.run / "events.jsonl"
            with path.open("r+b") as stream:
                stream.truncate(start + 100)
            raise OSError("synthetic interrupted append")

        with patch.object(Journal, "append", partial_then_error):
            with self.assertRaisesRegex(Invalid, "PARTIAL_JOURNAL_AFTER_APPEND"):
                self.apply(prepared)
        for name in ("before-events.jsonl", "owner-authorization.txt", "receipt.json"):
            self.assertTrue((stage / name).is_file(), name)
        self.assertEqual(Journal(self.root).inspect_recovery()["validSequence"], len(self.prefix.splitlines()))

    def test_partial_support_write_is_removed_before_append(self):
        prepared = self.prepare()
        stage = Path(prepared["manifestPath"]).parent
        def interrupted_source(_path):
            yield b"partial-prefix"
            raise OSError("synthetic partial support write")

        with patch.object(repair, "_prefix_chunks", side_effect=interrupted_source):
            with self.assertRaises(OSError):
                self.apply(prepared)
        for name in ("before-events.jsonl", "owner-authorization.txt", "receipt.json"):
            self.assertFalse((stage / name).exists(), name)
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_fault_before_support_write_keeps_stage_clean(self):
        prepared = self.prepare()
        stage = Path(prepared["manifestPath"]).parent
        with patch.object(repair, "_stage_file", side_effect=OSError("synthetic before write")):
            with self.assertRaises(OSError):
                self.apply(prepared)
        for name in ("before-events.jsonl", "owner-authorization.txt", "receipt.json"):
            self.assertFalse((stage / name).exists(), name)
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_support_file_collisions_preserve_unowned_bytes(self):
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
        with patch.object(repair.uuid, "uuid4", return_value=SimpleNamespace(hex="e" * 32)):
            with self.assertRaises(FileExistsError):
                self.apply(prepared)
        self.assertEqual(temp.read_bytes(), b"unowned-temp")
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_append_failure_before_write_retains_receipts_without_retry(self):
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

    def test_unreviewed_readme_cannot_authorize_selection(self):
        prepared = self.prepare()
        readme = Path(self.temp.name) / "README.md"
        readme.write_text("This is a nonempty README, not the owner decision.\n", encoding="utf-8")
        with self.assertRaises(Invalid):
            self.apply(prepared, approval_file=readme)
        self.assertEqual(self.journal.read_bytes(), self.prefix)

    def test_applied_pin_opens_scoped_owner_attempt_through_cli(self):
        """Real old pin + actual new CLI command on a short synthetic blocked task."""
        f = team_tests.TeamTests(methodName="test_new_attempt_requires_new_quoted_owner_brief")
        old = zone_tests.ZoneRuntimeRepairTests(methodName="test_second_immutable_repair_preserves_first_manifest_prefix_runtime_and_attempts")
        f.setUp()
        old.setUp()
        try:
            f.state["tasks"][0]["requirements"].append("G01")
            f.team.prepare()
            shutil.copytree(zone_tests.ZoneRuntimeRepairTests.package, f.store.run / "runtime")
            f.state["runtimeDigest"] = zone_tests.repair.OFFICIAL_DIGEST
            f.host.save("synthetic-official-runtime")
            old.first_maintenance()  # Constructs exact 3dce187 source in a disposable root.
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
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)

            first_cli = first_source / "tools/repair_task_zones.py"
            historical(first_cli, "apply", prepared=historical(first_cli, "prepare", candidate=first_source))
            second_cli = zone_tests.ZoneRuntimeRepairTests.maintenance / "tools/repair_task_zones.py"
            historical(second_cli, "apply", prepared=historical(second_cli, "prepare",
                                                               candidate=zone_tests.ZoneRuntimeRepairTests.maintenance))
            f.state = f.store.load()
            f.host.s = f.state
            f.team = team_tests.TaskPipeline(f.host)
            f.attempt("T01")["terminal"] = True
            f.host.block("T01", "Synthetic owner review")
            original_attempt = copy.deepcopy(f.attempt("T01"))
            prior = (f.store.run / "events.jsonl").read_bytes()
            sha = hashlib.sha256(prior).hexdigest()
            words = Path(f.home) / "owner-decision.md"
            words.write_text("Question: Retry T01 and T06 under the old scope?\nOwner: Да давай\n", encoding="utf-8")
            source_sha = file_hash(words)
            prepared = repair.prepare(f.root, candidate_package=ROOT / "skills/aispa", expected_journal_sha256=sha,
                                      expected_approval_sha256=source_sha, owner_quote="Да давай")
            selected = repair.apply(f.root, manifest_file=prepared["manifestPath"],
                                    expected_manifest_sha256=prepared["manifest"]["sha256"],
                                    expected_journal_sha256=sha, approval_file=words,
                                    expected_approval_sha256=source_sha, owner_quote="Да давай", confirm=True)
            command = [sys.executable, "-B", selected["cli"], "--root", str(f.root), "new-attempt", "T01",
                       "--owner-source-file", str(words), "--source-sha256", source_sha,
                       "--owner-decision-id", "synthetic-owner-decision-001", "--quote", "Да давай"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = f.store.load()
            self.assertEqual(state["taskHistory"]["T01"][0], original_attempt)
            self.assertEqual(state["taskHistory"]["T01"][-1]["authorization"]["kind"], "owner-task-attempt")
            self.assertEqual(next(t for t in state["tasks"] if t["id"] == "T01")["status"], "pending")
            self.assertTrue((f.store.run / "events.jsonl").read_bytes().startswith(prior))
            self.assertEqual(repair.inspect(f.root)["newRuntimeDigest"], selected["newRuntimeDigest"])
            from aispa_core.dashboard_reader import DisplayJournal
            from aispa_core.dashboard_snapshot import calculate
            from aispa_core.display import project
            asset = b"synthetic current viewer"
            loaded = {"run": state["run"], "version": state["version"],
                      "dashboardVersion": state["version"], "readerMode": "compatible-view",
                      "assetSha256": hashlib.sha256(asset).hexdigest()}
            before_view = (f.store.run / "events.jsonl").read_bytes()
            view, basis = calculate(f.root, DisplayJournal, loaded, state["runtimeDigest"],
                                    lambda: asset, project)
            self.assertEqual(next(t for t in view["tasks"] if t["id"] == "T01")["attemptNumber"], 2)
            self.assertEqual(basis["journalSha256"], hashlib.sha256(before_view).hexdigest())
            self.assertEqual((f.store.run / "events.jsonl").read_bytes(), before_view)
        finally:
            old.tearDown()
            f.tearDown()


if __name__ == "__main__":
    unittest.main()
