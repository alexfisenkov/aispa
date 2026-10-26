"""Per-source RED revisions through the existing public recorder.

All command receipts below come from Workflow.verify in disposable repositories.
The initial unsupported-policy failure is a recorder assertion, not an import of
the proposed implementation. These fixtures never edit an active AISpa run.
"""
import copy
import json
from pathlib import Path
import sys
import unittest

import test_test_intent as fixture
import test_team as team_fixture
from aispa_core import executions, test_intent
from aispa_core.contracts import task_basis
from aispa_core.store import Invalid, digest, file_hash


CRITERIA = ["Existing failure boundaries stay checked", "All current sources pass"]
BOUNDARIES = ["input", "output", "storage", "protocol", "dependency"]
INITIAL_REFUSAL = "incremental RED must admit five real per-source bindings"


class IncrementalTestIntentTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.TestIntentTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.root, self.store, self.state = self.fx.root, self.fx.store, self.fx.state
        self.flow = self.fx.flow
        self.fx.test_file.unlink()
        self.state["policy"].update(incrementalTestIntent=True, mixedEvidence=True,
                                    errorBoundaryChecks=True)
        self.task = self.state["tasks"][0]
        self.task["criteria"] = list(CRITERIA)
        self.values = {boundary: 0 for boundary in BOUNDARIES}
        self.rows = [self.source(boundary) for boundary in BOUNDARIES]
        self.write_app()
        # Configure this isolated fixture before its first contract. This is not
        # a migration of an already-started production attempt or its evidence.
        self.attempt.pop("testBaseline", None)
        test_intent.capture_baseline(self.store, self.state, "T01")
        self.store.append(self.state, "incremental-fixture-configured")

    @property
    def attempt(self):
        return self.state["taskHistory"]["T01"][-1]

    def write_app(self):
        (self.root / "app.py").write_text("VALUES = " + repr(self.values) + "\n",
                                          encoding="utf-8")

    def source(self, name, expected=1, *, path=None, message=None):
        path = path or "tests/test_" + name + ".py"
        failure = message or path + ": expected " + name + " equals " + str(expected)
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("import runpy\n"
                        "scope = runpy.run_path('app.py')\n"
                        "assert scope['VALUES'].get(" + repr(name) + ", 0) == "
                        + str(expected) + ", " + repr(failure) + "\n", encoding="utf-8")
        return {"path": path, "sha256": file_hash(file), "criteria": list(CRITERIA),
                "expectedFailure": failure}

    @staticmethod
    def semantic(row, report):
        cases = [{key: case[key] for key in
                  ("id", "boundary", "scenario", "expected", "criterion", "kind")}
                 for case in report["failureChecks"]["cases"]
                 if case.get("kind") == "automated"
                 and case.get("test") == {key: row[key] for key in ("path", "sha256")}]
        return digest({**{key: row[key] for key in ("path", "sha256", "criteria", "expectedFailure")},
                       "failureCases": sorted(cases, key=lambda case: case["id"])})

    def execute(self, paths, *, success=False, command=None, timeout=10, task="T01"):
        if command is None:
            # Run every requested source even if another fails. Successful paths
            # are emitted only after actual execution, never as expected errors.
            program = ("import runpy,sys,traceback\nfailed=False\n"
                       "for path in sys.argv[1:]:\n"
                       " try:\n  runpy.run_path(path,run_name='__main__')\n"
                       "  print('PASS '+path)\n"
                       " except BaseException:\n  failed=True\n  traceback.print_exc()\n"
                       "sys.exit(1 if failed else 0)\n")
            command = [sys.executable, "-B", "-c", program, *paths]
        self.assertEqual(self.flow.verify(command, timeout, task), success)
        ref = executions.record(self.store, self.state, kind="task", purpose="check", task_id=task)
        self.store.append(self.state, "execution-recorded")
        return ref

    def red(self, row, **options):
        ref = self.execute([row["path"]], **options)
        body = executions.resolve(self.store, self.state, ref)
        self.assertIn(row["expectedFailure"], self.store.text(body["output"]))
        return ref

    def report(self, rows):
        return {"mode": "mixed", "attempt": self.attempt["id"],
                "basis": task_basis(self.store, self.state, "T01"),
                "criteria": list(CRITERIA), "newTestFiles": copy.deepcopy(rows),
                "routes": [{"criterion": c, "kind": "automated",
                            "reason": "Executable source-bound regression",
                            "procedure": "Run all current declared test sources",
                            "expected": "Every declared assertion passes"} for c in CRITERIA],
                "failureChecks": {"cases": [
                    {"id": "E" + str(i + 1), "boundary": boundary,
                     "scenario": "Reject invalid " + boundary + " fixture value",
                     "expected": "Assertion refuses the invalid value",
                     "criterion": CRITERIA[0], "kind": "automated",
                     "test": {key: rows[i][key] for key in ("path", "sha256")}}
                    for i, boundary in enumerate(BOUNDARIES)], "notApplicable": []}}

    def save(self, report, *, initial=False):
        try:
            ref = test_intent.record(self.store, self.state, "T01", report)
        except Invalid as error:
            if initial:
                self.fail(INITIAL_REFUSAL + ": " + str(error))
            raise
        self.store.append(self.state, "test-intent-recorded:T01")
        return ref

    def initial(self):
        rows = [dict(row, redExecution=self.red(row)) for row in self.rows]
        report = self.report(rows)
        ref = self.save(report, initial=True)
        self.assertEqual(self.store.json(ref)["schema"], 2)
        return ref, report

    def implement(self, *, extra=None):
        self.values.update({boundary: 1 for boundary in BOUNDARIES})
        if extra:
            self.values.update(extra)
        self.write_app()

    def green(self, rows):
        return self.execute([row["path"] for row in rows], success=True)

    def inherited(self, ref, rows):
        previous_report = self.store.json(ref)["report"]
        return [{**{key: row[key] for key in ("path", "sha256", "criteria", "expectedFailure")},
                 "redFrom": {"intent": copy.deepcopy(ref), "path": row["path"],
                             "sha256": row["sha256"], "semanticSha256": self.semantic(row, previous_report)}}
                for row in rows]

    def ready_revision(self):
        first, original = self.initial()
        self.implement()
        self.green(original["newTestFiles"])
        self.assertTrue(test_intent.valid(self.store, self.state, "T01"))
        sixth = self.source("repair")
        red = self.red(sixth)
        report = self.report([*self.inherited(first, original["newTestFiles"]),
                              dict(sixth, redExecution=red)])
        return first, original, sixth, report

    def refuse(self, report):
        before_state = digest(self.state)
        before_journal = self.store.read_journal()["last"]["hash"]
        with self.assertRaises(Invalid):
            test_intent.record(self.store, self.state, "T01", report)
        self.assertEqual(digest(self.state), before_state)
        self.assertEqual(self.store.read_journal()["last"]["hash"], before_journal)

    def test_five_unchanged_bindings_and_one_new_red_then_complete_green(self):
        first, original, sixth, report = self.ready_revision()
        working_app = (self.root / "app.py").read_bytes()
        old_body = (self.store.run / first["file"]).read_bytes()
        second = self.save(report)
        self.assertEqual((self.root / "app.py").read_bytes(), working_app)
        self.assertEqual((self.store.run / first["file"]).read_bytes(), old_body)
        saved = self.store.json(second)
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(len(saved["redBindings"]), 6)
        old_ids = [row["redExecutionId"] for row in self.store.json(first)["redBindings"]]
        self.assertEqual([row["redExecutionId"] for row in saved["redBindings"][:5]], old_ids)
        self.assertEqual(saved["redBindings"][-1]["redExecutionId"], report["newTestFiles"][-1]["redExecution"]["id"])
        self.assertFalse(test_intent.valid(self.store, self.state, "T01"))
        self.implement(extra={"repair": 1})
        self.green(report["newTestFiles"])
        self.assertTrue(test_intent.valid(self.store, self.state, "T01"))
        self.assertTrue(test_intent.history_valid(self.store, self.state, "T01"))
        self.assertEqual(test_intent.view(self.store, self.state, "T01")["sourceCount"], 6)
        self.assertEqual(self.attempt["testIntentHistory"], [first, second])
        self.assertEqual(self.attempt["id"], "T01-A01")

    def test_unchanged_pre_submit_revision_uses_exact_previous_binding(self):
        first, original = self.initial()
        report = self.report(self.inherited(first, original["newTestFiles"]))
        second = self.save(report)
        third_report = self.report(self.inherited(second, report["newTestFiles"]))
        third = self.save(third_report)
        self.assertEqual(self.store.json(third)["revision"], 3)
        stale_predecessor = self.report(self.inherited(first, original["newTestFiles"]))
        self.refuse(stale_predecessor)

    def test_semantic_only_change_requires_new_red_even_with_same_source_sha(self):
        first, original = self.initial()
        changed = self.report(self.inherited(first, original["newTestFiles"]))
        changed["newTestFiles"][0]["criteria"] = [CRITERIA[0]]
        self.refuse(changed)
        # The exact same bytes really fail again after revision one; only the
        # criterion assignment changes, so this is a new semantic binding.
        row = copy.deepcopy(original["newTestFiles"][0])
        row["criteria"] = [CRITERIA[0]]
        row["redExecution"] = self.red(row)
        changed["newTestFiles"][0] = row
        self.save(changed)

    def test_expected_failure_only_change_cannot_reuse_prior_red(self):
        first, original = self.initial()
        changed = self.report(self.inherited(first, original["newTestFiles"]))
        changed["newTestFiles"][0]["expectedFailure"] += " altered semantics"
        changed["newTestFiles"][0]["redFrom"]["semanticSha256"] = self.semantic(changed["newTestFiles"][0], changed)
        self.refuse(changed)

    def test_criteria_order_is_part_of_the_exact_semantic_binding(self):
        first, original = self.initial()
        changed = self.report(self.inherited(first, original["newTestFiles"]))
        changed["newTestFiles"][0]["criteria"].reverse()
        self.refuse(changed)

    def test_source_local_new_failure_case_needs_fresh_red_with_unchanged_bytes(self):
        first, original = self.initial()
        changed = self.report(self.inherited(first, original["newTestFiles"]))
        new_case = dict(copy.deepcopy(changed["failureChecks"]["cases"][0]),
                        id="E06", criterion=CRITERIA[1],
                        scenario="Reject invalid input before complete source acceptance")
        changed["failureChecks"]["cases"].append(new_case)
        self.refuse(changed)
        row = copy.deepcopy(original["newTestFiles"][0])
        row["redExecution"] = self.red(row)
        changed["newTestFiles"][0] = row
        second = self.save(changed)
        bindings = self.store.json(second)["redBindings"]
        self.assertNotEqual(bindings[0]["semanticSha256"], self.store.json(first)["redBindings"][0]["semanticSha256"])
        self.assertEqual([row["redExecutionId"] for row in bindings[1:]],
                         [row["redExecutionId"] for row in self.store.json(first)["redBindings"][1:]])

    def test_manual_route_and_unrelated_manual_case_do_not_rebind_automated_sources(self):
        first, original = self.initial()
        changed = self.report(self.inherited(first, original["newTestFiles"]))
        changed["routes"].append(dict(changed["routes"][0], kind="process",
                                     procedure="Inspect the documented handoff"))
        changed["failureChecks"]["cases"].append({"id": "M01", "boundary": "input",
            "scenario": "Inspect rejection in the operator procedure", "expected": "Refusal is visible",
            "criterion": CRITERIA[0], "kind": "process",
            "source": {"path": "README.md", "sha256": file_hash(self.root / "README.md")}})
        second = self.save(changed)
        self.assertEqual([row["semanticSha256"] for row in self.store.json(second)["redBindings"]],
                         [row["semanticSha256"] for row in self.store.json(first)["redBindings"]])

    def test_changed_source_sha_requires_its_new_addressed_red(self):
        first, original = self.initial()
        changed = self.report(self.inherited(first, original["newTestFiles"]))
        row = self.source("input", expected=2)
        changed["newTestFiles"][0].update({key: row[key] for key in row})
        changed["failureChecks"]["cases"][0]["test"]["sha256"] = row["sha256"]
        changed["basis"] = task_basis(self.store, self.state, "T01")
        self.refuse(changed)
        changed["newTestFiles"][0] = dict(row, redExecution=self.red(row))
        changed["basis"] = task_basis(self.store, self.state, "T01")
        self.save(changed)

    def test_old_source_criterion_route_and_failure_boundary_cannot_disappear(self):
        first, original, sixth, valid = self.ready_revision()
        mutations = [lambda r: r["newTestFiles"].pop(0),
                     lambda r: r["criteria"].pop(),
                     lambda r: r["routes"].pop(),
                     lambda r: r["failureChecks"]["cases"].pop(),
                     lambda r: r["failureChecks"]["cases"][0].update(scenario="weaker replacement"),
                     lambda r: r["routes"][0].update(procedure="weaker replacement")]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                report = copy.deepcopy(valid)
                mutate(report)
                self.refuse(report)

    def test_foreign_attempt_task_and_unsealed_receipt_are_rejected(self):
        first, original, sixth, valid = self.ready_revision()
        wrong_attempt = copy.deepcopy(valid)
        wrong_attempt["attempt"] = "T01-A02"
        self.refuse(wrong_attempt)
        self.state["tasks"].append(dict(self.task, id="T02", title="Foreign task", zones=["app.py", "tests"]))
        self.state["taskHistory"]["T02"] = [{"id": "T02-A01", "roles": {}, "actors": {},
            "rounds": [], "submitted": None, "acceptance": None, "terminal": False}]
        self.store.append(self.state, "foreign-task-fixture")
        other = self.execute([sixth["path"]], task="T02")
        valid["basis"] = task_basis(self.store, self.state, "T01")
        foreign = copy.deepcopy(valid)
        foreign["newTestFiles"][-1]["redExecution"] = other
        self.refuse(foreign)
        fake = copy.deepcopy(valid)
        fake["newTestFiles"][-1]["redExecution"] = self.store.artifact("executions", b"{}", ".json")
        self.refuse(fake)

    def test_predecessor_from_another_attempt_is_not_reusable(self):
        first, original = self.initial()
        self.state["taskHistory"]["T01"].append({"id": "T01-A02", "roles": {}, "actors": {},
            "rounds": [], "submitted": None, "acceptance": None, "terminal": False})
        test_intent.capture_baseline(self.store, self.state, "T01")
        self.store.append(self.state, "second-attempt-fixture")
        self.refuse(self.report(self.inherited(first, original["newTestFiles"])))

    def test_wrong_predecessor_sha_semantic_or_source_identity_is_rejected(self):
        first, original, sixth, valid = self.ready_revision()
        for key, value in (("sha256", "0" * 64), ("semanticSha256", "0" * 64),
                           ("path", "tests/test_unrelated.py")):
            with self.subTest(field=key):
                wrong = copy.deepcopy(valid)
                wrong["newTestFiles"][0]["redFrom"][key] = value
                self.refuse(wrong)
        wrong = copy.deepcopy(valid)
        wrong["newTestFiles"][0]["redFrom"]["intent"]["sha256"] = "0" * 64
        self.refuse(wrong)

    def test_stale_red_candidate_is_rejected(self):
        first, original, sixth, report = self.ready_revision()
        self.values["unrelated"] = 1
        self.write_app()
        report["basis"] = task_basis(self.store, self.state, "T01")
        self.refuse(report)

    def test_fabricated_receipt_body_has_no_executor_event(self):
        first, original, sixth, report = self.ready_revision()
        real = executions.resolve(self.store, self.state, report["newTestFiles"][-1]["redExecution"])
        fabricated = copy.deepcopy(real)
        fabricated["journal"]["sequence"] -= 1
        fabricated["id"] = digest({k: v for k, v in fabricated.items() if k != "id"})
        ref = self.store.artifact("executions", json.dumps(fabricated).encode(), ".json")
        report["newTestFiles"][-1]["redExecution"] = dict(ref, id=fabricated["id"])
        self.refuse(report)

    def test_real_command_mutation_cannot_be_red(self):
        first, original = self.initial()
        sixth = self.source("repair")
        program = ("from pathlib import Path\nimport runpy\n"
                   "Path('app.py').write_text(Path('app.py').read_text()+'# mutated\\n')\n"
                   "runpy.run_path(" + repr(sixth["path"]) + ",run_name='__main__')\n")
        ref = self.execute([], command=[sys.executable, "-B", "-c", program])
        self.assertTrue(executions.resolve(self.store, self.state, ref)["changedCode"])
        report = self.report([*self.inherited(first, original["newTestFiles"]), dict(sixth, redExecution=ref)])
        self.refuse(report)

    def test_real_technical_failure_cannot_be_red(self):
        first, original = self.initial()
        sixth = self.source("repair")
        ref = self.execute([], command=["aispa-no-such-incremental-fixture-command", sixth["path"]])
        self.assertTrue(executions.resolve(self.store, self.state, ref)["launchError"])
        report = self.report([*self.inherited(first, original["newTestFiles"]), dict(sixth, redExecution=ref)])
        self.refuse(report)

    def test_wrong_failure_and_source_path_cannot_borrow_other_red(self):
        first, original, sixth, valid = self.ready_revision()
        wrong = copy.deepcopy(valid)
        wrong["newTestFiles"][-1]["expectedFailure"] = "a failure absent from the actual command output"
        self.refuse(wrong)
        unrelated = self.source("unrelated")
        wrong = copy.deepcopy(valid)
        wrong["newTestFiles"][-1].update(unrelated)
        wrong["basis"] = task_basis(self.store, self.state, "T01")
        self.refuse(wrong)

    def test_new_source_outside_lawful_write_scope_is_rejected(self):
        first, original = self.initial()
        undeclared = self.source("repair", path="outside/test_repair.py")
        # verify's candidate legitimately omits outside/. A plausible output is
        # insufficient to confer write scope on this newly declared source.
        red = self.red(undeclared)
        report = self.report([*self.inherited(first, original["newTestFiles"]),
                              dict(undeclared, redExecution=red)])
        self.refuse(report)

    def test_repair_revision_keeps_original_submission_and_critic_intents(self):
        _fx, first, original, _report = self.reviewed_pipeline()
        before_submit, before_critic = copy.deepcopy(self.attempt["submitted"]), copy.deepcopy(self.attempt["rounds"][0])
        sixth = self.source("repair")
        report = self.report([*self.inherited(first, original["newTestFiles"]), dict(sixth, redExecution=self.red(sixth))])
        second = self.save(report)
        self.assertEqual(self.attempt["submitted"], before_submit)
        self.assertEqual(self.attempt["rounds"][0], before_critic)
        self.assertEqual(self.attempt["testIntent"], second)
        self.assertEqual(len(self.attempt["rounds"]), 1)
        self.assertFalse(self.attempt["terminal"])
        self.implement(extra={"repair": 1})
        self.green(report["newTestFiles"])
        self.assertTrue(test_intent.valid(self.store, self.state, "T01"))
        self.assertTrue(test_intent.history_valid(self.store, self.state, "T01"))

    def reviewed_pipeline(self):
        """Supported Team APIs and real command receipts through first non-PASS."""
        fx = team_fixture.TeamTests()
        fx.setUp()
        self.addCleanup(fx.tearDown)
        self.root, self.store, self.state = fx.root, fx.store, fx.state
        self.flow = self.fx.flow.__class__(self.store, self.state)
        self.task = self.state["tasks"][0]
        self.task.update(zones=["app.py", "tests"], criteria=list(CRITERIA))
        self.state.setdefault("policy", {}).update(testIntent=True, incrementalTestIntent=True,
                                                   mixedEvidence=True, errorBoundaryChecks=True)
        self.rows = [self.source(boundary) for boundary in BOUNDARIES]
        self.write_app()
        fx.team.prepare()
        fx.team.start_guard("T01")
        self.task["status"] = "running"
        # The second critic is genuinely assigned only after repair submission.
        for role in ("worker", "critic1", "acceptor"):
            fx.team.assign("T01", role, "fixture", "incremental-" + role, "session")
        test_intent.capture_baseline(self.store, self.state, "T01")
        self.store.append(self.state, "incremental-team-fixture-started")
        first, original = self.initial()
        self.implement()
        self.green(original["newTestFiles"])

        def report(role="worker", **extra):
            fx.evidence = copy.deepcopy(self.attempt["verification"]["output"])
            routes = [{"criterion": c, "kind": "automated", "status": "passed",
                       "note": "Actual complete fixture GREEN", "artifact": fx.evidence["file"]}
                      for c in CRITERIA]
            return fx.report(role=role, routeChecks=routes, **extra)

        fx.team.submit("T01", report())
        fx.team.review("T01", 1, report("critic1", findings=[{
            "id": "F1", "severity": "blocking", "text": "Add the missing negative case",
            "criterion": CRITERIA[0]}]))
        return fx, first, original, report

    def test_real_pipeline_repair_and_second_critic_bind_their_respective_intents(self):
        fx, first, original, report = self.reviewed_pipeline()
        original_submission = copy.deepcopy(self.attempt["submitted"])
        original_critic = copy.deepcopy(self.attempt["rounds"][0])
        sixth = self.source("repair")
        revised = self.report([*self.inherited(first, original["newTestFiles"]),
                               dict(sixth, redExecution=self.red(sixth))])
        second = self.save(revised)
        self.implement(extra={"repair": 1})
        self.green(revised["newTestFiles"])
        fx.team.repair("T01", report(previousReport=original_critic["report"]["sha256"], addresses=["F1"]))
        fx.team.assign("T01", "critic2", "fixture", "incremental-critic2", "session")
        fx.team.review("T01", 2, report("critic2", findings=[],
            previousReport=original_critic["report"]["sha256"],
            repairReceipt=self.attempt["repair"]["report"]["sha256"], resolutions=[{
                "id": "F1", "status": "resolved", "note": "New negative case and full GREEN checked",
                "artifact": self.attempt["verification"]["output"]["file"]}]))
        self.assertEqual(self.attempt["submitted"], original_submission)
        self.assertEqual(self.attempt["rounds"][0], original_critic)
        self.assertEqual(self.attempt["submitted"]["testIntent"], first)
        self.assertEqual(self.attempt["rounds"][0]["testIntent"], first)
        self.assertEqual(self.attempt["repair"]["testIntent"], second)
        self.assertEqual(self.attempt["rounds"][1]["testIntent"], second)
        fx.accept(routeChecks=[{"criterion": c, "kind": "automated", "status": "passed",
            "note": "All current fixture sources checked", "artifact": self.attempt["verification"]["output"]["file"]}
            for c in CRITERIA])
        self.assertTrue(team_fixture.historical_proof_valid(self.store, self.state, "T01"))
        self.assertTrue(team_fixture.direct_proof_current(self.store, self.state, "T01"))

    def test_revision_is_frozen_after_first_pass_repair_submission_or_critic2(self):
        first, original, sixth, report = self.ready_revision()
        for boundary in ("submitted-awaiting-first-critic", "first-pass", "repair-submitted", "critic2-assigned", "critic2-finished", "terminal"):
            with self.subTest(boundary=boundary):
                previous = copy.deepcopy(self.attempt)
                if boundary == "submitted-awaiting-first-critic":
                    self.attempt["submitted"] = {"fixture": "awaiting first critic"}
                elif boundary == "first-pass":
                    self.attempt["rounds"] = [{"number": 1, "passed": True}]
                else:
                    self.attempt["rounds"] = [{"number": 1, "passed": False}]
                    if boundary == "repair-submitted":
                        self.attempt["repair"] = {"fixture": "submitted repair"}
                    if boundary == "critic2-assigned":
                        self.attempt["actors"]["critic2"] = "fixture second critic"
                    if boundary == "critic2-finished":
                        self.attempt["rounds"].append({"number": 2, "passed": False})
                    if boundary == "terminal":
                        self.attempt["terminal"] = True
                self.store.append(self.state, "revision-boundary-fixture:" + boundary)
                self.refuse(report)
                self.state["taskHistory"]["T01"][-1] = previous
                self.store.append(self.state, "revision-boundary-fixture-restored")

    def test_old_intent_and_red_output_tampering_invalidate_history(self):
        first, original, sixth, report = self.ready_revision()
        self.save(report)
        path = self.store.run / first["file"]
        raw = path.read_bytes()
        path.write_bytes(raw + b" ")
        self.assertFalse(test_intent.history_valid(self.store, self.state, "T01"))
        path.write_bytes(raw)
        self.assertTrue(test_intent.history_valid(self.store, self.state, "T01"))
        receipt = executions.resolve(self.store, self.state, original["newTestFiles"][0]["redExecution"])
        output = self.store.run / receipt["output"]["file"]
        output.write_bytes(output.read_bytes() + b"tampered\n")
        self.assertFalse(test_intent.history_valid(self.store, self.state, "T01"))

    def test_current_green_cannot_hide_changed_source_or_partial_source_run(self):
        first, original, sixth, report = self.ready_revision()
        self.save(report)
        self.implement(extra={"repair": 1})
        self.green([report["newTestFiles"][-1]])
        self.assertFalse(test_intent.valid(self.store, self.state, "T01"),
                         "GREEN must execute all current declared sources")
        self.green(report["newTestFiles"])
        self.assertTrue(test_intent.valid(self.store, self.state, "T01"))
        path = self.root / original["newTestFiles"][0]["path"]
        path.write_text(path.read_text(encoding="utf-8") + "# changed after GREEN\n", encoding="utf-8")
        self.assertFalse(test_intent.valid(self.store, self.state, "T01"))


