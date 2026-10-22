"""Check modes in scenarios, model routing, role estimates and the status view."""
import unittest

import test_design_contract
import test_lean_control
from aispa_core import design_contract, display, packets, planning, preparation
from aispa_core.store import Invalid


class ScenarioModeTests(unittest.TestCase):
    setUp = test_design_contract.DesignContractTests.setUp
    tearDown = test_design_contract.DesignContractTests.tearDown
    feature = test_design_contract.DesignContractTests.feature

    def mode(self, mode, lean=True):
        if lean:
            self.state["policy"] = {"leanControl": True}
        self.state["checkMode"] = mode
        self.state["depth"] = "balanced"

    def only(self, report, names):
        report["features"][0]["dimensions"] = [c for c in report["features"][0]["dimensions"] if c["name"] in names]
        return report

    def test_fast_needs_only_main_and_error(self):
        self.mode("fast")
        self.assertEqual(design_contract.required_dimensions(self.state), ("main", "error"))
        design_contract.record_scenarios(self.store, self.state, self.only(self.feature("balanced"), ("main", "error")))
        self.assertTrue(design_contract.valid_scenarios(self.store, self.state))
        with self.assertRaises(Invalid):
            design_contract.record_scenarios(self.store, self.state, self.only(self.feature("balanced"), ("main",)))

    def test_fast_keeps_report_depth_semantics(self):
        self.mode("fast")
        report = self.only(self.feature("deep"), ("main", "error"))
        with self.assertRaises(Invalid):
            design_contract.record_scenarios(self.store, self.state, report)

    def test_max_needs_all_dimensions_with_counterexamples(self):
        self.mode("max")
        self.assertEqual(design_contract.required_dimensions(self.state), design_contract.DIMENSIONS)
        report = self.feature("balanced")
        with self.assertRaises(Invalid):
            design_contract.record_scenarios(self.store, self.state, report)
        for cell in report["features"][0]["dimensions"]:
            cell["counterexample"] = "Break the " + cell["name"] + " path."
        design_contract.record_scenarios(self.store, self.state, report)
        self.assertTrue(design_contract.valid_scenarios(self.store, self.state))
        short = self.only(self.feature("balanced"), design_contract.DIMENSIONS[:7])
        with self.assertRaises(Invalid):
            design_contract.record_scenarios(self.store, self.state, short)

    def test_scenarios_are_judged_by_the_mode_they_were_recorded_under(self):
        self.mode("balanced")
        design_contract.record_scenarios(self.store, self.state, self.feature("balanced"))
        self.assertTrue(design_contract.valid_scenarios(self.store, self.state))
        self.state["checkMode"] = "max"  # the owner switched later: no new scenarios are demanded
        self.assertTrue(design_contract.valid_scenarios(self.store, self.state))
        with self.assertRaises(Invalid):  # a fresh record in max still needs every dimension
            design_contract.record_scenarios(self.store, self.state, self.only(self.feature("balanced"), ("main", "error")))
        self.state["checkMode"] = "fast"
        self.assertTrue(design_contract.valid_scenarios(self.store, self.state))

    def test_same_report_is_recorded_again_under_a_new_mode(self):
        self.mode("balanced")
        report = self.feature("balanced")
        design_contract.record_scenarios(self.store, self.state, report)
        self.assertEqual(self.state["featureScenarios"]["checkMode"], "balanced")
        self.state["checkMode"] = "fast"
        design_contract.record_scenarios(self.store, self.state, report)
        self.assertEqual(self.state["featureScenarios"]["checkMode"], "fast")
        self.assertEqual(len(self.state["featureScenarioHistory"]), 2)
        self.state["checkMode"] = "balanced"
        with self.assertRaises(Invalid):  # a fast-only report is not enough once balanced
            design_contract.record_scenarios(self.store, self.state, self.only(self.feature("balanced"), ("main", "error")))

    def test_old_run_ignores_check_mode(self):
        self.mode("fast", lean=False)
        self.assertEqual(design_contract.required_dimensions(self.state), design_contract.DIMENSIONS)
        with self.assertRaises(Invalid):
            design_contract.record_scenarios(self.store, self.state, self.only(self.feature("balanced"), ("main", "error")))

    def test_balanced_follows_depth(self):
        self.mode("balanced")
        self.assertEqual(design_contract.required_dimensions(self.state),
                         design_contract.LEAN_REQUIRED["balanced"])

    def test_template_matches_mode(self):
        for mode, expected in (("fast", ("main", "error")), ("max", design_contract.DIMENSIONS)):
            with self.subTest(mode=mode):
                self.mode(mode)
                rows = preparation.template(self.store, self.state, "scenarios")["features"][0]["dimensions"]
                self.assertEqual(tuple(c["name"] for c in rows), expected)
        self.mode("fast", lean=False)
        self.state["policy"] = {}
        rows = preparation.template(self.store, self.state, "scenarios")["features"][0]["dimensions"]
        self.assertEqual(len(rows), 8)


