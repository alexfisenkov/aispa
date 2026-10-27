import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import blind, cleanroom
from aispa_core.store import Invalid, Journal


class BlindReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-blind-")
        self.root = Path(self.temp.name).resolve()
        (self.root / ".gitignore").write_text(".aispa/\n", encoding="utf-8", newline="\n")
        (self.root / "app.py").write_text("print('verified application')\n", encoding="utf-8", newline="\n")
        self.git("init", "-q")
        self.commit()
        self.store = Journal(self.root)
        self.state = self.store.create("blind", "Blind fixture")
        source = self.store.artifact("briefs", b"Build export. Keep data private.\n", ".md")
        self.state["briefs"] = [dict(source, id="B001")]
        self.state["requirements"] = [
            {"id": "R01", "text": "Export", "kind": "requested", "scope": "active", "core": True,
             "brief": "B001", "quote": "Build export.", "acceptance": ["Export works"]},
            {"id": "G01", "text": "Privacy", "kind": "constraint", "scope": "active", "core": True,
             "brief": "B001", "quote": "Keep data private.", "acceptance": ["Private data stays private"]}]
        self.state["actors"] = {"worker-actor-id": {"id": "worker-actor-id", "nativeId": "native-worker"}}
        self.state["cleanroom"] = cleanroom.prepare(self.store, self.state)
        self.state["cleanrun"] = cleanroom.execute(self.store, self.state, [sys.executable, "app.py"], 5)
        self.evidence = self.state["cleanrun"]["output"]

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True).stdout

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")

    def report(self, status="passed", quote="Build export.", brief="B001"):
        return {"basis": blind.basis(self.store, self.state), "reviewer": "native-final-acceptor", "independent": True,
                "observations": [{"brief": brief, "quote": quote, "status": status,
                                  "note": "Observed directly from original brief", "artifact": self.evidence["file"]}]}

    def final(self, requirement="R01"):
        saved = self.state["blindReview"]
        return {"reviewer": saved["reviewer"], "independent": True, "blindReport": saved["report"]["sha256"],
                "blindMapping": [{"observation": 0, "requirement": requirement}], "unmapped": [],
                "checks": [{"requirement": requirement, "criterion": "Export works", "status": "passed"}]}

    def test_no_prior_record_blocks_reconciliation(self):
        self.assertFalse(blind.valid(self.store, self.state))
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, {"reviewer": "native-final-acceptor"})

    def test_basis_uses_original_sources_and_code_not_registry_or_spec(self):
        before = blind.basis(self.store, self.state)
        self.state["requirements"].append({"id": "R99", "quote": "different registry"})
        self.state["spec"] = {"file": "different-spec", "sha256": "different"}
        self.state["tasks"].append({"id": "T99"})
        self.assertEqual(before, blind.basis(self.store, self.state))
        new = self.store.artifact("briefs", b"Add owner request", ".md")
        self.state["briefs"].append(dict(new, id="B002"))
        self.assertNotEqual(before, blind.basis(self.store, self.state))

    def test_record_seals_report_and_does_not_append_journal(self):
        history = (self.store.run / "events.jsonl").read_bytes()
        report = self.report()
        result = blind.record(self.store, self.state, report)
        self.assertTrue(blind.valid(self.store, self.state))
        self.assertEqual(history, (self.store.run / "events.jsonl").read_bytes())
        self.assertEqual(blind.mapping(self.store, self.state, self.final()), [])
        report["observations"][0]["status"] = "failed"
        result["observations"][0]["status"] = "failed"
        self.assertTrue(blind.valid(self.store, self.state))
        self.assertEqual(json.loads(self.store.text(self.state["blindReview"]["report"]))["observations"][0]["status"], "passed")

    def test_reject_task_actor_digest_and_native_identity_and_changed_final_reviewer(self):
        for reviewer in ("worker-actor-id", "native-worker", "native-worker ", ""):
            report = self.report()
            report["reviewer"] = reviewer
            with self.subTest(reviewer=reviewer), self.assertRaises(Invalid):
                blind.record(self.store, self.state, report)
        blind.record(self.store, self.state, self.report())
        final = self.final()
        final["reviewer"] = "another-reviewer"
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, final)
        self.state["actors"]["new-actor"] = {"id": "new-actor", "nativeId": "native-final-acceptor"}
        self.assertFalse(blind.valid(self.store, self.state))

    def test_all_source_briefs_exact_quotes_and_unique_observations_required(self):
        source = self.store.artifact("briefs", b"Add PDF export too.\n", ".md")
        self.state["briefs"].append(dict(source, id="B002"))
        with self.assertRaises(Invalid):
            blind.record(self.store, self.state, self.report())
        report = self.report()
        report["observations"].append(dict(report["observations"][0], brief="B002", quote="Invented quote"))
        with self.assertRaises(Invalid):
            blind.record(self.store, self.state, report)
        report["observations"][1]["quote"] = "Add PDF export too."
        report["observations"].append(copy.deepcopy(report["observations"][0]))
        with self.assertRaises(Invalid):
            blind.record(self.store, self.state, report)
        report["observations"].pop()
        blind.record(self.store, self.state, report)
        self.assertTrue(blind.valid(self.store, self.state))

    def test_passed_observation_requires_actual_nonempty_evidence(self):
        report = self.report()
        report["observations"][0]["artifact"] = "evidence/missing.txt"
        with self.assertRaises(Invalid):
            blind.record(self.store, self.state, report)
        empty = self.store.artifact("evidence", b"", ".txt")
        report["observations"][0]["artifact"] = empty["file"]
        with self.assertRaises(Invalid):
            blind.record(self.store, self.state, report)

    def test_first_observations_cannot_be_replaced_after_full_context(self):
        blind.record(self.store, self.state, self.report("failed"))
        with self.assertRaises(Invalid):
            blind.record(self.store, self.state, self.report("passed"))
        self.state["spec"] = {"file": "revealed registry"}
        with self.assertRaises(Invalid):
            blind.record(self.store, self.state, self.report("passed"))

    def test_report_evidence_and_record_tampering_invalidates(self):
        blind.record(self.store, self.state, self.report())
        saved = self.state["blindReview"]
        for artifact in (saved["report"], saved["record"], self.evidence, self.state["briefs"][0]):
            path = self.store.run / artifact["file"]
            before = path.read_bytes()
            path.write_text("tampered", encoding="utf-8", newline="\n")
            self.assertFalse(blind.valid(self.store, self.state))
            path.write_bytes(before)
            self.assertTrue(blind.valid(self.store, self.state))
        saved["observations"][0]["note"] = "silently changed"
        self.assertFalse(blind.valid(self.store, self.state))

    def test_same_candidate_reverification_cannot_replace_original_observations(self):
        blind.record(self.store, self.state, self.report())
        original = copy.deepcopy(self.state["blindReview"])
        candidate = blind.basis(self.store, self.state)
        before = (self.root / "app.py").read_text(encoding="utf-8")
        (self.root / "app.py").write_text("changed", encoding="utf-8", newline="\n")
        self.assertFalse(blind.valid(self.store, self.state))
        (self.root / "app.py").write_text(before, encoding="utf-8", newline="\n")
        self.assertTrue(blind.valid(self.store, self.state))
        self.state["cleanrun"] = cleanroom.execute(self.store, self.state, [sys.executable, "app.py"], 5)
        self.assertTrue(blind.valid(self.store, self.state))
        self.assertEqual(candidate, blind.basis(self.store, self.state))
        with self.assertRaises(Invalid):
            blind.record(self.store, self.state, self.report())
        self.state["cleanroom"] = cleanroom.prepare(self.store, self.state)
        self.state["cleanrun"] = cleanroom.execute(self.store, self.state, [sys.executable, "app.py"], 5)
        self.assertEqual(candidate, blind.basis(self.store, self.state))
        self.assertTrue(blind.valid(self.store, self.state))
        report = self.report()
        report["reviewer"] = "fresh-but-not-a-new-candidate"
        with self.assertRaises(Invalid):
            blind.record(self.store, self.state, report)
        self.assertEqual(original, self.state["blindReview"])

    def changed_candidate(self):
        (self.root / "app.py").write_text("print('new commit')\n", encoding="utf-8", newline="\n")
        self.commit()
        self.state["cleanroom"] = cleanroom.prepare(self.store, self.state)
        self.state["cleanrun"] = cleanroom.execute(self.store, self.state, [sys.executable, "app.py"], 5)

    def test_new_candidate_requires_fresh_reviewer_and_preserves_immutable_history(self):
        blind.record(self.store, self.state, self.report("failed"))
        original = copy.deepcopy(self.state["blindReview"])
        self.changed_candidate()
        self.assertFalse(blind.valid(self.store, self.state))
        with self.assertRaises(Invalid):
            blind.record(self.store, self.state, self.report())
        report = self.report()
        report["reviewer"] = "native-final-second-candidate"
        blind.record(self.store, self.state, report)
        self.assertTrue(blind.valid(self.store, self.state))
        self.assertEqual(self.state["blindHistory"], [original])
        self.assertEqual(self.state["blindReview"]["previous"], original["record"]["sha256"])
        self.assertEqual(json.loads(self.store.text(original["report"]))["observations"][0]["status"], "failed")

    def test_corrupted_or_removed_historical_records_invalidate_latest_review(self):
        blind.record(self.store, self.state, self.report())
        self.changed_candidate()
        report = self.report()
        report["reviewer"] = "native-final-second-candidate"
        blind.record(self.store, self.state, report)
        previous = self.state["blindHistory"][0]
        for artifact in (previous["report"], previous["candidate"], previous["record"]):
            path = self.store.run / artifact["file"]
            original = path.read_bytes()
            path.write_text("corrupted history", encoding="utf-8", newline="\n")
            self.assertFalse(blind.valid(self.store, self.state))
            path.write_bytes(original)
            self.assertTrue(blind.valid(self.store, self.state))
        self.state["blindHistory"] = []
        self.assertFalse(blind.valid(self.store, self.state))

    def test_mapping_cannot_omit_repeat_or_misattribute_observations(self):
        report = self.report()
        report["observations"].append(dict(report["observations"][0], quote="Keep data private."))
        blind.record(self.store, self.state, report)
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, self.final())
        final = self.final()
        final["blindMapping"].append({"observation": 0, "requirement": "G01"})
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, final)
        final["blindMapping"][1]["observation"] = 1
        self.assertEqual(blind.mapping(self.store, self.state, final), [])
        report_hash = final["blindReport"]
        final["blindReport"] = "0" * 64
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, final)
        final["blindReport"] = report_hash
        final["blindMapping"][1]["requirement"] = "R01"
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, final)

    def test_unmapped_original_request_cannot_be_dropped(self):
        blind.record(self.store, self.state, self.report())
        final = self.final(None)
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, final)
        final["unmapped"] = ["Original export request is missing in current registry"]
        failures = blind.mapping(self.store, self.state, final)
        self.assertEqual(len(failures), 1)
        self.assertIsNone(failures[0]["requirement"])
        self.assertEqual(failures[0]["status"], "blocked")
        final["blindMapping"][0]["requirement"] = "R99"
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, final)

    def test_initial_failure_survives_final_passed_rows_and_optional_addition(self):
        blind.record(self.store, self.state, self.report("failed"))
        final = self.final()
        failures = blind.mapping(self.store, self.state, final)
        self.assertEqual([(r["requirement"], r["status"]) for r in failures], [("R01", "failed")])
        self.state["requirements"].append({"id": "A01", "kind": "addition", "scope": "active", "parent": "R01"})
        final["blindMapping"][0]["requirement"] = "A01"
        self.assertEqual({r["requirement"] for r in blind.mapping(self.store, self.state, final)}, {"R01", "A01"})

    def test_historical_scope_uses_full_original_text_and_mapped_original_quote(self):
        historical = {"id": "H-" + "a" * 64, "text": "Build export with CSV. Also build unrelated dashboard.",
                      "origin": {"run": "previous", "file": "briefs/original.md", "sha256": "fixture"},
                      "applicableQuotes": ["Build export with CSV."]}
        ledger = {"items": [{"mapping": {"run": self.state["run"], "requirement": "R01",
                              "originalSource": {"source": "a" * 64, "quote": "Build export with CSV."}}}]}
        self.state["carryover"] = {"fixture": True}
        with patch.object(blind.carryover, "brief_sources", return_value=[historical]), \
             patch.object(blind.carryover, "view", return_value=ledger):
            report = self.report()
            row = dict(report["observations"][0], brief=historical["id"], quote="CSV", status="not_tested")
            report["observations"].append(row)
            blind.record(self.store, self.state, report)
            final = self.final()
            final["blindMapping"].append({"observation": 1, "requirement": "R01"})
            failures = blind.mapping(self.store, self.state, final)
            self.assertEqual([(r["requirement"], r["status"]) for r in failures], [("R01", "not_tested")])
        self.state.pop("blindReview")
        with patch.object(blind.carryover, "brief_sources", return_value=[historical]):
            report = self.report()
            report["observations"].append(dict(row, quote="unrelated dashboard"))
            with self.assertRaises(Invalid):
                blind.record(self.store, self.state, report)

    def test_old_owner_scope_decision_is_context_only_not_active_check(self):
        old = {"id": "H-" + "b" * 64, "text": "Defer export for now.",
               "origin": {"run": "previous", "file": "briefs/defer.md", "sha256": "fixture"},
               "applicableQuotes": []}
        self.state["carryover"] = {"fixture": True}
        with patch.object(blind.carryover, "brief_sources", return_value=[old]):
            report = self.report()
            blind.record(self.store, self.state, report)
            self.assertTrue(blind.valid(self.store, self.state))
            self.assertEqual(blind.mapping(self.store, self.state, self.final()), [])
            self.state.pop("blindReview")
            report["observations"].append(dict(report["observations"][0], brief=old["id"], quote="Defer export for now."))
            with self.assertRaises(Invalid):
                blind.record(self.store, self.state, report)

    def scope_fixture(self, action, decision_status="passed"):
        quote = {"drop": "Drop export.", "defer": "Defer export.", "restore": "Restore export."}[action]
        source = self.store.artifact("briefs", quote.encode(), ".md")
        self.state["briefs"].append(dict(source, id="B002"))
        decision = {"action": action, "brief": "B002", "quote": quote}
        self.state["requirements"][0].update(
            scope={"drop": "dropped", "defer": "deferred", "restore": "active"}[action], decision=decision)
        report = self.report("not_tested")
        report["observations"].append(dict(report["observations"][0], brief="B002", quote=quote, status=decision_status))
        blind.record(self.store, self.state, report)
        final = self.final()
        final["blindMapping"][0]["scopeDecision"] = copy.deepcopy(decision)
        final["blindMapping"].append({"observation": 1, "requirement": "R01", "scopeDecision": copy.deepcopy(decision)})
        return final

    def test_current_owner_defer_and_drop_allow_original_implementation_exclusion(self):
        for action in ("defer", "drop"):
            with self.subTest(action=action):
                if self.state.get("blindReview"):
                    self.state.pop("blindReview")
                    self.state["briefs"].pop()
                final = self.scope_fixture(action)
                self.assertEqual(blind.mapping(self.store, self.state, final), [])
                without = copy.deepcopy(final)
                without["blindMapping"][0].pop("scopeDecision")
                with self.assertRaises(Invalid):
                    blind.mapping(self.store, self.state, without)

    def test_scope_decision_must_match_recorded_owner_source_exactly(self):
        final = self.scope_fixture("defer")
        final["blindMapping"][0]["scopeDecision"]["quote"] = "Invented owner decision"
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, final)
        final["blindMapping"][0]["scopeDecision"] = copy.deepcopy(self.state["requirements"][0]["decision"])
        self.state["requirements"][0]["decision"]["quote"] = "Missing in original owner text"
        for row in final["blindMapping"]:
            row["scopeDecision"] = copy.deepcopy(self.state["requirements"][0]["decision"])
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, final)

    def test_failed_owner_decision_blocks_overall_even_if_original_excluded(self):
        final = self.scope_fixture("drop", decision_status="failed")
        failures = blind.mapping(self.store, self.state, final)
        self.assertEqual([(f["requirement"], f["status"]) for f in failures], [(None, "failed")])

    def test_restore_decision_cannot_hide_failed_active_original_implementation(self):
        final = self.scope_fixture("restore")
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, final)
        final["blindMapping"][0].pop("scopeDecision")
        failures = blind.mapping(self.store, self.state, final)
        self.assertEqual([(f["requirement"], f["status"]) for f in failures], [("R01", "not_tested")])


