"""Receipt-backed progress and live HTTP regression; no model invocations."""
import copy
import json
from pathlib import Path
import sys
import time
import unittest
from urllib.request import urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import dashboard_server, display_details
from aispa_core.contracts import basis
from aispa_core.display import project
from tests import test_workflow as workflow_tests
from tests import test_team as team_tests
from aispa_core.team import direct_proof_current, task_proof_valid


class DescendantProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fx = team_tests.TeamTests("test_fresh_descendant_can_reconfirm_ancestor_only_within_allowed_set")
        self.fx.setUp()
        fx = self.fx
        fx.host.task("T02").update(dependsOn=["T01"], zones=["a.txt", "b.txt"], wave=2)
        fx.team.prepare()
        fx.complete("T01")
        fx.start("T02")
        (fx.root / "a.txt").write_text("Intentional descendant integration", encoding="utf-8")
        fx.verify("T02")
        fx.reviews("T02")
        self.assertTrue(fx.accept("T02"))

    def tearDown(self):
        self.fx.tearDown()

    def test_valid_descendant_result_is_completed_in_task_requirement_and_wave(self):
        fx = self.fx
        before = copy.deepcopy(fx.state)
        journal = (fx.store.run / "events.jsonl").read_bytes()
        self.assertFalse(direct_proof_current(fx.store, fx.state, "T01"))
        self.assertTrue(task_proof_valid(fx.store, fx.state, "T01"))
        view = project(fx.store, fx.state)
        task = next(row for row in view["tasks"] if row["id"] == "T01")
        self.assertEqual(display_details.task_status(task), "completed")
        self.assertEqual(view["waveDetails"][0]["status"], "completed")
        self.assertEqual(view["waveDetails"][0]["completed"], 1)
        self.assertEqual(view["requirements"][0]["progress"]["status"], "completed")
        self.assertEqual(task["criteriaDetails"][0]["status"], "passed")
        self.assertEqual(task["criteriaDetails"][0]["source"], "descendant_proof")
        self.assertEqual(task["proofProvenance"]["kind"], "descendant")
        self.assertEqual(task["proofProvenance"]["task"], "T02")
        self.assertEqual(task["proofProvenance"]["attempt"], fx.attempt("T02")["id"])
        self.assertEqual(fx.state, before)
        self.assertEqual((fx.store.run / "events.jsonl").read_bytes(), journal)

    def test_confirmed_result_keeps_historical_phase_basis_and_recorded_dates(self):
        fx = self.fx
        records = fx.attempt("T01")
        historical = {"implementation": records["submitted"], "critic_1": records["rounds"][0],
                      "critic_2": records["rounds"][1], "task_acceptance": records["acceptance"]}
        events = fx.store.read_journal(include_events=True)["events"]

        def receipt_at(record):
            # These sealed phase records have no own timestamp. Their actual
            # recorded date is the first verified event containing this exact
            # receipt, rather than today's projection time or a fabricated at.
            for event in events:
                for attempt in event["state"].get("taskHistory", {}).get("T01", []):
                    saved = [attempt.get("submitted"), attempt.get("acceptance"), *attempt.get("rounds", [])]
                    if record in saved:
                        return event["at"]
            self.fail("Historical phase has no matching verified journal event")

        task = project(fx.store, fx.state)["tasks"][0]
        self.assertTrue(task["proofCurrent"])
        steps = {row["id"]: row for row in task["phaseSteps"]}
        for phase, record in historical.items():
            with self.subTest(phase=phase):
                self.assertEqual(steps[phase].get("basis"), record["basis"])
                self.assertEqual(steps[phase].get("recordedAt"), receipt_at(record))
                self.assertFalse(steps[phase].get("basisCurrent"))
                self.assertTrue(steps[phase].get("historical"))

    def test_later_code_or_corrupt_descendant_revokes_task_and_wave_together(self):
        fx = self.fx
        report = fx.attempt("T02")["acceptance"]["report"]
        path = fx.store.run / report["file"]
        original = path.read_bytes()
        path.write_bytes(b"Corrupted descendant acceptance")
        view = project(fx.store, fx.state)
        self.assertFalse(view["tasks"][0]["proofCurrent"])
        self.assertEqual(display_details.task_status(view["tasks"][0]), "stale")
        self.assertEqual(view["waveDetails"][0]["status"], "stale")
        path.write_bytes(original)
        (fx.root / "a.txt").write_text("Later unverified change", encoding="utf-8")
        view = project(fx.store, fx.state)
        self.assertFalse(view["tasks"][0]["proofCurrent"])
        self.assertEqual(view["waveDetails"][0]["status"], "stale")


