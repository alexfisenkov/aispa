"""Explicit light completion, conservative routing and immutable escalation."""
import copy
import json
import os
import subprocess
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
import test_team
from aispa_core import task_tiers, task_reports, test_intent, integration, planning, display, packets
from aispa_core.contracts import basis, definition, task_basis, validate_plan
from aispa_core.store import Invalid, file_hash
from aispa_core.team import TaskPipeline, direct_proof_current, historical_proof_valid, task_proof_valid
from aispa_core.workflow import Workflow


class TaskTierTests(unittest.TestCase):
    task = test_team.TeamTests.task
    attempt = test_team.TeamTests.attempt
    report = test_team.TeamTests.report
    verify = test_team.TeamTests.verify

    def setUp(self):
        test_team.TeamTests.setUp(self)
        self.state["policy"] = {"taskTiers": True, "adaptiveCritique": True}
        self.state["tasks"] = [self.light_task()]
        self.state["taskHistory"] = {}
        self.state["actors"] = {}
        self.team.prepare()

    tearDown = test_team.TeamTests.tearDown

    def light_task(self, tid="T01", zone="a.txt", deps=None):
        return dict(self.task(tid, "R01", zone, deps), requirements=["R01", "R02", "G01"],
                    tier="light", tierReason="A bounded wording correction", changeKind="documentation",
                    risk="low", riskReason="Plain prose only")

    def start_light(self, tid="T01", real=False):
        self.team.start_guard(tid)
        self.host.task(tid)["status"] = "running"
        self.team.assign(tid, "worker", "fixture", tid + "-writer", "session")
        self.store.append(self.state, "task-started:" + tid)
        if real:
            return Workflow(self.store, self.state)
        self.verify(tid)

    def finish_light(self, tid="T01"):
        self.start_light(tid)
        return self.team.submit(tid, self.report(tid))

    def test_real_verify_intent_submit_is_verified_completion_not_acceptance(self):
        self.state["policy"]["testIntent"] = True
        test_intent.capture_baseline(self.store, self.state, "T01")
        flow = self.start_light(real=True)
        (self.root / "a.txt").write_text("Revised prose\n", encoding="utf-8")
        intent = {"mode": "not_applicable", "attempt": self.attempt()["id"],
                  "basis": task_basis(self.store, self.state, "T01"), "criteria": self.host.task("T01")["criteria"],
                  "notApplicable": [{"criterion": c, "purpose": "docs", "reason": "Only plain prose changes",
                      "source": {"path": "a.txt", "sha256": file_hash(self.root / "a.txt")}}
                     for c in self.host.task("T01")["criteria"]]}
        test_intent.record(self.store, self.state, "T01", intent)
        self.store.append(self.state, "test-intent-recorded:T01")
        self.assertTrue(flow.verify([sys.executable, "-c", "from pathlib import Path; assert Path('a.txt').read_text()=='Revised prose\\n'; print('prose checked')"], 10, "T01"))
        receipt = self.team.submit("T01", self.report())
        self.assertEqual(receipt["completionKind"], "verified_completion")
        self.assertEqual(set(self.attempt()["roles"]), {"worker"})
        self.assertIsNone(self.attempt()["acceptance"])
        self.assertEqual(self.attempt()["rounds"], [])
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))
        self.assertFalse(self.state["gates"].get("acceptance", {}).get("passed", False))

    def test_missing_verify_and_failed_evidence_cannot_complete(self):
        self.start_light()
        self.attempt()["verification"] = None
        with self.assertRaises(Invalid):
            self.team.submit("T01", self.report())
        self.verify()
        path = self.report()
        report = json.loads(path.read_text()); report["checks"][0]["status"] = "failed"
        path.write_text(json.dumps(report))
        with self.assertRaises(Invalid):
            self.team.submit("T01", path)
        self.assertEqual(self.host.task("T01")["status"], "running")

    def test_stale_and_tampered_receipts_are_not_current(self):
        self.finish_light()
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        (self.root / "a.txt").write_text("changed", encoding="utf-8")
        self.assertFalse(direct_proof_current(self.store, self.state, "T01"))
        self.attempt()["verifiedCompletion"]["passed"] = False
        self.assertFalse(historical_proof_valid(self.store, self.state, "T01"))

    def test_light_has_no_task_review_actors_templates_or_completion(self):
        self.start_light()
        for role in ("critic1", "critic2", "acceptor"):
            with self.subTest(role=role), self.assertRaises(Invalid):
                self.team.assign("T01", role, "fixture", role, "session")
            with self.subTest(template=role), self.assertRaises(Invalid):
                task_reports.template(self.team, "T01", role)
        with self.assertRaises(Invalid):
            self.team.accept("T01", self.report())

    def test_invalid_light_risk_kind_and_zones_rejected(self):
        cases = [{"risk": "normal"}, {"changeKind": "behavior"}, {"tierReason": ""},
                 {"zones": ["."]}, {"zones": ["docs"]}, {"zones": ["app.py"]},
                 {"zones": ["AGENTS.md"]}, {"zones": ["api-contract.md"]},
                 {"zones": ["config.css"]}, {"zones": ["src/style.css"]}, {"zones": ["image.svg"]}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(Invalid):
                validate_plan(self.state, [{**self.light_task(), **changes}])
        plain = self.light_task()
        for key in task_tiers.FIELDS:
            plain.pop(key)
        self.assertEqual(validate_plan(self.state, [plain])[0]["tier"], "heavy")

    def test_legacy_definition_and_low_risk_do_not_change_route(self):
        raw = self.light_task()
        for key in task_tiers.FIELDS:
            raw.pop(key)
        legacy = dict(self.state, policy={})
        parsed = validate_plan(legacy, [raw])[0]
        self.assertNotIn("tier", definition(parsed))
        self.state["tasks"] = [parsed]
        self.state["taskHistory"] = {}
        self.team.prepare()
        self.assertEqual(set(self.attempt()["roles"]), {"worker", "critic1", "critic2", "acceptor"})
        with self.assertRaises(Invalid):
            validate_plan(legacy, [self.light_task()])

    def test_started_task_cannot_downgrade_or_rename(self):
        self.start_light()
        renamed = self.light_task(tid="T03")
        with self.assertRaises(Invalid):
            self.team.validate_replan([renamed])
        self.team.promote("T01", "Executable behavior discovered")
        self.assertEqual(self.attempt()["tier"], "heavy")
        changed = dict(self.light_task(), tierReason="Changed reason")
        with self.assertRaises(Invalid):
            self.team.validate_replan([changed])
        with self.assertRaises(Invalid):
            self.team.promote("T01", "Try to renew heavy budget")

    def test_promotion_preserves_old_receipt_and_invalidates_proof(self):
        self.finish_light()
        old = copy.deepcopy(self.attempt())
        self.team.promote("T01", "A behavioral risk appeared")
        self.assertEqual(self.state["taskHistory"]["T01"][0], old)
        self.assertEqual(set(self.attempt()["roles"]), {"worker", "critic1", "critic2", "acceptor"})
        self.assertFalse(self.attempt()["actors"])
        self.assertNotIn("proof", self.host.task("T01"))
        self.team.start_guard("T01")
        self.assertFalse(direct_proof_current(self.store, self.state, "T01"))

    def test_reset_cannot_erase_completed_light(self):
        self.finish_light()
        with self.assertRaises(Invalid):
            self.team.reset_guard("T01")

    def test_out_of_scope_executable_and_link_changes_require_promotion(self):
        self.start_light()
        (self.root / "app.py").write_text("danger = True", encoding="utf-8")
        self.verify()
        with self.assertRaises(Invalid):
            self.team.submit("T01", self.report())
        (self.root / "app.py").unlink()
        if os.name != "nt":
            (self.root / "a.txt").chmod(0o755)
            self.verify()
            with self.assertRaises(Invalid):
                self.team.submit("T01", self.report())

    def test_light_descendant_does_not_reattest_changed_ancestor(self):
        first = self.state["tasks"][0]
        second = self.light_task("T02", "b.txt", ["T01"])
        self.state["tasks"].append(second)
        self.team.prepare()
        self.finish_light("T01")
        self.start_light("T02")
        (self.root / "a.txt").write_text("ancestor changed", encoding="utf-8")
        self.verify("T02")
        with self.assertRaises(Invalid):
            self.team.submit("T02", self.report("T02"))
        self.assertFalse(task_proof_valid(self.store, self.state, "T01"))

    def test_nonpass_worker_never_completes(self):
        self.start_light()
        for extra in ({"verdict": "FAIL"}, {"findings": [{"severity": "warning"}]}):
            with self.subTest(extra=extra), self.assertRaises(Invalid):
                self.team.submit("T01", self.report(**extra))
        self.assertIsNone(self.attempt()["submitted"])
        self.assertNotIn("verifiedCompletion", self.attempt())

    def test_mixed_plan_counts_and_merge_downgrade(self):
        light = self.light_task()
        heavy = dict(self.light_task("T02", "b.txt", ["T01"]), tier="heavy", tierReason="Full review")
        advice = planning.analyze(self.store, self.state, [light, heavy])
        self.assertEqual(advice["taskRoleSlots"], {"minimum": 4, "maximum": 5})
        self.assertEqual(advice["taskTiers"]["T01"]["taskCritics"], {"minimum": 0, "maximum": 0})
        final = dict(light, zones=["a.txt", "b.txt"], criteria=light["criteria"] + heavy["criteria"])
        report = {"basis": advice["basis"], "draft": [light, heavy], "groups": [
            {"task": "T01", "from": ["T01", "T02"], "reason": "One documentation outcome"}], "separate": []}
        with self.assertRaisesRegex(Invalid, "heavy"):
            planning.review(self.store, self.state, [final], report)

    def test_heavy_obligation_cannot_reappear_as_new_light_id(self):
        self.team.promote("T01", "Full review needed")
        extra = self.store.artifact("briefs", b"Continue work with a new attempt", ".md")
        self.state["briefs"].append(dict(extra, id="B002"))
        self.team.new_attempt("T01", "B002", "Continue work with a new attempt")
        with self.assertRaises(Invalid):
            self.team.validate_replan([self.light_task("T03")])

    def test_light_dependency_remains_valid_for_heavy_consumer(self):
        other = dict(self.light_task("T02", "b.txt", ["T01"]), tier="heavy", tierReason="Independent behavior review")
        self.state["tasks"].append(other)
        self.team.prepare()
        self.team.validate_replan(copy.deepcopy(self.state["tasks"]))
        self.finish_light("T01")
        self.team.start_guard("T02")
        self.host.task("T02")["status"] = "running"
        for role in ("worker", "critic1", "acceptor"):
            self.team.assign("T02", role, "fixture", "heavy-" + role, "session")
        self.verify("T02")
        self.team.submit("T02", self.report("T02"))
        self.team.review("T02", 1, self.report("T02", "critic1", verdict="PASS", findings=[]))
        trace = [{"requirement": req["id"], "brief": req["brief"], "quote": req["quote"], "result": "aligned",
                  "note": "Compared fixture", "artifact": self.evidence["file"]} for req in self.team.trace_requirements("T02")]
        self.assertTrue(self.team.accept("T02", self.report("T02", "acceptor", verdict="PASS", trace=trace,
            previousReport=self.attempt("T02")["rounds"][0]["report"]["sha256"], inheritedProofs=self.team.inherited_proofs("T02"))))
        self.assertTrue(task_proof_valid(self.store, self.state, "T01"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T02"))

    def test_cli_verify_submit_and_display_preserve_distinct_completion(self):
        import aispa
        self.start_light()
        self.state["runtimeDigest"] = aispa.runtime_digest()
        for name in ("coverage", "plan"):
            self.state["gates"][name] = {"passed": True, "basis": basis(self.store, self.state, name),
                "report": self.store.artifact("reviews", b"{}", ".json")}
        self.store.append(self.state, "fixture-cli-prepared")
        cli = Path(aispa.__file__)
        command = [sys.executable, "-B", str(cli), "--root", str(self.root)]
        verified = subprocess.run(command + ["verify", "--task", "T01", "--", sys.executable, "-c",
            "from pathlib import Path; assert Path('a.txt').read_text()=='a'; print('checked')"], capture_output=True, text=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        loaded = self.store.load()
        self.state.clear(); self.state.update(loaded)
        submitted = subprocess.run(command + ["submit", "T01", "--file", str(self.report())], capture_output=True, text=True)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        loaded = self.store.load()
        task = display.project(self.store, loaded)["tasks"][0]
        self.assertEqual(task["completionKind"], "verified_completion")
        self.assertEqual(task["perTaskReview"], "skipped_light")
        self.assertEqual(task["maximumCriticRounds"], 0)
        self.assertTrue(task["proofCurrent"])
        packet = packets.build(self.store, loaded, TaskPipeline(test_team.Host(self.store, loaded)), "task", "T01")
        self.assertEqual(packet["maximumCriticRounds"], 0)
        self.assertEqual(packet["perTaskReview"], "skipped_light")
        self.assertEqual(packets.routing(loaded, loaded["tasks"][0], "worker", loaded["taskHistory"]["T01"][-1])["taskRoleSlots"],
                         {"minimum": 1, "maximum": 1})

    def _live_flow(self):
        for name in ("coverage", "plan"):
            self.state["gates"][name] = {"passed": True, "basis": basis(self.store, self.state, name),
                "report": self.store.artifact("reviews", b"{}", ".json")}
        self.store.append(self.state, "fixture-workflow-ready")
        return Workflow(self.store, self.state)

    def _plan_file(self, tasks):
        self.serial += 1
        path = self.home / ("new-plan-" + str(self.serial) + ".json")
        path.write_text(json.dumps(tasks), encoding="utf-8")
        return path

    def test_promoted_started_light_can_expand_plan_before_heavy_assignment_only(self):
        (self.root / "docs").mkdir()
        (self.root / "docs/help.md").write_text("Help", encoding="utf-8")
        self.state["tasks"] = [self.light_task(zone="docs/help.md")]
        self.state["taskHistory"] = {}
        self.team.prepare()
        flow = self._live_flow()
        flow.start("T01", basis(self.store, self.state, "task", "T01"))
        flow.pipeline.assign("T01", "worker", "fixture", "light-started", "session")
        old = copy.deepcopy(self.attempt())
        self.assertIn("startedAt", self.host.task("T01"))
        flow.pipeline.promote("T01", "Help reveals behavior requiring app.py")
        expanded = dict(self.light_task(zone="docs/help.md"), tier="heavy", tierReason="Behavior discovered",
                        changeKind="behavior", risk="normal", zones=["docs/help.md", "app.py"])
        for change in ({"requirements": ["R01", "G01"]}, {"criteria": ["replacement criterion"]},
                       {"zones": ["app.py"]}, {"tier": "light"}):
            with self.subTest(change=change), self.assertRaises(Invalid):
                flow.plan(self._plan_file([{**expanded, **change}]))
        flow.plan(self._plan_file([expanded]))
        self.assertEqual(self.state["taskHistory"]["T01"][0], old)
        self.assertEqual(self.host.task("T01")["zones"], ["docs/help.md", "app.py"])
        self.assertEqual(self.host.task("T01")["tier"], "heavy")
        self.assertTrue(self.attempt()["cardHistory"])
        flow.pipeline.assign("T01", "worker", "fixture", "heavy-started", "session")
        with self.assertRaises(Invalid):
            flow.plan(self._plan_file([{**expanded, "zones": ["docs/help.md", "app.py", "other.py"]}]))
        self.assertEqual(self.state["taskHistory"]["T01"][0], old)

    def test_owner_retry_cannot_remove_heavy_coverage_using_unchanged_light_peer(self):
        heavy = dict(self.light_task(), tier="heavy", tierReason="Behavior reviewed", zones=["app.py"])
        light = self.light_task("T02", "b.txt")
        self.state["tasks"] = [heavy, light]
        self.state["taskHistory"] = {}
        self.team.prepare()
        flow = self._live_flow()
        owner = self.home / "owner.md"
        owner.write_text("Try the heavy task again", encoding="utf-8")
        addition = flow.brief(owner)
        flow.pipeline.new_attempt("T01", addition["id"], "Try the heavy task again")
        flow.seal()
        flow = self._live_flow()
        with self.assertRaisesRegex(Invalid, "heavy"):
            flow.plan(self._plan_file([light]))
        # An unchanged light peer remains legal while heavy coverage survives.
        flow.plan(self._plan_file([heavy, light]))
        self.assertEqual([t["id"] for t in self.state["tasks"]], ["T01", "T02"])

    def test_prompt_and_known_instruction_paths_are_heavy_only(self):
        flow = self._live_flow()
        for zone in ("prompts/system.md", "system_prompt.txt", "instructions.md", "developer.md",
                     "copilot-instructions.md", ".github/instructions/review.md", ".cursor/notes.md",
                     "CODEX.md", "windsurf/instructions.txt", "aider.md", "AGENTS.override.md",
                     "SOUL.md", "HEARTBEAT.md", "IDENTITY.md", "USER.md", "MEMORY.md", "BOOTSTRAP.md"):
            with self.subTest(zone=zone):
                source = dict(self.light_task(), zones=[zone])
                with self.assertRaises(Invalid):
                    flow.plan(self._plan_file([source]))
                parsed = validate_plan(self.state, [{**source, "tier": "heavy"}])
                self.assertEqual(parsed[0]["tier"], "heavy")

    def _peer_heavy_setup(self):
        (self.root / "app.py").write_text("VALUE = 1", encoding="utf-8")
        heavy = dict(self.light_task("T02", "app.py"), tier="heavy", tierReason="Behavior needs review")
        self.state["tasks"].append(heavy)
        self.team.prepare()
        self.start_light("T01")
        self.team.start_guard("T02")
        self.host.task("T02")["status"] = "running"
        for role in ("worker", "critic1", "acceptor"):
            self.team.assign("T02", role, "fixture", "peer-" + role, "session")

    def _complete_heavy_peer(self):
        self.verify("T02")
        self.team.submit("T02", self.report("T02"))
        self.team.review("T02", 1, self.report("T02", "critic1", verdict="PASS", findings=[]))
        trace = [{"requirement": req["id"], "brief": req["brief"], "quote": req["quote"], "result": "aligned",
                  "note": "Compared fixture", "artifact": self.evidence["file"]} for req in self.team.trace_requirements("T02")]
        self.team.accept("T02", self.report("T02", "acceptor", verdict="PASS", trace=trace,
            previousReport=self.attempt("T02")["rounds"][0]["report"]["sha256"], inheritedProofs=[]))
        self.assertTrue(direct_proof_current(self.store, self.state, "T02"))

    def test_light_cannot_exempt_stale_completed_heavy_peer_writes(self):
        self._peer_heavy_setup()
        (self.root / "app.py").write_text("VALUE = 2", encoding="utf-8")
        self._complete_heavy_peer()
        (self.root / "app.py").write_text("VALUE = 3", encoding="utf-8")
        self.assertFalse(direct_proof_current(self.store, self.state, "T02"))
        self.verify("T01")
        with self.assertRaisesRegex(Invalid, "promote-task"):
            self.team.submit("T01", self.report("T01"))
        self.assertEqual(self.host.task("T01")["status"], "running")

    def test_light_exempts_current_completed_heavy_peer_writes(self):
        self._peer_heavy_setup()
        (self.root / "app.py").write_text("VALUE = 2", encoding="utf-8")
        self._complete_heavy_peer()
        self.verify("T01")
        self.team.submit("T01", self.report("T01"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))

    def test_light_retains_deliberate_running_peer_concurrency(self):
        self._peer_heavy_setup()
        (self.root / "app.py").write_text("VALUE = 2", encoding="utf-8")
        self.verify("T01")
        self.team.submit("T01", self.report("T01"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))
        self.assertEqual(self.host.task("T02")["status"], "running")

    def test_promotion_replan_stays_frozen_after_specialization_hides_assigned_actor(self):
        # No startedAt here: the historical actor binding itself must freeze
        # the promoted contract even if team specialization clears actors.
        flow = self._live_flow()
        flow.pipeline.promote("T01", "Need behavioral implementation")
        expanded = dict(self.light_task(), tier="heavy", tierReason="Behavior discovered",
                        changeKind="behavior", risk="normal", zones=["a.txt", "app.py"])
        flow.plan(self._plan_file([expanded]))
        actor = flow.pipeline.assign("T01", "worker", "fixture", "assigned-heavy", "session")
        config = self.home / "specialize.json"
        config.write_text(json.dumps({"tasks": {"T01": {"worker": {"specialization": "Domain worker"}}}}), encoding="utf-8")
        flow.pipeline.team(config)
        self.assertFalse(self.attempt()["actors"])
        self.assertTrue(any(actor in revision.get("actors", {}).values() for revision in self.attempt()["cardHistory"]))
        with self.assertRaisesRegex(Invalid, "заморожены"):
            flow.plan(self._plan_file([{**expanded, "zones": ["a.txt", "app.py", "other.py"]}]))


if __name__ == "__main__":
    unittest.main()
