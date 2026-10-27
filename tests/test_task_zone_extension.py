"""Synthetic role fixtures, real local RED/GREEN commands; no model run."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import executions, participants, phase_receipts, planning, repairs, test_intent
from aispa_core import task_zone_extension as extension
from aispa_core.contracts import basis, definition, gate_valid, task_basis, validate_plan
from aispa_core.store import Invalid, Journal, file_hash
from aispa_core.team import TaskPipeline, _history_valid, acceptance_chain, direct_proof_current, historical_proof_valid
from aispa_core.workflow import Workflow
from test_team import Host


class ZoneExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-zone-epoch-🙂-")
        self.root = Path(self.temp.name)
        (self.root / ".gitignore").write_text(".aispa/\n")
        (self.root / "app.py").write_text("VALUE = 0\n")
        (self.root / "extra.py").write_text("EXTRA = 0\n")
        (self.root / "last.txt").write_text("pending descendant\n")
        for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Fixture", "-c",
                     "user.email=fixture@example.invalid", "commit", "-qm", "fixture")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        self.store = Journal(self.root)
        self.state = self.store.create("epochs", "Synthetic zone epoch fixture")
        self.state["policy"] = {"testIntent": True, "adaptiveCritique": True,
                                "phaseReceipts": True, "planReview": True,
                                "repairDiagnosis": True}
        if getattr(self, "lean", False):
            self.state["policy"]["leanControl"] = True
            self.state["checkMode"] = "max"
        if getattr(self, "focused", False):
            # 0.5.0 focused run: cards appear when a role is assigned.
            self.state["policy"].update(leanControl=True, focusedReview=True, taskTiers=True)
            self.state["checkMode"] = "balanced"
        source = self.store.artifact("briefs", b"Set VALUE and EXTRA to one.\n", ".md")
        self.state["briefs"] = [{**source, "id": "B001"}]
        self.state["requirements"] = [{"id": "R01", "text": "Set both values",
            "acceptance": ["both equal one"], "brief": "B001", "quote": "Set VALUE and EXTRA to one.",
            "kind": "requested", "scope": "active", "core": True}]
        self.state.update(sealed=True, core=["R01"], spec=self.store.artifact("specs", b"Set both values to one.", ".md"))
        raw = [{"id": "T01", "title": "Both values", "requirements": ["R01"],
                "dependsOn": [], "zones": ["app.py", "tests/test_app.py"], "readZones": [],
                "criteria": ["both equal one"], "risk": "normal", "riskReason": ""},
               {"id": "T02", "title": "Use both values", "requirements": ["R01"],
                "dependsOn": ["T01"], "zones": ["last.txt"], "readZones": [],
                "criteria": ["consumer documented"], "risk": "normal", "riskReason": ""}]
        if getattr(self, "focused", False):
            for row in raw:
                row.update(tier="heavy", tierReason="Fixture foundation", changeKind="feature")
        self.state["tasks"] = validate_plan(self.state, raw)
        self.planner = participants.register(self.store, self.state, "coordinator", "fixture",
                                             "synthetic-planner", "synthetic-session")
        self.state["planReview"] = planning.review(self.store, self.state, self.state["tasks"], self.review(self.state["tasks"]))
        self.state["gates"]["plan"] = {"passed": True, "basis": basis(self.store, self.state, "plan"),
            "report": self.store.artifact("plans", json.dumps(raw).encode(), ".json")}
        phase_receipts.record(self.store, self.state, "planner", self.state["gates"]["plan"]["report"], actor=self.planner)
        self.host = Host(self.store, self.state)
        self.team = TaskPipeline(self.host)
        self.team.prepare()
        self.attempt = self.state["taskHistory"]["T01"][-1]
        self.attempt["lightBaseline"] = self.store.artifact("team-records",
            json.dumps({"records": self.store.code_records()}).encode(), ".json")
        self.host.task("T01").update(status="running", phase="implementation", startedAt="2026-10-26T00:00:00Z")
        test_intent.capture_baseline(self.store, self.state, "T01")
        self.host.save("task-started:T01")
        self.team.assign("T01", "worker", "fixture", "synthetic-worker", "synthetic-session")
        self.team.assign("T01", "critic1", "fixture", "synthetic-critic1", "synthetic-session")
        self.flow = Workflow(self.store, self.state)
        (self.root / "tests").mkdir()
        (self.root / "tests/test_app.py").write_text("import runpy\nassert runpy.run_path('app.py')['VALUE'] == 1, 'VALUE must equal one'\n")
        self.initial_intent = self.intent(["tests/test_app.py"], "VALUE must equal one")
        (self.root / "app.py").write_text("VALUE = 1\n")
        self.green()
        self.team.submit("T01", self.report("worker"))
        findings = [{"id": "F-EXTRA", "severity": "blocking", "criterion": "both equal one", "text": "EXTRA remains zero",
                     **({"blockingClass": "brief_drift"} if getattr(self, "focused", False) else {})},
                    {"id": "W-TEST", "severity": "warning", "criterion": "both equal one", "text": "Cover EXTRA explicitly"}]
        self.addresses = ["F-EXTRA"] if getattr(self, "focused", False) else ["F-EXTRA", "W-TEST"]
        self.assertFalse(self.team.review("T01", 1, self.report("critic1", verdict="FAIL", findings=findings)))
        self.before = copy.deepcopy(self.state)
        self.prefix = (self.store.run / "events.jsonl").read_bytes()
        self.old_artifacts = {p.relative_to(self.store.run).as_posix(): p.read_bytes()
                              for p in self.store.run.rglob("*") if p.is_file() and p.name != "events.jsonl"}

    def tearDown(self):
        self.temp.cleanup()

    def review(self, tasks):
        advice = planning.analyze(self.store, self.state, tasks)
        return {"basis": advice["basis"], "draft": [definition(t) for t in tasks],
                "groups": [{"task": t["id"], "from": [t["id"]], "reason": "Keep the established boundary"} for t in tasks],
                "separate": [{"suggestion": s["id"], "reason": "Separate consumer and implementation"} for s in advice["suggestions"]]}

    def proposal(self, additions=None):
        additions = additions or ["extra.py", "tests/test_extra.py"]
        tasks = copy.deepcopy(self.state["tasks"])
        tasks[0]["zones"].extend(additions)
        return {"attempt": self.attempt["id"], "contract": self.attempt["contract"],
                "reason": "The already approved value requirement needs its implementation and regression test",
                "files": [{"path": p, "requirement": "R01", "reason": "Implements the existing value criterion"} for p in additions],
                "planReview": self.review(tasks)}

    def extend(self, report=None, **kw):
        return extension.apply(self.host, "T01", report or self.proposal(), actor=kw.pop("actor", self.planner),
                               expected_journal_sha256=kw.pop("expected_journal_sha256", file_hash(self.store.run / "events.jsonl")), **kw)

    def intent(self, paths, failure):
        command = [sys.executable, "-B", "-c", "; ".join("exec(compile(open(%r).read(),%r,'exec'))" % (p, p) for p in paths)]
        self.assertFalse(self.flow.verify(command, 10, "T01"))
        red = executions.record(self.store, self.state, kind="task", purpose="check", task_id="T01")
        self.host.save("execution-recorded")
        report = {"mode": "behavior_change", "attempt": self.attempt["id"],
                  "basis": task_basis(self.store, self.state, "T01"), "criteria": ["both equal one"],
                  "redExecution": red, "newTestFiles": [{"path": p, "sha256": file_hash(self.root / p),
                      "criteria": ["both equal one"], "expectedFailure": failure} for p in paths]}
        ref = test_intent.record(self.store, self.state, "T01", report)
        self.host.save("test-intent-recorded:T01")
        return ref

    def green(self):
        paths = ["tests/test_app.py"] + (["tests/test_extra.py"] if (self.root / "tests/test_extra.py").exists() else [])
        command = [sys.executable, "-B", "-c", "; ".join("exec(compile(open(%r).read(),%r,'exec'))" % (p, p) for p in paths)]
        self.assertTrue(self.flow.verify(command, 10, "T01"))

    def report(self, role, **extra):
        evidence = self.attempt["verification"]["output"]
        report = {"basis": task_basis(self.store, self.state, "T01"), "reviewer": self.attempt["actors"][role],
                  "independent": role != "worker", "unmapped": [], "checks": [{"requirement": r,
                    "criterion": c, "status": "passed", "note": "Synthetic role checked real command evidence",
                    "artifact": evidence["file"]} for r, c in sorted(self.team.expected_checks("T01", role))]}
        report.update(extra)
        if self.attempt.get("reviewPolicy") in ("lean", "focused") and role.startswith("critic"):
            report["inheritedProofs"] = []
            report["trace"] = [{"requirement": "R01", "brief": "B001", "quote": "Set VALUE and EXTRA to one.",
                                "result": "aligned", "note": "Synthetic source trace fixture", "artifact": evidence["file"]}]
        path = self.root / ".aispa" / ("synthetic-" + role + ".json")
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def finish_repair(self, fail_second=False, stop_after_repair=False, repair_plan=None):
        (self.root / "tests/test_extra.py").write_text("import runpy\nassert runpy.run_path('extra.py')['EXTRA'] == 1, 'EXTRA must equal one'\n")
        # Both files must be visible in the real failing command/output. The first
        # test also gains the EXTRA assertion so each preserved source sees RED.
        (self.root / "tests/test_app.py").write_text("import runpy\nassert runpy.run_path('app.py')['VALUE'] == 1\nassert runpy.run_path('extra.py')['EXTRA'] == 1, 'EXTRA must equal one'\n")
        # Execute each file, then exit with the real assertion result; no fabricated receipt.
        paths = ["tests/test_app.py", "tests/test_extra.py"]
        command = [sys.executable, "-B", "-c", "import runpy; failed=False\nfor p in " + repr(paths) + ":\n try: runpy.run_path(p)\n except AssertionError as e: print(p, str(e)); failed=True\nraise SystemExit(1 if failed else 0)"]
        self.assertFalse(self.flow.verify(command, 10, "T01"))
        red = executions.record(self.store, self.state, kind="task", purpose="check", task_id="T01")
        self.host.save("execution-recorded")
        report = {"mode": "behavior_change", "attempt": self.attempt["id"], "basis": task_basis(self.store, self.state, "T01"),
                  "criteria": ["both equal one"], "redExecution": red,
                  "newTestFiles": [{"path": p, "sha256": file_hash(self.root / p), "criteria": ["both equal one"],
                                    "expectedFailure": "EXTRA must equal one"} for p in paths]}
        test_intent.record(self.store, self.state, "T01", report)
        self.host.save("test-intent-revised:T01")
        (self.root / "extra.py").write_text("EXTRA = 1\n")
        self.green()
        diagnosis = repair_plan or {"basis": repairs.basis(self.state, "T01"),
            "cause": "implementation_gap", "diagnosis": "Missing EXTRA behavior and regression", "route": "same",
            "reason": "Local implementation gap", "changes": "Implement EXTRA and run both regressions",
            "addresses": list(self.addresses), "context": []}
        if repair_plan is not False:
            repairs.record(self.store, self.state, "T01", diagnosis)
            self.host.save("repair-plan-recorded:T01")
        first = self.attempt["rounds"][0]
        receipt = self.team.repair("T01", self.report("worker", previousReport=first["report"]["sha256"], addresses=list(self.addresses)))
        if stop_after_repair:
            return receipt
        self.team.assign("T01", "critic2", "fixture", "synthetic-fresh-critic2", "synthetic-session")
        evidence = self.attempt["verification"]["output"]["file"]
        findings = [{"id": "F-SECOND", "severity": "blocking", "criterion": "both equal one", "text": "Synthetic independent refusal"}] if fail_second else []
        self.assertEqual(not fail_second, self.team.review("T01", 2, self.report("critic2", verdict="FAIL" if fail_second else "PASS", findings=findings,
            previousReport=first["report"]["sha256"], repairReceipt=receipt["report"]["sha256"],
            resolutions=[{"id": i, "status": "resolved", "note": "Verified both real tests", "artifact": evidence} for i in self.addresses])))

    def test_same_attempt_old_fail_new_red_green_and_second_critic(self):
        receipt = self.extend()
        self.assertEqual(self.attempt["id"], self.before["taskHistory"]["T01"][-1]["id"])
        self.assertEqual(self.attempt["submitted"], self.before["taskHistory"]["T01"][-1]["submitted"])
        self.assertEqual(self.attempt["rounds"], self.before["taskHistory"]["T01"][-1]["rounds"])
        self.assertEqual(self.state["actors"], self.before["actors"])
        self.assertEqual(self.state["policy"], self.before["policy"])
        self.assertEqual(self.state["phaseReceipts"][:-1], self.before["phaseReceipts"])
        self.assertEqual(receipt["addedBaseline"]["source"], self.attempt["lightBaseline"])
        self.assertTrue(gate_valid(self.store, self.state, "plan"))
        self.assertTrue(planning.valid(self.store, self.state))
        self.assertFalse(test_intent.valid(self.store, self.state, "T01"))
        self.finish_repair()
        self.assertTrue(_history_valid(self.store, self.state, "T01"))
        final = acceptance_chain(self.store, self.state, "T01", self.attempt, require_acceptor=False)
        self.assertEqual(final["number"], 2)
        self.assertNotEqual(self.attempt["submitted"]["testIntent"], self.attempt["repair"]["testIntent"])
        self.assertEqual(self.attempt["submitted"]["testIntent"], self.initial_intent)
        self.assertTrue(test_intent.history_valid(self.store, self.state, "T01"))
        self.assertTrue((self.store.run / "events.jsonl").read_bytes().startswith(self.prefix))
        for name, raw in self.old_artifacts.items():
            self.assertEqual((self.store.run / name).read_bytes(), raw, name)

    def test_second_nonpass_keeps_two_used_rounds_and_blocks_reset_extension(self):
        self.extend()
        self.finish_repair(fail_second=True)
        self.assertTrue(self.attempt["terminal"])
        self.assertEqual(len(self.attempt["rounds"]), 2)
        with self.assertRaises(Invalid): self.team.reset_guard("T01")
        with self.assertRaises(Invalid): self.extend()

    def test_stale_cas_wrong_actor_and_broad_path_denied_without_append(self):
        for kwargs in ({"expected_journal_sha256": "0" * 64}, {"actor": self.attempt["actors"]["worker"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Invalid): self.extend(**kwargs)
        for path in (".", "tests", "../escape.py", "*.py"):
            with self.subTest(path=path), self.assertRaises((Invalid, ValueError)):
                value = self.proposal()
                value["files"][0]["path"] = path
                self.extend(value)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), self.prefix)

    def test_used_descendant_and_tampered_extension_history_denied(self):
        descendant = self.state["taskHistory"]["T02"][-1]
        self.team.assign("T02", "worker", "fixture", "synthetic-used-descendant", "synthetic-session")
        with self.assertRaises(Invalid): self.extend()
        # Independent new fixture is covered by other tests; a used descendant is never reset.
        self.assertTrue(descendant["actors"])

    def test_history_loss_and_actor_forgery_fail_closed(self):
        self.extend()
        original = copy.deepcopy(self.attempt["zoneExtensions"])
        self.attempt["zoneExtensions"] = []
        self.assertFalse(_history_valid(self.store, self.state, "T01"))
        self.attempt["zoneExtensions"] = original
        actor = self.state["actors"][self.attempt["actors"]["worker"]]
        actor["card"] = self.attempt["roles"]["worker"]["sha256"]
        self.assertFalse(_history_valid(self.store, self.state, "T01"))

    def test_repair_submission_and_zero_file_extension_are_denied(self):
        empty = self.proposal()
        empty["files"] = []
        with self.assertRaises(Invalid): self.extend(empty)
        self.extend()
        self.finish_repair(stop_after_repair=True)
        with self.assertRaises(Invalid): self.extend()
        with self.assertRaises(Invalid): self.team.reset_guard("T01")

    def test_late_or_tampered_start_baseline_is_rejected(self):
        original = self.attempt["lightBaseline"]
        self.attempt["lightBaseline"] = self.store.artifact("team-records",
            json.dumps({"records": self.store.code_records()}).encode(), ".json")
        self.host.save("synthetic-post-implementation-snapshot")
        with self.assertRaisesRegex(Invalid, "первому старту"): self.extend()
        self.attempt["lightBaseline"] = original
        self.host.save("synthetic-restore-real-baseline")
        (self.store.run / original["file"]).write_bytes(b"{}")
        with self.assertRaises(Invalid): self.extend()

    def test_prepared_extension_is_read_only_and_old_red_cannot_cross_epoch(self):
        value = self.proposal()
        before = {p.relative_to(self.store.run).as_posix(): p.read_bytes()
                  for p in self.store.run.rglob("*") if p.is_file()}
        extension.prepare(self.host, "T01", value, actor=self.planner)
        self.assertEqual(before, {p.relative_to(self.store.run).as_posix(): p.read_bytes()
                                for p in self.store.run.rglob("*") if p.is_file()})
        original = test_intent._saved(self.store, self.initial_intent)["report"]
        self.extend(value)
        original["basis"] = task_basis(self.store, self.state, "T01")
        with self.assertRaises(Invalid): test_intent.record(self.store, self.state, "T01", original)

    def test_full_plan_zone_conflict_and_changed_review_delivery_are_rejected(self):
        # An unrelated reader cannot be silently reordered or granted a dependency.
        self.state["tasks"].append({"id": "T03", "title": "Independent reader", "requirements": ["R01"],
            "dependsOn": [], "zones": ["independent.txt"], "readZones": ["extra.py"], "criteria": ["reads extra"],
            "risk": "normal", "riskReason": "", "wave": 1, "status": "pending"})
        self.host.save("synthetic-independent-reader")
        with self.assertRaisesRegex(Invalid, "пересекаются"): self.extend()

    def test_repeated_epoch_is_monotonic_and_historical_proofs_survive_later_appends(self):
        self.extend()
        # Establish an actual new-epoch RED without submitting a repair.
        (self.root / "tests/test_app.py").write_text("import runpy\nassert runpy.run_path('extra.py')['EXTRA'] == 1, 'EXTRA must equal one'\n")
        self.intent(["tests/test_app.py"], "EXTRA must equal one")
        self.extend(self.proposal(["third.py"]))
        self.assertEqual(len(self.attempt["zoneExtensions"]), 2)
        self.assertEqual(self.attempt["zoneExtensions"][1]["before"], self.attempt["zoneExtensions"][0]["after"])
        self.assertEqual(len(self.attempt["rounds"]), 1)
        self.finish_repair()
        for i in range(4): self.host.save("synthetic-later-append:" + str(i))
        self.assertTrue(_history_valid(self.store, self.state, "T01"))
        self.assertTrue(test_intent.history_valid(self.store, self.state, "T01"))
        self.assertEqual(acceptance_chain(self.store, self.state, "T01", self.attempt, require_acceptor=False)["number"], 2)

    def test_final_acceptance_keeps_epoch_chain_current_after_further_appends(self):
        self.extend()
        self.finish_repair()
        self.team.assign("T01", "acceptor", "fixture", "synthetic-fresh-acceptor", "synthetic-session")
        evidence = self.attempt["verification"]["output"]["file"]
        self.assertTrue(self.team.accept("T01", self.report("acceptor", previousReport=self.attempt["rounds"][-1]["report"]["sha256"],
            inheritedProofs=[], trace=[{"requirement": "R01", "brief": "B001", "quote": "Set VALUE and EXTRA to one.",
                "result": "aligned", "note": "Synthetic acceptor checked the existing source", "artifact": evidence}], verdict="PASS")))
        for i in range(3): self.host.save("synthetic-after-acceptance:" + str(i))
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        with self.assertRaises(Invalid): self.extend()

    def test_lean_second_critic_accepts_without_changing_prior_fail(self):
        fixture = ZoneExtensionTests(methodName="test_same_attempt_old_fail_new_red_green_and_second_critic")
        fixture.lean = True
        fixture.setUp()
        try:
            first = copy.deepcopy(fixture.attempt["rounds"][0])
            fixture.extend()
            fixture.finish_repair()
            self.assertEqual(fixture.host.task("T01")["status"], "completed")
            self.assertEqual(fixture.attempt["rounds"][0], first)
            self.assertTrue(historical_proof_valid(fixture.store, fixture.state, "T01"))
        finally:
            fixture.tearDown()

    def test_focused_second_critic_is_assigned_and_passes_after_zone_extension(self):
        fixture = ZoneExtensionTests(methodName="test_same_attempt_old_fail_new_red_green_and_second_critic")
        fixture.focused = True
        fixture.setUp()
        try:
            attempt = fixture.attempt
            self.assertEqual(attempt["reviewPolicy"], "focused")
            # Focused: before the extension critic2 has no card yet (cards appear on assignment).
            self.assertNotIn("critic2", attempt["roles"])
            first = copy.deepcopy(attempt["rounds"][0])
            receipt = fixture.extend()
            # The extension opens the epoch with the critic2 card of the new contract.
            self.assertIn("critic2", receipt["after"]["roles"])
            self.assertNotIn("critic2", receipt["before"]["roles"])
            self.assertEqual(attempt["roles"], receipt["after"]["roles"])
            card = fixture.store.json(attempt["roles"]["critic2"])
            self.assertEqual((card["contract"], card["zones"]), (attempt["contract"], fixture.host.task("T01")["zones"]))
            self.assertTrue(extension.history_valid(fixture.store, fixture.state, "T01", attempt))
            fixture.finish_repair()
            self.assertEqual(attempt["actors"]["critic2"], fixture.state["actors"][attempt["actors"]["critic2"]]["id"])
            self.assertTrue(attempt["rounds"][1]["passed"])
            self.assertEqual(fixture.host.task("T01")["status"], "completed")
            self.assertEqual(attempt["rounds"][0], first)
            self.assertTrue(_history_valid(fixture.store, fixture.state, "T01"))
            self.assertTrue(historical_proof_valid(fixture.store, fixture.state, "T01"))
        finally:
            fixture.tearDown()

    def test_blocked_status_is_rejected_before_prepare_or_append(self):
        self.host.block("T01", "Synthetic explicit blocked repair")
        request = self.proposal()
        before = {p.relative_to(self.store.run).as_posix(): p.read_bytes()
                  for p in self.store.run.rglob("*") if p.is_file()}
        with self.assertRaises(Invalid): extension.prepare(self.host, "T01", request, actor=self.planner)
        with self.assertRaises(Invalid): self.extend(request)
        self.assertEqual(before, {p.relative_to(self.store.run).as_posix(): p.read_bytes()
                                for p in self.store.run.rglob("*") if p.is_file()})
        self.assertEqual(self.host.task("T01")["status"], "blocked")
        self.assertEqual(len(self.attempt["rounds"]), 1)
        self.assertFalse(self.attempt.get("zoneExtensions"))

    def test_file_boundary_is_rechecked_after_prepare_before_apply(self):
        request = self.proposal(["future.py"])
        extension.prepare(self.host, "T01", request, actor=self.planner)
        path = self.root / "future.py"
        path.mkdir()
        (path / "unapproved_child.py").write_text("VALUE = 1\n")
        with self.assertRaises(Invalid): self.extend(request)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), self.prefix)
        (path / "unapproved_child.py").unlink()
        path.rmdir()
        path.symlink_to(self.root / "app.py")
        with self.assertRaises(Invalid): self.extend(request)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), self.prefix)

    def test_file_boundary_blocks_verification_after_absent_becomes_directory(self):
        self.extend(self.proposal(["extra.py", "tests/test_extra.py", "future.py"]))
        first = copy.deepcopy(self.attempt["rounds"][0])
        prefix = (self.store.run / "events.jsonl").read_bytes()
        path = self.root / "future.py"
        path.mkdir()
        (path / "unapproved_child.py").write_text("VALUE = 1\n")
        marker = self.root / ".aispa/unsafe-verification-ran"
        with self.assertRaises(Invalid):
            self.flow.verify([sys.executable, "-B", "-c", "from pathlib import Path; Path('.aispa/unsafe-verification-ran').touch()"], 10, "T01")
        self.assertFalse(marker.exists())
        self.assertEqual(prefix, (self.store.run / "events.jsonl").read_bytes())
        self.assertTrue(_history_valid(self.store, self.state, "T01"))
        self.assertEqual(self.attempt["rounds"][0], first)
        self.assertFalse(self.attempt.get("repair"))
        with self.assertRaises(Invalid): self.team.reset_guard("T01")

    def test_file_boundary_blocks_file_to_directory_or_symlink_after_green(self):
        self.extend(self.proposal(["extra.py", "tests/test_extra.py", "future.py"]))
        (self.root / "future.py").write_text("VALUE = 1\n")
        self.finish_repair(stop_after_repair=True)
        # A genuine GREEN and repair exist; changing the exact file must still
        # invalidate live use before critic2 can be assigned or accepted.
        path = self.root / "future.py"
        path.unlink()
        path.mkdir()
        (path / "unapproved_child.py").write_text("VALUE = 1\n")
        for kind in ("directory", "symlink"):
            with self.subTest(kind=kind):
                prefix = (self.store.run / "events.jsonl").read_bytes()
                self.assertFalse(test_intent.valid(self.store, self.state, "T01"))
                with self.assertRaises(Invalid):
                    self.team.assign("T01", "critic2", "fixture", "synthetic-unapproved-critic2", "synthetic-session")
                self.assertEqual(prefix, (self.store.run / "events.jsonl").read_bytes())
                self.assertTrue(_history_valid(self.store, self.state, "T01"))
                self.assertTrue(test_intent.history_valid(self.store, self.state, "T01"))
            if kind == "directory":
                (path / "unapproved_child.py").unlink()
                path.rmdir()
                path.symlink_to(self.root / "app.py")

    def test_file_boundary_keeps_accepted_history_but_removes_current_proof(self):
        fixture = ZoneExtensionTests(methodName="test_same_attempt_old_fail_new_red_green_and_second_critic")
        fixture.lean = True
        fixture.setUp()
        try:
            fixture.extend(fixture.proposal(["extra.py", "tests/test_extra.py", "future.py"]))
            (fixture.root / "future.py").write_text("VALUE = 1\n")
            fixture.finish_repair()
            self.assertTrue(direct_proof_current(fixture.store, fixture.state, "T01"))
            old = copy.deepcopy(fixture.attempt)
            prefix = (fixture.store.run / "events.jsonl").read_bytes()
            path = fixture.root / "future.py"
            path.unlink()
            path.mkdir()
            (path / "unapproved_child.py").write_text("VALUE = 1\n")
            self.assertFalse(direct_proof_current(fixture.store, fixture.state, "T01"))
            self.assertTrue(historical_proof_valid(fixture.store, fixture.state, "T01"))
            self.assertEqual(old, fixture.attempt)
            self.assertEqual(prefix, (fixture.store.run / "events.jsonl").read_bytes())
        finally:
            fixture.tearDown()

    def test_file_boundary_blocks_symlink_before_command_and_midcommand_type_change(self):
        self.extend(self.proposal(["extra.py", "tests/test_extra.py", "future.py"]))
        path = self.root / "future.py"
        path.symlink_to(self.root / "app.py")
        prefix = (self.store.run / "events.jsonl").read_bytes()
        marker = self.root / ".aispa/unsafe-verification-ran"
        with self.assertRaises(Invalid):
            self.flow.verify([sys.executable, "-B", "-c", "from pathlib import Path; Path('.aispa/unsafe-verification-ran').touch()"], 10, "T01")
        self.assertFalse(marker.exists())
        self.assertEqual(prefix, (self.store.run / "events.jsonl").read_bytes())
        path.unlink()
        path.write_text("VALUE = 1\n")
        command = "from pathlib import Path; p=Path('future.py'); p.unlink(); p.mkdir(); (p/'unapproved_child.py').write_text('VALUE = 1\\n'); print('command completed')"
        with self.assertRaises(Invalid): self.flow.verify([sys.executable, "-B", "-c", command], 10, "T01")
        self.assertTrue(path.is_dir())
        self.assertIsNone(self.attempt.get("verification"))
        self.assertTrue((self.store.run / "events.jsonl").read_bytes().startswith(prefix))
        self.assertEqual(len(self.attempt["rounds"]), 1)
        self.assertEqual(self.attempt["rounds"][0], self.before["taskHistory"]["T01"][-1]["rounds"][0])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX special-file fixture")
    def test_file_boundary_rejects_special_file_without_opening_it(self):
        path = self.root / "future.py"
        os.mkfifo(path)
        with self.assertRaises(Invalid): extension._exact_file(self.store, "future.py")
        path.unlink()
        self.extend(self.proposal(["future.py"]))
        os.mkfifo(path)
        with self.assertRaises(Invalid): task_basis(self.store, self.state, "T01")
        self.assertTrue(_history_valid(self.store, self.state, "T01"))


if __name__ == "__main__":
    unittest.main()