class FocusedRecheckTests(unittest.TestCase):
    """Focused (0.5.0): after a checked repair a fresh acceptor from a new clean clone
    rechecks only the diverged items; acceptance keeps earlier observations of the rest."""
    tearDown, git, commit, report = (BlindReviewTests.tearDown, BlindReviewTests.git,
                                     BlindReviewTests.commit, BlindReviewTests.report)

    def setUp(self):
        BlindReviewTests.setUp(self)
        self.state["policy"] = {"focusedReview": True, "leanControl": True, "cleanClone": True}

    def full(self, export="failed"):
        report = self.report(status=export)
        report["observations"].append({"brief": "B001", "quote": "Keep data private.", "status": "passed",
                                       "note": "Observed privacy", "artifact": self.evidence["file"]})
        return blind.record(self.store, self.state, report)

    def repaired(self, first, status="passed"):
        (self.root / "app.py").write_text("print('repaired application')\n", encoding="utf-8", newline="\n")
        self.commit()
        self.state["cleanroom"] = cleanroom.prepare(self.store, self.state)
        self.state["cleanrun"] = cleanroom.execute(self.store, self.state, [sys.executable, "app.py"], 5)
        self.evidence = self.state["cleanrun"]["output"]
        self.state.setdefault("finalRepairs", []).append(
            {"id": "F%02d" % (len(self.state["finalRepairs"]) + 1), "kind": "blind", "items": ["R01"], "status": status,
             "recheckOf": dict(first["report"]), "rounds": []})

    def recheck(self, first, reviewer="native-recheck", quote="Build export.", items=("R01",)):
        report = self.report(quote=quote)
        report.update(reviewer=reviewer, recheckOf={"file": first["report"]["file"], "sha256": first["report"]["sha256"]},
                      items=list(items))
        return report

    def test_recheck_is_fresh_limited_to_items_and_combined_for_mapping(self):
        first = self.full()
        self.repaired(first, status="open")
        with self.assertRaisesRegex(Invalid, "после проверенного ремонта"):
            blind.record(self.store, self.state, self.recheck(first))
        self.state["finalRepairs"][0]["status"] = "passed"
        from aispa_core import packets
        packet = packets.build(self.store, self.state, None, "blind")
        self.assertEqual(packet["recheck"]["items"], ["R01"])
        self.assertEqual(packet["recheck"]["quotes"], [{"brief": "B001", "quote": "Build export."}])
        self.assertNotIn("requirements", packet)
        whole = dict(self.report(), reviewer="native-other")
        with self.assertRaisesRegex(Invalid, "recheckOf и items"):
            blind.record(self.store, self.state, whole)
        with self.assertRaisesRegex(Invalid, "новый итоговый приёмщик"):
            blind.record(self.store, self.state, self.recheck(first, reviewer="native-final-acceptor"))
        with self.assertRaisesRegex(Invalid, "только расходившиеся"):
            blind.record(self.store, self.state, self.recheck(first, quote="Keep data private."))
        with self.assertRaisesRegex(Invalid, "ровно расходившиеся"):
            blind.record(self.store, self.state, self.recheck(first, items=("R01", "G01")))
        self.state["finalRepairs"][0]["worker"] = {"id": "repair-worker", "nativeId": "native-recheck"}
        with self.assertRaisesRegex(Invalid, "ремонтов"):
            blind.record(self.store, self.state, self.recheck(first))
        del self.state["finalRepairs"][0]["worker"]
        saved = blind.record(self.store, self.state, self.recheck(first))
        self.assertEqual((saved["items"], saved["repair"]), (["R01"], "F01"))
        self.assertNotEqual(saved["cleanroom"], first["cleanroom"])
        self.assertTrue(blind.valid(self.store, self.state))
        combined = blind.observations(self.store, self.state)
        self.assertEqual([(row["quote"], row["status"]) for row in combined],
                         [("Keep data private.", "passed"), ("Build export.", "passed")])
        final = {"reviewer": "native-recheck", "independent": True, "blindReport": saved["report"]["sha256"],
                 "unmapped": [], "blindMapping": [{"observation": 0, "requirement": "G01"},
                                                  {"observation": 1, "requirement": "R01"}]}
        self.assertEqual(blind.mapping(self.store, self.state, final), [])
        final["blindMapping"].pop()
        with self.assertRaises(Invalid):
            blind.mapping(self.store, self.state, final)
        with self.assertRaisesRegex(Invalid, "после проверенного ремонта"):
            blind.record(self.store, self.state, self.recheck(first, reviewer="native-third"))

    def test_divergent_items_name_failed_requirements(self):
        self.full()
        final = {"reviewer": "native-final-acceptor", "independent": True,
                 "blindReport": self.state["blindReview"]["report"]["sha256"], "unmapped": [],
                 "blindMapping": [{"observation": 0, "requirement": "R01"}, {"observation": 1, "requirement": "G01"}],
                 "checks": [{"requirement": "G01", "criterion": "Private data stays private", "status": "passed"}]}
        self.assertEqual(blind.divergent_items(self.store, self.state, final), ["R01"])
        self.assertEqual(blind.item_quotes(self.store, self.state, ["R01"]),
                         [{"requirement": "R01", "brief": "B001", "quote": "Build export."}])


if __name__ == "__main__":
    unittest.main()
