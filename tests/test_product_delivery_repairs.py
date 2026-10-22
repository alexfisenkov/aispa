"""Contract regression tests, not a model/product-quality evaluation."""
import copy
import unittest

from tests import test_ideas as idea_fixtures, test_team as team_fixtures
from aispa_core import ideas, product, delivery, repairs
from aispa_core.contracts import scope_basis
from aispa_core.store import Invalid


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.fx = idea_fixtures.IdeaTests()
        self.fx.setUp()
        self.fx.state["policy"] = {"productChallenge": True}

    def tearDown(self):
        self.fx.tearDown()

    def test_five_specific_probes_mandatory_and_new_decision_invalidates_scope(self):
        report = self.fx.report()
        for mutate in (lambda r: r.pop("challenge"),
                       lambda r: r["challenge"].pop(),
                       lambda r: r["challenge"][0].update(requirements=["unknown"]),
                       lambda r: r["challenge"][0].update(validation=""),
                       lambda r: r["challenge"][0].update(status="proven")):
            bad = copy.deepcopy(report); mutate(bad)
            self.fx.rejected(bad)
        ideas.record(self.fx.store, self.fx.state, report)
        before = scope_basis(self.fx.state)
        report["challenge"][0]["decision"] = "Choose another approach while retaining all requirements."
        ideas.record(self.fx.store, self.fx.state, report)
        self.assertNotEqual(before, scope_basis(self.fx.state))

    def test_independent_spec_check_requires_all_probes_and_actual_quotes(self):
        challenge = self.fx.report()["challenge"]
        for row in challenge:
            row.update(decision="Keep local: " + row["probe"], validation="Check the actual result: " + row["probe"])
        spec = "\n".join(row["decision"] + ". " + row["validation"] for row in challenge)
        report = {"ideaChecks": [{"probe": row["probe"], "status": "covered", "specQuote": row["decision"] + ". " + row["validation"],
                                 "note": "Synthetic correspondence, no semantic proof."} for row in challenge]}
        self.assertTrue(product.coverage(self.fx.state, report, spec, challenge))
        report["ideaChecks"][0]["status"] = "partial"
        self.assertFalse(product.coverage(self.fx.state, report, spec, challenge))
        report["ideaChecks"][0]["status"] = "covered"
        with self.assertRaises(Invalid):
            product.coverage(self.fx.state, report, "Cloud only", challenge)
        bad = copy.deepcopy(report)
        bad["ideaChecks"][0]["specQuote"] = report["ideaChecks"][1]["specQuote"]
        with self.assertRaises(Invalid):
            product.coverage(self.fx.state, bad, spec, challenge)
        report["ideaChecks"].pop()
        with self.assertRaises(Invalid):
            product.coverage(self.fx.state, report, spec, challenge)


