"""Stories 68-69: agent decisions after seal are bound to current requirements."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
import aispa
from aispa_core import carryover, project_context
from aispa_core.store import Invalid
import test_workflow as helpers


class LinkedDecisionTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    git = helpers.WorkflowTests.git
    commit = helpers.WorkflowTests.commit
    file = helpers.WorkflowTests.file

    def prepare(self, linked=True):
        self.state["policy"] = {"linkedDecisions": True} if linked else {}
        self.flow.brief(self.file("Build export. Show totals. Keep data private.", "brief.md"))
        self.flow.requirements(self.file([
            {"id": "R01", "text": "Export", "core": True, "brief": "B001", "quote": "Build export.",
             "acceptance": ["Local CSV"]},
            {"id": "R02", "text": "Totals", "brief": "B001", "quote": "Show totals.", "acceptance": ["Sum shown"]},
            {"id": "G01", "text": "Privacy", "kind": "constraint", "brief": "B001",
             "quote": "Keep data private.", "acceptance": ["No network"]}]))

    def journal(self):
        return (self.store.run / "events.jsonl").read_bytes()

    def test_decision_records_its_current_requirements(self):
        self.prepare()
        self.flow.decision("CSV uses a semicolon separator", requirements=["R01", "G01"])
        note = self.state["notes"][-1]
        self.assertEqual(note["text"], "CSV uses a semicolon separator")
        self.assertEqual(note["requirements"], ["R01", "G01"])
        self.assertIn("at", note)
        self.assertEqual(self.store.load()["notes"][-1], note)

    def test_unknown_or_dropped_requirement_is_refused_without_writes(self):
        self.prepare()
        self.flow.brief(self.file("Drop totals for now.", "drop.md"))
        self.flow.scope("R02", "drop", "B002", "Drop totals for now.")
        for bad in (["R09"], ["R01", "R02"]):
            before, journal = copy.deepcopy(self.state), self.journal()
            with self.subTest(requirements=bad), self.assertRaises(Invalid) as caught:
                self.flow.decision("Some choice", requirements=bad)
            self.assertIn("R01", str(caught.exception))
            self.assertIn("G01", str(caught.exception))
            self.assertNotIn("R02,", str(caught.exception).split(":")[-1])
            self.assertEqual(self.state, before)
            self.assertEqual(self.journal(), journal)

    def test_plain_decision_keeps_the_old_form(self):
        self.prepare()
        self.flow.decision("Keep defaults")
        self.assertEqual(set(self.state["notes"][-1]), {"at", "text"})

    def test_old_policy_refuses_requirement_binding(self):
        self.prepare(linked=False)
        before = copy.deepcopy(self.state)
        with self.assertRaises(Invalid):
            self.flow.decision("Bound choice", requirements=["R01"])
        self.assertEqual(self.state, before)
        self.flow.decision("Old choice")
        self.assertEqual(set(self.state["notes"][-1]), {"at", "text"})

    def test_cli_accepts_repeated_requirement_flags(self):
        self.prepare()
        self.state["runtimeDigest"] = aispa.runtime_digest()
        self.flow.save("synthetic-runtime-fixture")
        result = subprocess.run([sys.executable, "-B", str(Path(aispa.__file__)), "--root", str(self.root), "--full",
                                 "decision", "Totals are rounded", "--requirement", "R02", "--requirement", "R01"],
                                cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.store.load()["notes"][-1]["requirements"], ["R02", "R01"])
        refused = subprocess.run([sys.executable, "-B", str(Path(aispa.__file__)), "--root", str(self.root), "--full",
                                  "decision", "Unknown", "--requirement", "R77"],
                                 cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("R77", refused.stderr)

    def test_context_and_carryover_read_both_forms(self):
        self.prepare()
        self.state["notes"].append({"at": "2026-10-01T00:00:00+00:00", "text": "Legacy note"})
        self.flow.decision("Linked note", requirements=["R01"])
        project_context.initialize(self.store, self.state, "codex")
        self.flow.save("context-initialized")
        decisions = (self.store.home / "context/decisions.md").read_text(encoding="utf-8")
        self.assertIn("Legacy note", decisions)
        self.assertIn("Linked note", decisions)
        self.assertIn('"R01"', decisions)
        self.flow.close("cancelled", "Hand over to a new run")
        payload = carryover.capture(self.store, self.state)
        notes = [row["note"] for row in payload["decisions"] if row["kind"] == "agent_note"]
        self.assertIn({"at": "2026-10-01T00:00:00+00:00", "text": "Legacy note"}, notes)
        self.assertTrue(any(row.get("requirements") == ["R01"] for row in notes))


if __name__ == "__main__":
    unittest.main()
