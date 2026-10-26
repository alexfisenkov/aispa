import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import design_contract as design
from aispa_core.contracts import add_requirements
from aispa_core.store import Invalid, Journal


class DesignContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-design-")
        self.root = Path(self.temp.name)
        self.store = Journal(self.root)
        self.state = self.store.create("design", "Feature design")
        self.state["briefs"] = [{**self.store.artifact("briefs", b"Build export. Keep data private.\n", ".md"),
                                 "id": "B001"}]
        self.state["requirements"] = [
            {"id": "R01", "kind": "requested", "scope": "active", "brief": "B001",
             "quote": "Build export.", "text": "Build export", "acceptance": ["Export works"]},
            {"id": "G01", "kind": "constraint", "scope": "active", "brief": "B001",
             "quote": "Keep data private.", "text": "Keep private", "acceptance": ["No disclosure"]},
        ]
        self.state["spec"] = self.store.artifact("specs", b"The export action writes a local CSV file.\n", ".md")
        self.reference = self.root / "README.md"
        self.reference.write_text("Existing local CSV adapter.\n", encoding="utf-8", newline="\n")

    def tearDown(self):
        self.temp.cleanup()

    def feature(self, depth="balanced"):
        return {"basis": design.scenario_basis(self.state), "depth": depth,
                "depthReason": "The owner requests a complete user flow before implementation.",
                "features": [{"requirement": "R01", "changeKind": "requested",
                              "sourceQuote": "Build export.",
                              "specQuote": "The export action writes a local CSV file.",
                              "dimensions": [
                                  {"name": name, "status": "covered", "scenario": name + " user path",
                                   "verification": "Run the " + name + " case with local CSV input.",
                                   "counterexample": "Try malformed " + name + " input." if depth == "deep" else ""}
                                  for name in design.DIMENSIONS]}]}

    def interfaces(self):
        return {"basis": design.interface_basis(self.state), "interfaces": [
            {"name": "csv.export", "signature": "export_csv(rows: list[dict], path: Path) -> Path",
             "ownerZone": "src/export.py", "consumers": ["src/cli.py"],
             "testSeam": "Call export_csv with an in-memory row list and a temp path.",
             "requirements": ["R01", "G01"],
             "references": [{"file": "README.md", "sha256": hashlib.sha256(self.reference.read_bytes()).hexdigest()}],
             "referenceReason": "Existing adapter in the repository explains local CSV behavior."}]}

    def registered_feature(self, quote, addition=False):
        source = quote + "\nUnregistered sentence.\n"
        self.state["briefs"] = [{**self.store.artifact("briefs", source.encode("utf-8"), ".md"),
                                 "id": "B001"}]
        self.state["requirements"] = []
        items = [{"id": "R01", "text": "Build export", "brief": "B001", "quote": quote,
                  "acceptance": ["Export works"]}]
        if addition:
            items.append({"id": "A01", "kind": "addition", "parent": "R01",
                          "text": "Add a preview", "acceptance": ["Preview works"]})
        self.state["requirements"] = add_requirements(self.store, self.state, items)
        report = self.feature()
        report["features"][0]["sourceQuote"] = quote
        if addition:
            extra = copy.deepcopy(report["features"][0])
            extra.update(requirement="A01", changeKind="addition")
            report["features"].append(extra)
        return report

    def tasks(self, depends=True):
        return [
            {"id": "T01", "zones": ["src/export.py"], "readZones": [],
             "requirements": ["R01", "G01"], "dependsOn": []},
            {"id": "T02", "zones": ["src/cli.py"], "readZones": [],
             "requirements": ["R01"], "dependsOn": ["T01"] if depends else []},
        ]

    def test_scenarios_cover_every_dimension_and_bind_source_and_spec(self):
        report = self.feature()
        receipt = design.record_scenarios(self.store, self.state, report)
        self.assertTrue(design.valid_scenarios(self.store, self.state))
        self.assertEqual(design.record_scenarios(self.store, self.state, report), receipt)
        self.assertEqual(len(self.state["featureScenarioHistory"]), 1)
        self.store.append(self.state, "scenario-reviewed")
        self.assertTrue(design.valid_scenarios(self.store, self.store.load()))
        self.state["spec"] = self.store.artifact("specs", b"New specification.\n", ".md")
        self.assertFalse(design.valid_scenarios(self.store, self.state))

    def test_missing_dimension_bad_na_and_source_quote_fail(self):
        report = self.feature()
        report["features"][0]["dimensions"].pop()
        with self.assertRaises(Invalid):
            design.record_scenarios(self.store, self.state, report)
        report = self.feature()
        report["features"][0]["dimensions"][2].update(status="not_applicable", scenario=" ")
        with self.assertRaises(Invalid):
            design.record_scenarios(self.store, self.state, report)
        report = self.feature()
        report["features"][0]["sourceQuote"] = "Keep data private."
        with self.assertRaises(Invalid):
            design.record_scenarios(self.store, self.state, report)

    def test_registered_unicode_quotes_at_and_above_1600_round_trip(self):
        for length in (1600, 1601, 4096):
            for addition in (False, True):
                with self.subTest(length=length, addition=addition):
                    quote = ("Экспорт 🦊 e\u0301.\n" * length)[:length]
                    report = self.registered_feature(quote, addition)
                    receipt = design.record_scenarios(self.store, self.state, report)
                    self.assertTrue(design.valid_scenarios(self.store, self.state))
                    saved = json.loads(self.store.text(receipt))["report"]
                    self.assertEqual([row["sourceQuote"] for row in saved["features"]],
                                     [quote] * (2 if addition else 1))

    def test_long_quotes_reject_alteration_truncation_and_unregistered_source_text(self):
        quote = "Дословная цитата 🦊. " * 100
        for addition in (False, True):
            report = self.registered_feature(quote, addition)
            for value in (quote[:-1], "X" + quote[1:], "Unregistered sentence.", "", None, []):
                with self.subTest(addition=addition, value_type=type(value).__name__):
                    bad = copy.deepcopy(report)
                    bad["features"][-1]["sourceQuote"] = value
                    with self.assertRaises(Invalid):
                        design.record_scenarios(self.store, self.state, bad)

    def test_long_quote_still_requires_actual_source_inclusion(self):
        report = self.registered_feature("Исходная цитата 🦊. " * 100)
        forged = "Absent from the immutable brief. " * 100
        self.state["requirements"][0]["quote"] = forged
        report["basis"] = design.scenario_basis(self.state)
        report["features"][0]["sourceQuote"] = forged
        with self.assertRaisesRegex(Invalid, "зарегистрированным источником"):
            design.record_scenarios(self.store, self.state, report)

    def test_long_quote_does_not_bypass_stale_basis_or_brief_hash(self):
        quote = "Исходная цитата 🦊. " * 100
        report = self.registered_feature(quote)
        design.record_scenarios(self.store, self.state, report)
        self.state["requirements"][0]["acceptance"].append("Preview works")
        with self.assertRaisesRegex(Invalid, "сценарии устарели"):
            design.record_scenarios(self.store, self.state, report)
        self.assertFalse(design.valid_scenarios(self.store, self.state))
        report = self.registered_feature(quote)
        design.record_scenarios(self.store, self.state, report)
        brief = self.store.run / self.state["briefs"][0]["file"]
        brief.write_text(quote + "\nTampered suffix.\n", encoding="utf-8")
        with self.assertRaisesRegex(Invalid, "исходный бриф изменён"):
            design.record_scenarios(self.store, self.state, report)
        self.assertFalse(design.valid_scenarios(self.store, self.state))

    def test_registered_quote_preserves_overall_report_size_boundary(self):
        report = self.registered_feature("Я")
        overhead = len(json.dumps(report, ensure_ascii=False, allow_nan=False)) - 1
        for excess in (0, 1):
            with self.subTest(excess=excess):
                report = self.registered_feature("Я" * (design.MAX_REPORT_CHARS - overhead + excess))
                self.assertEqual(len(json.dumps(report, ensure_ascii=False, allow_nan=False)),
                                 design.MAX_REPORT_CHARS + excess)
                if excess:
                    with self.assertRaisesRegex(Invalid, "слишком большой отчёт"):
                        design.record_scenarios(self.store, self.state, report)
                else:
                    design.record_scenarios(self.store, self.state, report)
                    self.assertTrue(design.valid_scenarios(self.store, self.state))

    def test_long_source_quote_keeps_other_text_limits(self):
        report = self.registered_feature("Исходная цитата 🦊. " * 100)
        for field, limit in (("depthReason", 600), ("specQuote", 3000),
                             ("scenario", 1600), ("verification", 1600), ("counterexample", 1600)):
            with self.subTest(field=field):
                bad = copy.deepcopy(report)
                target = bad if field == "depthReason" else bad["features"][0]
                if field in ("scenario", "verification", "counterexample"):
                    target = target["dimensions"][0]
                target[field] = "Я" * (limit + 1)
                with self.assertRaises(Invalid):
                    design.record_scenarios(self.store, self.state, bad)

    def test_deep_requires_counterexample_and_strict_rejects_addition_as_refinement(self):
        deep = self.feature("deep")
        deep["features"][0]["dimensions"][0]["counterexample"] = ""
        with self.assertRaises(Invalid):
            design.record_scenarios(self.store, self.state, deep)
        self.state["requirements"].append({"id": "A01", "kind": "addition", "scope": "active",
                                           "parent": "R01", "text": "Add cloud export", "acceptance": ["Cloud export works"]})
        strict = self.feature("strict")
        addition = copy.deepcopy(strict["features"][0])
        addition.update(requirement="A01", changeKind="requested")
        strict["features"].append(addition)
        strict["basis"] = design.scenario_basis(self.state)
        with self.assertRaises(Invalid):
            design.record_scenarios(self.store, self.state, strict)
        strict["features"][1]["changeKind"] = "addition"
        with self.assertRaises(Invalid):
            design.record_scenarios(self.store, self.state, strict)

    def test_interfaces_have_hash_bound_references_and_plan_bindings(self):
        report = self.interfaces()
        design.record_interfaces(self.store, self.state, report)
        self.assertTrue(design.valid_interfaces(self.store, self.state))
        self.assertEqual(design.validate_plan_bindings(self.store, self.state, self.tasks()),
                         [{"interface": "csv.export", "provider": "T01", "consumers": ["T02"]}])
        with self.assertRaises(Invalid):
            design.validate_plan_bindings(self.store, self.state, self.tasks(depends=False))
        self.reference.write_text("Changed implementation.\n", encoding="utf-8", newline="\n")
        self.assertFalse(design.valid_interfaces(self.store, self.state))
        self.assertTrue(design.valid_interfaces(self.store, self.state, check_sources=False))
        self.assertEqual(self.store.text(design.reference_snapshots(self.store, self.state)[0]["snapshot"]),
                         "Existing local CSV adapter.\n")
        self.reference.unlink()
        self.assertTrue(design.valid_interfaces(self.store, self.state, check_sources=False))

    def test_interfaces_reject_missing_owner_wrong_requirement_and_broken_reference(self):
        report = self.interfaces()
        reference = report["interfaces"][0]["references"][0]
        reference["path"] = reference.pop("file")
        with self.assertRaisesRegex(Invalid, "указатель материала требует путь и SHA-256"):
            design.record_interfaces(self.store, self.state, report)
        report = self.interfaces()
        report["interfaces"][0]["references"][0]["sha256"] = "0" * 64
        with self.assertRaises(Invalid):
            design.record_interfaces(self.store, self.state, report)
        report = self.interfaces()
        design.record_interfaces(self.store, self.state, report)
        tasks = self.tasks()
        tasks[0]["zones"] = ["src/other.py"]
        with self.assertRaises(Invalid):
            design.validate_plan_bindings(self.store, self.state, tasks)
        tasks = self.tasks()
        tasks[0]["requirements"] = ["R01"]
        with self.assertRaises(Invalid):
            design.validate_plan_bindings(self.store, self.state, tasks)

    def test_explicit_empty_contracts_do_not_invent_features_or_interfaces(self):
        self.state["requirements"] = self.state["requirements"][1:]
        scenarios = {"basis": design.scenario_basis(self.state), "depth": "strict",
                     "depthReason": "This brief contains only a privacy constraint.",
                     "features": [], "noFeatureReason": "No active product feature is requested."}
        design.record_scenarios(self.store, self.state, scenarios)
        self.assertTrue(design.valid_scenarios(self.store, self.state))
        interfaces = {"basis": design.interface_basis(self.state), "interfaces": [],
                      "noInterfaceReason": "The constraint requires no cross-module callable API."}
        design.record_interfaces(self.store, self.state, interfaces)
        self.assertTrue(design.valid_interfaces(self.store, self.state))
        self.assertEqual(design.validate_plan_bindings(self.store, self.state, self.tasks()), [])


if __name__ == "__main__":
    unittest.main()