class RoutingModeTests(unittest.TestCase):
    def state(self, lean=True, mode="balanced"):
        policy = {"taskTiers": True, "adaptiveCritique": True}
        if lean:
            policy["leanControl"] = True
        return {"policy": policy, "checkMode": mode, "tasks": [self.task()]}

    def task(self, zone="src/report", risk="normal"):
        return {"id": "T01", "zones": [zone], "readZones": [], "requirements": ["R01"], "dependsOn": [], "risk": risk}

    def route(self, state, role="critic1", task=None, attempt=None):
        task = task or state["tasks"][0]
        return packets.routing(state, task, role, attempt or {"reviewPolicy": "lean", "rounds": []})

    def test_ordinary_critic_gets_standard_in_lean(self):
        state = self.state()
        for role in ("critic1", "critic2"):
            route = self.route(state, role)
            self.assertEqual(route["tier"], "standard")
            self.assertIn("Дешёвая модель по умолчанию", route["modelRule"])

    def test_only_sensitive_zone_gets_strong(self):
        state = self.state()
        self.assertEqual(self.route(state, task=self.task("src/auth/login.py"))["tier"], "strong")
        # The owner named security, keys and sensitive data only: risk and repair do not raise the model.
        self.assertEqual(self.route(state, task=self.task(risk="high"))["tier"], "standard")
        repaired = {"reviewPolicy": "lean", "rounds": [], "repairPlan": {"x": 1}}
        self.assertEqual(self.route(state, attempt=repaired)["tier"], "standard")
        self.assertNotIn("высокого риска", packets.MODEL_RULE)
        self.assertNotIn("repair", packets.MODEL_RULE.lower())

    def test_key_files_and_personal_data_get_strong(self):
        state = self.state()
        for zone in (".env", "config/.env.production", "certs/server.pem", "deploy/tls.key", "certs/store.p12",
                     "certs/store.pfx", "home/id_rsa", "home/id_rsa.pub", ".npmrc", "web/.pypirc", ".netrc",
                     "app/credentials", "app/credentials.json", "src/user_data/load.py", "src/userdata.py",
                     "src/customer.py", "src/client_data.py", "data/персональные.csv", "src/персональные_данные.py"):
            with self.subTest(zone=zone):
                self.assertEqual(self.route(state, task=self.task(zone))["tier"], "strong")
        for zone in ("src/environment.py", "src/keyboard.py", "docs/monkey.md", "src/clients.py"):
            with self.subTest(zone=zone):
                self.assertEqual(self.route(state, task=self.task(zone))["tier"], "standard")

    def test_strong_model_list_is_narrow(self):
        state = self.state()
        for zone in ("src/security/policy.py", "config/secrets.py", "src/tokens.py", "src/login/auth.py",
                     "src/payments/stripe.py", "app/checkout.py", "src/billing.py", "src/personal/profile.py",
                     "src/pii.py", "app/пароли.py"):
            with self.subTest(zone=zone):
                self.assertEqual(self.route(state, task=self.task(zone))["tier"], "strong")
        for zone in ("package.json", "db/schema.sql", "Dockerfile", "skills/aispa/SKILL.md", "prompts/worker.md",
                     "src/report", "src/author.py", "migrations/001.py", "pyproject.toml"):
            with self.subTest(zone=zone):
                self.assertEqual(self.route(state, task=self.task(zone))["tier"], "standard")

    def test_foundation_does_not_force_strong_in_lean(self):
        state = self.state()
        state["tasks"].append({"id": "T02", "dependsOn": ["T01"], "zones": [], "risk": "normal"})
        self.assertEqual(self.route(state)["tier"], "standard")

    def test_without_lean_control_nothing_changes(self):
        state = self.state(lean=False)
        route = self.route(state, attempt={"reviewPolicy": "adaptive", "rounds": []})
        self.assertEqual(route["tier"], "strong")
        self.assertNotIn("modelRule", route)

    def test_fast_attempt_is_worker_only_and_says_so(self):
        state = self.state(mode="fast")
        attempt = {"tier": "fast", "tierReason": "Режим быстро", "plannedTier": "heavy"}
        route = self.route(state, "worker", attempt=attempt)
        self.assertEqual((route["taskTier"], route["perTaskReview"], route["tier"]), ("fast", "skipped_fast", "standard"))
        self.assertIn("fast mode", route["reason"])
        self.assertEqual(route["taskRoleSlots"], {"minimum": 1, "maximum": 1})
        with self.assertRaises(Invalid):
            self.route(state, "critic1", attempt=attempt)
        sensitive = packets.routing(state, self.task("src/auth/login.py"), "worker", attempt)
        self.assertEqual(sensitive["tier"], "strong")


