"""Extra maintenance attempt: isolated fixtures, synthetic roles, real commands."""
import copy
import json
import unittest
from unittest.mock import patch

import test_task_zone_extension as fixtures
from aispa_core import repairs, task_zone_extension, test_intent
from aispa_core.contracts import task_basis
from aispa_core.store import Invalid
from aispa_core.team import _history_valid, direct_proof_current, historical_proof_valid


class ExtraAttemptTests(unittest.TestCase):
    def fixture(self, *, lean=False, revisions=1):
        f = fixtures.ZoneExtensionTests(methodName="test_same_attempt_old_fail_new_red_green_and_second_critic")
        f.lean = lean
        green = f.green
        def fill():
            green()
            if f.attempt.get("submitted") or f.attempt.get("rounds"):
                return
            while len(f.attempt["testIntentHistory"]) < revisions:
                report = copy.deepcopy(test_intent._saved(f.store, f.attempt["testIntent"])["report"])
                report["basis"] = task_basis(f.store, f.state, "T01")
                test_intent.record(f.store, f.state, "T01", report)
                f.host.save("existing-intent-revision:T01")
        f.green = fill
        f.setUp()
        self.addCleanup(f.tearDown)
        return f

    def diagnosis(self, f, route="fresh"):
        return {"basis": repairs.basis(f.state, "T01"), "cause": "missing_context" if route == "fresh" else "implementation_gap",
                "diagnosis": "Read the omitted value implementation and its regression", "route": route,
                "reason": "Retain the recorded repair worker and use the complete current epoch",
                "changes": "Implement both approved values and verify both regressions",
                "addresses": ["F-EXTRA", "W-TEST"], "context": ["Existing R01 and both exact value sources"]}

    def record(self, f, route="fresh"):
        ref = repairs.record(f.store, f.state, "T01", self.diagnosis(f, route))
        f.host.save("repair-diagnosed:T01")
        return ref

    def replace(self, f, suffix="new"):
        return f.team.assign("T01", "worker", "fixture", "synthetic-extra-" + suffix,
                             "synthetic-session", model="synthetic-model")

    @staticmethod
    def snapshot(f):
        return {p.relative_to(f.store.run).as_posix(): p.read_bytes()
                for p in f.store.run.rglob("*") if p.is_file()}

    def test_new_worker_after_extension_uses_fresh_route_without_an_extra_assignment(self):
        f = self.fixture(lean=True)
        original = f.attempt["actors"]["worker"]
        f.extend()
        diagnosis = self.record(f)
        with self.assertRaises(Invalid): repairs.worker_guard(f.store, f.state, "T01", original)
        worker = self.replace(f)
        self.assertEqual(repairs.worker_guard(f.store, f.state, "T01"), diagnosis)
        before = self.snapshot(f)
        with self.assertRaises(Invalid): self.replace(f, "another")
        with self.assertRaises(Invalid): self.record(f)
        self.assertEqual(before, self.snapshot(f))
        f.finish_repair(repair_plan=False)
        self.assertEqual(f.host.task("T01")["status"], "completed")
        self.assertEqual(f.attempt["writers"], [original, worker])
        self.assertEqual(len(f.attempt["rounds"]), 2)
        self.assertTrue(direct_proof_current(f.store, f.state, "T01"))

    def test_replacement_diagnoses_across_multiple_epochs_preserve_every_prior_ref(self):
        f = self.fixture(lean=True)
        refs = [self.record(f)]
        worker = self.replace(f)
        f.extend()
        refs.append(self.record(f))
        (f.root / "tests/test_app.py").write_text("import runpy\nassert runpy.run_path('extra.py')['EXTRA'] == 1, 'EXTRA must equal one'\n")
        f.intent(["tests/test_app.py"], "EXTRA must equal one")
        f.extend(f.proposal(["third.py"]))
        with self.assertRaises(Invalid): repairs.worker_guard(f.store, f.state, "T01")
        refs.append(self.record(f))
        f.finish_repair(repair_plan=False)
        for i in range(3): f.host.save("extra-later-append:" + str(i))
        self.assertEqual(f.attempt["repairPlans"], refs)
        self.assertEqual(f.attempt["actors"]["worker"], worker)
        self.assertEqual(len(f.attempt["actorHistory"]), 1)
        self.assertEqual(len(f.attempt["zoneExtensions"]), 2)
        self.assertEqual(len(repairs.epoch_history(f.store, f.state, "T01")), 3)
        self.assertTrue(test_intent.history_valid(f.store, f.state, "T01"))
        self.assertTrue(_history_valid(f.store, f.state, "T01"))
        self.assertTrue(direct_proof_current(f.store, f.state, "T01"))

    def test_replacement_epoch_refuses_route_change_actor_forgery_and_history_loss_readonly(self):
        f = self.fixture()
        old = self.record(f)
        worker = self.replace(f)
        f.extend()
        before = self.snapshot(f)
        with self.assertRaises(Invalid): self.record(f, "same")
        self.assertEqual(before, self.snapshot(f))
        actor = copy.deepcopy(f.state["actors"][worker])
        f.state["actors"][worker]["fresh"] = False
        with self.assertRaises(Invalid): self.record(f)
        self.assertEqual(before, self.snapshot(f))
        f.state["actors"][worker] = actor
        new = self.record(f)
        refs = copy.deepcopy(f.attempt["repairPlans"])
        for changed in ([], [new], [new, old]):
            f.attempt["repairPlans"] = changed
            snapshot = self.snapshot(f)
            with self.assertRaises(Invalid): repairs.read(f.store, f.state, "T01")
            self.assertEqual(snapshot, self.snapshot(f))
        f.attempt["repairPlans"] = refs
        source = f.store.run / old["file"]
        raw = source.read_bytes()
        source.write_bytes(b"{}")
        snapshot = self.snapshot(f)
        with self.assertRaises(Invalid): repairs.read(f.store, f.state, "T01")
        self.assertEqual(snapshot, self.snapshot(f))
        source.write_bytes(raw)
        self.assertEqual(repairs.worker_guard(f.store, f.state, "T01"), new)

    def test_schema2_provenance_cannot_be_relabelled_as_worker_authorship(self):
        f = self.fixture()
        f.extend()
        new = self.record(f, "same")
        path = f.store.run / new["file"]
        raw = path.read_bytes()
        value = json.loads(raw)
        self.assertIsNone(value["provenance"]["author"])
        value["provenance"]["author"] = f.attempt["actors"]["worker"]
        path.write_text(json.dumps(value))
        before = self.snapshot(f)
        with self.assertRaises(Invalid): repairs.read(f.store, f.state, "T01")
        self.assertEqual(before, self.snapshot(f))
        path.write_bytes(raw)
        self.assertEqual(repairs.worker_guard(f.store, f.state, "T01"), new)

    def test_zc3_replacement_before_extension_refreshes_diagnosis_with_provenance(self):
        f = self.fixture()
        original = f.attempt["actors"]["worker"]
        old = self.record(f)
        old_bytes = (f.store.run / old["file"]).read_bytes()
        worker = self.replace(f)
        repairs.worker_guard(f.store, f.state, "T01")
        f.extend()
        with self.assertRaises(Invalid): repairs.worker_guard(f.store, f.state, "T01")
        new = self.record(f)
        payload = json.loads(f.store.text(new))
        self.assertEqual(payload["provenance"], {"author": None, "assignedWorker": worker})
        self.assertEqual(payload["previous"], old)
        self.assertEqual(payload["epoch"], 1)
        self.assertEqual(payload["taskContract"], f.attempt["contract"])
        self.assertEqual(repairs.worker_guard(f.store, f.state, "T01"), new)
        f.finish_repair(repair_plan=False)
        f.team.assign("T01", "acceptor", "fixture", "synthetic-extra-acceptor", "synthetic-session")
        evidence = f.attempt["verification"]["output"]["file"]
        self.assertTrue(f.team.accept("T01", f.report("acceptor", previousReport=f.attempt["rounds"][-1]["report"]["sha256"],
            inheritedProofs=[], trace=[{"requirement": "R01", "brief": "B001", "quote": "Set VALUE and EXTRA to one.",
                "result": "aligned", "note": "Synthetic acceptor checked the approved values", "artifact": evidence}], verdict="PASS")))
        f.host.save("extra-later-append")
        self.assertEqual((f.store.run / old["file"]).read_bytes(), old_bytes)
        self.assertEqual(f.attempt["writers"], [original, worker])
        self.assertEqual(len(f.attempt["rounds"]), 2)
        self.assertEqual(f.attempt["rounds"][0], f.before["taskHistory"]["T01"][-1]["rounds"][0])
        self.assertTrue(_history_valid(f.store, f.state, "T01"))
        self.assertTrue(direct_proof_current(f.store, f.state, "T01"))

    def test_zc4_exhausted_revision_budget_refuses_prepare_and_apply_readonly(self):
        f = self.fixture(revisions=16)
        before = self.snapshot(f)
        project = {p.relative_to(f.root).as_posix(): p.read_bytes() for p in f.root.rglob("*") if p.is_file()}
        state = copy.deepcopy(f.state)
        with self.assertRaisesRegex(Invalid, "revision|ревизи|исчерпан"):
            task_zone_extension.prepare(f.host, "T01", f.proposal(), actor=f.planner)
        with self.assertRaisesRegex(Invalid, "revision|ревизи|исчерпан"): f.extend()
        self.assertEqual(before, self.snapshot(f))
        self.assertEqual(state, f.state)
        self.assertEqual(project, {p.relative_to(f.root).as_posix(): p.read_bytes() for p in f.root.rglob("*") if p.is_file()})

    def test_zc4_last_available_revision_is_consumed_without_reset(self):
        f = self.fixture(lean=True, revisions=15)
        old = copy.deepcopy(f.attempt["testIntentHistory"])
        f.extend()
        f.finish_repair()
        self.assertEqual(f.attempt["testIntentHistory"][:-1], old)
        self.assertEqual(len(f.attempt["testIntentHistory"]), 16)
        self.assertEqual(len(f.attempt["rounds"]), 2)
        self.assertTrue(historical_proof_valid(f.store, f.state, "T01"))

    def test_shared_revision_budget_helper_controls_both_record_and_extension(self):
        with patch.object(test_intent, "revision_limit", return_value=8):
            f = self.fixture(revisions=8)
            before = self.snapshot(f)
            with self.assertRaises(Invalid): f.extend()
            self.assertEqual(before, self.snapshot(f))
        # A consistent synthetic legacy limit, not a scalableInputs migration:
        # current admission settings must never rejudge historical provenance.
        with patch.object(test_intent, "MAX_REVISIONS", 32), patch.object(test_intent, "revision_limit", return_value=32):
            f = self.fixture(lean=True, revisions=31)
            f.extend()
            f.finish_repair()
            self.assertEqual(len(f.attempt["testIntentHistory"]), 32)
            with self.assertRaises(Invalid): test_intent.require_revision_available(f.state, f.attempt)
            self.assertTrue(test_intent.history_valid(f.store, f.state, "T01"))

    def test_zc5_full_prepare_accepts_extensionless_and_literal_bracket_files_readonly(self):
        for name, exists in [("Makefile", True), ("Dockerfile", False), ("LICENSE", True),
                             ("routes/[slug].tsx", True), ("routes/[future].tsx", False)]:
            with self.subTest(path=name, exists=exists):
                f = self.fixture()
                path = f.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if exists:
                    path.write_text("literal exact file\n")
                if name.startswith("routes/"):
                    (path.parent / "s.tsx").write_text("unapproved neighboring literal\n")
                before = {p.relative_to(f.root).as_posix(): p.read_bytes()
                          for p in f.root.rglob("*") if p.is_file()}
                state = copy.deepcopy(f.state)
                prepared = task_zone_extension.prepare(f.host, "T01", f.proposal([name]), actor=f.planner)
                self.assertEqual(prepared["files"], [name])
                self.assertEqual(prepared["plan"][0]["zones"], [*f.host.task("T01")["zones"], name])
                self.assertEqual(before, {p.relative_to(f.root).as_posix(): p.read_bytes()
                                         for p in f.root.rglob("*") if p.is_file()})
                self.assertEqual(state, f.state)

    def test_zc5_extensionless_and_literal_bracket_files_recheck_live_type(self):
        f = self.fixture()
        names = ["Dockerfile", "routes/[slug].tsx"]
        f.extend(f.proposal(names))
        for name in names:
            path = f.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("literal ordinary file\n")
        task_basis(f.store, f.state, "T01")
        for name in names:
            with self.subTest(path=name):
                path = f.root / name
                path.unlink()
                path.mkdir()
                child = path / "unapproved_child.py"
                child.write_text("SIDE_EFFECT = 1\n")
                before = self.snapshot(f)
                marker = f.root / ".aispa/zc5-unsafe-ran"
                with self.assertRaises(Invalid):
                    f.flow.verify([fixtures.sys.executable, "-B", "-c",
                                   "from pathlib import Path; Path('.aispa/zc5-unsafe-ran').touch()"], 10, "T01")
                self.assertFalse(marker.exists())
                self.assertEqual(before, self.snapshot(f))
                child.unlink()
                path.rmdir()
                path.write_text("literal ordinary file\n")
        self.assertTrue(_history_valid(f.store, f.state, "T01"))

    def test_zc5_literal_paths_keep_secret_confined_wildcard_and_directory_guards(self):
        f = self.fixture()
        (f.root / "existing-directory").mkdir()
        (f.root / "linked-parent").symlink_to(f.root / "tests", target_is_directory=True)
        for name in ["existing-directory", "linked-parent/Makefile", ".env", "private.key",
                     "../Dockerfile", "/tmp/Dockerfile", ".", "routes/*.tsx", "routes/?.tsx"]:
            with self.subTest(path=name):
                before = self.snapshot(f)
                state = copy.deepcopy(f.state)
                with self.assertRaises((Invalid, ValueError)):
                    task_zone_extension.prepare(f.host, "T01", f.proposal([name]), actor=f.planner)
                self.assertEqual(before, self.snapshot(f))
                self.assertEqual(state, f.state)


if __name__ == "__main__":
    unittest.main()
