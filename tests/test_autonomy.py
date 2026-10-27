"""Briefing closes once; autonomy neither grants permissions nor changes scope."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
import aispa
from aispa_core import packets, project_context
from aispa_core.store import Invalid
import test_workflow as helpers


class AutonomyTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    git = helpers.WorkflowTests.git
    commit = helpers.WorkflowTests.commit
    file = helpers.WorkflowTests.file

    def prepare(self, mode="important", *, seal=False):
        self.state.update(mode=mode, policy={"firstBriefingClosure": True})
        self.flow.brief(self.file("Build export. Keep data private.", "brief.md"))
        self.flow.requirements(self.file([
            {"id": "R01", "text": "Export", "core": True, "brief": "B001",
             "quote": "Build export.", "acceptance": ["Local CSV"]},
            {"id": "G01", "text": "Privacy", "kind": "constraint", "brief": "B001",
             "quote": "Keep data private.", "acceptance": ["No network"]},
        ]))
        if seal:
            self.flow.seal()

    def call(self, *args, compact=False, expected=0):
        self.state["runtimeDigest"] = aispa.runtime_digest()
        self.flow.save("synthetic-runtime-fixture")
        result = subprocess.run([sys.executable, "-B", str(Path(aispa.__file__)),
                                 "--root", str(self.root), *(["--compact"] if compact else []), *args],
                                cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        self.state = self.store.load()
        self.flow.s = self.state
        self.flow.pipeline.s = self.state
        return json.loads(result.stdout) if expected == 0 else result

    def test_auto_cannot_open_an_interview_even_before_seal(self):
        self.prepare("auto")
        before = copy.deepcopy(self.state)
        with self.assertRaises(Invalid):
            self.flow.wait("Which export format?")
        self.assertEqual(self.state, before)

    def test_preseal_interview_wait_still_requires_actual_answer(self):
        self.prepare("interview")
        self.flow.wait("Which export format?")
        with self.assertRaises(Invalid):
            self.flow.seal()
        self.flow.answered(self.file("Use CSV.", "answer.md"))
        self.assertIsNone(self.state["waiting"])
        self.assertNotIn("firstBriefingClosedAt", self.state)

    def test_new_brief_cannot_reopen_questions_after_first_seal(self):
        self.prepare(seal=True)
        closed = self.state["firstBriefingClosedAt"]
        requirements, core = copy.deepcopy(self.state["requirements"]), self.state["core"][:]
        self.flow.brief(self.file("Also support UTF-8.", "later.md"))
        self.assertFalse(self.state["sealed"])
        with self.assertRaises(Invalid):
            self.flow.wait("A new question after briefing")
        with self.assertRaises(Invalid):
            self.flow.spec(self.file("Stale specification", "spec.md"))
        self.assertEqual(self.state["firstBriefingClosedAt"], closed)
        self.assertEqual(self.state["requirements"], requirements)
        self.assertEqual(self.state["core"], core)

    def test_requirements_refresh_and_reseal_do_not_reset_closure(self):
        self.prepare(seal=True)
        closed = self.state["firstBriefingClosedAt"]
        self.flow.brief(self.file("Use UTF-8.", "later.md"))
        self.flow.requirements(self.file([{"id": "R02", "text": "Encoding", "brief": "B002",
                                          "quote": "Use UTF-8.", "acceptance": ["UTF-8 CSV"]}]))
        with self.assertRaises(Invalid):
            self.flow.wait("Reopened by new requirements")
        self.assertFalse(self.flow.seal())
        self.assertEqual(self.state["firstBriefingClosedAt"], closed)

    def test_resume_and_owner_check_mode_change_preserve_closure(self):
        self.prepare(seal=True)
        closed = self.state["firstBriefingClosedAt"]
        self.flow.close("paused", "Synthetic host pause")
        self.flow.resume()
        self.state["policy"]["leanControl"] = True
        self.state["checkMode"] = "balanced"
        self.flow.check_mode("fast", self.file("Use fast checking.", "mode.md"))
        self.assertEqual(self.state["firstBriefingClosedAt"], closed)
        with self.assertRaises(Invalid):
            self.flow.wait("Reopened after resume or mode switch")

    def test_legacy_run_keeps_its_original_wait_behavior(self):
        self.prepare("auto")
        self.state["policy"].pop("firstBriefingClosure")
        self.flow.seal()
        self.assertNotIn("firstBriefingClosedAt", self.state)
        self.flow.brief(self.file("Legacy supplement.", "legacy.md"))
        self.flow.wait("Legacy question")
        self.assertEqual(self.state["waiting"]["kind"], "owner_question")
        packet = packets.build(self.store, self.state, self.flow.pipeline, "coverage")
        self.assertNotIn("autonomy", packet)

    def test_failed_seal_does_not_close_briefing_or_notify(self):
        self.state.update(mode="interview", policy={"firstBriefingClosure": True})
        self.call("seal", expected=2)
        self.assertNotIn("firstBriefingClosedAt", self.state)
        self.assertNotIn("requiredHostActions", self.call("status"))

    def test_batch_seal_is_rejected_before_earlier_mutation(self):
        self.prepare()
        self.state["runtimeDigest"] = aispa.runtime_digest()
        self.flow.save("synthetic-runtime-fixture")
        steps = self.file([["decision", "Must never be recorded"], ["seal"]], "batch.json")
        journal = self.store.run / "events.jsonl"
        before = journal.read_bytes()
        result = subprocess.run([sys.executable, "-B", str(Path(aispa.__file__)), "--root", str(self.root),
                                 "batch", "--file", str(steps)], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("шаг недопустим в пакете: seal", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(journal.read_bytes(), before)

    def test_explicit_new_init_does_not_inherit_closed_briefing_or_scope(self):
        self.prepare(seal=True)
        self.flow.close("cancelled", "Synthetic previous run finished")
        result = self.call("init", "--slug", "next", "--title", "Next owner request",
                           "--mode", "interview", "--runtime", "codex", "--check", "balanced")
        self.assertTrue(self.state["policy"]["firstBriefingClosure"])
        self.assertIsNone(result["autonomy"]["briefingClosedAt"])
        self.assertTrue(result["autonomy"]["mayAsk"])
        self.assertEqual(self.state["briefs"], [])
        self.assertEqual(self.state["requirements"], [])
        self.assertEqual(self.state["core"], [])
        # Story 82: init only starts the dashboard; it is opened once from the answer of `dashboard start`.
        self.assertEqual(result["requiredHostActions"][0]["action"], "start-dashboard")

    def test_first_seal_notifies_once_and_compact_status_retains_contract(self):
        self.prepare()
        result = self.call("seal", compact=True)
        action, = result["requiredHostActions"]
        self.assertEqual(action["action"], "notify-autonomous-start")
        for text in ("Бриф готов.", "работаю самостоятельно", "можете отойти", "финале"):
            self.assertIn(text, action["message"])
        self.assertNotIn("delivered", action)
        contract = result["autonomy"]
        self.assertEqual(contract["mode"], "important")
        self.assertFalse(contract["mayAsk"])
        self.assertEqual(contract["briefingClosedAt"], self.state["firstBriefingClosedAt"])
        saved = json.loads(Path(result["absolutePath"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["autonomy"], contract)
        self.assertEqual(saved["requiredHostActions"], result["requiredHostActions"])
        for boundary in ("existingPermissionsOnly", "hostApprovalRulesUnchanged", "scopeAndRequirementsUnchanged"):
            self.assertIs(contract["authority"][boundary], True)
        for response in (self.call("seal"), self.call("status"), self.call("status", compact=True)):
            self.assertNotIn("requiredHostActions", response)
            self.assertEqual(response["autonomy"], contract)
        self.flow.brief(self.file("Owner supplement.", "supplement.md"))
        self.assertNotIn("requiredHostActions", self.call("seal"))

    def test_context_cards_and_memory_keep_the_same_autonomy_contract(self):
        self.prepare(seal=True)
        self.flow.spec(self.file("Local CSV. No network.", "spec.md"))
        report = {"basis": aispa.basis(self.store, self.state, "coverage"), "reviewer": "synthetic",
                  "independent": True, "unmapped": [], "checks": [
                      {"requirement": "R01", "status": "covered", "specQuote": "Local CSV"},
                      {"requirement": "G01", "status": "covered", "specQuote": "No network"}]}
        self.flow.coverage(self.file(report))
        self.flow.plan(self.file([{"id": "T01", "title": "Export", "requirements": ["R01", "G01"],
                                   "dependsOn": [], "zones": ["app.py"], "criteria": ["Local CSV", "No network"]}]))
        packet = packets.build(self.store, self.state, self.flow.pipeline, "task", "T01", "worker")
        contract = packet["autonomy"]
        self.assertFalse(contract["mayAsk"])
        for card in self.state["taskHistory"]["T01"][-1]["roles"].values():
            self.assertEqual(json.loads(self.store.text(card))["autonomy"], contract)
        packet["briefs"] = ["Large original " * 10000]
        envelope = json.loads(Path(packets.write(self.store, packet)["absolutePath"]).read_text(encoding="utf-8"))
        self.assertEqual(envelope["autonomy"], contract)
        project_context.initialize(self.store, self.state, "codex")
        project_context.refresh(self.store, self.state)
        for name in ("memory.md", "progress.md"):
            text = (self.store.home / "context" / name).read_text(encoding="utf-8")
            self.assertIn('"mayAsk": false', text)
            self.assertIn(self.state["firstBriefingClosedAt"], text)


class InterviewBudgetTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    git = helpers.WorkflowTests.git
    commit = helpers.WorkflowTests.commit
    file = helpers.WorkflowTests.file
    prepare = AutonomyTests.prepare

    def budget(self, minutes=15, started_minutes_ago=30):
        from datetime import datetime, timedelta, timezone
        self.prepare("interview")
        self.state["policy"]["interviewBudget"] = True
        self.state["interview"] = {"scope": "standard", "durationMinutes": minutes}
        self.state["createdAt"] = (datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)).isoformat()

    def test_view_reports_minutes_elapsed_and_exceeded_before_seal(self):
        from aispa_core import autonomy
        self.budget(15, 30)
        value = autonomy.view(self.state)["interviewBudget"]
        self.assertEqual(value["minutes"], 15)
        self.assertGreaterEqual(value["elapsedMinutes"], 30)
        self.assertLess(value["elapsedMinutes"], 31)
        self.assertTrue(value["exceeded"])
        from datetime import datetime, timezone
        self.state["createdAt"] = datetime.now(timezone.utc).isoformat()
        self.assertFalse(autonomy.view(self.state)["interviewBudget"]["exceeded"])

    def test_budget_stops_growing_after_seal(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        from aispa_core import autonomy
        self.budget(15, 5)
        self.flow.seal()
        frozen = autonomy.view(self.state)["interviewBudget"]
        self.assertFalse(frozen["exceeded"])
        later = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        with patch.object(autonomy, "timestamp", return_value=later):
            self.assertEqual(autonomy.view(self.state)["interviewBudget"], frozen)

    def test_runs_without_the_flag_have_no_budget_field(self):
        from aispa_core import autonomy
        self.prepare("interview")
        self.assertNotIn("interviewBudget", autonomy.view(self.state))

    def test_start_message_says_when_the_person_is_needed(self):
        from aispa_core import autonomy
        self.prepare(seal=True)
        message = autonomy.start_request(self.state)[0]["message"]
        for text in ("Бриф готов.", "можете отойти", "доступа или полномочия", "блокер", "продолжай"):
            self.assertIn(text, message)


if __name__ == "__main__":
    unittest.main()
