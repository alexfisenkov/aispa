"""Sealed legacy epoch cards, real local commands, no owner run or model calls."""
import copy
import json
import sys
import unittest
from unittest.mock import patch

import test_task_zone_extension as fixtures
import test_check_modes as mode_fixtures
from aispa_core import check_modes, planning, repairs, test_intent
from aispa_core.contracts import basis, definition, task_basis, task_closure, validate_plan
from aispa_core.store import Invalid
from aispa_core.team import (TaskPipeline, _artifact_json, _card_closure, _history_valid, _role_data,
                             direct_proof_current, historical_proof_valid, task_proof_valid)


class DeferredZoneEpochTests(unittest.TestCase):
    def fixture(self, *, lean=True):
        f = fixtures.ZoneExtensionTests(methodName="test_same_attempt_old_fail_new_red_green_and_second_critic")
        f.lean = True  # Deferral is a max/lean policy; target may retain adaptive review.
        original = TaskPipeline.prepare

        def prepare(pipeline):
            state, store = f.state, f.store
            parent = {**definition(state["tasks"][0]), "id": "T00", "title": "Deferred predecessor",
                      "zones": ["deferred.txt"], "criteria": ["predecessor completed"], "dependsOn": []}
            raw = [parent, *[definition(t) for t in state["tasks"]]]
            raw[1]["dependsOn"] = ["T00"]
            state["tasks"] = validate_plan(state, raw)
            state["planReview"] = planning.review(store, state, state["tasks"], f.review(state["tasks"]))
            state["gates"]["plan"] = {"passed": True, "basis": basis(store, state, "plan"),
                                      "report": store.artifact("plans", json.dumps(raw).encode(), ".json")}
            # The predecessor exercises actual exhaustion/extra-wave guards.
            # Its own synthetic product is unrelated to target RED/GREEN.
            state["policy"]["testIntent"] = False
            original(pipeline)
            self.defer_parent(f)
            state["policy"]["testIntent"] = True
            if not lean:
                state["taskHistory"]["T01"][-1]["reviewPolicy"] = "adaptive"
            pipeline._cards("T01", state["taskHistory"]["T01"][-1])

        with patch.object(TaskPipeline, "prepare", prepare):
            f.setUp()
        self.addCleanup(f.tearDown)
        def proposal(additions):
            tasks = copy.deepcopy(f.state["tasks"])
            next(t for t in tasks if t["id"] == "T01")["zones"].extend(additions)
            return {"attempt": f.attempt["id"], "contract": f.attempt["contract"],
                    "reason": "Existing value criterion needs these exact files",
                    "files": [{"path": p, "requirement": "R01", "reason": "Existing value regression"} for p in additions],
                    "planReview": f.review(tasks)}
        f.proposal = proposal
        return f

    def defer_parent(self, f):
        pipeline, state = f.team, f.state
        for wave in range(2):
            attempt = state["taskHistory"]["T00"][-1]
            pipeline.start_guard("T00")
            f.host.task("T00").update(status="running", phase="implementation")
            (f.root / "deferred.txt").write_text("synthetic predecessor wave " + str(wave))
            for role in ("worker", "critic1"):
                pipeline.assign("T00", role, "fixture", f"synthetic-parent-{wave}-{role}", "synthetic-session")
            flow = fixtures.Workflow(f.store, state)
            self.assertTrue(flow.verify([sys.executable, "-B", "-c", "print('synthetic predecessor check')"], 10, "T00"))

            def report(role, **extra):
                evidence = attempt["verification"]["output"]["file"]
                value = {"basis": task_basis(f.store, state, "T00"), "reviewer": attempt["actors"][role],
                         "independent": role != "worker", "unmapped": [],
                         "checks": [{"requirement": r, "criterion": c, "status": "passed",
                                     "note": "Synthetic real command", "artifact": evidence}
                                    for r, c in sorted(pipeline.expected_checks("T00", role))], **extra}
                if role.startswith("critic"):
                    value.update(inheritedProofs=[], trace=[{"requirement": "R01", "brief": "B001",
                        "quote": "Set VALUE and EXTRA to one.", "result": "aligned",
                        "note": "Synthetic parent source", "artifact": evidence}])
                return self.report_path(f, value)

            pipeline.submit("T00", report("worker"))
            finding = [{"id": "F-PARENT", "severity": "blocking", "criterion": "predecessor completed", "text": "Synthetic refusal"}]
            self.assertFalse(pipeline.review("T00", 1, report("critic1", verdict="FAIL", findings=finding)))
            (f.root / "deferred.txt").write_text("synthetic predecessor repair " + str(wave))
            self.assertTrue(flow.verify([sys.executable, "-B", "-c", "print('synthetic predecessor repaired check')"], 10, "T00"))
            repairs.record(f.store, state, "T00", {"basis": repairs.basis(state, "T00"), "cause": "implementation_gap",
                "diagnosis": "Synthetic predecessor refusal", "route": "same", "reason": "Fixture repair",
                "changes": "Retry synthetic predecessor", "addresses": ["F-PARENT"], "context": []})
            f.host.save("repair-diagnosed:T00")
            first = attempt["rounds"][0]
            receipt = pipeline.repair("T00", report("worker", previousReport=first["report"]["sha256"], addresses=["F-PARENT"]))
            pipeline.assign("T00", "critic2", "fixture", f"synthetic-parent-{wave}-critic2", "synthetic-session")
            self.assertFalse(pipeline.review("T00", 2, report("critic2", verdict="FAIL", findings=finding,
                previousReport=first["report"]["sha256"], repairReceipt=receipt["report"]["sha256"],
                resolutions=[{"id": "F-PARENT", "status": "resolved", "note": "Old refusal addressed; fresh refusal remains",
                              "artifact": attempt["verification"]["output"]["file"]}])))
            if wave == 0:
                pipeline.extra_wave("T00")
        self.assertTrue(check_modes.is_deferred(state, f.host.task("T00"), f.store))

    @staticmethod
    def report_path(f, value):
        path = f.root / ".aispa/synthetic-parent.json"
        path.write_text(json.dumps(value))
        return path

    def extend_twice(self, f, *, legacy=False):
        original = TaskPipeline._closure
        def closure(pipeline, tid, skip=None):
            return task_closure(pipeline.s, tid) if pipeline is f.team else original(pipeline, tid, skip)
        for names in (["extra.py", "tests/test_extra.py"], ["third.py"]):
            with patch.object(TaskPipeline, "_closure", closure) if legacy else patch.object(TaskPipeline, "_closure", original):
                f.extend(f.proposal(names))
            if names[0] == "extra.py":
                (f.root / "tests/test_app.py").write_text("import runpy\nassert runpy.run_path('extra.py')['EXTRA'] == 1, 'EXTRA must equal one'\n")
                f.intent(["tests/test_app.py"], "EXTRA must equal one")

    def test_two_epoch_producer_keeps_declared_deferral_and_accepted_descendant(self):
        f = self.fixture()
        before = copy.deepcopy(f.state["taskHistory"]["T00"])
        self.extend_twice(f)
        card = json.loads(f.store.text(f.attempt["roles"]["critic2"]))
        self.assertEqual(card["deferredDependencies"], ["T00"])
        self.assertEqual([t["id"] for t in card["closure"]], ["T01"])
        f.finish_repair()
        for i in range(2): f.host.save("later-append:" + str(i))
        reloaded = f.store.load()
        self.assertEqual(reloaded["taskHistory"]["T00"], before)
        self.assertEqual(len(f.attempt["zoneExtensions"]), 2)
        self.assertEqual(len(f.attempt["rounds"]), 2)
        self.assertTrue(_history_valid(f.store, reloaded, "T01"))
        self.assertTrue(task_proof_valid(f.store, reloaded, "T01"))

    def test_legacy_sealed_full_closure_readback_without_rewriting_any_artifact(self):
        for lean in (True, False):
            with self.subTest(lean=lean):
                f = self.fixture(lean=lean)
                self.extend_twice(f, legacy=True)
                f.finish_repair()
                if not lean:
                    f.team.assign("T01", "acceptor", "fixture", "synthetic-acceptor", "synthetic-session")
                    self.assertTrue(f.team.accept("T01", f.report("acceptor", verdict="PASS", inheritedProofs=[],
                        previousReport=f.attempt["rounds"][-1]["report"]["sha256"],
                        trace=[{"requirement": "R01", "brief": "B001", "quote": "Set VALUE and EXTRA to one.",
                                "result": "aligned", "note": "Checked source", "artifact": f.attempt["verification"]["output"]["file"]}])))
                f.host.save("later-append")
                before = {p.relative_to(f.store.run).as_posix(): p.read_bytes() for p in f.store.run.rglob("*") if p.is_file()}
                reloaded = f.store.load()
                self.assertTrue(historical_proof_valid(f.store, reloaded, "T01"))
                self.assertTrue(direct_proof_current(f.store, reloaded, "T01"))
                self.assertEqual(before, {p.relative_to(f.store.run).as_posix(): p.read_bytes() for p in f.store.run.rglob("*") if p.is_file()})

    def test_declared_deferral_must_match_attempt_and_original_epoch_card(self):
        f = self.fixture()
        self.extend_twice(f, legacy=True)
        ref = f.attempt["roles"]["critic2"]
        original = json.loads(f.store.text(ref))
        for declaration in ([], ["T00", "T00"], ["T01"], "T00", ["T00", "T99"]):
            with self.subTest(declaration=declaration):
                card = {**original, "deferredDependencies": declaration}
                f.attempt["roles"]["critic2"] = {**_artifact_json(f.store, "roles", card),
                    "checks": ref["checks"], "specialization": ref["specialization"]}
                f.attempt["zoneExtensions"][-1]["after"]["roles"]["critic2"] = f.attempt["roles"]["critic2"]
                with self.assertRaises(Invalid): _role_data(f.store, f.attempt, "critic2")
        f.attempt["deferredDependencies"] = ["T00", "T99"]
        with self.assertRaisesRegex(Invalid, "исходной карточки"):
            _role_data(f.store, f.attempt, "critic2")
        f.attempt["deferredDependencies"] = ["T00"]
        f.attempt["roles"]["critic2"] = ref
        f.attempt["zoneExtensions"][-1]["after"]["roles"]["critic2"] = ref
        self.assertTrue(_history_valid(f.store, f.state, "T01"))

    def test_only_declared_predecessors_are_omitted_and_target_cannot_be_deferred(self):
        card = {"task": "T03", "closure": [{"id": tid} for tid in ("T01", "T02", "T03")],
                "deferredDependencies": ["T01"]}
        self.assertEqual([t["id"] for t in _card_closure(card, "T03")], ["T02", "T03"])
        with self.assertRaises(Invalid): _card_closure({**card, "deferredDependencies": ["T03"]}, "T03")

    def test_worker_only_historical_reader_honors_the_same_sealed_declaration(self):
        f = mode_fixtures.DeferredDependencyTests(methodName="test_descendant_is_built_without_deferred_criteria")
        f.setUp()
        self.addCleanup(f.tearDown)
        f.defer("T01")
        original = TaskPipeline._closure
        def legacy(pipeline, tid, skip=None):
            return task_closure(pipeline.s, tid) if pipeline is f.team and tid == "T02" else original(pipeline, tid, skip)
        # Exercise the shared worker reader against a sealed card of the same
        # legacy shape. Worker-only tasks cannot themselves acquire zone epochs.
        with patch.object(TaskPipeline, "_closure", legacy):
            f.team.switch_check_mode("fast", mode_fixtures.FAST_WORDS)
        f.begin("T02", "worker")
        f.team.submit("T02", f.report("T02"))
        self.assertTrue(historical_proof_valid(f.store, f.store.load(), "T02"))
        self.assertTrue(direct_proof_current(f.store, f.store.load(), "T02"))


if __name__ == "__main__":
    unittest.main()
