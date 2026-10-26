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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core import runtime_zone_repair as repair
from aispa_core.store import Invalid, Journal, digest, file_hash

CLI = ROOT / "skills/aispa/tools/repair_task_zones.py"
MAINTENANCE_COMMIT = "e2f3ddb8284bb6539c1eaa3e1eddd5d1501eda98"


class ZoneRuntimeRepairTests(unittest.TestCase):
    def official_init(self, setup):
        """Give a synthetic fixture the official pin from its first event."""
        original_create = Journal.create
        def create_with_official_pin(store, slug, title, initializer=None):
            def initialize(state):
                if initializer is not None:
                    initializer(state)
                state["runtimeDigest"] = repair.OFFICIAL_DIGEST
            state = original_create(store, slug, title, initialize)
            shutil.copytree(self.package, store.run / "runtime")
            return state
        with patch.object(Journal, "create", create_with_official_pin):
            setup()

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
        cls.maintenance = Path(cls.release.name) / "exact-maintenance"
        names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", MAINTENANCE_COMMIT, "skills/aispa"],
                                        cwd=ROOT).decode().splitlines()
        for name in names:
            path = cls.maintenance / name.removeprefix("skills/aispa/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(["git", "show", MAINTENANCE_COMMIT + ":" + name], cwd=ROOT))
        # The fixture must stay the exact historical package even when the
        # current repair readers receive later safety fixes.
        historical_tools = {
            "tools/aispa_core/runtime_zone_repair.py": "679efa6e3f571c53c741ce0108b1427ed174339ffbeadd19481af612aa9e08fc",
            "tools/repair_task_zones.py": "a5d50af397f3360369d22fd9210f1d34d56d950fe4bf2863fb8014a5b10a8f7a",
        }
        for name, expected_sha in historical_tools.items():
            assert file_hash(cls.maintenance / name) == expected_sha, name

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
        return self.historical("prepare", candidate_package=self.maintenance,
                               expected_journal_sha256=self.sha)

    def historical(self, action, *, project_root=None, **options):
        command = [sys.executable, "-B", str(self.maintenance / "tools/repair_task_zones.py"),
                   "--root", str(project_root or self.root), action]
        for key, value in options.items():
            flag = "--" + key.replace("_", "-")
            if isinstance(value, bool):
                if value:
                    command.append(flag)
            elif value is not None:
                command.extend((flag, str(value)))
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertNotIn("Traceback", result.stderr)
        if result.returncode:
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            raise Invalid(result.stderr.strip())
        return json.loads(result.stdout)

    def apply(self, prepared, **kw):
        args = {"manifest_file": prepared["manifestPath"], "expected_manifest_sha256": prepared["manifest"]["sha256"],
                "expected_journal_sha256": self.sha, "approval_file": self.approval, "confirm": True}
        args.update(kw)
        return self.historical("apply", **args)

    def first_maintenance(self):
        """Exact previously selected package, before this uncommitted repair."""
        source = Path(self.temp.name) / "first-maintenance"
        names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "3dce187", "skills/aispa"], cwd=ROOT).decode().splitlines()
        for name in names:
            path = source / name.removeprefix("skills/aispa/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(["git", "show", "3dce187:" + name], cwd=ROOT))
        self.assertEqual(digest(repair._tree(source)), "3e41d77d07a803ac6c193be9a28759ea17574e418d5c891fd8d3f0167c8c4bf1")
        cli = source / "tools/repair_task_zones.py"
        prepared = self.call(cli, "prepare", "--candidate-package", str(source), "--expected-journal-sha256", self.sha)
        self.call(cli, "apply", "--manifest-file", prepared["manifestPath"],
                  "--expected-manifest-sha256", prepared["manifest"]["sha256"],
                  "--expected-journal-sha256", self.sha, "--approval-file", str(self.approval), "--confirm")
        self.sha = file_hash(self.history)
        return prepared

    def test_second_immutable_repair_preserves_first_manifest_prefix_runtime_and_attempts(self):
        first = self.first_maintenance()
        before = self.store.load()
        prefix = self.history.read_bytes()
        retained = {p.relative_to(self.run).as_posix(): p.read_bytes() for p in self.run.rglob("*")
                    if p.is_file() and p.name != "events.jsonl"}
        prepared = self.prepare()
        self.assertEqual(prefix, self.history.read_bytes())
        self.assertNotEqual(prepared["runtime"], first["runtime"])
        manifest = json.loads(Path(prepared["manifestPath"]).read_text())
        self.assertEqual(manifest["oldRuntimeDigest"], first["newRuntimeDigest"])
        result = self.apply(prepared)
        state = self.store.load()
        expected = {**before, **{key: state[key] for key in ("runtimeDigest", "runtimeZoneRepair", "updatedAt")}}
        self.assertEqual(state, expected)
        self.assertTrue(self.history.read_bytes().startswith(prefix))
        self.assertEqual(len(self.history.read_bytes().splitlines()), len(prefix.splitlines()) + 1)
        for name, raw in retained.items():
            self.assertEqual((self.run / name).read_bytes(), raw, name)
        self.assertTrue(repair.inspect(self.root)["alreadyRepaired"])
        self.call(Path(result["cli"]), "participant", "--role", "coordinator", "--runtime", "codex",
                  "--native-id", "synthetic-second-repair", "--session", "synthetic")
        self.sha = file_hash(self.history)
        self.assertEqual(self.apply(prepared)["newRuntimeDigest"], result["newRuntimeDigest"])
        with self.assertRaises(Invalid): self.prepare()  # Never a general third repair route.

    def test_second_repair_rejects_stale_cas_or_changed_first_manifest_readonly(self):
        first = self.first_maintenance()
        prefix = self.history.read_bytes()
        with self.assertRaises(Invalid):
            repair.prepare(self.root, candidate_package=ROOT / "skills/aispa", expected_journal_sha256="0" * 64)
        prepared = self.prepare()
        self.assertEqual(prefix, self.history.read_bytes())
        parent = Path(first["manifestPath"])
        os.chmod(parent, 0o644)
        parent.write_bytes(parent.read_bytes() + b"\n")
        with self.assertRaises(Invalid): self.apply(prepared)
        self.assertEqual(prefix, self.history.read_bytes())

    def test_malformed_manifest_object_is_a_clean_cli_refusal(self):
        prepared = self.prepare()
        path = Path(prepared["manifestPath"])
        os.chmod(path, 0o644)
        path.write_text("[]\n")
        result = self.call(CLI, "apply", "--manifest-file", str(path),
            "--expected-manifest-sha256", file_hash(path), "--expected-journal-sha256", self.sha,
            "--approval-file", str(self.approval), "--confirm", expected=2)
        self.assertIn("manifest должен быть объектом", result.stderr)
        self.assertEqual(self.prefix, self.history.read_bytes())

    def mixed_fixture(self, *, synthetic=False, omit_attachment=False):
        """Actual mixed observation schema; its auxiliary blob is arbitrary data."""
        import test_mixed_evidence
        from aispa_core.team import _seal
        fixture = test_mixed_evidence.MixedEvidenceTests(methodName="test_receipts_and_attachments_bound_and_sealed")
        self.official_init(fixture.setUp)
        self.addCleanup(fixture.doCleanups)
        module, report, observations = fixture.observations()
        fx = fixture.fx
        blob = fx.store.artifact("evidence", json.dumps({"synthetic": synthetic,
            "fixtureRun": "20261025T223633-team-ed35bcc7",
            "phaseOrigins": {"report": {"file": "reviews/8a4ba5c1b0d041adb4c6c9ae480deac9.json", "sha256": "5" * 64}},
            # A blob cannot declare observation edges just by resembling one.
            "attachments": [{"file": "evidence/foreign-description.txt", "sha256": "6" * 64}]}).encode(), ".json")
        body, old_ref, screenshot = observations["browser"]
        body["attachments"].append(blob)
        observation = fx.store.artifact("evidence", json.dumps(body).encode(), ".json")
        for row in report["routeChecks"]:
            if row["artifact"] == old_ref["file"]:
                row["artifact"] = observation["file"]
        artifacts, failures = module.check(fx.store, fx.state, "T01", report)
        self.assertFalse(failures)
        if omit_attachment:
            artifacts = [ref for ref in artifacts if ref != blob]
        attempt = fx.state["taskHistory"]["T01"][-1]
        attempt["submitted"] = _seal(fx.store, {"testIntent": attempt["testIntent"],
            "report": fx.store.artifact("reviews", json.dumps(report).encode(), ".json"),
            "artifacts": artifacts, "passed": True, "basis": report["basis"], "actor": "synthetic-fixture"})
        structural = fx.store.artifact("evidence", b"actual structural attachment", ".txt")
        fx.state["gates"]["fixture"] = {"report": fx.store.artifact("reviews",
            json.dumps({"nestedProof": structural}).encode(), ".json")}
        fx.store.append(fx.state, "synthetic-mixed-evidence")
        return fx, {"blob": blob, "observation": observation, "screenshot": screenshot, "structural": structural}

    def test_mixed_attachment_foreign_description_is_opaque_with_or_without_synthetic_flag(self):
        for synthetic in (False, True):
            with self.subTest(synthetic=synthetic):
                fx, refs = self.mixed_fixture(synthetic=synthetic)
                before = {p.relative_to(fx.store.run).as_posix(): p.read_bytes() for p in fx.store.run.rglob("*") if p.is_file()}
                self.assertFalse(repair.inspect(fx.root)["alreadyRepaired"])
                self.assertEqual(before, {p.relative_to(fx.store.run).as_posix(): p.read_bytes() for p in fx.store.run.rglob("*") if p.is_file()})

    def test_mixed_declared_attachment_omitted_from_sealed_artifacts_is_refused(self):
        fx, _ = self.mixed_fixture(omit_attachment=True)
        prefix = (fx.store.run / "events.jsonl").read_bytes()
        with self.assertRaisesRegex(Invalid, "[Mm]ixed|приложен"):
            repair.inspect(fx.root)
        self.assertEqual(prefix, (fx.store.run / "events.jsonl").read_bytes())

    def test_mixed_blob_observation_screenshot_and_nested_structural_tamper_still_refused(self):
        fx, refs = self.mixed_fixture()
        for kind, ref in refs.items():
            with self.subTest(kind=kind):
                path = fx.store.run / ref["file"]
                raw = path.read_bytes()
                path.write_bytes(raw + b"tampered")
                with self.assertRaises(Invalid): repair.inspect(fx.root)
                path.write_bytes(raw)
        self.assertFalse(repair.inspect(fx.root)["alreadyRepaired"])

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

    def test_current_broad_package_is_refused_by_historical_repair_guard(self):
        before = {p.relative_to(self.run).as_posix(): p.read_bytes()
                  for p in self.run.rglob("*") if p.is_file()}
        with self.assertRaisesRegex(Invalid, "узкий|maintenance"):
            repair.prepare(self.root, candidate_package=ROOT / "skills/aispa",
                           expected_journal_sha256=self.sha)
        self.assertEqual(before, {p.relative_to(self.run).as_posix(): p.read_bytes()
                                  for p in self.run.rglob("*") if p.is_file()})

    def test_official_runtime_copy_supports_failed_attempt_epoch_and_real_repair(self):
        from test_task_zone_extension import ZoneExtensionTests
        from aispa_core import participants, phase_receipts
        from aispa_core.contracts import basis
        from aispa_core.team import _history_valid, acceptance_chain
        fixture = ZoneExtensionTests(methodName="test_same_attempt_old_fail_new_red_green_and_second_critic")
        self.official_init(fixture.setUp)
        try:
            state, store = fixture.state, fixture.store
            author = participants.register(store, state, "coverage", "fixture", "synthetic-coverage", "synthetic-session")
            ref = store.artifact("reviews", b"{}\n", ".json")
            state["gates"]["coverage"] = {"passed": True, "basis": basis(store, state, "coverage"), "report": ref}
            phase_receipts.record(store, state, "coverage", ref, actor=author)
            fixture.host.save("synthetic-official-runtime-and-coverage")
            prefix = (store.run / "events.jsonl").read_bytes()
            sha = file_hash(store.run / "events.jsonl")
            prepared = self.historical("prepare", project_root=fixture.root,
                candidate_package=self.maintenance, expected_journal_sha256=sha)
            result = self.historical("apply", project_root=fixture.root, manifest_file=prepared["manifestPath"],
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
