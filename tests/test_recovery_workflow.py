import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import recovery
from aispa_core.store import Invalid, Journal, digest, timestamp
from test_preflight import optional_module


class RecoveryWorkflowTests(unittest.TestCase):
    def test_managed_paused_verification_is_budgeted_without_clearing_recovery(self):
        resources = optional_module("resources")
        self.assertIsNotNone(resources, "Paused recovery is still a managed test-process caller")
        self.state.update(policy={"managedExecution": True}, hostSession="recovery-session")
        resources.configure(self.store, self.state, {"schema": 1, "scope": "project", "limits":
            {"agents": 1, "browsers": 1, "testProcesses": 1, "services": 1}})
        self.store.append(self.state, 'managed-recovery-fixture')
        ctx = resources.context(self.store, self.state, allow_paused=True)
        lease = resources.reserve(ctx, "testProcesses")
        with patch.object(recovery.process_runner, "_start") as start, self.assertRaises(Invalid):
            self.check()
        start.assert_not_called()
        self.assertTrue(self.state["journalRecovery"]["pending"])
        self.assertEqual(self.state["status"], "paused")
        resources.finish(ctx, lease["id"], outcome="launch_failed", confirmed=True)
        self.assertEqual(self.check()["exitCode"], 0)
        self.assertTrue(self.state["journalRecovery"]["pending"])
        self.assertEqual(self.state["status"], "paused")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-recovery-flow-🙂-")
        self.root = Path(self.temp.name).resolve()
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        (self.root / ".gitignore").write_text(".aispa/\n", encoding="utf-8")
        (self.root / "app.py").write_text("print('ok')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
        self.store = Journal(self.root)
        state = self.store.create("recovery", "Recovery workflow")
        state["taskHistory"] = {"T01": [{"id": "A01", "rounds": [{"number": 1}]}]}
        self.store.append(state, "history-fixture")
        with (self.store.run / "events.jsonl").open("ab") as stream:
            stream.write(b'{"sequence":3,"previous":')
        candidate = self.store.inspect_recovery()
        self.store.apply_recovery(candidate["journalSha256"], confirmed=True)
        self.state = self.store.load()

    def tearDown(self):
        self.temp.cleanup()

    def reviewer(self, *, fresh=True, role="recovery-reviewer"):
        runtime, native_id, session = "codex", "reviewer-01", "session-01"
        aid = digest([runtime, native_id, session])
        record = {"id": aid, "runtime": runtime, "nativeId": native_id, "session": session,
                  "role": role, "external": False, "fresh": fresh, "model": "test-model",
                  "run": self.state["run"], "at": timestamp(), "identityEvidence": "host-declared"}
        record["record"] = self.store.artifact("participants", (json.dumps(record,
            ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(), ".json")
        self.state.setdefault("participants", {})[aid] = record
        self.store.append(self.state, "participant-assigned")
        return aid

    def check(self, result=None):
        if result is None:
            receipt = recovery.verify(self.store, self.state, [sys.executable, "-c", "print('ok')"], 5)
        else:
            with patch.object(recovery.process_runner, "run", return_value=result):
                receipt = recovery.verify(self.store, self.state, [sys.executable, "-c", "print('ok')"], 5)
        self.state["journalRecovery"]["verification"] = receipt
        self.store.append(self.state, "recovery-verification-executed")
        return receipt

    def report(self, reviewer, *, decision="resume", effects="none"):
        fs = self.store.artifact("evidence", b"Inspected project files and backup.\n", ".txt")
        external = self.store.artifact("evidence", b"Checked external effect ledger.\n", ".txt")
        return {"basis": recovery.context(self.store, self.state)["basis"],
                "reviewer": reviewer, "independent": True, "decision": decision,
                "filesystem": {"inspected": True, "paths": ["app.py", ".aispa"],
                               "note": "Inspected source and recovery backup", "evidence": [fs]},
                "externalEffects": {"status": effects, "note": "Reconciled possible side effects",
                                    "evidence": [external]},
                "note": "Resume only after explicit review"}

    def test_success_requires_saved_fresh_proof_and_leaves_run_paused(self):
        actor = self.reviewer()
        receipt = self.check()
        self.assertEqual(receipt["purpose"], "recovery")
        report = self.report(actor)
        result = recovery.reconcile(self.store, self.state, report)
        self.assertEqual(result["decision"], "resume")
        self.assertFalse(self.state["journalRecovery"]["pending"])
        self.assertEqual(self.state["status"], "paused")
        self.assertEqual(self.state["taskHistory"]["T01"][0]["rounds"], [{"number": 1}])
        self.store.append(self.state, "recovery-reconciled")
        loaded = self.store.load()
        self.assertFalse(loaded["journalRecovery"]["pending"])
        self.assertEqual(loaded["status"], "paused")

    def test_unknown_external_effects_cannot_clear_pending(self):
        actor = self.reviewer()
        self.check()
        report = self.report(actor, effects="unknown")
        with self.assertRaises(Invalid):
            recovery.reconcile(self.store, self.state, report)
        self.assertTrue(self.state["journalRecovery"]["pending"])
        report["decision"] = "blocked"
        result = recovery.reconcile(self.store, self.state, report)
        self.assertEqual(result["decision"], "blocked")
        self.assertTrue(self.state["journalRecovery"]["pending"])
        self.store.append(self.state, "recovery-blocked")
        with self.assertRaises(Invalid):
            recovery.context(self.store, self.state)
        self.check()
        self.assertEqual(recovery.context(self.store, self.state)["requiredRole"], "recovery-reviewer")

    def test_failing_or_truncated_verification_cannot_reconcile(self):
        self.reviewer()
        for result in ((b"failed", 1, False, False, False),
                       (b"truncated", 0, False, True, False),
                       (b"timeout", 124, True, False, False),
                       (b"secret", 0, False, False, True)):
            with self.subTest(result=result[1:]):
                self.check(result)
                with self.assertRaises(Invalid):
                    recovery.context(self.store, self.state)

    def test_code_and_journal_cas_reject_stale_report(self):
        actor = self.reviewer()
        self.check()
        report = self.report(actor)
        (self.root / "app.py").write_text("print('changed')\n", encoding="utf-8")
        with self.assertRaises(Invalid):
            recovery.reconcile(self.store, self.state, report)
        (self.root / "app.py").write_text("print('ok')\n", encoding="utf-8")
        self.store.append(self.state, "unrelated-event")
        with self.assertRaises(Invalid):
            recovery.reconcile(self.store, self.state, report)

    def test_report_needs_fresh_independent_reviewer_and_immutable_evidence(self):
        actor = self.reviewer(fresh=False)
        self.check()
        report = self.report(actor)
        with self.assertRaises(Invalid):
            recovery.reconcile(self.store, self.state, report)
        self.state["participants"][actor]["fresh"] = True
        self.store.append(self.state, "tampered-participant")
        with self.assertRaises(Invalid):
            recovery.reconcile(self.store, self.state, report)

    def test_missing_or_changed_backup_blocks_verification(self):
        backup = self.store.run / self.state["journalRecovery"]["backupFile"]
        backup.write_bytes(b"changed")
        with self.assertRaises(Invalid):
            recovery.verify(self.store, self.state, [sys.executable, "-c", "print('ok')"], 5)

    def test_reviewer_evidence_is_immutable_and_paths_are_confined(self):
        actor = self.reviewer()
        self.check()
        report = self.report(actor)
        evidence = self.store.run / report["filesystem"]["evidence"][0]["file"]
        evidence.write_text("changed", encoding="utf-8")
        with self.assertRaises(Invalid):
            recovery.reconcile(self.store, self.state, report)
        report = self.report(actor)
        report["filesystem"]["paths"] = ["../outside"]
        with self.assertRaises(Invalid):
            recovery.reconcile(self.store, self.state, report)

    def test_unsaved_recovery_receipt_cannot_be_used(self):
        self.reviewer()
        self.state["journalRecovery"]["verification"] = recovery.verify(
            self.store, self.state, [sys.executable, "-c", "print('ok')"], 5)
        with self.assertRaises(Invalid):
            recovery.context(self.store, self.state)


if __name__ == "__main__":
    unittest.main()
