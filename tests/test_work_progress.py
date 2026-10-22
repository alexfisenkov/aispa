"""Explicit host progress is wall-clock telemetry, never a model or proof run."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core import work_progress
from aispa_core.store import Invalid, Journal
from aispa_core.workflow import Workflow


T0 = "2026-10-22T10:00:00+00:00"
T1 = "2026-10-22T10:01:00+00:00"
T2 = "2026-10-22T10:02:00+00:00"
T3 = "2026-10-22T10:03:00+00:00"


class WorkProgressTests(unittest.TestCase):
    def state(self):
        return {"status": "running", "waiting": None, "gates": {"coverage": {"passed": False}}}

    def test_all_supported_phases_require_explicit_begin_and_never_grant_pass(self):
        self.assertEqual(len(work_progress.PHASES), 14)
        for phase in work_progress.PHASES:
            with self.subTest(phase=phase):
                state = self.state()
                gates = copy.deepcopy(state["gates"])
                self.assertTrue(work_progress.begin(state, phase, T0))
                view = work_progress.view(state, T1)
                self.assertEqual(view["phase"], phase)
                self.assertEqual(view["elapsedSeconds"], 60)
                self.assertEqual(view["label"], work_progress.LABELS[phase])
                self.assertNotIn("passed", view)
                self.assertTrue(work_progress.end(state, T1))
                self.assertEqual(state["gates"], gates)
                self.assertEqual(state["status"], "running")
                self.assertEqual(work_progress.view(state, T3)["elapsedSeconds"], 60)

    def test_repeated_begin_and_end_are_idempotent_and_read_does_not_mutate(self):
        state = self.state()
        work_progress.begin(state, "spec", T0)
        before = copy.deepcopy(state)
        self.assertFalse(work_progress.begin(state, "spec", T1))
        self.assertEqual(state, before)
        self.assertEqual(work_progress.view(state, T2)["elapsedSeconds"], 120)
        self.assertEqual(state, before)
        work_progress.end(state, T2)
        ended = copy.deepcopy(state)
        self.assertFalse(work_progress.end(state, T3))
        self.assertEqual(state, ended)

    def test_phase_change_closes_previous_interval_and_explicit_restart_is_new(self):
        state = self.state()
        work_progress.begin(state, "idea", T0)
        previous = state["workProgress"]
        self.assertTrue(work_progress.begin(state, "brief", T1))
        self.assertEqual(previous["finishedAt"], T1)
        self.assertEqual(previous["elapsedSeconds"], 60)
        self.assertEqual(state["workProgress"]["startedAt"], T1)
        self.assertEqual(work_progress.view(state, T2)["elapsedSeconds"], 60)
        work_progress.end(state, T2)
        self.assertTrue(work_progress.begin(state, "brief", T3))
        self.assertEqual(state["workProgress"]["startedAt"], T3)
        self.assertEqual(state["workProgress"]["elapsedSeconds"], 0)

    def test_pause_wait_and_resume_exclude_idle_time_not_cpu_measurement(self):
        for pause_kind in ("paused", "blocked", "owner_question"):
            with self.subTest(pause_kind=pause_kind):
                state = self.state()
                work_progress.begin(state, "coverage", T0)
                if pause_kind == "owner_question":
                    state["waiting"] = {"kind": pause_kind}
                else:
                    state["status"] = pause_kind
                work_progress.sync(state, "run-paused", T1)
                self.assertNotIn("activeSince", state["workProgress"])
                self.assertNotIn("finishedAt", state["workProgress"])
                self.assertEqual(work_progress.view(state, T2)["elapsedSeconds"], 60)
                state.update(status="running", waiting=None)
                work_progress.sync(state, "run-resumed", T2)
                self.assertEqual(work_progress.view(state, T3)["elapsedSeconds"], 120)

    def test_known_completion_only_ends_matching_existing_phase(self):
        for event, phase in work_progress.COMPLETION_EVENTS.items():
            with self.subTest(event=event):
                state = self.state()
                work_progress.sync(state, event, T0)
                self.assertIsNone(work_progress.view(state, T0))
                work_progress.begin(state, phase, T0)
                work_progress.sync(state, "participant-assigned:coverage", T1)
                self.assertNotIn("finishedAt", state["workProgress"])
                work_progress.sync(state, event + ":fixture", T2)
                self.assertEqual(state["workProgress"]["finishedAt"], T2)
                self.assertFalse(state["gates"]["coverage"]["passed"])
        state = self.state()
        work_progress.begin(state, "spec", T0)
        work_progress.sync(state, "idea-reviewed", T1)
        self.assertNotIn("finishedAt", state["workProgress"])

    def test_task_start_and_terminal_states_end_without_reopening(self):
        for event, status in (("task-started:T01", "running"),
                              ("delivery-recorded", "accepted"),
                              ("delivery-recorded", "delivered_with_remaining"),
                              ("run-cancelled", "cancelled")):
            with self.subTest(event=event, status=status):
                state = self.state()
                work_progress.begin(state, "plan", T0)
                state["status"] = status
                work_progress.sync(state, event, T1)
                self.assertEqual(state["workProgress"]["finishedAt"], T1)
                state["status"] = "running"
                work_progress.sync(state, "run-resumed", T2)
                self.assertEqual(work_progress.view(state, T3)["elapsedSeconds"], 60)
                self.assertNotIn("activeSince", state["workProgress"])

    def test_missing_legacy_field_stays_unknown_and_invalid_begin_changes_nothing(self):
        state = self.state()
        before = copy.deepcopy(state)
        self.assertIsNone(work_progress.view(state, T0))
        self.assertFalse(work_progress.end(state, T0))
        work_progress.sync(state, "plan-validated", T0)
        self.assertEqual(state, before)
        for phase in ("implementation", "arbitrary text", "", None):
            with self.assertRaises(Invalid):
                work_progress.begin(state, phase, T0)
            self.assertEqual(state, before)
        for status in ("paused", "blocked", "cancelled", "accepted"):
            state["status"] = status
            with self.assertRaises(Invalid):
                work_progress.begin(state, "idea", T0)
        state.update(status="running", waiting={"kind": "owner_question"})
        with self.assertRaises(Invalid):
            work_progress.begin(state, "idea", T0)


class WorkProgressWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-work-progress-")
        self.store = Journal(self.temp.name)
        self.state = self.store.create("progress", "Progress fixture")
        self.flow = Workflow(self.store, self.state)
        self.now = T0
        self.clock = patch("aispa_core.workflow.timestamp", side_effect=lambda: self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def test_workflow_save_syncs_pauses_owner_wait_resume_and_task_start(self):
        self.flow.progress_begin("brief")
        journal = self.store.run / "events.jsonl"
        original = journal.read_bytes()
        self.now = T1
        self.flow.progress_begin("brief")
        self.assertEqual(journal.read_bytes(), original)
        self.flow.wait("Synthetic question")
        self.assertNotIn("activeSince", self.store.load()["workProgress"])
        answer = Path(self.temp.name) / "answer.md"
        answer.write_text("Synthetic answer", encoding="utf-8")
        self.now = T2
        self.flow.answered(str(answer))
        self.assertEqual(work_progress.view(self.store.load(), T3)["elapsedSeconds"], 120)
        self.now = T3
        self.flow.close("paused", "Synthetic pause")
        self.assertNotIn("activeSince", self.store.load()["workProgress"])
        self.flow.resume()
        self.flow.save("task-started:T01")
        self.assertEqual(self.store.load()["workProgress"]["finishedAt"], T3)

    def test_end_is_available_while_paused_and_pending_recovery_blocks_changes(self):
        self.flow.progress_begin("idea")
        self.now = T1
        self.flow.close("paused", "Synthetic pause")
        self.now = T2
        self.flow.progress_end()
        self.assertEqual(self.store.load()["workProgress"]["elapsedSeconds"], 60)
        self.state["journalRecovery"] = {"pending": True}
        with self.assertRaises(Invalid):
            self.flow.progress_end()


class WorkProgressCliTests(unittest.TestCase):
    def test_cli_roundtrip_batch_and_validation_preserve_gates_and_host_actions(self):
        with tempfile.TemporaryDirectory(prefix="aispa-progress-cli-") as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            cli = ROOT / "skills/aispa/tools/aispa.py"

            def call(*args, expected=0):
                result = subprocess.run([sys.executable, "-B", str(cli), "--root", str(root), *args],
                    capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                return json.loads(result.stdout) if expected == 0 else result

            initial = call("init", "--slug", "progress", "--title", "Progress fixture", "--mode", "auto",
                           "--runtime", "codex", "--check", "balanced")
            self.assertTrue(initial["requiredHostActions"])
            cli = root / ".aispa" / initial["runDirectory"] / "runtime/tools/aispa.py"
            store = Journal(root)
            initial_state = store.load()
            self.assertNotIn("workProgress", initial_state)
            begun = call("progress", "begin", "discovery")["receipt"]
            same = call("progress", "begin", "discovery")["receipt"]
            self.assertEqual(begun["startedAt"], same["startedAt"])
            ended = call("progress", "end")["receipt"]
            self.assertIn("finishedAt", ended)
            self.assertNotIn("activeSince", ended)
            call("progress", "begin", "implementation", expected=2)
            call("progress", "end", "free text", expected=2)
            steps = root / ".aispa/progress-batch.json"
            steps.write_text(json.dumps([["progress", "begin", "spec"], ["progress", "end"]]), encoding="utf-8")
            self.assertTrue(call("batch", "--file", str(steps))["ok"])
            final = store.load()
            self.assertEqual(final["gates"], initial_state["gates"])
            self.assertEqual(final["tasks"], [])
            self.assertEqual(final["status"], "running")
            self.assertIn("finishedAt", final["workProgress"])