class DetailsTests(unittest.TestCase):
    def setUp(self):
        self.fx = workflow_tests.WorkflowTests("test_empty_run_cannot_finish")
        self.fx.setUp()

    def tearDown(self):
        self.fx.tearDown()

    def view(self):
        return project(self.fx.store, self.fx.state)

    def start(self):
        fx = self.fx
        fx.flow.start("T01", basis(fx.store, fx.state, "task", "T01"))
        for role in ("worker", "critic1", "critic2", "acceptor"):
            fx.flow.pipeline.assign("T01", role, "codex", "details-" + role, "fixture")
        fx.verify("T01")

    def test_mixed_summary_exposes_only_route_types_and_recomputes_counts(self):
        from aispa_core import test_intent
        fx = self.fx
        fx.prepare()
        criterion = fx.state["tasks"][0]["criteria"][0]
        summary = {"mode": "mixed", "counts": {"automated": 999}, "routes": [
            {"criterion": criterion, "kind": kind, "reason": "private", "procedure": "/private/path", "expected": "raw output"}
            for kind in ("automated", "browser", "process")]}
        with patch.object(test_intent, "route_summary", return_value=summary):
            projected = self.view()["tasks"][0]["evidenceSummary"]
        self.assertEqual(projected["counts"], {"automated": 1, "browser": 1, "process": 1})
        self.assertEqual(projected["routes"], [{"criterion": criterion, "kind": k}
                                             for k in ("automated", "browser", "process")])
        for malformed in (None, {}, {"mode": "mixed", "routes": [None]},
                          {"mode": "mixed", "routes": [{"criterion": criterion, "kind": "future"}]}):
            with self.subTest(summary=malformed), patch.object(test_intent, "route_summary", return_value=malformed):
                self.assertNotIn("evidenceSummary", self.view()["tasks"][0])
        self.assertNotIn("evidenceSummary", self.view()["tasks"][0])

    def test_gate_missing_running_passed_failed_and_stale_are_distinct(self):
        fx = self.fx
        self.assertEqual(self.view()["gateDetails"]["coverage"]["status"], "pending")
        fx.state["workProgress"] = {"phase": "coverage", "startedAt": fx.state["createdAt"],
                                    "activeSince": fx.state["createdAt"], "elapsedSeconds": 0}
        self.assertEqual(self.view()["gateDetails"]["coverage"]["status"], "running")
        fx.state.pop("workProgress")
        fx.prepare()
        self.assertEqual(self.view()["gateDetails"]["plan"]["counts"], {"passed": 1, "failed": 0, "pending": 0})
        report = fx.header("coverage")
        report["checks"] = [{"requirement": "R01", "status": "partial"}]
        self.assertFalse(fx.flow.coverage(fx.file(report)))
        detail = self.view()["gateDetails"]["coverage"]
        self.assertEqual((detail["status"], detail["evidenceCurrent"], detail["counts"]["failed"]), ("failed", True, 1))
        fx.coverage()
        gate = fx.state["gates"]["coverage"]
        (fx.store.run / gate["report"]["file"]).write_text("broken", encoding="utf-8")
        detail = self.view()["gateDetails"]["coverage"]
        self.assertEqual(detail["status"], "stale")
        self.assertFalse(detail["evidenceCurrent"])

    def test_requirement_tracks_queue_implementation_review_repair_and_blocker(self):
        fx = self.fx
        fx.prepare()
        self.assertEqual(self.view()["requirements"][0]["progress"]["status"], "queued")
        self.start()
        view = self.view()
        self.assertEqual(view["requirements"][0]["progress"]["status"], "implementation")
        self.assertEqual(view["tasks"][0]["requirementsDetails"][0]["text"], "Команда готовности")
        self.assertEqual(view["tasks"][0]["criteriaDetails"][0]["source"], "task_state")
        fx.flow.pipeline.submit("T01", fx.task_report("T01", "worker"))
        view = self.view()
        self.assertEqual(view["requirements"][0]["progress"]["status"], "review")
        self.assertEqual(view["tasks"][0]["criteriaDetails"][0]["source"], "checked_report")
        criterion = view["tasks"][0]["criteriaDetails"][0]
        self.assertEqual(criterion["status"], "reported")
        self.assertEqual(criterion["checkedBy"], "worker")
        self.assertTrue(criterion["provisional"])
        finding = {"id": "F1", "severity": "blocking", "text": "Fix", "criterion": "Выводит ready"}
        fx.flow.pipeline.review("T01", 1, fx.task_report("T01", "critic1", findings=[finding]))
        self.assertEqual(self.view()["requirements"][0]["progress"]["status"], "repair")
        self.assertEqual(self.view()["tasks"][0]["criteriaDetails"][0]["status"], "failed")
        fx.flow.block("T01", "Fixture obstacle")
        self.assertEqual(self.view()["requirements"][0]["progress"]["status"], "blocked")

    def test_completed_is_not_requirement_acceptance_and_drift_revokes_both(self):
        fx = self.fx
        fx.prepare(); fx.completed()
        completed = self.view()
        criterion = completed["tasks"][0]["criteriaDetails"][0]
        self.assertEqual((criterion["status"], criterion["checkedBy"], criterion["provisional"]), ("passed", "acceptor", False))
        progress = completed["requirements"][0]["progress"]
        self.assertEqual((progress["status"], progress["completed"], progress["total"]), ("completed", 1, 1))
        self.assertEqual(progress["acceptance"]["status"], "pending")
        fx.flow.wave(1, fx.file(fx.assessment("wave")))
        fx.flow.acceptance(fx.file(fx.assessment()))
        view = self.view()
        self.assertEqual(view["requirements"][0]["progress"]["acceptance"]["status"], "passed")
        self.assertEqual(view["waveDetails"][0]["review"]["status"], "passed")
        (fx.root / "app.py").write_text("print('drift')\n", encoding="utf-8")
        view = self.view()
        self.assertEqual(view["requirements"][0]["progress"]["status"], "stale")
        self.assertEqual(view["requirements"][0]["progress"]["acceptance"]["status"], "stale")
        self.assertEqual(view["waveDetails"][0]["review"]["status"], "stale")
        self.assertEqual(view["tasks"][0]["criteriaDetails"][0]["status"], "stale")

    def test_partial_requirement_and_excluded_scope_never_become_completed_early(self):
        state = {"requirements": [{"id": "R01", "scope": "active", "acceptance": ["a"]},
                                  {"id": "R02", "scope": "deferred", "acceptance": ["b"]}]}
        tasks = [{"id": "T01", "requirements": ["R01"], "status": "completed", "proofCurrent": True},
                 {"id": "T02", "requirements": ["R01"], "status": "pending"}]
        details = {"acceptance": {"status": "pending", "evidenceCurrent": False}}
        result = display_details.requirements(state, tasks, details, {})
        self.assertEqual((result["R01"]["status"], result["R01"]["completed"], result["R01"]["total"]), ("partial", 1, 2))
        self.assertEqual(result["R02"]["status"], "excluded")
        self.assertEqual(result["R02"]["acceptance"]["status"], "excluded")

    def test_corrupt_final_report_revokes_current_assessment_without_code_drift(self):
        fx = self.fx
        fx.accepted()
        self.assertTrue(self.view()["remainingAssessed"])
        ref = fx.state["gates"]["acceptance"]["report"]
        (fx.store.run / ref["file"]).write_text("broken", encoding="utf-8")
        view = self.view()
        self.assertFalse(view["remainingAssessed"])
        self.assertEqual(view["gateDetails"]["acceptance"]["status"], "stale")

    def test_failed_requirement_is_distinct_from_passed_core_gate(self):
        fx = self.fx
        fx.prepare(secondary=True); fx.completed()
        fx.flow.block("T02", "Export unavailable")
        fx.flow.wave(1, fx.file(fx.assessment("wave", {"R02": "blocked"})))
        fx.flow.acceptance(fx.file(fx.assessment(failed={"R02": "blocked"})))
        view = self.view()
        self.assertEqual(view["gateDetails"]["acceptance"]["status"], "passed")
        self.assertEqual(view["gateDetails"]["acceptance"]["counts"], {"passed": 1, "failed": 1, "pending": 0})
        self.assertEqual(view["requirements"][1]["progress"]["acceptance"]["status"], "failed")

    def test_fast_wave_is_skipped_and_corrupt_checkpoint_is_stale(self):
        fx = self.fx
        fx.prepare(); fx.completed()
        fx.flow.wave(1, fx.file(fx.assessment("wave")))
        ref = fx.state["waves"]["1"]["report"]
        (fx.store.run / ref["file"]).write_text("broken", encoding="utf-8")
        self.assertEqual(self.view()["waveDetails"][0]["review"]["status"], "stale")
        state = copy.deepcopy(fx.state)
        state.setdefault("policy", {})["leanControl"] = True
        state["checkMode"] = "fast"
        self.assertEqual(display_details.waves(fx.store, state, self.view()["tasks"])[0]["review"]["status"], "skipped")

    def test_wave_pending_running_failed_and_plan_drift(self):
        fx = self.fx
        fx.prepare(); fx.completed()
        self.assertEqual(self.view()["waveDetails"][0]["review"]["status"], "pending")
        fx.state["workProgress"] = {"phase": "wave", "startedAt": fx.state["createdAt"],
                                    "activeSince": fx.state["createdAt"], "elapsedSeconds": 0}
        self.assertEqual(self.view()["waveDetails"][0]["review"]["status"], "running")
        self.assertFalse(fx.flow.wave(1, fx.file(fx.assessment("wave", {"R01": "failed"}))))
        self.assertEqual(self.view()["waveDetails"][0]["review"]["status"], "failed")
        fx.state["tasks"][0]["title"] = "Synthetic plan drift"
        self.assertEqual(self.view()["waveDetails"][0]["review"]["status"], "stale")

    def test_earlier_wave_checkpoint_remains_historical_after_crossing_but_not_after_corruption(self):
        fx = self.fx
        fx.prepare(secondary=True)
        tasks = [{k: t[k] for k in ("id", "title", "requirements", "dependsOn", "zones", "criteria")}
                 for t in fx.state["tasks"]]
        tasks[1]["dependsOn"] = ["T01"]
        fx.flow.plan(fx.file(tasks)); fx.completed()
        fx.flow.wave(1, fx.file(fx.assessment("wave", {"R02": "not_tested"})))
        fx.flow.start("T02", basis(fx.store, fx.state, "task", "T02"))
        review = self.view()["waveDetails"][0]["review"]
        self.assertEqual(review["status"], "passed")
        self.assertFalse(review["basisCurrent"])
        self.assertEqual(review["reason"], "accepted_checkpoint")
        ref = fx.state["waves"]["1"]["report"]
        (fx.store.run / ref["file"]).write_text("broken", encoding="utf-8")
        self.assertEqual(self.view()["waveDetails"][0]["review"]["status"], "stale")

    def test_http_two_completed_pending_null_and_running_clock_returns_fresh_json(self):
        fx = self.fx
        fx.prepare(secondary=True)
        tasks = [{k: t[k] for k in ("id", "title", "requirements", "dependsOn", "zones", "criteria")}
                 for t in fx.state["tasks"]]
        for index in (3, 4):
            tasks.append({"id": "T0" + str(index), "title": "Pending fixture", "requirements": ["R01"],
                          "dependsOn": [], "zones": ["fixture" + str(index) + ".py"], "criteria": ["ready"]})
        fx.flow.plan(fx.file(tasks)); fx.completed("T01"); fx.completed("T02")
        fx.flow.start("T03", basis(fx.store, fx.state, "task", "T03"))
        # One request traverses Journal.load -> display -> estimate in a real
        # detached HTTP service, including the projected null clock from T04.
        service = dashboard_server.start(fx.store)
        try:
            status_url = service["url"].replace("dashboard.html", "status.json")
            requested_url, deadline = status_url, time.monotonic() + 10
            while time.monotonic() < deadline:
                with urlopen(requested_url, timeout=max(.1, deadline - time.monotonic())) as response:
                    view = json.load(response)
                    if response.code == 200:
                        self.assertEqual(view["calculation"]["state"], "ready")
                        break
                    self.assertEqual(response.code, 202)
                    self.assertEqual(view["calculation"]["state"], "calculating")
                    calculation_id = view["calculation"]["id"]
                    self.assertRegex(calculation_id, r"^[0-9a-f]{32}$")
                    requested_url = status_url + "?calculation=" + calculation_id
                time.sleep(.01)
            else:
                self.fail("Addressed dashboard calculation did not become ready within 10 seconds")
            self.assertEqual(view["estimate"]["reason"], "estimate")
            self.assertEqual(view["estimate"]["sampleCount"], 2)
            self.assertIsNone(view["tasks"][3]["elapsedSeconds"])
            self.assertGreaterEqual(view["tasks"][2]["elapsedSeconds"], 0)
            self.assertEqual(view["requirements"][0]["progress"]["completed"], 1)
            self.assertEqual(view["requirements"][0]["progress"]["total"], 3)
        finally:
            dashboard_server.stop(fx.store)


if __name__ == "__main__":
    unittest.main()
