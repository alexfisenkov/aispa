"""Synthetic offline fixtures; no owner project or actual consent is modified."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core import runtime_repair as repair
from aispa_core.portable import is_link
from aispa_core.store import Invalid, digest, file_hash

CLI = ROOT / "skills/aispa/tools/repair_source_quotes.py"


class RuntimeRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The tag is mandatory. CI fetches full history; unavailable tag is a
        # failure, never a skip or a fixture silently made from working code.
        cls.release = tempfile.TemporaryDirectory(prefix="aispa-official-035-")
        cls.package = Path(cls.release.name) / "aispa"
        names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "v0.3.5", "skills/aispa"], cwd=ROOT).decode().splitlines()
        assert len(names) == 89, "official v0.3.5 fixture must have all 89 files"
        for name in names:
            target = cls.package / name.removeprefix("skills/aispa/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.check_output(["git", "show", "v0.3.5:" + name], cwd=ROOT))
        cls.official_files = repair._tree(cls.package)
        assert digest(cls.official_files) == repair.OFFICIAL_DIGEST
        patched = dict(cls.official_files, **{repair.DESIGN_PATH: repair.REPAIRED_DESIGN_SHA})
        assert digest(patched) == repair.REPAIRED_DIGEST

    @classmethod
    def tearDownClass(cls):
        cls.release.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-repair-")
        self.root = Path(self.temp.name).resolve() / "проект with spaces 🙂"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.call(self.package / "tools/aispa.py", "init", "--slug", "repair", "--title", "Synthetic fixture",
                  "--mode", "auto", "--runtime", "codex", "--check", "balanced")
        self.store = repair.RepairJournal(self.root)
        state = self.store.load()
        self.run = self.store.run
        self.original = self.run / "runtime"
        self.history = self.run / "events.jsonl"
        with self.store.lock():
            self.brief = {**self.store.artifact("briefs", ("Owner fixture. " * 200).encode(), ".md"), "id": "B001"}
            state["briefs"].append(self.brief)
            evidence = self.store.artifact("evidence", b"synthetic test evidence\n", ".log")
            state["requirements"] = [{"id": "R1", "kind": "requested", "scope": "active", "brief": "B001", "quote": "Owner fixture.", "text": "preserve this fixture requirement", "acceptance": ["preserved"], "proof": evidence}]
            state["gates"]["fixture"] = {"proof": evidence, "passed": True}
            self.store.append(state, "synthetic-source-and-proof")
        self.approval = Path(self.temp.name) / "synthetic-owner-approval.txt"
        self.approval.write_text("SYNTHETIC TEST CONSENT ONLY: approve the narrow source quote repair.\n", encoding="utf-8")
        self.before = self.history.read_bytes()
        self.sha = file_hash(self.history)
        self.before_state = copy.deepcopy(self.store.load())

    def tearDown(self):
        self.temp.cleanup()

    def call(self, cli, *args, expected=0):
        result = subprocess.run([sys.executable, "-B", str(cli), "--root", str(self.root), *args],
                                cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return json.loads(result.stdout) if not expected else result

    def apply(self, **kwargs):
        values = {"confirm": True, "expected_journal_sha256": self.sha, "approval_file": self.approval}
        values.update(kwargs)
        return repair.apply(self.root, **values)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and ".git" not in p.parts}

    def test_official_tag_constants_and_exact_patch(self):
        old = (self.package / repair.DESIGN_PATH).read_bytes()
        self.assertEqual(hashlib.sha256(old).hexdigest(), repair.ORIGINAL_DESIGN_SHA)
        self.assertEqual(old.count(repair.OLD_TEXT), 1)
        self.assertEqual(hashlib.sha256(old.replace(repair.OLD_TEXT, repair.NEW_TEXT)).hexdigest(), repair.REPAIRED_DESIGN_SHA)

    def test_default_inspection_is_read_only(self):
        before = self.snapshot()
        info = self.call(CLI)
        self.assertEqual(info["journalSha256"], self.sha)
        self.assertFalse(info["alreadyRepaired"])
        self.assertFalse(info["approvalSemanticsVerified"])
        self.assertEqual(info["newRuntimeDigest"], repair.REPAIRED_DIGEST)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.run / "runtime-repairs").exists())

    def test_repair_preserves_every_old_byte_and_only_allowed_state_fields(self):
        original_files = repair._tree(self.original)
        old_artifacts = self.snapshot()
        result = self.apply()
        after = self.history.read_bytes()
        self.assertTrue(after.startswith(self.before))
        self.assertEqual(len(after.splitlines()), len(self.before.splitlines()) + 1)
        state = self.store.load()
        expected = copy.deepcopy(self.before_state)
        for field in ("runtimeDigest", "runtimeRepairs", "updatedAt"):
            expected[field] = state[field]
        self.assertEqual(expected, state)
        self.assertEqual(state["version"], "0.3.5")
        self.assertEqual(state["runtimeDigest"], repair.REPAIRED_DIGEST)
        self.assertEqual(repair._tree(self.original), original_files)
        self.assertEqual(repair._tree(Path(result["runtime"]))[repair.DESIGN_PATH], repair.REPAIRED_DESIGN_SHA)
        for name, raw in old_artifacts.items():
            if name != self.history.relative_to(self.root).as_posix():
                self.assertEqual((self.root / name).read_bytes(), raw, name)
        receipt = state["runtimeRepairs"][0]
        self.assertEqual((self.run / receipt["beforeJournal"]["file"]).read_bytes(), self.before)
        self.assertEqual((self.run / receipt["approval"]["file"]).read_bytes(), self.approval.read_bytes())
        self.assertEqual(Path(result["cli"]).parts[-3:], ("runtime", "tools", "aispa.py"))
        self.assertEqual(subprocess.check_output([sys.executable, "-B", result["cli"], "--version"]).decode().strip(), "0.3.5")

    def test_repaired_pinned_cli_can_make_regular_write_and_original_cannot(self):
        result = self.apply()
        old = self.call(self.original / "tools/aispa.py", "participant", "--role", "coordinator", "--runtime", "codex",
                        "--native-id", "synthetic-old", "--session", "synthetic", expected=2)
        self.assertIn("Пакет изменён", old.stderr)
        current = self.call(Path(result["cli"]), "participant", "--role", "coordinator", "--runtime", "codex",
                            "--native-id", "synthetic-repaired", "--session", "synthetic")
        actor = current["receipt"]["actor"]
        self.assertEqual(self.store.load()["participants"][actor]["nativeId"], "synthetic-repaired")
        self.assertEqual(repair.inspect(self.root)["newRuntimeDigest"], repair.REPAIRED_DIGEST)
        with self.assertRaises(Invalid):
            self.apply()  # Old CAS cannot authorize anything after further writes.

    def test_registered_long_quote_scenarios_fail_before_and_pass_after_repair_via_cli(self):
        from aispa_core.contracts import add_requirements
        from aispa_core.design_contract import DIMENSIONS, scenario_basis
        quote = "Дословный исходный запрос 🦊. " * 100
        self.assertGreater(len(quote), 1600)
        state = self.store.load()
        state["briefs"] = [{**self.store.artifact("briefs", (quote + "\n").encode(), ".md"), "id": "B001"}]
        state["requirements"] = []
        state["requirements"] = add_requirements(self.store, state, [
            {"id": "R01", "text": "Build export", "brief": "B001", "quote": quote,
             "acceptance": ["Export works"]}])
        state["spec"] = self.store.artifact("specs", b"The export action writes a local CSV file.\n", ".md")
        state["sealed"] = True
        self.store.append(state, "synthetic-registered-long-source")
        report = {"basis": scenario_basis(state), "depth": "balanced",
                  "depthReason": "Synthetic full source quotation regression fixture.",
                  "features": [{"requirement": "R01", "changeKind": "requested", "sourceQuote": quote,
                                "specQuote": "The export action writes a local CSV file.",
                                "dimensions": [{"name": name, "status": "covered", "scenario": name + " path",
                                                "verification": "Run the " + name + " local CSV case.",
                                                "counterexample": ""} for name in DIMENSIONS]}]}
        report_path = Path(self.temp.name) / "synthetic-scenarios.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        denied = self.call(self.original / "tools/aispa.py", "scenarios", "--file", str(report_path), expected=2)
        self.assertIn("цитата исходного брифа", denied.stderr)
        self.before = self.history.read_bytes()
        self.sha = file_hash(self.history)
        result = self.apply()
        repair_prefix = self.history.read_bytes()
        self.call(Path(result["cli"]), "scenarios", "--file", str(report_path))
        state = self.store.load()
        saved = json.loads((self.run / state["featureScenarios"]["file"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["report"]["features"][0]["sourceQuote"], quote)
        self.assertTrue(self.history.read_bytes().startswith(repair_prefix))
        self.assertTrue(repair_prefix.startswith(self.before))
        self.assertEqual(state["policy"], self.before_state["policy"])
        self.assertEqual(state["version"], "0.3.5")
        self.assertEqual(sum(json.loads(line)["event"] == repair.EVENT for line in self.history.read_bytes().splitlines()), 1)

    def test_idempotent_retry_appends_nothing(self):
        first = self.apply()
        before = self.history.read_bytes()
        second = self.apply()
        self.assertEqual(first["cli"], second["cli"])
        self.assertEqual(before, self.history.read_bytes())
        self.assertEqual(len(list((self.run / "runtime-repairs").iterdir())), 1)

    def test_preexisting_exact_manual_patch_is_supported_but_original_untouched(self):
        path = self.original / repair.DESIGN_PATH
        raw = path.read_bytes().replace(repair.OLD_TEXT, repair.NEW_TEXT)
        path.write_bytes(raw)
        result = self.apply()
        self.assertEqual(result["sourceRuntimeDigest"], repair.REPAIRED_DIGEST)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.store.load()["runtimeDigest"], repair.REPAIRED_DIGEST)
        # An already approved manual patch has the identical repaired digest;
        # its original CLI therefore remains a valid same-version writer too.
        current = self.call(self.original / "tools/aispa.py", "participant", "--role", "coordinator", "--runtime", "codex",
                            "--native-id", "synthetic-manual-patched", "--session", "synthetic")
        actor = current["receipt"]["actor"]
        self.assertEqual(self.store.load()["participants"][actor]["nativeId"], "synthetic-manual-patched")
        self.assertEqual(path.read_bytes(), raw)

    def test_python_cache_ignored_but_not_copied(self):
        cache = self.original / "tools/aispa_core/__pycache__"
        cache.mkdir()
        (cache / "bad.cpython-311.pyc").write_bytes(b"never execute/copy this cache")
        result = self.apply()
        self.assertFalse(list(Path(result["runtime"]).rglob("*.pyc")))
        self.assertTrue((cache / "bad.cpython-311.pyc").exists())

    def test_wrong_recorded_digest_denied_even_for_exact_release_tree(self):
        state = self.store.load()
        state["runtimeDigest"] = "a" * 64
        self.store.append(state, "synthetic-wrong-runtime")
        with self.assertRaisesRegex(Invalid, "digest"):
            repair.inspect(self.root)

    def test_unknown_runtime_file_changes_missing_and_extras_denied(self):
        target = self.original / "LICENSE"
        old = target.read_bytes()
        for operation in ("change", "missing", "extra", "empty-directory", "design-change"):
            with self.subTest(operation=operation):
                extra = self.original / "unexpected"
                design = self.original / repair.DESIGN_PATH
                design_old = design.read_bytes()
                if operation == "change": target.write_bytes(old + b"modified")
                elif operation == "missing": target.unlink()
                elif operation == "extra": extra.write_text("extra")
                elif operation == "empty-directory": extra.mkdir()
                else: design.write_bytes(design_old + b"\n")
                with self.assertRaises(Invalid): repair.inspect(self.root)
                target.write_bytes(old)
                design.write_bytes(design_old)
                if extra.is_dir(): extra.rmdir()
                else: extra.unlink(missing_ok=True)
        self.assertEqual(self.before, self.history.read_bytes())

    def test_runtime_link_and_cache_link_rejected(self):
        # Windows requires a junction fixture for directories if symlink creation
        # is not privileged; no platform skip of the boundary check.
        for name in ("extra-link", "__pycache__"):
            link = self.original / name
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.package)], check=True, capture_output=True)
            else:
                link.symlink_to(self.package, target_is_directory=True)
            try:
                self.assertTrue(is_link(link))
                with self.assertRaisesRegex(Invalid, "ссылка/junction"):
                    repair.inspect(self.root)
            finally:
                if os.name == "nt": os.rmdir(link)
                else: link.unlink()

    def test_tampered_brief_or_evidence_denied(self):
        state = self.store.load()
        for ref in (self.brief, state["gates"]["fixture"]["proof"]):
            path = self.run / ref["file"]
            before = path.read_bytes()
            path.write_bytes(before + b"tampered")
            with self.assertRaises(Invalid): repair.inspect(self.root)
            path.write_bytes(before)
        self.assertEqual(self.before, self.history.read_bytes())

    def test_missing_evidence_and_nested_json_evidence_denied(self):
        proof = self.store.load()["gates"]["fixture"]["proof"]
        path = self.run / proof["file"]
        path.unlink()
        with self.assertRaises(Invalid): self.apply()
        path.write_bytes(b"synthetic test evidence\n")
        state = self.store.load()
        nested = self.store.artifact("reviews", json.dumps({"nestedProof": proof}).encode(), ".json")
        state["gates"]["fixture"] = {"report": nested}
        state["requirements"] = []
        self.store.append(state, "synthetic-nested-report")
        path.write_bytes(b"changed nested proof")
        with self.assertRaises(Invalid): repair.inspect(self.root)

    def test_stale_journal_cas_denied_without_staging(self):
        state = self.store.load()
        self.store.append(state, "synthetic-concurrent-write")
        with self.assertRaisesRegex(Invalid, "журнал изменился"):
            self.apply()
        self.assertFalse((self.run / "runtime-repairs").exists())

    def test_no_consent_missing_empty_invalid_or_link_approval_denied(self):
        for kwargs in ({"confirm": False}, {"expected_journal_sha256": None}, {"approval_file": None},
                       {"approval_file": Path(self.temp.name) / "missing"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Invalid): self.apply(**kwargs)
        for raw in (b" \n", b"\xff"):
            self.approval.write_bytes(raw)
            with self.assertRaises(Invalid): self.apply()
        self.approval.write_text("SYNTHETIC TEST CONSENT", encoding="utf-8")
        link = Path(self.temp.name) / "approval-link"
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.package)], check=True, capture_output=True)
        else:
            link.symlink_to(self.approval)
        try:
            with self.assertRaises(Invalid): self.apply(approval_file=link)
        finally:
            if os.name == "nt": os.rmdir(link)
            else: link.unlink()
        self.assertEqual(self.before, self.history.read_bytes())
        self.assertFalse((self.run / "runtime-repairs").exists())
        denied = self.call(CLI, "--apply", "--confirm", "--expected-journal-sha256", self.sha, expected=2)
        self.assertIn("approval-file", denied.stderr)

    def test_terminal_wrong_version_pending_recovery_and_dirty_journal_denied(self):
        for changes in ({"status": "accepted"}, {"version": "0.3.6"}, {"journalRecovery": {"pending": True}}):
            with self.subTest(changes=changes):
                state = copy.deepcopy(self.before_state)
                state.update(changes)
                self.store._verified = None
                self.store.append(state, "synthetic-denial-state")
                with self.assertRaises(Invalid): repair.inspect(self.root)
                self.history.write_bytes(self.before)
        self.history.write_bytes(self.before + b'{"sequence":')
        with self.assertRaises(Invalid): repair.inspect(self.root)

    def test_append_failure_leaves_old_state_usable_and_orphan_unselected(self):
        with patch.object(repair.RepairJournal, "append", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError): self.apply()
        self.assertEqual(self.history.read_bytes(), self.before)
        self.assertEqual(self.store.load()["runtimeDigest"], repair.OFFICIAL_DIGEST)
        original = repair.inspect(self.root)
        self.assertFalse(original["alreadyRepaired"])
        self.assertEqual(len(list((self.run / "runtime-repairs").iterdir())), 1)
        result = self.apply()
        self.assertTrue(result["alreadyRepaired"])
        self.assertEqual(len(list((self.run / "runtime-repairs").iterdir())), 2)

    def test_tampered_repair_receipts_or_original_runtime_rejected(self):
        self.apply()
        entry = self.store.load()["runtimeRepairs"][0]
        approval = self.run / entry["approval"]["file"]
        raw = approval.read_bytes()
        approval.write_bytes(raw + b"tamper")
        with self.assertRaises(Invalid): repair.inspect(self.root)
        approval.write_bytes(raw)
        receipt = approval.parent / "receipt.json"
        receipt_raw = receipt.read_bytes()
        receipt.write_bytes(b"{}")
        with self.assertRaises(Invalid): repair.inspect(self.root)
        receipt.write_bytes(receipt_raw)
        (self.original / "LICENSE").write_bytes(b"tamper")
        with self.assertRaises(Invalid): repair.inspect(self.root)

    def test_journal_lock_denies_apply_without_append(self):
        with self.store.lock(), self.assertRaises(Invalid): self.apply()
        self.assertEqual(self.before, self.history.read_bytes())

    def test_standalone_cli_apply_returns_new_exact_pinned_cli(self):
        value = self.call(CLI, "--apply", "--confirm", "--expected-journal-sha256", self.sha,
                          "--approval-file", str(self.approval))
        self.assertEqual(value["version"], "0.3.5")
        self.assertTrue(Path(value["cli"]).is_file())
        self.assertTrue(self.history.read_bytes().startswith(self.before))

    def test_cli_json_and_diagnostics_are_utf8_even_with_legacy_pipe_encoding(self):
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        command = [sys.executable, "-B", str(CLI), "--root", str(self.root)]
        result = subprocess.run(command, env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(json.loads(result.stdout.decode("utf-8"))["originalRuntime"], str(self.original))
        denied = subprocess.run(command + ["--apply"], env=env, capture_output=True, timeout=30)
        self.assertEqual(denied.returncode, 2)
        self.assertIn("требуется", denied.stderr.decode("utf-8"))
        self.assertNotIn("Traceback", denied.stderr.decode("utf-8"))
        self.assertEqual(self.before, self.history.read_bytes())


if __name__ == "__main__":
    unittest.main()