class RoleEstimateTests(unittest.TestCase):
    def state(self, mode, lean=True, tiers=True):
        policy = {"adaptiveCritique": True}
        if tiers:
            policy["taskTiers"] = True
        if lean:
            policy["leanControl"] = True
        return {"policy": policy, "checkMode": mode}

    tasks = [{"tier": "standard"}, {"tier": "heavy"}]

    def test_modes_change_the_estimate(self):
        estimate = lambda state: planning._role_estimate(state, self.tasks)
        self.assertEqual(estimate(self.state("balanced")), {"minimum": 3, "maximum": 4})
        self.assertEqual(estimate(self.state("fast")), {"minimum": 2, "maximum": 2})
        self.assertEqual(estimate(self.state("max")), {"minimum": 4, "maximum": 6})

    def test_fast_without_declared_classes(self):
        state = self.state("fast", tiers=False)
        self.assertEqual(planning._role_estimate(state, [{}, {}]), {"minimum": 2, "maximum": 2})

    def test_old_run_ignores_mode(self):
        state = self.state("fast", lean=False, tiers=False)
        state["policy"] = {}
        self.assertEqual(planning._role_estimate(state, [{}, {}]), 8)


class DisplayModeTests(unittest.TestCase):
    setUp = test_lean_control.LeanReviewTests.setUp
    tearDown = test_lean_control.LeanReviewTests.tearDown
    task = test_lean_control.LeanReviewTests.task
    attempt = test_lean_control.LeanReviewTests.attempt

    def test_view_reports_mode_and_deferred(self):
        view = display.project(self.store, self.state)
        self.assertEqual((view["checkMode"], view["checkModeLabel"]), ("balanced", "Сбалансированно"))
        self.assertIs(view["tasks"][0]["deferred"], False)
        self.state["checkMode"] = "max"
        self.state["tasks"][0].update(status="blocked")
        self.attempt().update(terminal=True, authorization={"kind": "extra-wave"})
        view = display.project(self.store, self.state)
        self.assertEqual((view["checkMode"], view["checkModeLabel"]), ("max", "Максимум"))
        self.assertIs(view["tasks"][0]["deferred"], False)  # an unsealed record is not a deferral
        self.state["checkMode"] = "fast"
        self.assertEqual(display.project(self.store, self.state)["checkModeLabel"], "Быстро")

    def test_view_lists_mode_changes_with_owner_words(self):
        self.assertEqual(display.project(self.store, self.state)["checkModeHistory"], [])
        words = self.store.artifact("mode-requests", ("Теперь быстро. " + "я" * 400).encode(), ".md")
        self.state["checkMode"] = "fast"
        self.state["checkModeHistory"] = [
            {"mode": "balanced", "at": "2026-10-22T09:00:00Z", "source": "init"},
            {"mode": "fast", "previous": "balanced", "request": words, "at": "2026-10-22T10:00:00Z"}]
        view = display.project(self.store, self.state)
        self.assertEqual(len(view["checkModeHistory"]), 1)  # the start is not a switch
        row = view["checkModeHistory"][0]
        self.assertEqual((row["mode"], row["previous"], row["at"]), ("fast", "balanced", "2026-10-22T10:00:00Z"))
        self.assertTrue(row["text"].startswith("Теперь быстро."))
        self.assertEqual(len(row["text"]), 300)
        self.assertEqual(view["remaining"]["checkModeChanges"], view["checkModeHistory"])

    def test_old_run_view_is_balanced(self):
        self.state["policy"] = {}
        self.state["checkMode"] = "fast"
        view = display.project(self.store, self.state)
        self.assertEqual((view["checkMode"], view["checkModeHistory"]), ("balanced", []))
        self.assertNotIn("skippedChecks", view["remaining"])

    def test_planned_tier_comes_from_attempt(self):
        self.attempt().update(tier="fast", plannedTier="heavy")
        task = display.project(self.store, self.state)["tasks"][0]
        self.assertEqual((task["tier"], task["plannedTier"]), ("fast", "heavy"))


if __name__ == "__main__":
    unittest.main()