class IncrementalLegacyCompatibilityTests(unittest.TestCase):
    def test_old_policy_retains_schema1_and_its_single_red_contract(self):
        fx = fixture.TestIntentTests()
        fx.setUp()
        self.addCleanup(fx.tearDown)
        failure = "tests/test_app.py: expected VALUE equals one"
        fx.test_file.write_text("assert VALUE == 1, " + repr(failure) + "\n", encoding="utf-8")
        command = [sys.executable, "-B", "-c", "import runpy; scope=runpy.run_path('app.py'); "
                   "exec(compile(open('tests/test_app.py').read(),'tests/test_app.py','exec'),scope)"]
        self.assertFalse(fx.flow.verify(command, 10, "T01"))
        first_red = executions.record(fx.store, fx.state, kind="task", purpose="check", task_id="T01")
        fx.store.append(fx.state, "execution-recorded")
        first = fx.record_behavior(first_red)
        self.assertEqual(fx.store.json(first)["schema"], 1)
        fx.green()
        self.assertTrue(test_intent.valid(fx.store, fx.state, "T01"))
        fx.test_file.write_text("assert VALUE == 2\n", encoding="utf-8")
        with self.assertRaises(Invalid):
            test_intent.record(fx.store, fx.state, "T01", fx.behavior_report(first_red))
        fx.state["taskHistory"]["T01"][-1]["rounds"].append({"number": 1, "passed": False})
        fx.store.append(fx.state, "legacy-first-critic")
        with self.assertRaises(Invalid):
            test_intent.record(fx.store, fx.state, "T01", fx.behavior_report(first_red))


if __name__ == "__main__":
    unittest.main()
