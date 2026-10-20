import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import briefmap
from aispa_core.store import Invalid, Journal


class BriefMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-briefmap-")
        self.store = Journal(self.temp.name)
        self.state = self.store.create("map", "Source mapping")
        self.add_brief("Build export. Keep data private.\n")
        self.state["requirements"] = [
            {"id": "R01", "kind": "requested", "scope": "active", "brief": "B001",
             "quote": "Build export.", "text": "Build export", "acceptance": ["Export works"]},
            {"id": "G01", "kind": "constraint", "scope": "active", "brief": "B001",
             "quote": "Keep data private.", "text": "Keep data private", "acceptance": ["No disclosure"]},
        ]
        self.roles = {"analyst-1": "brief-analyst", "reviewer-2": "brief-reviewer"}

    def tearDown(self):
        self.temp.cleanup()

    def add_brief(self, text):
        ref = self.store.artifact("briefs", text.encode("utf-8"), ".md")
        ref["id"] = "B%03d" % (len(self.state["briefs"]) + 1)
        self.state["briefs"].append(ref)

    def actor_check(self, state, actor, role):
        return self.roles.get(actor) == role

    def source_report(self, actor, *, context=None):
        context = context or briefmap.source_context(self.store, self.state)
        return {"basis": context["basis"], "actor": actor, "units": [
            {**unit, "kind": "intent", "summary": "Explicit owner request", "reason": ""}
            for brief in context["briefs"] for unit in brief["units"]]}

    def reconcile_report(self, *, ids=None):
        ids = ids or ["R01", "G01"]
        units = briefmap.source_context(self.store, self.state)["briefs"][0]["units"]
        return {"basis": briefmap.reconciliation_basis(self.state), "actor": "reviewer-2",
                "rows": [{"brief": unit["brief"], "start": unit["start"], "end": unit["end"],
                          "disposition": "requirement", "requirements": [ids[i]],
                          "reason": "Maps the complete original request."}
                         for i, unit in enumerate(units)]}

    def prepare(self):
        analyst = self.source_report("analyst-1")
        briefmap.record_extraction(self.store, self.state, analyst, self.actor_check)
        reviewer = self.source_report("reviewer-2")
        briefmap.record_review(self.store, self.state, reviewer, self.actor_check)
        return analyst, reviewer

    def test_two_requests_require_independent_complete_source_and_mapping(self):
        original = self.store.text(self.state["briefs"][0])
        analyst, reviewer = self.prepare()
        # Exact Unicode ranges make the privacy sentence a separate source unit.
        self.assertEqual([u["text"] for u in analyst["units"]], ["Build export.", "Keep data private."])
        self.assertEqual([u["text"] for u in reviewer["units"]], ["Build export.", "Keep data private."])
        receipt = briefmap.record_reconciliation(self.store, self.state, self.reconcile_report(), self.actor_check)
        self.assertTrue(briefmap.valid(self.store, self.state, self.actor_check))
        self.assertEqual(briefmap.record_reconciliation(self.store, self.state, self.reconcile_report(), self.actor_check), receipt)
        self.assertEqual(len(self.state["briefMapHistory"]), 1)
        self.assertEqual(original, self.store.text(self.state["briefs"][0]))
        self.store.append(self.state, "mapped")
        self.assertTrue(briefmap.valid(self.store, self.store.load(), self.actor_check))

    def test_omitted_second_sentence_fails_before_seal_even_if_registered_privacy_is_missing(self):
        report = self.source_report("analyst-1")
        report["units"].pop()
        before = copy.deepcopy(self.state)
        with self.assertRaises(Invalid):
            briefmap.record_extraction(self.store, self.state, report, self.actor_check)
        self.assertEqual(self.state, before)
        self.prepare()
        self.state["requirements"] = self.state["requirements"][:1]
        with self.assertRaises(Invalid):
            briefmap.record_reconciliation(self.store, self.state,
                                           self.reconcile_report(ids=["R01", "R01"]), self.actor_check)
        self.assertFalse(briefmap.valid(self.store, self.state, self.actor_check))

    def test_reviewer_gets_original_only_and_must_be_distinct(self):
        analyst = self.source_report("analyst-1")
        briefmap.record_extraction(self.store, self.state, analyst, self.actor_check)
        context = briefmap.source_context(self.store, self.state)
        self.assertNotIn("analyst-1", json.dumps(context))
        bad = self.source_report("analyst-1")
        with self.assertRaises(Invalid):
            briefmap.record_review(self.store, self.state, bad, self.actor_check)
        self.roles["analyst-1"] = "brief-reviewer"
        with self.assertRaises(Invalid):
            briefmap.record_review(self.store, self.state, bad, self.actor_check)

    def test_disagreement_is_saved_but_not_valid(self):
        analyst = self.source_report("analyst-1")
        briefmap.record_extraction(self.store, self.state, analyst, self.actor_check)
        reviewer = self.source_report("reviewer-2")
        reviewer["units"][1].update(kind="context", reason="Reviewer considers this background.")
        briefmap.record_review(self.store, self.state, reviewer, self.actor_check)
        report = self.reconcile_report()
        report["rows"][1].update(disposition="disagreement", requirements=[], reason="Privacy meaning disputed.")
        briefmap.record_reconciliation(self.store, self.state, report, self.actor_check)
        self.assertFalse(briefmap.valid(self.store, self.state, self.actor_check))
        report["rows"][1]["disposition"] = "context"
        with self.assertRaises(Invalid):
            briefmap.record_reconciliation(self.store, self.state, report, self.actor_check)

    def test_one_sentence_may_map_multiple_original_requirements(self):
        self.state["briefs"][0] = {**self.store.artifact("briefs", b"Build export.\n", ".md"), "id": "B001"}
        self.state["requirements"][1].update(brief="B001", quote="Build export.")
        self.prepare()
        report = self.reconcile_report()
        report["rows"][0]["requirements"] = ["R01", "G01"]
        briefmap.record_reconciliation(self.store, self.state, report, self.actor_check)
        self.assertTrue(briefmap.valid(self.store, self.state, self.actor_check))

    def test_new_brief_invalidates_all_stages_and_requirements_change_only_mapping(self):
        self.prepare()
        briefmap.record_reconciliation(self.store, self.state, self.reconcile_report(), self.actor_check)
        self.state["requirements"][0]["text"] = "Build an export action"
        self.assertFalse(briefmap.valid(self.store, self.state, self.actor_check))
        self.assertEqual(briefmap.source_basis(self.state), self.state["briefExtraction"]["basis"])
        self.add_brief("No cloud upload.\n")
        self.assertNotEqual(briefmap.source_basis(self.state), self.state["briefExtraction"]["basis"])
        self.assertFalse(briefmap.valid(self.store, self.state, self.actor_check))

    def test_unicode_offsets_and_context_reason(self):
        self.add_brief("Привет 👋.\nКонтекст.\n")
        report = self.source_report("analyst-1")
        extra = [u for u in report["units"] if u["brief"] == "B002"]
        self.assertEqual(extra[0]["text"], "Привет 👋.")
        self.assertEqual(extra[0]["start"], 0)
        self.assertEqual(extra[0]["end"], len("Привет 👋."))
        extra[1]["kind"] = "context"
        with self.assertRaises(Invalid):
            briefmap.record_extraction(self.store, self.state, report, self.actor_check)
        extra[1]["reason"] = "Project background, no requested change."
        briefmap.record_extraction(self.store, self.state, report, self.actor_check)


if __name__ == "__main__":
    unittest.main()
