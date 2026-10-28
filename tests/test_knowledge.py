"""Adversarial knowledge-unit tests; native identities here are explicit fixtures."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import cleanroom, executions, knowledge
from aispa_core.store import Invalid, Journal, digest
from aispa_core.workflow import Workflow


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa knowledge space-🙂-")
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.write(".gitignore", ".aispa/\nignored/\n__pycache__/\n")
        self.write("README.md", "# Fixture\nUse the exact CLI command on Unicode input.\n")
        self.write("app.py", "print('checked fixture')\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
        self.store = Journal(self.root)
        self.state = self.store.create("knowledge", "Knowledge fixture")
        self.actor = knowledge.assign(self.store, self.state, "fixture", "fresh-writer", "test-session")
        self.store.append(self.state, "test-writer-assigned")
        self.workflow = Workflow(self.store, self.state)
        # This is a real process receipt. Only final acceptance is mocked in unit helpers.
        self.workflow.verify([sys.executable, "app.py"], 10)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True).stdout

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def source(self, name="app.py"):
        return {"path": name, "sha256": hashlib.sha256((self.root / name).read_bytes()).hexdigest()}

    def report(self, entries=None):
        entries = [{"key": "cli.encoding", "kind": "trap", "text": "Read input as UTF-8.",
                    "sources": [self.source()]}] if entries is None else entries
        covered = {entry["kind"] for entry in entries}
        return {"writer": self.actor["id"], "basis": self.store.code(), "entries": entries,
                "uncertainties": {kind: "No independently established lesson in this fixture."
                                  for kind in knowledge.KINDS if kind not in covered}}

    def command_entry(self, kind="project"):
        receipt = self.state["verification" if kind == "project" else "cleanrun"]
        return {"key": "cli.check", "kind": "command", "text": "Run the fixture CLI.", "sources": [self.source()],
                "command": list(receipt["command"]), "verification": {"kind": kind, "sha256": digest(receipt)}}

    def record(self, report=None):
        with patch.object(knowledge, "gate_valid", return_value=True):
            result = knowledge.record(self.store, self.state, report or self.report())
        self.store.append(self.state, "test-knowledge-recorded")
        return result

    def test_fresh_independent_writer_and_immutable_card(self):
        self.assertEqual(self.actor["role"], "knowledge-writer")
        for field in ("nativeId", "session", "fresh", "model"):
            changed = copy.deepcopy(self.state)
            changed["knowledge"]["writer"][field] = "changed"
            with self.subTest(field=field), patch.object(knowledge, "gate_valid", return_value=True), self.assertRaises(Invalid):
                knowledge.record(self.store, changed, self.report())
        for native, roles in (("worker", {"actor-1": {"nativeId": "worker"}}),
                              ("critic", {"critic": {"nativeId": "other"}})):
            state = copy.deepcopy(self.state)
            state["actors"] = roles
            with self.subTest(native=native), self.assertRaises(Invalid):
                knowledge.assign(self.store, state, "fixture", native, "changed-session")
        self.state["blindReview"] = {"reviewer": "blind-native"}
        with self.assertRaises(Invalid):
            knowledge.assign(self.store, self.state, "fixture", "blind-native", "any-session")
        with self.assertRaises(Invalid):
            knowledge.assign(self.store, self.state, "fixture", "fresh-writer", "different-session")
        with self.assertRaises(Invalid):
            knowledge.assign(self.store, self.state, "fixture", "new-writer", "session", fresh=False)

    def test_final_repair_participants_cannot_write_project_knowledge(self):
        """Workers and checkers of final repairs, replaced ones too, are not fresh knowledge authors."""
        def person(native):
            return {"id": "id-" + native, "nativeId": native, "session": "repair-session"}
        self.state["finalRepairs"] = [{"id": "F01", "kind": "findings", "status": "passed",
                                       "worker": person("repair-worker"),
                                       "rounds": [{"number": 1, "checker": person("repair-checker")}],
                                       "replaced": [{"role": "worker", "person": person("lost-repair-worker")}]}]
        for native in ("repair-worker", "repair-checker", "lost-repair-worker"):
            with self.subTest(native=native), self.assertRaisesRegex(Invalid, "автор знаний должен отличаться"):
                knowledge.assign(self.store, copy.deepcopy(self.state), "fixture", native, "knowledge-session")
        self.assertEqual(knowledge.assign(self.store, copy.deepcopy(self.state), "fixture", "unrelated-writer",
                                          "knowledge-session")["nativeId"], "unrelated-writer")

    def test_acceptance_current_verification_and_settled_tasks_required(self):
        with self.assertRaises(Invalid):
            knowledge.record(self.store, self.state, self.report())
        with patch.object(knowledge, "gate_valid", return_value=True):
            for status in ("pending", "running"):
                state = copy.deepcopy(self.state)
                state["tasks"] = [{"id": "T1", "status": status}]
                with self.subTest(status=status), self.assertRaises(Invalid):
                    knowledge.record(self.store, state, self.report())
            self.state["tasks"] = [{"id": "T1", "status": "blocked"}]
            self.store.append(self.state, "test-blocked-remaining")
            knowledge.record(self.store, self.state, self.report())
        self.write("app.py", "print('changed')\n")
        with patch.object(knowledge, "gate_valid", return_value=True), self.assertRaises(Invalid):
            knowledge.record(self.store, self.state, self.report())

    def test_exact_sources_and_explicit_selection_with_stale_exclusion(self):
        self.record()
        self.assertTrue(knowledge.valid(self.store, self.state))
        self.assertEqual(knowledge.query(self.store, self.state)["entries"], [])
        view = knowledge.query(self.store, self.state, paths=["app.py"])
        self.assertEqual(len(view["entries"]), 1)
        self.assertTrue(view["historicalOnly"])
        self.assertEqual(knowledge.query(self.store, self.state, paths=["docs"])["entries"], [])
        self.write("app.py", "print('new source')\n")
        self.assertFalse(knowledge.valid(self.store, self.state))
        self.assertTrue(knowledge.valid(self.store, self.state, check_sources=False))
        view = knowledge.query(self.store, self.state, kinds=["trap"])
        self.assertEqual(view["entries"], [])
        self.assertEqual(view["omitted"]["stale"], 1)

    def test_source_deleted_ignored_or_symlinked_is_never_reused(self):
        self.record()
        original = (self.root / "app.py").read_bytes()
        (self.root / "app.py").unlink()
        self.assertEqual(knowledge.query(self.store, self.state, paths=["app.py"])["omitted"]["stale"], 1)
        (self.root / "app.py").write_bytes(original)
        self.write(".gitignore", ".aispa/\napp.py\n")
        self.assertEqual(knowledge.query(self.store, self.state, kinds=["trap"])["omitted"]["stale"], 1)

    def test_command_requires_real_exact_successful_receipt_and_output(self):
        report = self.report([self.command_entry()])
        self.record(report)
        view = knowledge.query(self.store, self.state, kinds=["command"])
        row = view["entries"][0]
        self.assertEqual(row["command"], [sys.executable, "app.py"])
        self.assertTrue(row["evidence"]["verifiedAtRecording"])
        self.assertEqual(row["evidence"]["outputSha256"], self.state["verification"]["output"]["sha256"])
        self.assertNotIn("output", row["evidence"])
        for edit in (lambda e: e.update(command=["python", "not-run.py"]),
                     lambda e: e["verification"].update(sha256="0" * 64),
                     lambda e: e.update(verification={"kind": "made-up", "sha256": "0" * 64})):
            changed = self.report([self.command_entry()])
            edit(changed["entries"][0])
            with self.subTest(edit=edit), self.assertRaises(Invalid):
                self.record(changed)
        output = self.store.run / self.state["verification"]["output"]["file"]
        output.write_text("tampered output", encoding="utf-8")
        self.assertFalse(knowledge.valid(self.store, self.state, check_sources=False))
        with self.assertRaises(Invalid):
            knowledge.query(self.store, self.state, kinds=["command"])

    def test_fabricated_unsaved_project_receipt_is_rejected(self):
        self.state["verification"]["command"] = [sys.executable, "never-executed.py"]
        with self.assertRaises(Invalid):
            self.record(self.report([self.command_entry()]))

    def test_failed_or_redacted_receipt_cannot_be_called_verified(self):
        self.workflow.verify([sys.executable, "-c", "raise SystemExit(1)"], 10)
        with self.assertRaises(Invalid):
            self.record(self.report([self.command_entry()]))
        self.workflow.verify([sys.executable, "app.py"], 10)
        self.state["verification"]["redacted"] = True
        self.store.append(self.state, "test-corrupt-receipt")
        with self.assertRaises(Invalid):
            self.record(self.report([self.command_entry()]))

    def test_real_cleanrun_command_evidence_is_bound_to_clone(self):
        self.state["cleanroom"] = cleanroom.prepare(self.store, self.state)
        self.state["cleanrun"] = cleanroom.execute(self.store, self.state, [sys.executable, "app.py"], 10)
        self.store.append(self.state, "test-cleanrun")
        self.record(self.report([self.command_entry("cleanrun")]))
        row = knowledge.query(self.store, self.state, kinds=["command"])["entries"][0]
        self.assertEqual(row["evidence"]["kind"], "cleanrun")
        self.assertEqual(row["evidence"]["receiptSha256"], digest(self.state["cleanrun"]))

    def test_historical_execution_can_be_curated_after_source_candidate_drift(self):
        historical = executions.record(self.store, self.state, kind="project", purpose="run")
        self.store.append(self.state, "execution-recorded")
        self.write("app.py", "print('changed')\n")
        self.workflow.verify([sys.executable, "app.py"], 10)
        entry = {"key": "cli.historical", "kind": "command", "text": "Earlier CLI invocation succeeded.",
                 "sources": [self.source()], "command": [sys.executable, "app.py"],
                 "verification": {"kind": "execution", "sha256": historical["sha256"]}}
        self.record(self.report([entry]))
        row = knowledge.query(self.store, self.state, kinds=["command"])["entries"][0]
        self.assertTrue(row["evidence"]["verifiedAtRecording"])
        self.assertFalse(row["evidence"]["currentAtCuration"])
        self.assertEqual(row["evidence"]["executionId"], historical["id"])

    def test_failed_recorded_execution_is_not_command_proof(self):
        self.workflow.verify([sys.executable, "-c", "raise SystemExit(2)"], 10)
        failed = executions.record(self.store, self.state, kind="project", purpose="test")
        self.store.append(self.state, "execution-recorded")
        self.workflow.verify([sys.executable, "app.py"], 10)
        entry = {"key": "cli.failed", "kind": "command", "text": "A failed command cannot be recommended.",
                 "sources": [self.source()], "command": [sys.executable, "-c", "raise SystemExit(2)"],
                 "verification": {"kind": "execution", "sha256": failed["sha256"]}}
        with self.assertRaises(Invalid):
            self.record(self.report([entry]))

    def test_categories_need_honest_explanations_and_empty_result_is_allowed(self):
        report = self.report()
        report["uncertainties"].pop("command")
        with self.assertRaises(Invalid):
            self.record(report)
        self.record(self.report([]))
        self.assertTrue(knowledge.valid(self.store, self.state))
        self.assertEqual(knowledge.query(self.store, self.state, kinds=["trap"])["entries"], [])

    def test_superseding_appends_and_preserves_original_artifact(self):
        first = self.record()
        before = (self.store.run / first["file"]).read_bytes()
        second = self.report()
        second["entries"][0]["text"] = "Use UTF-8 and reject malformed input."
        self.record(second)
        self.assertEqual((self.store.run / first["file"]).read_bytes(), before)
        history = knowledge._history(self.store, self.state)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["entries"][0]["supersedes"], history[0]["entries"][0]["id"])
        self.assertEqual(knowledge.query(self.store, self.state, paths=["app.py"])["entries"][0]["text"], second["entries"][0]["text"])
        self.state["knowledge"]["records"].reverse()
        self.assertFalse(knowledge.valid(self.store, self.state, check_sources=False))

    def test_traversal_secret_paths_and_secrets_in_text_rejected(self):
        for name in ("../app.py", "/app.py", "C:/app.py", "docs/CON.md", "docs/file.md:stream", ".env", "private/key.py"):
            report = self.report()
            report["entries"][0]["sources"][0]["path"] = name
            with self.subTest(path=name), self.assertRaises(Invalid):
                self.record(report)
        report = self.report()
        report["entries"][0]["text"] = "api_key = '" + "sk-" + "privateFixture0000000000000'"
        with self.assertRaises(Invalid):
            self.record(report)
        self.write("docs/safe.md", "access_token = '" + "ghp_" + "privateFixture0000000000000'\n")
        self.workflow.verify([sys.executable, "app.py"], 10)
        report = self.report()
        report["entries"][0]["sources"] = [self.source("docs/safe.md")]
        with self.assertRaises(Invalid):
            self.record(report)
        for name in ("../", "C:/outside", "docs/NUL"):
            with self.subTest(query=name), self.assertRaises(Invalid):
                knowledge.query(self.store, self.state, paths=[name])

    def test_symlink_file_and_parent_are_rejected_before_source_read(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other)
            (outside / "README.md").write_text("OUTSIDE_PRIVATE_DATA", encoding="utf-8")
            (self.root / "linked.md").symlink_to(outside / "README.md")
            (self.root / "docs").symlink_to(outside, target_is_directory=True)
            for name in ("linked.md", "docs/README.md"):
                with self.subTest(path=name), self.assertRaises(Invalid):
                    knowledge._sources(self.store, [{"path": name, "sha256": "0" * 64}], {name})

    def test_late_native_identity_collision_invalidates_curation(self):
        self.record()
        self.state["actors"] = {"later-task-actor": {"nativeId": "fresh-writer"}}
        self.assertFalse(knowledge.valid(self.store, self.state, check_sources=False))
        with self.assertRaises(Invalid):
            knowledge.query(self.store, self.state, kinds=["trap"])

    def test_record_and_query_bounds_never_silently_drop_history(self):
        rows = [{"key": f"cli.trap-{i}", "kind": "trap", "text": "Bounded fixture lesson " * 20,
                 "sources": [self.source()]} for i in range(6)]
        self.record(self.report(rows))
        result = knowledge.query(self.store, self.state, paths=["."], limit=2)
        self.assertEqual(len(result["entries"]), 2)
        self.assertEqual(result["omitted"]["limit"], 4)
        result = knowledge.query(self.store, self.state, paths=["."], max_chars=256)
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["omitted"]["limit"], 6)
        with patch.object(knowledge, "MAX_RECORDS", 1), self.assertRaises(Invalid):
            self.record()

    def close(self):
        self.state["status"] = "accepted"
        self.store.append(self.state, "test-terminal")

    def next_run(self, payload):
        state = self.store.create("next", "Next knowledge fixture", lambda new: knowledge.attach(self.store, new, payload))
        self.state = state
        return state

    def test_terminal_capture_and_attach_preserve_knowledge_without_old_pass(self):
        self.record(self.report([self.command_entry()]))
        with self.assertRaises(Invalid):
            knowledge.capture(self.store, self.state)
        self.close()
        payload = knowledge.capture(self.store, self.state)
        self.next_run(payload)
        self.assertFalse(knowledge.valid(self.store, self.state))
        row = knowledge.query(self.store, self.state, kinds=["command"])["entries"][0]
        self.assertTrue(row["evidence"]["verifiedAtRecording"])
        self.assertIsNone(self.state["verification"])
        self.assertEqual(self.state["tasks"], [])
        self.assertNotEqual(row["originRun"], self.state["run"])
        self.write("app.py", "print('next run edits')\n")
        self.assertEqual(knowledge.query(self.store, self.state, paths=["app.py"])["omitted"]["stale"], 1)

    def test_attach_rechecks_closed_history_and_command_evidence(self):
        self.record(self.report([self.command_entry()]))
        self.close()
        payload = knowledge.capture(self.store, self.state)
        output = self.store.run / self.state["verification"]["output"]["file"]
        output.write_text("changed after capture", encoding="utf-8")
        with self.assertRaises(Invalid):
            self.next_run(payload)
        self.assertEqual(self.store.load()["status"], "accepted")

    def test_handoff_artifact_is_self_contained_hash_protected_and_lossless(self):
        self.record()
        self.record()
        self.close()
        payload = knowledge.capture(self.store, self.state)
        self.next_run(payload)
        projected = knowledge._history(self.store, self.state)
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["archivedRecordCount"], 2)
        pages = list(knowledge._walk(self.store, self.state))
        self.assertEqual(len(pages[-1][2]), 2)
        path = self.store.run / self.state["knowledge"]["inherited"]["file"]
        value = json.loads(path.read_text(encoding="utf-8"))
        value["records"][0]["entries"][0]["text"] = "tampered"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(Invalid):
            knowledge.query(self.store, self.state, kinds=["trap"])

    def test_next_run_uses_new_writer_and_supersedes_without_erasing_history(self):
        self.record()
        self.close()
        payload = knowledge.capture(self.store, self.state)
        self.next_run(payload)
        with self.assertRaises(Invalid):
            knowledge.assign(self.store, self.state, "fixture", "fresh-writer", "different-session")
        self.actor = knowledge.assign(self.store, self.state, "fixture", "second-fresh-writer", "test-session-2")
        self.store.append(self.state, "test-second-writer")
        self.workflow = Workflow(self.store, self.state)
        self.workflow.verify([sys.executable, "app.py"], 10)
        self.record()
        self.assertTrue(knowledge.valid(self.store, self.state))
        history = knowledge._history(self.store, self.state)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["entries"][0]["supersedes"], history[0]["entries"][0]["id"])
        self.assertEqual(len(knowledge.query(self.store, self.state, kinds=["trap"])["entries"]), 1)
        self.close()
        second = knowledge.capture(self.store, self.state)
        self.assertEqual(len(second["records"]), 1)
        self.assertEqual(second["records"][0]["archivedRecordCount"], 2)

    def compact(self):
        ref = knowledge.compact(self.store, self.state)
        self.store.append(self.state, "test-knowledge-compacted")
        return ref

    def test_sixty_fifth_record_in_one_run_has_real_compaction_recovery(self):
        first = self.record()
        original = (self.store.run / first["file"]).read_bytes()
        for _ in range(63):
            self.record()
        with self.assertRaisesRegex(Invalid, "knowledge-compact"):
            self.record()
        self.compact()
        self.assertEqual(self.state["knowledge"]["records"], [])
        self.assertTrue(knowledge.valid(self.store, self.state))
        self.record()
        self.assertEqual(len(self.state["knowledge"]["records"]), 1)
        self.assertEqual((self.store.run / first["file"]).read_bytes(), original)
        self.assertEqual(knowledge.query(self.store, self.state, kinds=["trap"])["archive"]["archivedRecordCount"], 64)

    def test_sixty_five_terminal_runs_stay_bounded_without_losing_provenance(self):
        for number in range(65):
            if number:
                self.actor = knowledge.assign(self.store, self.state, "fixture", f"writer-{number}", "test-session")
                self.store.append(self.state, "test-new-writer")
                self.workflow = Workflow(self.store, self.state)
                self.workflow.verify([sys.executable, "app.py"], 10)
            self.record()
            self.assertTrue(knowledge.valid(self.store, self.state))
            self.close()
            payload = knowledge.capture(self.store, self.state)
            self.assertEqual(len(payload["records"]), 1)
            self.assertEqual(payload["records"][0]["archivedRecordCount"], number + 1)
            self.assertLess(len(knowledge._json(payload)), 8000)
            if number < 64:
                self.next_run(payload)
                self.assertFalse(knowledge.valid(self.store, self.state))
        self.next_run(payload)
        with self.assertRaises(Invalid):
            knowledge.assign(self.store, self.state, "fixture", "fresh-writer", "new-session")

    def test_compaction_before_first_curation_cannot_satisfy_finish(self):
        self.record()
        self.close()
        self.next_run(knowledge.capture(self.store, self.state))
        self.actor = knowledge.assign(self.store, self.state, "fixture", "new-writer", "session")
        self.store.append(self.state, "test-writer")
        self.compact()
        self.assertFalse(knowledge.valid(self.store, self.state))

    def test_hot_overflow_is_explicit_and_archive_search_recovers_exact_entries(self):
        rows = [{"key": f"trap.{name}", "kind": "trap", "text": f"Fixture {name}.", "sources": [self.source()]}
                for name in ("first", "second", "last")]
        self.record(self.report(rows))
        with patch.object(knowledge, "MAX_HOT_ENTRIES", 1):
            self.compact()
        current = knowledge.query(self.store, self.state, kinds=["trap"])
        self.assertEqual([row["key"] for row in current["entries"]], ["trap.last"])
        self.assertIsNotNone(current["archive"]["unread"])
        archived = knowledge.query(self.store, self.state, kinds=["trap"], include_archive=True)
        self.assertEqual({row["key"] for row in archived["entries"]}, {row["key"] for row in rows})
        self.assertIsNone(archived["archive"]["unread"])
        # Updating an archived key must supersede the actual old ID, not create an unrelated definition.
        old = next(row for row in archived["entries"] if row["key"] == "trap.first")
        changed = dict(rows[0], text="Updated archived lesson.")
        self.record(self.report([changed]))
        new = knowledge.query(self.store, self.state, kinds=["trap"], include_archive=True)
        first = next(row for row in new["entries"] if row["key"] == "trap.first")
        self.assertEqual(first["supersedes"], old["id"])
        self.assertEqual(first["text"], changed["text"])
        wrong_kind = dict(changed, kind="decision")
        with self.assertRaises(Invalid):
            self.record(self.report([wrong_kind]))

    def test_archive_budget_reports_unread_ancestry_and_old_versions_never_resurrect(self):
        first = self.record()
        first_source = (self.root / "app.py").read_bytes()
        self.compact()
        self.write("app.py", "print('new version')\n")
        self.workflow.verify([sys.executable, "app.py"], 10)
        self.record()
        self.compact()
        (self.root / "app.py").write_bytes(first_source)
        view = knowledge.query(self.store, self.state, kinds=["trap"], include_archive=True, archive_depth=1)
        self.assertEqual(view["entries"], [])
        self.assertEqual(view["omitted"]["stale"], 1)
        self.assertIsNotNone(view["archive"]["unread"])
        view = knowledge.query(self.store, self.state, kinds=["trap"], include_archive=True, archive_depth=4)
        self.assertEqual(view["entries"], [])
        self.assertIsNone(view["archive"]["unread"])
        # Immutable canonical records stay in place and are verified when traversed.
        (self.store.run / first["file"]).write_text("tampered", encoding="utf-8")
        with self.assertRaises(Invalid):
            knowledge.query(self.store, self.state, kinds=["trap"], include_archive=True)

    def test_archive_pointer_cannot_escape_and_compaction_requires_saved_state(self):
        self.record()
        self.state["notes"].append("unsaved change")
        with self.assertRaises(Invalid):
            knowledge.compact(self.store, self.state)
        self.state["notes"].pop()
        self.compact()
        for run in ("../../outside", "C:/outside", "/outside", "nested/run"):
            with self.subTest(run=run), self.assertRaises(Invalid):
                knowledge._archive_state(self.store, {"run": run, "file": "knowledge/x.json", "sha256": "0" * 64})


if __name__ == "__main__":
    unittest.main()
