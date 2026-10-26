import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import discovery, ideas, product
from aispa_core.store import Invalid, Journal


def synthetic_report(store, state, *, depth="compact"):
    """Explicit test fixture, not evidence of semantic or commercial analysis."""
    return {
        "basis": ideas.basis(state), "depth": depth,
        "depthReason": "Synthetic fixture selects a bounded examination for a small CLI.",
        "summary": "Synthetic study of the requested local study-plan CLI; no claim about market demand.",
        "anchors": [{"brief": ref["id"], "sha256": ref["sha256"],
                     "quote": store.text(ref).splitlines()[0]} for ref in state["briefs"]],
        "coverage": [{"requirement": req["id"],
                      "role": {"requested": "outcome", "constraint": "constraint", "addition": "addition"}[req["kind"]],
                      "proposal": "Keep the exact acceptance conditions of " + req["id"] + " in the selected solution."}
                     for req in state["requirements"] if req.get("scope", "active") == "active"],
        "areas": [{"area": area, "status": "assessed" if area in ideas.CORE_AREAS else "unknown",
                   "text": {"problem": "The user needs a repeatable way to distribute lessons without overlap; actual demand remains untested.",
                            "user": "The brief names a local CLI, so the initial scenario assumes one person with local files.",
                            "outcome": "Produce a readable non-overlapping plan and explain invalid input without losing lessons.",
                            "alternatives": "Compare a local CLI with a spreadsheet: both can produce the requested plan; only the CLI is requested.",
                            "small_test": "Run the CLI on three synthetic lessons; compare the schedule with the expected no-overlap plan."}
                   .get(area, "This synthetic fixture does not establish " + area + "; the agent must investigate it in a real run.")}
                  for area in ideas.AREAS],
        "assumptions": [{"text": "The user can supply lesson durations.",
                         "validation": "Try the smallest example with missing and present durations.",
                         "consequence": "If absent, preserve input validation and report the missing data."}],
        "additions": [], "sourceObservations": [],
        **({"challenge": [{"probe": probe, "status": "assumption",
             "finding": "Synthetic test assumption for " + probe + "; no market evidence.",
             "decision": "Preserve requested local behavior; do not add services based on this assumption.",
             "validation": "Review actual usage with the owner before drawing product conclusions.",
             "requirements": [req["id"] for req in state["requirements"] if req.get("scope", "active") == "active"]}
            for probe in product.QUESTIONS]} if state.get("policy", {}).get("productChallenge") else {}),
    }


def record_synthetic_ideas(store, state):
    """Caller owns the journal event; this is never a production fallback."""
    return ideas.record(store, state, synthetic_report(store, state))


class IdeaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-ideas-")
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.write(".gitignore", ".aispa/\n")
        self.write("README.md", "# Study-plan fixture\n\nThis CLI reads lesson durations locally.\n")
        self.store = Journal(self.root)
        self.state = self.store.create("idea-fixture", "Synthetic idea fixture")
        self.state["briefs"] = [{**self.store.artifact("briefs", b"Build a local study-plan CLI.\nKeep inputs on this computer.\n", ".md"), "id": "B001"}]
        self.state["requirements"] = [
            {"id": "R01", "kind": "requested", "text": "Build the CLI", "brief": "B001",
             "quote": "Build a local study-plan CLI.", "core": True, "scope": "active",
             "acceptance": ["The generated plan does not overlap lessons."]},
            {"id": "G01", "kind": "constraint", "text": "Keep data local", "brief": "B001",
             "quote": "Keep inputs on this computer.", "core": True, "scope": "active",
             "acceptance": ["No network requests are made."]},
        ]

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def report(self, **kwargs):
        return synthetic_report(self.store, self.state, **kwargs)

    def rejected(self, report):
        before = copy.deepcopy(self.state)
        with self.assertRaises(Invalid):
            ideas.record(self.store, self.state, report)
        self.assertEqual(before, self.state)

    def test_source_bound_reusable_record_preserves_scope_and_owner_bytes(self):
        owner = self.store.text(self.state["briefs"][0])
        baseline = copy.deepcopy(self.state)
        journal = (self.store.run / "events.jsonl").read_bytes()
        report = self.report()
        report["additions"] = [{"parent": "R01", "text": "Offer a calendar export later.",
                                "rationale": "An optional way to reuse the same plan; not part of the original request."}]
        receipt = ideas.record(self.store, self.state, report)
        self.assertTrue(ideas.valid(self.store, self.state))
        self.assertEqual(ideas.record(self.store, self.state, report), receipt)
        self.assertEqual(len(self.state["ideaHistory"]), 1)
        self.assertEqual(len(list((self.store.run / "ideas").iterdir())), 1)
        self.assertEqual(journal, (self.store.run / "events.jsonl").read_bytes())
        self.assertEqual({k: v for k, v in self.state.items() if k not in {"ideaReview", "ideaHistory"}}, baseline)
        self.assertEqual(self.store.text(self.state["briefs"][0]), owner)
        self.store.append(self.state, "idea-reviewed")
        self.assertTrue(ideas.valid(self.store, self.store.load()))

    def test_missing_duplicate_unknown_areas_and_unexplained_status_rejected(self):
        report = self.report()
        report["areas"].pop()
        self.rejected(report)
        report = self.report()
        report["areas"][-1] = copy.deepcopy(report["areas"][0])
        self.rejected(report)
        report = self.report()
        report["areas"][0]["area"] = "market_success"
        self.rejected(report)
        report = self.report()
        report["areas"][0].update(status="not_applicable", text=" ")
        self.rejected(report)

    def test_missing_requirement_wrong_role_and_scope_decisions_rejected(self):
        report = self.report()
        report["coverage"].pop()
        self.rejected(report)
        report = self.report()
        report["coverage"][0]["role"] = "addition"
        self.rejected(report)
        report = self.report()
        report["coverage"][0]["scope"] = "deferred"
        self.rejected(report)
        report = self.report()
        report["additions"] = [{"parent": "R99", "text": "New function", "rationale": "No original requirement supports it."}]
        self.rejected(report)

    def test_bad_or_missing_quote_and_brief_hash_rejected(self):
        for field, value in (("quote", "Build a cloud service."), ("sha256", "0" * 64), ("brief", "B999")):
            report = self.report()
            report["anchors"][0][field] = value
            self.rejected(report)
        self.state["briefs"].append({**self.store.artifact("briefs", b"Also support Windows.\n", ".md"), "id": "B002"})
        report = self.report()
        report["anchors"].pop()
        self.rejected(report)

    def test_both_depths_require_examined_core_areas(self):
        for depth in ("compact", "deep"):
            report = self.report(depth=depth)
            report["assumptions"] = []
            for row in report["areas"]:
                row["status"] = "unknown"
            self.rejected(report)
            for area in ideas.CORE_AREAS:
                for status in ("unknown", "not_applicable"):
                    report = self.report(depth=depth)
                    next(row for row in report["areas"] if row["area"] == area)["status"] = status
                    self.rejected(report)
        self.assertTrue(ideas.record(self.store, self.state, self.report(depth="deep")))

    def test_seal_is_not_change_of_input_but_new_requirements_and_briefs_are(self):
        record_synthetic_ideas(self.store, self.state)
        self.state.update(sealed=True, core=["G01", "R01"], stage="spec")
        self.assertTrue(ideas.valid(self.store, self.state))
        self.state["requirements"][0]["text"] = "Changed intent"
        self.assertFalse(ideas.valid(self.store, self.state))
        record_synthetic_ideas(self.store, self.state)
        self.state["briefs"].append({**self.store.artifact("briefs", b"Also support Windows.\n", ".md"), "id": "B002"})
        self.assertFalse(ideas.valid(self.store, self.state))
        stale = ideas.view(self.store, self.state)
        self.assertTrue(stale["recorded"])
        self.assertFalse(stale["fresh"])
        self.assertNotIn("summary", stale)

    def test_new_study_appends_artifact_and_history_without_rewriting_prior(self):
        first = record_synthetic_ideas(self.store, self.state)
        first_bytes = (self.store.run / first["file"]).read_bytes()
        report = self.report()
        report["summary"] = "A revised approach with the same unmodified owner requirements."
        second = ideas.record(self.store, self.state, report)
        self.assertNotEqual(first, second)
        self.assertEqual(self.state["ideaHistory"], [first, second])
        self.assertEqual(first_bytes, (self.store.run / first["file"]).read_bytes())
        self.state["ideaReview"] = first
        self.assertFalse(ideas.valid(self.store, self.state))

    def test_tampered_study_and_owner_source_fail_closed(self):
        receipt = record_synthetic_ideas(self.store, self.state)
        path = self.store.run / receipt["file"]
        original = path.read_bytes()
        path.write_text("{}", encoding="utf-8")
        self.assertFalse(ideas.valid(self.store, self.state))
        path.write_bytes(original)
        self.assertTrue(ideas.valid(self.store, self.state))
        report = self.report()
        (self.store.run / self.state["briefs"][0]["file"]).write_text("Changed owner's words", encoding="utf-8")
        self.assertFalse(ideas.valid(self.store, self.state))
        self.rejected(report)

    def test_malformed_saved_report_and_path_never_escape_validation(self):
        receipt = record_synthetic_ideas(self.store, self.state)
        path = self.store.run / receipt["file"]
        saved = json.loads(path.read_text(encoding="utf-8"))
        for malformed in (None, [], {"basis": receipt["basis"]}):
            raw = json.dumps(dict(saved, report=malformed)).encode()
            path.write_bytes(raw)
            self.state["ideaReview"]["sha256"] = hashlib.sha256(raw).hexdigest()
            self.state["ideaHistory"][-1] = dict(self.state["ideaReview"])
            self.assertFalse(ideas.valid(self.store, self.state))
            self.assertFalse(ideas.view(self.store, self.state)["fresh"])
        self.state["ideaReview"]["file"] = "ideas/../../elsewhere.json"
        self.assertFalse(ideas.valid(self.store, self.state))

    def test_source_observations_bind_collected_quotes_and_current_file_hash(self):
        collection = discovery.collect(self.store, self.state)
        source = next(row for row in collection["sources"] if row["path"] == "README.md")
        report = self.report()
        report["collection"] = copy.deepcopy(self.state["discoveryCollection"])
        report["sourceObservations"] = [{"text": "The collected README describes local input; this is a source observation, not runtime proof.",
                                          "sources": [{"path": source["path"], "sha256": source["sha256"],
                                                       "quote": "This CLI reads lesson durations locally."}]}]
        bad = copy.deepcopy(report)
        bad["sourceObservations"][0]["sources"][0]["quote"] = "Commercial success is verified."
        self.rejected(bad)
        ideas.record(self.store, self.state, report)
        self.assertTrue(ideas.valid(self.store, self.state))
        self.write("README.md", "Changed project evidence.\n")
        self.assertFalse(ideas.valid(self.store, self.state))
        self.assertTrue(ideas.valid(self.store, self.state, check_sources=False))
        self.rejected(report)

    def test_source_observation_collection_tamper_and_ignored_sources_fail(self):
        collection = discovery.collect(self.store, self.state)
        source = next(row for row in collection["sources"] if row["path"] == "README.md")
        report = self.report()
        report["collection"] = copy.deepcopy(self.state["discoveryCollection"])
        report["sourceObservations"] = [{"text": "README documents local input.", "sources": [
            {"path": "README.md", "sha256": source["sha256"], "quote": "This CLI reads lesson durations locally."}]}]
        ideas.record(self.store, self.state, report)
        self.write(".gitignore", ".aispa/\nREADME.md\n")
        self.assertFalse(ideas.valid(self.store, self.state))
        self.write(".gitignore", ".aispa/\n")
        (self.store.run / report["collection"]["file"]).write_text("{}", encoding="utf-8")
        self.assertFalse(ideas.valid(self.store, self.state))

    def test_unknown_fields_optional_content_and_total_budget_fail_closed(self):
        report = self.report()
        report["verifiedMarketFacts"] = ["Demand is proven"]
        self.rejected(report)
        report = self.report()
        report["assumptions"][0]["validation"] = ""
        self.rejected(report)
        report = self.report(depth="deep")
        report["assumptions"] = [{"text": "x" * 1100, "validation": "y" * 1100, "consequence": "z" * 1100}] * 20
        self.rejected(report)
        report = self.report()
        report["areas"][0]["text"] = "x" * (ideas.LIMITS["compact"] + 1)
        self.rejected(report)

    def test_compact_view_bounded_and_explicit_about_omissions(self):
        report = self.report(depth="deep")
        report["assumptions"] = [{"text": "x" * 500, "validation": "y" * 500, "consequence": "z" * 500}] * 15
        record = ideas.record(self.store, self.state, report)
        before = copy.deepcopy(self.state)
        view = ideas.view(self.store, self.state)
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), ideas.MAX_VIEW_CHARS)
        self.assertGreater(view["omitted"]["assumptions"], 0)
        self.assertEqual(view["artifact"], record)
        self.assertEqual(view["assessment"], "structure-and-source-checks-only")
        self.assertEqual(ideas.view(self.store, self.state, compact=False)["report"], report)
        self.assertEqual(before, self.state)


if __name__ == "__main__":
    unittest.main()