def delivery_report(tasks):
    return {"profile": "small", "reason": "Synthetic small CLI; one observable command per task.",
            "slices": [{"id": t["id"], "kind": "user", "outcome": "Observable result of " + t["id"],
                        "tasks": [t["id"]], "demonstration": {"task": t["id"], "criterion": t["criteria"][0]},
                        "supports": []} for t in tasks],
            "migrations": [], "migrationReason": "New local tool; no existing consumers or stored format is replaced."}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [{"id": f"T0{i}", "dependsOn": [f"T0{i-1}"] if i > 1 else [],
                       "zones": ["consumer.py" if i == 2 else f"part{i}.py"], "criteria": ["compatible", "recoverable"]}
                      for i in (1, 2, 3)]
        self.report = delivery_report(self.tasks)
        self.report.update(profile="migration", migrationReason="Replace the storage adapter without losing consumers.",
                           migrations=[{"id": "M01", "reason": "Preserve callers while replacing storage",
                             "expand": ["T01"], "migrate": ["T02"], "contract": ["T03"],
                             "consumers": [{"path": "consumer.py", "task": "T02"}],
                             "compatibility": [{"task": t["id"], "criterion": "compatible"} for t in self.tasks],
                             "rollback": [{"task": t["id"], "criterion": "recoverable"} for t in self.tasks]}])

    def test_ordered_migration_and_full_visible_slices(self):
        delivery.validate(self.tasks, self.report)
        for i in (1, 2):
            tasks = copy.deepcopy(self.tasks); tasks[i]["dependsOn"] = []
            with self.assertRaises(Invalid):
                delivery.validate(tasks, self.report)

    def test_consumers_rollback_and_demonstrations_cannot_be_omitted(self):
        for mutate in (lambda r: r["slices"].pop(),
                       lambda r: r["slices"][0]["demonstration"].update(criterion="not a criterion"),
                       lambda r: r["migrations"][0]["rollback"].pop(),
                       lambda r: r["migrations"][0]["consumers"][0].update(path="../outside.py"),
                       lambda r: r["migrations"][0]["consumers"][0].update(path="unowned.py"),
                       lambda r: r["migrations"][0].update(contract=["T02"])):
            bad = copy.deepcopy(self.report); mutate(bad)
            with self.assertRaises(Invalid):
                delivery.validate(self.tasks, bad)

    def test_enabler_needs_actual_user_result_not_padding(self):
        self.report["slices"][0].update(kind="enabler", supports=["T02"])
        delivery.validate(self.tasks, self.report)
        self.report["slices"][0]["supports"] = []
        with self.assertRaises(Invalid):
            delivery.validate(self.tasks, self.report)

    def test_compatibility_cannot_stand_in_for_recovery(self):
        migration = self.report["migrations"][0]
        migration["rollback"] = copy.deepcopy(migration["compatibility"])
        with self.assertRaises(Invalid):
            delivery.validate(self.tasks, self.report)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.fx = team_fixtures.TeamTests()
        self.fx.setUp()
        self.fx.state["policy"] = {"repairDiagnosis": True}
        self.fx.start()
        self.fx.team.submit("T01", self.fx.report())
        self.fx.team.review("T01", 1, self.fx.report(role="critic1", findings=[
            {"id": "F1", "severity": "blocking", "text": "Wrong edge case", "criterion": "T01 works"}]))

    def tearDown(self):
        self.fx.tearDown()

    def report(self, cause="implementation_gap", route="same"):
        return {"basis": repairs.basis(self.fx.state, "T01"), "cause": cause, "route": route,
                "diagnosis": "Synthetic edge case diagnosis.", "reason": "Choose worker based on the diagnosed cause.",
                "changes": "Read the missing contract and handle empty input before returning.",
                "addresses": ["F1"], "context": ["Read a.txt and the task's empty-input criterion."]}

    def test_no_repair_or_replacement_without_diagnosis(self):
        with self.assertRaises(Invalid):
            self.fx.team.assign("T01", "worker", "fixture", "new", "session", model="test-model")
        first = self.fx.attempt()["rounds"][0]
        with self.assertRaises(Invalid):
            self.fx.team.repair("T01", self.fx.report(previousReport=first["report"]["sha256"], addresses=["F1"]))

    def test_same_worker_repair_keeps_two_rounds_and_source_binding(self):
        ref = repairs.record(self.fx.store, self.fx.state, "T01", self.report())
        self.fx.host.save("repair-diagnosed:T01")
        first = self.fx.attempt()["rounds"][0]
        receipt = self.fx.team.repair("T01", self.fx.report(previousReport=first["report"]["sha256"], addresses=["F1"]))
        self.assertEqual(receipt["diagnosis"], ref)
        with self.assertRaises(Invalid):
            repairs.record(self.fx.store, self.fx.state, "T01", self.report())
        self.fx.team.review("T01", 2, self.fx.report(role="critic2", findings=[],
            previousReport=first["report"]["sha256"], repairReceipt=receipt["report"]["sha256"],
            resolutions=[{"id": "F1", "status": "resolved", "note": "Observed fixed", "artifact": self.fx.evidence["file"]}]))
        with self.assertRaises(Invalid):
            self.fx.team.review("T01", 3, self.fx.report(role="critic2", findings=[]))

    def test_wrong_approach_requires_fresh_context_recorded_model(self):
        with self.assertRaises(Invalid):
            repairs.record(self.fx.store, self.fx.state, "T01", self.report("wrong_approach", "same"))
        repairs.record(self.fx.store, self.fx.state, "T01", self.report("wrong_approach", "fresh"))
        with self.assertRaises(Invalid):
            repairs.worker_guard(self.fx.store, self.fx.state, "T01")
        with self.assertRaises(Invalid):
            self.fx.team.assign("T01", "worker", "fixture", "new", "session")
        new = self.fx.team.assign("T01", "worker", "fixture", "new", "session", model="test-model")
        repairs.worker_guard(self.fx.store, self.fx.state, "T01", new)
        with self.assertRaises(Invalid):
            self.fx.team.assign("T01", "worker", "fixture", "newer", "session", model="test-model")

    def test_lean_fresh_worker_needs_no_model_id_but_must_be_fresh(self):
        self.fx.state["policy"]["leanControl"] = True
        repairs.record(self.fx.store, self.fx.state, "T01", self.report("wrong_approach", "fresh"))
        with self.assertRaises(Invalid):  # the same context is still not a new executor
            self.fx.team.assign("T01", "worker", "fixture", "stale", "session", fresh=False)
        new = self.fx.team.assign("T01", "worker", "fixture", "new", "session")  # no model ID
        self.assertIsNone(self.fx.state["actors"][new]["model"])
        repairs.worker_guard(self.fx.store, self.fx.state, "T01", new)
        self.assertIn("a strong model only for sensitive zones", repairs.view(self.fx.store, self.fx.state, "T01")["rule"])

    def test_all_findings_and_external_blocker_must_remain(self):
        bad = self.report(); bad["addresses"] = []
        with self.assertRaises(Invalid):
            repairs.record(self.fx.store, self.fx.state, "T01", bad)
        ref = repairs.record(self.fx.store, self.fx.state, "T01", self.report("external_blocker", "blocked"))
        with self.assertRaises(Invalid):
            repairs.worker_guard(self.fx.store, self.fx.state, "T01")
        (self.fx.store.run / ref["file"]).write_text("{}", encoding="utf-8")
        with self.assertRaises(Invalid):
            repairs.read(self.fx.store, self.fx.state, "T01")
