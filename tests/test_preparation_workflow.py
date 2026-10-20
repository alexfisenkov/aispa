import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import briefmap, preparation, participants, design_contract, timing
from aispa_core.contracts import verified, task_verified
from aispa_core.dashboard_export import archive
from aispa_core.store import Invalid
import test_workflow as helpers


class PreparationTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    git = helpers.WorkflowTests.git
    commit = helpers.WorkflowTests.commit
    file = helpers.WorkflowTests.file

    def sources(self):
        self.state["policy"] = {"sourceAccounting": True}
        self.flow.brief(self.file("Build export. Keep data private.", "brief.md"))
        self.flow.requirements(self.file([
            {"id": "R01", "text": "Export", "core": True, "brief": "B001", "quote": "Build export.",
             "acceptance": ["Local CSV"]},
            {"id": "G01", "text": "Privacy", "kind": "constraint", "brief": "B001",
             "quote": "Keep data private.", "acceptance": ["No network"]}
        ]))
        self.analyst = participants.register(self.store, self.state, "brief-analyst", "codex", "analyst", "session-a")
        self.reviewer = participants.register(self.store, self.state, "brief-reviewer", "codex", "reviewer", "session-r")
        self.flow.save("participants-recorded")

    def mapping(self):
        for kind, actor in (("brief-extract", self.analyst), ("brief-review", self.reviewer)):
            report = preparation.template(self.store, self.state, kind, actor=actor)
            for unit in report["units"]:
                unit.update(kind="intent", summary=unit["text"], reason="Explicit owner instruction")
            preparation.record(self.store, self.state, kind, report)
        report = preparation.template(self.store, self.state, "brief-map", actor=self.reviewer)
        for row, rid in zip(report["rows"], ("R01", "G01")):
            row.update(disposition="requirement", requirements=[rid], reason="Exact source accounted for")
        preparation.record(self.store, self.state, "brief-map", report)
        self.flow.save("brief-map-recorded")

    def test_seal_rejects_missing_source_read_and_new_brief_invalidates_it(self):
        self.sources()
        with self.assertRaises(Invalid):
            self.flow.seal()
        self.mapping()
        self.flow.seal()
        self.assertTrue(self.state["sealed"])
        self.flow.brief(self.file("Also support UTF-8.", "more.md"))
        with self.assertRaises(Invalid):
            self.flow.seal()

    def test_source_context_contains_neither_registry_nor_analyst_conclusions(self):
        self.sources()
        self.mapping()
        context = preparation.source_context(self.store, self.state)
        self.assertEqual(set(context), {"basis", "briefs"})
        self.assertNotIn("requirements", context)
        self.assertNotIn("briefExtraction", context)

    def test_unfilled_templates_cannot_be_saved_as_pass(self):
        self.sources()
        for kind, actor in (("brief-extract", self.analyst), ("brief-review", self.reviewer)):
            with self.assertRaises(Invalid):
                preparation.record(self.store, self.state, kind,
                                   preparation.template(self.store, self.state, kind, actor=actor))

    def test_wait_tracks_real_pause_and_cannot_be_opened_after_seal(self):
        self.sources()
        self.flow.wait("Which local CSV separator?")
        self.assertEqual(self.state["waiting"]["kind"], "owner_question")
        before = timing.elapsed(self.state, "2099-01-01T00:00:00Z")
        self.assertLess(before, 60)
        with self.assertRaises(Invalid):
            self.flow.requirements(self.file([{"id": "R03", "text": "No answer yet"}]))
        with self.assertRaises(Invalid):
            self.flow.seal()
        self.flow.answered(self.file("Use commas.", "answer.md"))
        self.assertIsNone(self.state["waiting"])
        self.assertEqual(self.store.text(self.state["briefs"][-1]), "Use commas.")
        self.assertFalse(self.state["sealed"])
        self.state["sealed"] = True  # Isolate the late-question guard.
        with self.assertRaises(Invalid):
            self.flow.wait("A late question")

    def test_truncated_zero_exit_cannot_pass_any_current_check(self):
        self.assertTrue(self.flow.verify([sys.executable, "-c", "print('ok')"], 10))
        self.state["verification"]["truncated"] = True
        self.assertFalse(verified(self.store, self.state))
        self.assertFalse(self.flow.verify([sys.executable, "-c", "print('x' * 1100000)"], 10))
        self.assertEqual(self.state["verification"]["exitCode"], 0)
        self.assertTrue(self.state["verification"]["truncated"])

    def test_dashboard_archive_escapes_script_content_and_is_private(self):
        self.state["title"] = "</script><script>alert('x')</script>"
        self.flow.save("title-set")
        result = archive(self.store, self.state)
        text = Path(result["absolutePath"]).read_text()
        self.assertTrue(result["private"])
        self.assertIn("window.AISPA_ARCHIVE=", text)
        self.assertIn('"archive": true', text)
        self.assertNotIn("fonts.googleapis.com", text)
        self.assertNotIn("fonts.gstatic.com", text)
        self.assertNotIn(self.state["title"], text)
        self.assertTrue(Path(result["absolutePath"]).is_relative_to(self.store.run))

    def test_owner_reference_requires_source_quote_and_freezes_the_read_bytes(self):
        self.sources()
        self.flow.brief(self.file("Use design.txt as the export reference.", "ref.md"))
        path = self.root / "design.txt"
        path.write_text("plain local table", encoding="utf-8")
        import hashlib
        report = {"path": "design.txt", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "brief": "B002", "quote": "Use design.txt as the export reference.",
                  "requirements": ["R01"], "use": "CSV field ordering"}
        result = preparation.owner_reference(self.store, self.state, report)
        path.write_text("changed", encoding="utf-8")
        self.assertEqual(self.store.text(result["snapshot"]), "plain local table")
        with self.assertRaises(Invalid):
            preparation.owner_reference(self.store, self.state, report)

    def test_owner_defer_preserves_original_source_and_new_decision_in_the_map(self):
        self.sources()
        self.mapping()
        self.flow.seal()
        self.flow.brief(self.file("Defer privacy until the next iteration.", "owner.md"))
        self.flow.scope("G01", "defer", "B002", "Defer privacy until the next iteration.")
        for kind, actor in (("brief-extract", self.analyst), ("brief-review", self.reviewer)):
            report = preparation.template(self.store, self.state, kind, actor=actor)
            for unit in report["units"]:
                unit.update(kind="intent", summary=unit["text"], reason="Original request or explicit owner change")
            preparation.record(self.store, self.state, kind, report)
        report = preparation.template(self.store, self.state, "brief-map", actor=self.reviewer)
        for row, rid in zip(report["rows"], ("R01", "G01", "G01")):
            row.update(disposition="requirement", requirements=[rid], reason="Source retained with owner decision")
        preparation.record(self.store, self.state, "brief-map", report)
        self.flow.seal()
        self.assertEqual(self.state["requirements"][1]["scope"], "deferred")
        self.assertTrue(self.state["sealed"])


if __name__ == "__main__":
    unittest.main()
