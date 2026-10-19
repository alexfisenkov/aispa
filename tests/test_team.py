import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core.contracts import require, task_basis, task_contract
from aispa_core.store import Invalid, Journal
from aispa_core.team import TaskPipeline, ROLES, direct_proof_current, historical_proof_valid, task_proof_valid


class Host:
    def __init__(self, store, state):
        self.store, self.s = store, state

    @staticmethod
    def read_input(path):
        return Path(path).read_text()

    def running(self):
        require(self.s["status"] == "running", "not running")

    def build_ready(self):
        self.running()

    def task(self, tid):
        task = next((t for t in self.s["tasks"] if t["id"] == tid), None)
        require(task is not None, "unknown task")
        return task

    def save(self, event):
        self.store.append(self.s, event)

    def block(self, tid, note):
        self.task(tid).update(status="blocked", note=note)
        self.task(tid).pop("proof", None)
        self.save("blocked:" + tid)


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-team-test-")
        self.home = Path(self.temp.name)
        self.root = self.home / "project"
        self.root.mkdir()
        (self.root / ".gitignore").write_text(".aispa/\n")
        (self.root / "a.txt").write_text("a")
        (self.root / "b.txt").write_text("b")
        for args in (("init", "-q"), ("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")):
            subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True)
        self.store = Journal(self.root)
        self.state = self.store.create("team", "Team fixture")
        brief = self.store.artifact("briefs", b"Build A. Build B. Keep data private.\n", ".md")
        self.state["briefs"] = [dict(brief, id="B001")]
        self.state["requirements"] = [
            {"id": "R01", "text": "A", "acceptance": ["A works"], "kind": "requested", "scope": "active", "core": True, "brief": "B001", "quote": "Build A."},
            {"id": "R02", "text": "B", "acceptance": ["B works"], "kind": "requested", "scope": "active", "core": False, "brief": "B001", "quote": "Build B."},
            {"id": "G01", "text": "Privacy", "acceptance": ["No private data"], "kind": "constraint", "scope": "active", "core": True, "brief": "B001", "quote": "Keep data private."},
        ]
        self.state["spec"] = self.store.artifact("specs", b"Implement A and B privately\n", ".md")
        self.state.update(sealed=True, core=["R01", "G01"])
        self.state["tasks"] = [self.task("T01", "R01", "a.txt"), self.task("T02", "R02", "b.txt")]
        self.host = Host(self.store, self.state)
        self.team = TaskPipeline(self.host)
        self.team.prepare()
        self.evidence = self.store.artifact("evidence", b"Verified fixture output\n", ".txt")
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def task(self, tid, rid, zone, deps=None):
        return {"id": tid, "title": tid + " title", "requirements": [rid], "dependsOn": deps or [], "zones": [zone],
                "readZones": [], "criteria": [tid + " works"], "wave": 1 if not deps else 2, "status": "pending"}

    def attempt(self, tid="T01"):
        return self.state["taskHistory"][tid][-1]

    def start(self, tid="T01"):
        self.team.start_guard(tid)
        self.host.task(tid)["status"] = "running"
        for role in ROLES:
            self.serial += 1
            self.team.assign(tid, role, "fixture", str(self.serial), "session")
        self.verify(tid)

    def verify(self, tid="T01"):
        self.attempt(tid)["verification"] = {"candidate": task_basis(self.store, self.state, tid), "exitCode": 0,
            "changedCode": False, "output": self.evidence, "command": ["fixture"]}

    def report(self, tid="T01", role="worker", **extra):
        value = {"basis": task_basis(self.store, self.state, tid), "reviewer": self.attempt(tid)["actors"][role],
                 "independent": role != "worker", "unmapped": [], "checks": [
                     {"requirement": rid, "criterion": c, "status": "passed", "note": "Observed fixture", "artifact": self.evidence["file"]}
                     for rid, c in sorted(self.team.expected_checks(tid, role))]}
        value.update(extra)
        self.serial += 1
        path = self.home / ("report-" + str(self.serial) + ".json")
        path.write_text(json.dumps(value))
        return path

    def reviews(self, tid="T01", fail_first=False):
        self.team.submit(tid, self.report(tid))
        findings = [{"id": "F1", "severity": "blocking", "text": "Fix fixture", "criterion": self.host.task(tid)["criteria"][0]}] if fail_first else []
        self.team.review(tid, 1, self.report(tid, "critic1", findings=findings))
        if fail_first:
            (self.root / self.host.task(tid)["zones"][0]).write_text("fixed")
            self.verify(tid)
            first = self.attempt(tid)["rounds"][0]
            self.team.repair(tid, self.report(tid, "worker", previousReport=first["report"]["sha256"], addresses=["F1"]))
        attempt = self.attempt(tid)
        resolutions = [{"id": "F1", "status": "resolved", "note": "Fixed and tested", "artifact": self.evidence["file"]}] if fail_first else []
        self.team.review(tid, 2, self.report(tid, "critic2", findings=[], previousReport=attempt["rounds"][0]["report"]["sha256"],
            repairReceipt=attempt["repair"]["report"]["sha256"] if attempt["repair"] else None, resolutions=resolutions))

    def accept(self, tid="T01", **extra):
        attempt = self.attempt(tid)
        trace = [{"requirement": req["id"], "brief": req["brief"], "quote": req["quote"], "result": "aligned",
                  "note": "Compared to result", "artifact": self.evidence["file"]} for req in self.team.trace_requirements(tid)]
        data = {"previousReport": attempt["rounds"][1]["report"]["sha256"], "inheritedProofs": self.team.inherited_proofs(tid), "trace": trace}
        data.update(extra)
        return self.team.accept(tid, self.report(tid, "acceptor", **data))

    def complete(self, tid="T01"):
        self.start(tid)
        self.reviews(tid)
        self.assertTrue(self.accept(tid))

    def test_two_rounds_and_separate_acceptor_required(self):
        self.start()
        with self.assertRaises(Invalid):
            self.team.accept("T01", self.report(role="acceptor"))
        self.team.submit("T01", self.report())
        self.team.review("T01", 1, self.report(role="critic1", findings=[]))
        self.assertEqual(self.host.task("T01")["phase"], "critic_2")
        with self.assertRaises(Invalid):
            self.team.accept("T01", self.report(role="acceptor"))
        self.team.review("T01", 2, self.report(role="critic2", findings=[], previousReport=self.attempt()["rounds"][0]["report"]["sha256"], repairReceipt=None, resolutions=[]))
        self.assertEqual(self.host.task("T01")["status"], "running")
        self.assertTrue(self.accept())
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))

    def test_actor_identity_cannot_alias_or_reuse(self):
        self.team.assign("T01", "worker", "fixture", "same", "session")
        for tid, role in (("T01", "critic1"), ("T02", "worker")):
            with self.subTest(tid=tid, role=role), self.assertRaises(Invalid):
                self.team.assign(tid, role, "fixture", "same", "session")
        with self.assertRaises(Invalid):
            self.team.assign("T01", "critic1", "fixture", "fresh", "session", fresh=False)

    def test_replacement_worker_repairs_without_erasing_original_writer(self):
        self.start()
        original = self.attempt()["actors"]["worker"]
        self.team.submit("T01", self.report())
        self.team.review("T01", 1, self.report(role="critic1", findings=[
            {"id": "F1", "severity": "blocking", "text": "Fix result", "criterion": "T01 works"}]))
        replacement = self.team.assign("T01", "worker", "fixture", "replacement", "session")
        self.assertNotEqual(original, replacement)
        self.assertEqual(self.attempt()["writers"], [original, replacement])
        self.assertEqual(self.attempt()["submitted"]["actor"], original)
        (self.root / "a.txt").write_text("repaired by replacement")
        self.verify()
        first = self.attempt()["rounds"][0]
        self.team.repair("T01", self.report(previousReport=first["report"]["sha256"], addresses=["F1"]))
        self.assertEqual(self.attempt()["repair"]["actor"], replacement)
        self.team.review("T01", 2, self.report(role="critic2", findings=[], previousReport=first["report"]["sha256"],
            repairReceipt=self.attempt()["repair"]["report"]["sha256"], resolutions=[
                {"id": "F1", "status": "resolved", "note": "Confirmed", "artifact": self.evidence["file"]}]))
        self.assertTrue(self.accept())
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))

    def test_reviewer_cannot_be_replaced_after_report(self):
        self.start()
        self.team.submit("T01", self.report())
        self.team.review("T01", 1, self.report(role="critic1", findings=[]))
        with self.assertRaises(Invalid):
            self.team.assign("T01", "critic1", "fixture", "other", "session")
        self.team.assign("T01", "critic2", "fixture", "replacement-second", "session")
        self.assertEqual(len(self.attempt()["actorHistory"]), 1)

    def test_source_and_owner_authorization_artifacts_remain_mandatory(self):
        self.complete()
        source_path = self.store.run / self.state["spec"]["file"]
        original = source_path.read_bytes()
        source_path.write_text("tampered source")
        self.assertFalse(historical_proof_valid(self.store, self.state, "T01"))
        source_path.write_bytes(original)
        source = self.store.artifact("briefs", b"Repeat A with the new agreed change.\n", ".md")
        self.state["briefs"].append(dict(source, id="B002"))
        self.team.new_attempt("T01", "B002", "Repeat A with the new agreed change.")
        self.team.prepare()
        self.complete()
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))
        auth = self.attempt()["authorization"]["record"]
        (self.store.run / auth["file"]).write_text("tampered owner decision")
        self.assertFalse(historical_proof_valid(self.store, self.state, "T01"))
        self.assertFalse(direct_proof_current(self.store, self.state, "T01"))

    def test_prepare_preserves_unchanged_cards_and_custom_cards(self):
        before = copy.deepcopy(self.attempt()["roles"])
        self.team.prepare()
        self.assertEqual(before, self.attempt()["roles"])
        path = self.home / "team.json"
        path.write_text(json.dumps({"tasks": {"T01": {"critic1": {"specialization": "Privacy expert", "checks": ["Specific risk checked"]}}}}))
        self.team.team(path)
        card = self.attempt()["roles"]["critic1"]
        self.assertIn("Specific risk checked", card["checks"])
        self.assertIn("T01 works", card["checks"])
        before = copy.deepcopy(self.attempt()["roles"])
        self.team.prepare()
        self.assertEqual(before, self.attempt()["roles"])

    def test_repair_preserves_first_round_and_binds_second(self):
        self.start()
        self.reviews(fail_first=True)
        self.assertFalse(self.attempt()["rounds"][0]["passed"])
        self.assertTrue(self.attempt()["rounds"][1]["passed"])
        self.assertNotEqual(self.attempt()["rounds"][0]["basis"], self.attempt()["rounds"][1]["basis"])
        self.assertTrue(self.accept())
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))

    def test_second_critic_failure_is_terminal_but_sibling_continues(self):
        self.start()
        self.team.submit("T01", self.report())
        self.team.review("T01", 1, self.report(role="critic1", findings=[]))
        self.assertFalse(self.team.review("T01", 2, self.report(role="critic2", findings=[
            {"id": "F2", "severity": "blocking", "text": "Still broken", "criterion": "T01 works"}],
            previousReport=self.attempt()["rounds"][0]["report"]["sha256"], repairReceipt=None, resolutions=[])))
        self.assertTrue(self.attempt()["terminal"])
        for operation in (lambda: self.team.start_guard("T01"), lambda: self.team.reset_guard("T01"),
                          lambda: self.team.review("T01", 3, self.report(role="critic2", findings=[]))):
            with self.assertRaises(Invalid):
                operation()
        self.complete("T02")
        self.assertEqual(self.state["status"], "running")

    def test_contract_frozen_after_review_and_rename_does_not_reset_budget(self):
        self.start()
        self.team.submit("T01", self.report())
        self.team.review("T01", 1, self.report(role="critic1", findings=[]))
        changed = copy.deepcopy(self.state["tasks"])
        changed[0]["criteria"].append("New")
        with self.assertRaises(Invalid):
            self.team.validate_replan(changed)
        renamed = copy.deepcopy(self.state["tasks"])
        renamed[0]["id"] = "T03"
        with self.assertRaises(Invalid):
            self.team.validate_replan(renamed)
        before = copy.deepcopy(self.attempt()["roles"])
        self.team.prepare()
        self.assertEqual(before, self.attempt()["roles"])

    def test_rebinding_cards_consumes_already_included_owner_briefs(self):
        source = self.store.artifact("briefs", b"Change requested before implementation.\n", ".md")
        self.state["briefs"].append(dict(source, id="B002"))
        self.team.prepare()
        self.assertEqual(self.attempt()["briefCount"], 2)
        self.start()
        self.reviews()
        with self.assertRaises(Invalid):
            self.team.new_attempt("T01", "B002", "Change requested before implementation.")
        self.assertEqual(len(self.state["taskHistory"]["T01"]), 1)

    def test_removed_reviewed_task_cannot_return_under_new_id(self):
        self.start()
        self.reviews()
        old_task = copy.deepcopy(self.host.task("T01"))
        req = next(r for r in self.state["requirements"] if r["id"] == "R01")
        req["scope"] = "dropped"
        remaining = [copy.deepcopy(self.host.task("T02"))]
        self.team.validate_replan(remaining)
        self.state["tasks"] = remaining
        req["scope"] = "active"
        replacement = self.task("T03", "R01", "a.txt")
        with self.assertRaisesRegex(Invalid, "переименовать"):
            self.team.validate_replan([*remaining, replacement])
        self.assertEqual(len(self.state["taskHistory"]["T01"][-1]["rounds"]), 2)
        # A genuinely new owner brief can reactivate the original historical ID
        # even though it is no longer present in the current task list.
        source = self.store.artifact("briefs", b"Restore T01 with a new implementation attempt.\n", ".md")
        self.state["briefs"].append(dict(source, id="B002"))
        self.team.new_attempt("T01", "B002", "Restore T01 with a new implementation attempt.")
        old_task["status"] = "pending"
        proposed = [*remaining, old_task]
        self.team.validate_replan(proposed)
        self.state["tasks"] = proposed
        self.team.prepare()
        self.assertEqual(len(self.state["taskHistory"]["T01"]), 2)
        self.assertEqual(len(self.state["taskHistory"]["T01"][0]["rounds"]), 2)
        self.assertEqual(self.attempt()["rounds"], [])
        self.team.start_guard("T01")

    def test_new_attempt_requires_new_quoted_owner_brief(self):
        self.complete()
        old = copy.deepcopy(self.attempt())
        with self.assertRaises(Invalid):
            self.team.new_attempt("T01", "B001", "Build A.")
        source = self.store.artifact("briefs", b"Change A to the new agreed behavior.\n", ".md")
        self.state["briefs"].append(dict(source, id="B002"))
        self.team.new_attempt("T01", "B002", "Change A to the new agreed behavior.")
        self.assertEqual(self.state["taskHistory"]["T01"][0], old)
        changed = copy.deepcopy(self.state["tasks"])
        changed[0]["criteria"].append("New owner behavior")
        self.team.validate_replan(changed)
        self.state["tasks"] = changed
        self.team.prepare()
        self.assertEqual(self.attempt()["rounds"], [])
        self.assertEqual(self.attempt()["contract"], task_contract(self.state, "T01"))

    def test_sibling_change_preserves_proof_own_change_invalidates(self):
        self.complete()
        (self.root / "b.txt").write_text("independent edit")
        self.verify("T02")
        self.assertTrue(direct_proof_current(self.store, self.state, "T01"))
        (self.root / "a.txt").write_text("own edit")
        self.assertFalse(direct_proof_current(self.store, self.state, "T01"))
        self.assertTrue(historical_proof_valid(self.store, self.state, "T01"))

    def test_fresh_descendant_can_reconfirm_ancestor_only_within_allowed_set(self):
        self.host.task("T02")["dependsOn"] = ["T01"]
        self.host.task("T02")["zones"] = ["a.txt", "b.txt"]
        self.team.prepare()
        self.complete()
        self.start("T02")
        (self.root / "a.txt").write_text("intentional descendant integration")
        self.verify("T02")
        self.reviews("T02")
        self.assertTrue(self.accept("T02"))
        self.assertFalse(direct_proof_current(self.store, self.state, "T01"))
        self.assertTrue(direct_proof_current(self.store, self.state, "T02"))
        self.assertTrue(task_proof_valid(self.store, self.state, "T01"))
        self.assertFalse(task_proof_valid(self.store, self.state, "T01", {"T01"}))
        self.assertTrue(task_proof_valid(self.store, self.state, "T01", {"T01", "T02"}))
        self.attempt("T01")["terminal"] = True
        self.assertFalse(task_proof_valid(self.store, self.state, "T01"))
        self.assertFalse(direct_proof_current(self.store, self.state, "T02"))

    def test_missing_trace_and_inherited_proof_are_rejected(self):
        self.host.task("T02")["dependsOn"] = ["T01"]
        self.team.prepare()
        self.complete()
        self.start("T02")
        self.reviews("T02")
        with self.assertRaises(Invalid):
            self.accept("T02", inheritedProofs=[])
        with self.assertRaises(Invalid):
            self.accept("T02", trace=[])
        self.assertTrue(self.accept("T02"))

    def test_artifact_tamper_invalidates_full_history(self):
        self.complete()
        first = self.attempt()["rounds"][0]["report"]
        (self.store.run / first["file"]).write_text("tampered")
        self.assertFalse(historical_proof_valid(self.store, self.state, "T01"))
        self.assertFalse(task_proof_valid(self.store, self.state, "T01"))

    def test_earlier_tamper_rejected_before_acceptance(self):
        self.start()
        self.reviews()
        submitted = self.attempt()["submitted"]["report"]
        (self.store.run / submitted["file"]).write_text("tampered")
        with self.assertRaises(Invalid):
            self.accept()
        self.assertEqual(self.host.task("T01")["status"], "running")

    def test_code_change_after_second_critic_cannot_skip_third_round_rule(self):
        self.start()
        self.reviews()
        (self.root / "a.txt").write_text("changed after reviews")
        self.verify()
        with self.assertRaises(Invalid):
            self.accept()
        with self.assertRaises(Invalid):
            self.team.reset_guard("T01")


if __name__ == "__main__":
    unittest.main()
