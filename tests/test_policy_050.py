"""Foundation of 0.5.0: policy flags, version, default check mode, CLI skeleton and the check command."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "skills/aispa"
CLI = PACKAGE / "tools/aispa.py"
CHECK = REPO / "tools/check.py"
sys.path.insert(0, str(PACKAGE / "tools"))

from aispa_core import VERSION  # noqa: E402
from aispa_core.store import Invalid, Journal  # noqa: E402

NEW_FLAGS = ("focusedReview", "modelRoutes", "measuredTiming", "deltaJournal", "linkedDecisions",
             "interviewBudget", "targetedRebrief", "prechangeRevision", "flowDetails", "resumeReport")
# The 0.4.0 policy as `initialize_run` wrote it at b914fad.
POLICY_040 = ("discovery", "cleanClone", "ideaReview", "planReview", "projectKnowledge", "productChallenge",
              "deliveryPlan", "repairDiagnosis", "deliveryDocs", "sourceAccounting", "designContract",
              "allRoleUsage", "integrationReview", "testIntent", "mixedEvidence", "incrementalTestIntent",
              "adaptiveCritique", "phaseReceipts", "executionObligations", "finalWaveReuse",
              "errorBoundaryChecks", "taskTiers", "leanControl", "projectDocuments", "firstBriefingClosure",
              "scalableInputs", "criticalProductFlows", "managedExecution")


def run(cli, root, *args, timeout=60):
    return subprocess.run([sys.executable, "-B", str(cli), "--root", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout)


class RunFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa policy 050 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)

    def init(self, *extra, runtime="claude-code"):
        result = run(CLI, self.root, *extra, "init", "--slug", "policy", "--title", "Policy 0.5.0",
                     "--mode", "auto", "--runtime", runtime)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        output = json.loads(result.stdout)
        store = Journal(self.root)
        state = store.load()
        self.cli = store.run / "runtime/tools/aispa.py"
        self.run_directory = store.run
        return output, state


class NewRunTests(RunFixture):
    def test_version_is_050(self):
        self.assertEqual(VERSION, "0.5.0")

    def test_new_init_writes_ten_new_flags_and_keeps_previous_ones(self):
        _, state = self.init("--full")
        self.assertEqual(state["version"], "0.5.0")
        for flag in NEW_FLAGS + POLICY_040:
            self.assertIs(state["policy"].get(flag), True, flag)

    def test_init_without_check_is_balanced_from_default(self):
        _, state = self.init("--full")
        self.assertEqual(state["checkMode"], "balanced")
        self.assertEqual(len(state["checkModeHistory"]), 1)
        self.assertEqual(state["checkModeHistory"][0]["source"], "default")
        self.assertEqual(state["checkModeHistory"][0]["mode"], "balanced")

    def test_init_with_check_keeps_the_owner_choice(self):
        result = run(CLI, self.root, "init", "--slug", "policy", "--title", "Policy", "--mode", "auto",
                     "--runtime", "codex", "--check", "fast")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = Journal(self.root).load()
        self.assertEqual(state["checkMode"], "fast")
        self.assertEqual(state["checkModeHistory"][0]["source"], "init")

    def test_init_advises_the_main_chat_model_of_its_runtime(self):
        full, _ = self.init("--full")
        advice = full["coordinatorAdvice"]
        self.assertEqual((advice["model"], advice["effortBriefPlan"], advice["effortLater"]),
                         ("claude-opus-5-5", "high", "medium"))
        self.assertIn("Fable или Astra", advice["note"])
        other = Path(self.temp.name) / "codex project"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)
        result = run(CLI, other, "init", "--slug", "codex", "--title", "Codex", "--mode", "auto", "--runtime", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        brief = json.loads(result.stdout)
        self.assertTrue(brief["compact"], "init answers briefly by default")
        self.assertEqual(brief["coordinatorAdvice"]["model"], "gpt-6.1-sol")
        self.assertEqual(brief["coordinatorAdvice"]["effortBriefPlan"], "high")
        self.assertEqual(brief["coordinatorAdvice"]["effortLater"], "medium")


class ReservedCommandTests(RunFixture):
    def test_new_commands_describe_their_arguments(self):
        expected = {("findings",): ["--status"],
                    ("final-repair",): ["open", "assign", "submit", "check"],
                    ("final-repair", "open"): ["--file"],
                    ("final-repair", "assign"): ["--role", "worker", "checker", "--runtime", "--native-id",
                                                 "--session", "--model", "--reasoning"],
                    ("final-repair", "submit"): ["--file"],
                    ("final-repair", "check"): ["--file"],
                    ("task-open",): ["--role", "--runtime", "--native-id", "--session", "--model", "--reasoning",
                                     "--model-evidence"],
                    ("task-close",): ["--role", "--file", "--usage"]}
        for command, words in expected.items():
            with self.subTest(command=command):
                result = run(CLI, self.root, *command, "-h")
                self.assertEqual(result.returncode, 0, result.stderr)
                for word in words:
                    self.assertIn(word, result.stdout)

    def test_reserved_commands_name_the_ticket_that_brings_them(self):
        self.init()
        evidence = self.root / "report.json"
        evidence.write_text("{}", encoding="utf-8")
        participant = ["--runtime", "claude-code", "--native-id", "agent-1", "--session", "session-1",
                       "--model", "claude-sonnet-5-5", "--reasoning", "medium"]
        before = (self.run_directory / "events.jsonl").read_bytes()
        for command, ticket in ((["final-repair", "open", "--file", str(evidence)], "06"),
                                (["final-repair", "assign", "--role", "checker", *participant], "06"),
                                (["task-open", "T01", "--role", "worker", *participant], "10"),
                                (["task-close", "T01", "--role", "worker", "--file", str(evidence)], "10")):
            with self.subTest(command=command):
                result = run(self.cli, self.root, *command)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("задаче " + ticket, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.assertEqual((self.run_directory / "events.jsonl").read_bytes(), before)

    def test_findings_lists_collected_findings_instead_of_the_placeholder(self):
        # Ticket 02 replaced the placeholder: an empty run lists nothing, read-only.
        self.init()
        before = (self.run_directory / "events.jsonl").read_bytes()
        empty = {"open": 0, "confirmed": 0, "fixed": 0, "remaining": 0}
        for command, status in ((["findings"], None), (["findings", "--status", "open"], "open")):
            with self.subTest(command=command):
                result = run(self.cli, self.root, *command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("задаче 02", result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["findings"], [])
                self.assertEqual(output["count"], 0)
                self.assertEqual(output["counts"], empty)
                self.assertEqual(output.get("status"), status)
        self.assertEqual((self.run_directory / "events.jsonl").read_bytes(), before)

    def test_findings_is_read_only_and_task_open_is_a_write(self):
        self.init()
        with (self.run_directory / "runtime/SKILL.md").open("a", encoding="utf-8") as stream:
            stream.write("\nChanged pinned runtime\n")
        read = run(self.cli, self.root, "findings")
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(read.stdout)["count"], 0)
        self.assertNotIn("Пакет изменён", read.stderr)
        write = run(self.cli, self.root, "task-open", "T01", "--role", "worker", "--runtime", "codex",
                    "--native-id", "a", "--session", "s")
        self.assertEqual(write.returncode, 2)
        self.assertIn("Пакет изменён", write.stderr)


class PreviousRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa run 040 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.store = Journal(self.root)

        def initialize(state):
            state["policy"] = dict.fromkeys(POLICY_040, True)
            state["runtime"] = "codex"
            state["runtimeDigest"] = "0" * 64
        with patch("aispa_core.store.VERSION", "0.4.0"):
            self.state = self.store.create("old", "Synthetic 0.4.0 run", initializer=initialize)
        self.events = self.store.run / "events.jsonl"

    def test_previous_run_gets_no_new_flags_and_new_package_refuses_its_writes(self):
        self.assertEqual(self.state["version"], "0.4.0")
        self.assertFalse(set(NEW_FLAGS) & set(self.state["policy"]))
        before = self.events.read_bytes()
        result = run(CLI, self.root, "decision", "Запись из нового пакета")
        self.assertEqual(result.returncode, 2, result.stdout)
        # Version guard of the active journal comes before the runtime digest check, as for 0.3.x runs.
        self.assertIn("активный прогон продолжайте закреплённым runtime", result.stderr)
        self.assertEqual(self.events.read_bytes(), before)

    def test_new_readers_and_terminal_handoff_accept_040(self):
        from aispa_core.dashboard_reader import SUPPORTED_VERSIONS, DisplayJournal
        self.assertIn("0.4.0", SUPPORTED_VERSIONS)
        self.assertEqual(DisplayJournal(self.root).load()["version"], "0.4.0")
        with self.assertRaises(Invalid):
            self.store.load(handoff_only=True)
        self.state["status"] = "cancelled"
        self.store.append(self.state, "synthetic-terminal")
        self.assertEqual(Journal(self.root).load(handoff_only=True)["version"], "0.4.0")


@unittest.skipUnless(shutil.which("node"), "node is required by the project check command")
class CheckCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa check ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n", encoding="utf-8")
        (tests / "dashboard.test.cjs").write_text(
            "const test = require('node:test');\ntest('ok', () => {});\n", encoding="utf-8")

    def check(self, *args):
        return subprocess.run([sys.executable, "-B", str(CHECK), "--root", str(self.root), *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=120,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_full_suite_passes_when_everything_passes(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_any_unittest_failure_fails_the_check(self):
        (self.root / "tests/test_bad.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.fail('red')\n",
            encoding="utf-8")
        self.assertNotEqual(self.check().returncode, 0)

    def test_dashboard_failure_fails_the_check(self):
        (self.root / "tests/dashboard.test.cjs").write_text(
            "const test = require('node:test');\nconst assert = require('node:assert');\n"
            "test('red', () => { assert.strictEqual(1, 2); });\n", encoding="utf-8")
        self.assertNotEqual(self.check().returncode, 0)

    def test_one_file_runs_only_that_file(self):
        (self.root / "tests/test_bad.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.fail('red')\n",
            encoding="utf-8")
        self.assertEqual(self.check("--file", "tests/test_ok.py").returncode, 0)
        self.assertNotEqual(self.check("--file", "tests/test_bad.py").returncode, 0)
        self.assertNotEqual(self.check("--file", "tests/test_missing.py").returncode, 0)


if __name__ == "__main__":
    unittest.main()
