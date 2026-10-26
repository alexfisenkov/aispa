"""Bounded scope revisions keep every exact file and the original attempt.

The initial RED uses existing graph/CLI behavior. Maintenance fixtures are only
loaded after the real source CLI admits its extension operation; an absent
future module is never imported at test discovery or used as a failing test.
"""
import copy
from contextlib import redirect_stderr
import importlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))

from aispa_core import scale
from aispa_core.scale.graph import DependencyIndex
from aispa_core.store import Invalid


class TaskZoneScaleTests(unittest.TestCase):
    def test_graph_budget_progress_is_items_not_bytes(self):
        with patch.dict(os.environ, {"AISPA_OPERATION_SECONDS": "1"}):
            with patch("aispa_core.scale.time.monotonic", side_effect=[0.0, 2.0]):
                budget = scale.Budget()
                with self.assertRaises(Invalid) as caught:
                    DependencyIndex([{"id": "T01", "dependsOn": []}], budget=budget)
        message = str(caught.exception)
        self.assertIn("RESOURCE_LIMIT", message)
        self.assertIn("dependency DAG validation", message)
        self.assertIn("completedItems=0", message,
                      "graph progress must be reported as completedItems")
        self.assertNotIn("completedBytes", message)

    def test_byte_transport_budget_keeps_byte_progress(self):
        budget = scale.Budget()
        with self.assertRaises(Invalid) as caught:
            budget.check(budget.max_bytes + 1, path="fixture input", progress=7)
        self.assertIn("completedBytes=7", str(caught.exception))
        self.assertNotIn("completedItems", str(caught.exception))

    def fixture(self, *, scalable=True):
        spec = importlib.util.spec_from_file_location(
            "aispa_zone_scale_cli", ROOT / "skills/aispa/tools/aispa.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        diagnostics = io.StringIO()
        try:
            with redirect_stderr(diagnostics):
                parsed = cli.parser().parse_args([
                    "--root", str(ROOT), "task-zone-extension", "T01"])
        except SystemExit as error:
            self.fail("task-zone-extension must be admitted by the source CLI: "
                      + str(error.code) + " " + diagnostics.getvalue())
        self.assertEqual(parsed.action, "task-zone-extension")

        # This is an actual producer fixture added by the maintenance import,
        # reached only after its public operation is admitted by the source CLI.
        fixtures = importlib.import_module("test_task_zone_extension")

        class PolicyFixture(fixtures.ZoneExtensionTests):
            def review(inner, tasks):
                if scalable:
                    inner.state["policy"]["scalableInputs"] = True
                return super().review(tasks)

        fixture = PolicyFixture(
            methodName="test_same_attempt_old_fail_new_red_green_and_second_critic")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture, cli.task_zone_extension

    @staticmethod
    def run_files(fixture):
        return {path.relative_to(fixture.store.run).as_posix(): path.read_bytes()
                for path in fixture.store.run.rglob("*") if path.is_file()}

    def continue_epoch(self, fixture):
        # A real new-epoch RED is still required by the legacy intent fixture.
        # The implementation remains intact; only its disposable test is revised.
        (fixture.root / "tests/test_app.py").write_text(
            "import runpy\nassert runpy.run_path('extra.py')['EXTRA'] == 1, "
            "'EXTRA must equal one'\n", encoding="utf-8")
        fixture.intent(["tests/test_app.py"], "EXTRA must equal one")

    def test_scalable_extension_keeps_all_33_files_and_original_history(self):
        fixture, extension = self.fixture()
        names = ["future/file_%02d.py" % i for i in range(33)]
        original = copy.deepcopy(fixture.attempt)
        prefix = (fixture.store.run / "events.jsonl").read_bytes()
        receipt = fixture.extend(fixture.proposal(names))
        self.assertEqual(receipt["files"], names)
        self.assertEqual(fixture.host.task("T01")["zones"][-33:], names)
        self.assertEqual(fixture.attempt["id"], original["id"])
        self.assertEqual(fixture.attempt["submitted"], original["submitted"])
        self.assertEqual(fixture.attempt["rounds"], original["rounds"])
        self.assertEqual(fixture.attempt["actors"], original["actors"])
        self.assertTrue((fixture.store.run / "events.jsonl").read_bytes().startswith(prefix))
        self.assertTrue(extension.history_valid(
            fixture.store, fixture.state, "T01", fixture.attempt))

    def test_scalable_ninth_epoch_retains_every_predecessor_without_new_attempt(self):
        fixture, extension = self.fixture()
        original = copy.deepcopy(fixture.attempt)
        receipts = []
        for number in range(9):
            if number:
                self.continue_epoch(fixture)
            receipts.append(fixture.extend(fixture.proposal([
                "future/epoch_%02d.py" % number])))
        self.assertEqual(len(fixture.attempt["zoneExtensions"]), 9)
        self.assertEqual([row["epoch"] for row in receipts], list(range(1, 10)))
        self.assertEqual([row["previous"] for row in receipts],
                         [None] + [row["record"]["sha256"] for row in receipts[:-1]])
        self.assertEqual(fixture.attempt["id"], original["id"])
        self.assertEqual(fixture.attempt["rounds"], original["rounds"])
        self.assertEqual(fixture.attempt["submitted"], original["submitted"])
        self.assertFalse(fixture.attempt.get("repair"))
        self.assertTrue(extension.history_valid(
            fixture.store, fixture.state, "T01", fixture.attempt))

    def test_legacy_33_file_request_is_refused_without_partial_history(self):
        fixture, _ = self.fixture(scalable=False)
        request = fixture.proposal(["future/file_%02d.py" % i for i in range(33)])
        before_state, before_files = copy.deepcopy(fixture.state), self.run_files(fixture)
        with self.assertRaisesRegex(Invalid, "32|точных файла"):
            fixture.extend(request)
        self.assertEqual(fixture.state, before_state)
        self.assertEqual(self.run_files(fixture), before_files)

    def test_legacy_ninth_epoch_is_refused_and_first_eight_remain_sealed(self):
        fixture, extension = self.fixture(scalable=False)
        for number in range(8):
            if number:
                self.continue_epoch(fixture)
            fixture.extend(fixture.proposal(["future/epoch_%02d.py" % number]))
        self.continue_epoch(fixture)
        request = fixture.proposal(["future/epoch_08.py"])
        before_state, before_files = copy.deepcopy(fixture.state), self.run_files(fixture)
        with self.assertRaisesRegex(Invalid, "исчерпаны расширения"):
            fixture.extend(request)
        self.assertEqual(fixture.state, before_state)
        self.assertEqual(self.run_files(fixture), before_files)
        self.assertEqual(len(fixture.attempt["zoneExtensions"]), 8)
        self.assertTrue(extension.history_valid(
            fixture.store, fixture.state, "T01", fixture.attempt))

    def test_operation_budget_refusal_keeps_all_scope_and_artifacts_unchanged(self):
        fixture, _ = self.fixture()
        request = fixture.proposal(["future/file_%02d.py" % i for i in range(33)])
        before_state, before_files = copy.deepcopy(fixture.state), self.run_files(fixture)
        with patch.dict(os.environ, {"AISPA_MAX_INPUT_BYTES": "64"}):
            with self.assertRaisesRegex(Invalid, "RESOURCE_LIMIT"):
                fixture.extend(request)
        self.assertEqual(fixture.state, before_state)
        self.assertEqual(self.run_files(fixture), before_files)
        self.assertFalse(fixture.attempt.get("zoneExtensions"))

    def test_mixed_case_exact_file_names_follow_portable_zone_identity(self):
        fixture, extension = self.fixture()
        # Exercise only portable matching semantics on POSIX; the final native
        # Windows CI separately checks the filesystem behavior of these names.
        with patch("aispa_core.store.match_name", side_effect=lambda value: value.casefold()):
            for name in ("Makefile", "Dockerfile", "Future/Module.py"):
                with self.subTest(name=name):
                    self.assertEqual(extension._exact_file(fixture.store, name), name.casefold())
        for name in ("../escape.py", ".git/config", "CON", "future/*.py"):
            with self.subTest(name=name), self.assertRaises((Invalid, ValueError)):
                extension._exact_file(fixture.store, name)

    def test_foreign_cas_and_invalid_path_do_not_create_epochs(self):
        fixture, _ = self.fixture()
        for mutation in ("cas", "path"):
            with self.subTest(mutation=mutation):
                request = fixture.proposal(["future/valid.py"])
                options = {"expected_journal_sha256": "0" * 64} if mutation == "cas" else {}
                if mutation == "path":
                    request["files"][0]["path"] = "../escape.py"
                before_state, before_files = copy.deepcopy(fixture.state), self.run_files(fixture)
                with self.assertRaises((Invalid, ValueError)):
                    fixture.extend(request, **options)
                self.assertEqual(fixture.state, before_state)
                self.assertEqual(self.run_files(fixture), before_files)


if __name__ == "__main__":
    unittest.main()
