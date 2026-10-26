"""Runner regressions use only isolated tiny suites, never project discovery."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


RUNNER_PATH = Path(__file__).resolve().parents[1] / "tools/run_contract_tests.py"
SPEC = importlib.util.spec_from_file_location("aispa_contract_test_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class CiRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa ci runner 🙂 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "tools").mkdir()
        (self.root / "tests").mkdir()
        self.script = self.root / "tools/run_contract_tests.py"
        self.script.write_bytes(RUNNER_PATH.read_bytes())

    def module(self, text, name="test_fixture.py"):
        (self.root / "tests" / name).write_text(textwrap.dedent(text), encoding="utf-8")

    @staticmethod
    def environment():
        return dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    def run_suite(self, *args, standard=False, without_pythonpath=False):
        command = ([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]
                   if standard else [sys.executable, "-B", str(self.script), *args])
        environment = self.environment()
        if without_pythonpath:
            environment.pop("PYTHONPATH", None)
        return subprocess.run(command, cwd=self.root, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", timeout=30)

    def tokens(self, output):
        return [int(value) for value in re.findall(r"OCCURRENCE (\d+)", output)]

    def duplicate_suite(self):
        self.module('''
            import unittest
            def setUpModule():
                print("MODULE_SETUP", flush=True)
            def tearDownModule():
                print("MODULE_TEARDOWN", flush=True)
            class TokenCase(unittest.TestCase):
                def __init__(self, token):
                    super().__init__("runTest")
                    self.token = token
                @classmethod
                def setUpClass(cls):
                    print("CLASS_SETUP", flush=True)
                @classmethod
                def tearDownClass(cls):
                    print("CLASS_TEARDOWN", flush=True)
                def id(self):
                    return "test_fixture.TokenCase.repeated_id"
                def runTest(self):
                    print("OCCURRENCE " + str(self.token), flush=True)
            def load_tests(loader, ordinary, pattern):
                cases = [TokenCase(index) for index in range(6)]
                return unittest.TestSuite([unittest.TestSuite(cases[:2]),
                    unittest.TestSuite([cases[2], unittest.TestSuite(cases[3:])])])
        ''')

    def result_suite(self):
        self.module('''
            import unittest
            class ResultCases(unittest.TestCase):
                def test_00_failure(self):
                    self.fail("ordinary failure diagnostic")
                def test_01_error(self):
                    raise RuntimeError("ordinary error diagnostic")
                def test_02_subtests(self):
                    for index in range(3):
                        with self.subTest(index=index):
                            if index == 0:
                                self.fail("subtest failure diagnostic")
                            if index == 1:
                                raise ValueError("subtest error diagnostic")
                @unittest.skip("fixture skip")
                def test_03_skip(self):
                    pass
                @unittest.expectedFailure
                def test_04_expected_failure(self):
                    self.fail("expected fixture failure")
                @unittest.expectedFailure
                def test_05_unexpected_success(self):
                    pass
                def test_06_after_failures(self):
                    print("AFTER_FAILURES", flush=True)
        ''')

    def test_occurrence_partition_preserves_nested_order_and_duplicate_instances(self):
        class Case(unittest.TestCase):
            def runTest(self):
                pass
            def id(self):
                return "same-id"
        shared = Case()
        leaves = [Case(), shared, Case(), shared, Case(), Case(), Case()]
        nested = unittest.TestSuite([unittest.TestSuite(leaves[:3]),
                                    unittest.BaseTestSuite(leaves[3:])])
        flattened = list(runner.flatten_suite(nested))
        self.assertEqual(flattened, leaves)
        for count in (1, 2, 3, 9):
            partitions = [runner.select_occurrences(flattened, count, index) for index in range(count)]
            indexes = [[position for position, _case in selected] for selected in partitions]
            self.assertEqual(sorted(position for selected in indexes for position in selected), list(range(7)))
            self.assertEqual(sum(len(selected) for selected in indexes), 7)
            for index, selected in enumerate(partitions):
                self.assertEqual([position for position, _case in selected], list(range(index, 7, count)))
                self.assertTrue(all(test is leaves[position] for position, test in selected))
        self.assertIs(flattened[1], flattened[3])

    def test_manifest_includes_order_unicode_and_duplicate_ids(self):
        class Identifier:
            def __init__(self, value):
                self.value = value
            def id(self):
                return self.value
        identifiers = ["duplicate", "тест", "duplicate", "last"]
        cases = [Identifier(value) for value in identifiers]
        expected = hashlib.sha256(json.dumps(identifiers, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(runner.manifest_sha256(cases), expected)
        self.assertNotEqual(runner.manifest_sha256(cases), runner.manifest_sha256(cases[::-1]))
        self.assertNotEqual(runner.manifest_sha256(cases), runner.manifest_sha256(cases[:2] + cases[3:]))

    def test_two_shards_execute_disjoint_complete_occurrences_with_shared_manifest(self):
        self.duplicate_suite()
        expected_sha = hashlib.sha256(json.dumps(["test_fixture.TokenCase.repeated_id"] * 6,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        partitions = []
        for index in (0, 1):
            result = self.run_suite("--shards", "2", "--shard-index", str(index))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("discovered=6 ordered_ids_sha256=" + expected_sha + " selected=3"
                          + " shard_index=" + str(index) + " shards=2", result.stdout)
            partitions.append(self.tokens(result.stdout))
            self.assertEqual(partitions[-1], list(range(index, 6, 2)))
            for marker in ("MODULE_SETUP", "MODULE_TEARDOWN", "CLASS_SETUP", "CLASS_TEARDOWN"):
                self.assertEqual(result.stdout.count(marker), 1, result.stdout)
        self.assertEqual(sorted(partitions[0] + partitions[1]), list(range(6)))
        self.assertFalse(set(partitions[0]) & set(partitions[1]))

    def test_default_full_matches_normal_discovery_order_duplicates_and_fixtures(self):
        self.duplicate_suite()
        standard = self.run_suite(standard=True)
        actual = self.run_suite()
        self.assertEqual((standard.returncode, actual.returncode), (0, 0), actual.stdout)
        self.assertEqual(self.tokens(actual.stdout), self.tokens(standard.stdout))
        self.assertEqual(self.tokens(actual.stdout), list(range(6)))
        self.assertIn("selected=6 shard_index=0 shards=1", actual.stdout)
        self.assertIn("Ran 6 tests", actual.stdout)
        for marker in ("MODULE_SETUP", "MODULE_TEARDOWN", "CLASS_SETUP", "CLASS_TEARDOWN"):
            self.assertEqual(actual.stdout.count(marker), standard.stdout.count(marker))

    def test_package_helpers_match_standard_discovery_without_pythonpath(self):
        (self.root / "tests/__init__.py").write_text("", encoding="utf-8")
        (self.root / "tests/helper.py").write_text("VALUE = 7\n", encoding="utf-8")
        (self.root / "project_helper.py").write_text("VALUE = 9\n", encoding="utf-8")
        self.module('''
            import unittest
            from tests import helper
            import project_helper
            class PackageImports(unittest.TestCase):
                def test_a_package_helper(self):
                    self.assertEqual(helper.VALUE, 7)
                def test_b_root_helper(self):
                    self.assertEqual(project_helper.VALUE, 9)
        ''')
        standard = self.run_suite(standard=True, without_pythonpath=True)
        self.assertEqual(standard.returncode, 0, standard.stdout)
        self.assertIn("Ran 2 tests", standard.stdout)
        ids = ["test_fixture.PackageImports.test_a_package_helper",
               "test_fixture.PackageImports.test_b_root_helper"]
        expected_sha = hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode("utf-8")).hexdigest()
        for args, selected in (((), 2), (("--shards", "2", "--shard-index", "0"), 1),
                               (("--shards", "2", "--shard-index", "1"), 1)):
            with self.subTest(args=args):
                result = self.run_suite(*args, without_pythonpath=True)
                self.assertEqual(result.returncode, standard.returncode, result.stdout)
                self.assertIn("discovered=2 ordered_ids_sha256=" + expected_sha
                              + " selected=" + str(selected), result.stdout)
                self.assertIn("Ran " + str(selected) + " test", result.stdout)
                self.assertNotIn("DISCOVERY ERROR:", result.stdout)

    def test_invalid_cli_arguments_fail_before_discovery(self):
        self.module('''
            from pathlib import Path
            Path("DISCOVERY_WAS_RUN").write_text("unexpected", encoding="utf-8")
        ''')
        invalid = [("--shards", "0"), ("--shards", "-1"), ("--shards", "no"),
                   ("--shards", "1.5"), ("--shard-index", "-1"),
                   ("--shards", "2", "--shard-index", "2"), ("--unknown",)]
        for args in invalid:
            with self.subTest(args=args):
                result = self.run_suite(*args)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("error:", result.stdout)
                self.assertFalse((self.root / "DISCOVERY_WAS_RUN").exists())
        for shards, index in ((True, 0), (2, False), (0, 0), (2, 2)):
            with self.assertRaises(ValueError):
                runner.select_occurrences([], shards, index)

    def test_empty_full_discovery_and_empty_valid_shard_never_pass(self):
        result = self.run_suite()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("discovered=0", result.stdout)
        self.assertIn("selected shard is empty", result.stdout)
        self.module('''
            import unittest
            class One(unittest.TestCase):
                def test_one(self):
                    pass
        ''')
        result = self.run_suite("--shards", "2", "--shard-index", "1")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("discovered=1", result.stdout)
        self.assertIn("selected=0 shard_index=1 shards=2", result.stdout)

    def test_failure_error_subtest_skip_and_expected_results_match_unittest(self):
        self.result_suite()
        standard, actual = self.run_suite(standard=True), self.run_suite()
        self.assertEqual((standard.returncode, actual.returncode), (1, 1))
        summary = "FAILED (failures=2, errors=2, skipped=1, expected failures=1, unexpected successes=1)"
        self.assertIn(summary, standard.stdout)
        self.assertIn(summary, actual.stdout)
        self.assertIn("Ran 7 tests", actual.stdout)
        after = actual.stdout.index("AFTER_FAILURES")
        for diagnostic in ("ordinary failure diagnostic", "ordinary error diagnostic",
                           "subtest failure diagnostic", "subtest error diagnostic"):
            self.assertLess(actual.stdout.index(diagnostic), after, actual.stdout)
        self.assertEqual(actual.stdout.count("IMMEDIATE FAIL:"), 2)
        self.assertEqual(actual.stdout.count("IMMEDIATE ERROR:"), 2)

    def test_class_fixture_error_is_reported_immediately_and_exits_nonzero(self):
        self.module('''
            import unittest
            class BadFixture(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise RuntimeError("class fixture diagnostic")
                def test_unreachable(self):
                    raise AssertionError("test must not execute")
            class Later(unittest.TestCase):
                def test_later(self):
                    print("LATER_CLASS_EXECUTED", flush=True)
        ''')
        result = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("IMMEDIATE ERROR: setUpClass", result.stdout)
        self.assertLess(result.stdout.index("class fixture diagnostic"), result.stdout.index("LATER_CLASS_EXECUTED"))
        self.assertIn("FAILED (errors=1)", result.stdout)

    def test_import_failure_is_visible_even_in_shard_selecting_only_healthy_case(self):
        self.module("import aispa_ci_runner_dependency_that_does_not_exist\n", "test_00_missing.py")
        self.module('''
            import unittest
            class Healthy(unittest.TestCase):
                def test_pass(self):
                    print("HEALTHY_EXECUTED", flush=True)
        ''', "test_99_healthy.py")
        for index in (0, 1):
            with self.subTest(index=index):
                result = self.run_suite("--shards", "2", "--shard-index", str(index))
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("discovered=2", result.stdout)
                self.assertIn("DISCOVERY ERROR:", result.stdout)
                self.assertIn("ModuleNotFoundError", result.stdout)
                self.assertIn("Contract discovery is incomplete", result.stdout)
                if index == 1:
                    self.assertIn("HEALTHY_EXECUTED", result.stdout)
                    self.assertLess(result.stdout.index("ModuleNotFoundError"), result.stdout.index("HEALTHY_EXECUTED"))

    def cancelled_output(self):
        log = self.root / "partial.log"
        with log.open("w", encoding="utf-8") as output:
            process = subprocess.Popen([sys.executable, "-B", str(self.script)], cwd=self.root,
                env=self.environment(), stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while not (self.root / "BLOCK_READY").exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail("Tiny cancellation fixture never reached its wait: " + log.read_text(encoding="utf-8"))
                    time.sleep(0.01)
                partial = log.read_text(encoding="utf-8")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
            self.assertNotEqual(process.returncode, 0)
        return partial

    def test_diagnostics_are_flushed_before_cancellation_without_final_summary(self):
        self.module('''
            from pathlib import Path
            import time
            import unittest
            class CancelCases(unittest.TestCase):
                def test_00_failure(self):
                    self.fail("diagnostic available before cancellation")
                def test_99_wait(self):
                    Path("BLOCK_READY").write_text("ready", encoding="utf-8")
                    while True:
                        time.sleep(0.01)
        ''')
        partial = self.cancelled_output()
        self.assertIn("IMMEDIATE FAIL:", partial)
        self.assertIn("AssertionError: diagnostic available before cancellation", partial)
        self.assertNotIn("Ran 2 tests", partial)

    def test_import_error_is_flushed_before_later_discovery_is_cancelled(self):
        self.module("import aispa_ci_runner_dependency_that_does_not_exist\n", "test_00_missing.py")
        self.module('''
            from pathlib import Path
            import time
            Path("BLOCK_READY").write_text("ready", encoding="utf-8")
            while True:
                time.sleep(0.01)
        ''', "test_99_wait.py")
        partial = self.cancelled_output()
        self.assertIn("DISCOVERY ERROR:", partial)
        self.assertIn("ModuleNotFoundError", partial)
        self.assertNotIn("Contract tests: discovered=", partial)


if __name__ == "__main__":
    unittest.main()
