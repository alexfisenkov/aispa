"""Exact release fixture and isolated copies only; never touches an owner run."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core import runtime_zone_repair as repair
from aispa_core.store import Invalid, Journal, digest, file_hash

CLI = ROOT / "skills/aispa/tools/repair_task_zones.py"


class ZoneRuntimeRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = tempfile.TemporaryDirectory(prefix="aispa-exact-040-")
        cls.package = Path(cls.release.name) / "aispa"
        names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "v0.4.0", "skills/aispa"], cwd=ROOT).decode().splitlines()
        assert len(names) == 93
        for name in names:
            path = cls.package / name.removeprefix("skills/aispa/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(["git", "show", "v0.4.0:" + name], cwd=ROOT))
        assert digest(repair._tree(cls.package)) == repair.OFFICIAL_DIGEST
        assert subprocess.check_output(["git", "rev-parse", "v0.4.0^{}"], cwd=ROOT).decode().strip() == repair.SOURCE_BASELINE

    @classmethod
    def tearDownClass(cls):
        cls.release.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-zone-runtime-🙂-")
        self.root = Path(self.temp.name) / "project with spaces"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.call(self.package / "tools/aispa.py", "init", "--slug", "zone-runtime", "--title", "Synthetic fixture",
                  "--mode", "auto", "--runtime", "codex", "--check", "max")
        self.store = Journal(self.root)
        self.before_state = self.store.load()
        self.run = self.store.run
        self.history = self.run / "events.jsonl"
        self.prefix = self.history.read_bytes()
        self.sha = file_hash(self.history)
        self.approval = Path(self.temp.name) / "synthetic-existing-owner-authorization.txt"
        self.approval.write_text("SYNTHETIC FIXTURE ONLY: existing owner authorization for the maintenance fix.\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def call(self, cli, *args, expected=0):
        result = subprocess.run([sys.executable, "-B", str(cli), "--root", str(self.root), *args],
                                cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return json.loads(result.stdout) if expected == 0 else result

    def prepare(self):
        return repair.prepare(self.root, candidate_package=ROOT / "skills/aispa", expected_journal_sha256=self.sha)

    def apply(self, prepared, **kw):
        args = {"manifest_file": prepared["manifestPath"], "expected_manifest_sha256": prepared["manifest"]["sha256"],
                "expected_journal_sha256": self.sha, "approval_file": self.approval, "confirm": True}
        args.update(kw)
        return repair.apply(self.root, **args)

    def test_inspect_is_strictly_read_only_and_prepare_does_not_select(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        info = self.call(CLI, "inspect")
        self.assertFalse(info["alreadyRepaired"])
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        prepared = self.prepare()
        self.assertFalse(prepared["selected"])
        self.assertEqual(self.prefix, self.history.read_bytes())
        self.assertEqual(self.before_state, self.store.load())
        manifest = json.loads(Path(prepared["manifestPath"]).read_text(encoding="utf-8"))
        self.assertTrue(repair.REQUIRED_DIFF <= {r["path"] for r in manifest["diff"]} <= repair.ALLOWED_DIFF)
        self.assertEqual(manifest["newRuntimeDigest"], digest(repair._tree(Path(prepared["runtime"]))))

    def test_exact_prefix_original_runtime_and_future_pinned_cli_write(self):
        original = repair._tree(self.run / "runtime")
        prepared = self.prepare()
        result = self.apply(prepared)
        self.assertTrue(self.history.read_bytes().startswith(self.prefix))
        self.assertEqual(len(self.history.read_bytes().splitlines()), len(self.prefix.splitlines()) + 1)
        expected = copy.deepcopy(self.before_state)
        state = self.store.load()
        for field in ("runtimeDigest", "runtimeZoneRepair", "updatedAt"):
            expected[field] = state[field]
        self.assertEqual(expected, state)
        self.assertEqual(original, repair._tree(self.run / "runtime"))
        self.assertEqual(state["version"], "0.4.0")
        self.assertEqual(state["policy"], self.before_state["policy"])
        self.assertEqual(result["newRuntimeDigest"], prepared["newRuntimeDigest"])
        denied = self.call(self.run / "runtime/tools/aispa.py", "participant", "--role", "coordinator", "--runtime", "codex",
                           "--native-id", "synthetic-old", "--session", "synthetic", expected=2)
        self.assertIn("Пакет изменён", denied.stderr)
        self.call(Path(result["cli"]), "participant", "--role", "coordinator", "--runtime", "codex",
                  "--native-id", "synthetic-new", "--session", "synthetic")
        self.assertTrue(repair.inspect(self.root)["alreadyRepaired"])
        with self.assertRaises(Invalid): self.apply(prepared)

    def test_stale_cas_tamper_and_missing_confirmation_denied(self):
        prepared = self.prepare()
        for kw in ({"confirm": False}, {"expected_journal_sha256": "0" * 64}, {"expected_manifest_sha256": "0" * 64}):
            with self.subTest(kw=kw), self.assertRaises(Invalid): self.apply(prepared, **kw)
        target = Path(prepared["runtime"]) / "tools/aispa_core/team.py"
        os.chmod(target, 0o644)
        target.write_bytes(target.read_bytes() + b"\n# tampered\n")
        with self.assertRaises(Invalid): self.apply(prepared)
        self.assertEqual(self.prefix, self.history.read_bytes())

    def test_original_nonofficial_tree_and_unrelated_candidate_change_denied(self):
        target = self.run / "runtime/LICENSE"
        original = target.read_bytes()
        target.write_bytes(original + b"changed")
        with self.assertRaises(Invalid): repair.inspect(self.root)
        target.write_bytes(original)
        source = Path(self.temp.name) / "candidate"
        shutil.copytree(ROOT / "skills/aispa", source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (source / "tools/aispa_core/failure_checks.py").write_text("# forbidden unrelated T01 source code\n")
        with self.assertRaises(Invalid):
            repair.prepare(self.root, candidate_package=source, expected_journal_sha256=self.sha)
        self.assertEqual(self.prefix, self.history.read_bytes())

    def test_official_runtime_copy_supports_failed_attempt_epoch_and_real_repair(self):
        from test_task_zone_extension import ZoneExtensionTests
        from aispa_core import participants, phase_receipts
        from aispa_core.contracts import basis
        from aispa_core.team import _history_valid, acceptance_chain
        fixture = ZoneExtensionTests(methodName="test_same_attempt_old_fail_new_red_green_and_second_critic")
        fixture.setUp()
        try:
            state, store = fixture.state, fixture.store
            shutil.copytree(self.package, store.run / "runtime")
            state["runtimeDigest"] = repair.OFFICIAL_DIGEST
            author = participants.register(store, state, "coverage", "fixture", "synthetic-coverage", "synthetic-session")
            ref = store.artifact("reviews", b"{}\n", ".json")
            state["gates"]["coverage"] = {"passed": True, "basis": basis(store, state, "coverage"), "report": ref}
            phase_receipts.record(store, state, "coverage", ref, actor=author)
            fixture.host.save("synthetic-official-runtime-and-coverage")
            prefix = (store.run / "events.jsonl").read_bytes()
            sha = file_hash(store.run / "events.jsonl")
            prepared = repair.prepare(fixture.root, candidate_package=ROOT / "skills/aispa", expected_journal_sha256=sha)
            result = repair.apply(fixture.root, manifest_file=prepared["manifestPath"],
                expected_manifest_sha256=prepared["manifest"]["sha256"], expected_journal_sha256=sha,
                approval_file=self.approval, confirm=True)
            report_path = fixture.root / ".aispa/synthetic-zone-extension.json"
            report_path.write_text(json.dumps(fixture.proposal()), encoding="utf-8")
            result_cli = subprocess.run([sys.executable, "-B", result["cli"], "--root", str(fixture.root),
                "task-zone-extension", "T01", "--file", str(report_path), "--actor", fixture.planner,
                "--expected-journal-sha256", file_hash(store.run / "events.jsonl"), "--apply"],
                capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result_cli.returncode, 0, result_cli.stdout + result_cli.stderr)
            fixture.state.clear()
            fixture.state.update(store.load())
            fixture.attempt = fixture.state["taskHistory"]["T01"][-1]
            fixture.finish_repair()
            self.assertTrue(_history_valid(store, fixture.state, "T01"))
            self.assertEqual(acceptance_chain(store, fixture.state, "T01", fixture.attempt, require_acceptor=False)["number"], 2)
            self.assertTrue((store.run / "events.jsonl").read_bytes().startswith(prefix))
            self.assertTrue(repair.inspect(fixture.root)["alreadyRepaired"])
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
