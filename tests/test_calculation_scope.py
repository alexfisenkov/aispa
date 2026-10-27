"""Behavioral boundaries for one read-only projection, including CLI publication."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import dashboard_reader, dashboard_snapshot, display
from aispa_core.store import Invalid, Journal, digest
from aispa_core.verified_reads import ResourceLimit, VerifiedReads, current_scope
import test_cli_output as cli_helpers
import test_workflow as helpers


class CalculationScopeTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    git = helpers.WorkflowTests.git
    commit = helpers.WorkflowTests.commit
    file = helpers.WorkflowTests.file

    def reader(self):
        reader = dashboard_reader.SnapshotJournal(self.root)
        return reader, reader.load()

    def calculation(self, projection):
        asset = b"frozen dashboard fixture"
        loaded = {"run": self.state["run"], "version": self.state["version"],
                  "assetSha256": hashlib.sha256(asset).hexdigest(),
                  "dashboardVersion": self.state["version"], "readerMode": "strict"}
        return dashboard_snapshot.calculate(self.root, dashboard_reader.SnapshotJournal,
                                            loaded, self.state.get("runtimeDigest"), lambda: asset, projection)

    def test_explicit_scope_has_operation_constant_full_journal_hashes(self):
        reader, _ = self.reader()
        with patch.object(Journal, "_hash_file", wraps=Journal._hash_file) as hashes:
            with reader._reads.scope():
                for _ in range(12):
                    reader.read_journal(include_events=True)
                    reader.event_pair(1)
                    reader.task_timeline("T01")
                basis = reader.finish_snapshot()
        self.assertGreaterEqual(hashes.call_count, 1, "Final validation must hash raw journal bytes")
        self.assertLessEqual(hashes.call_count, 2,
                             "Nested projection rehashed events.jsonl instead of reusing its explicit scope")
        self.assertEqual(basis["journalSha256"], hashlib.sha256((self.store.run / "events.jsonl").read_bytes()).hexdigest())

    def test_cached_artifact_json_and_journal_refuse_expired_scope(self):
        ref = self.store.artifact("reports", b'{"value":"stable"}', ".json")
        reader, _ = self.reader()
        with self.assertRaisesRegex(ResourceLimit, "operationSeconds"), reader._reads.scope():
            self.assertEqual(reader.json(ref)["value"], "stable")
            reader.read_journal(include_events=True)
            reader.code()
            reader._reads._stream_budget().deadline = -1.0
            for operation in (lambda: reader.artifact_bytes(ref), lambda: reader.json(ref),
                              lambda: reader.read_journal(include_events=True), reader.code):
                with self.subTest(operation=operation), self.assertRaisesRegex(Invalid, "operationSeconds"):
                    operation()

    def test_final_pointer_checks_use_checked_fresh_reads(self):
        reader, _ = self.reader()
        pointer = self.store.home / "active.json"
        original = Path.read_bytes

        def guarded(path, *args, **kwargs):
            if path == pointer:
                raise AssertionError("raw active pointer Path.read_bytes")
            return original(path, *args, **kwargs)

        with reader._reads.scope(), patch.object(Path, "read_bytes", guarded):
            reader.finish_snapshot()

    def test_final_candidate_digest_cannot_finish_after_deadline_outside_scope(self):
        reader, _ = self.reader()
        original = dashboard_reader.digest

        def expire_after_digest(value):
            result = original(value)
            reader._reads._stream_budget().deadline = -1.0
            return result

        with patch.object(dashboard_reader, "digest", side_effect=expire_after_digest):
            with self.assertRaisesRegex(ResourceLimit, "operationSeconds"):
                reader.finish_snapshot()

    def test_malformed_active_pointer_is_a_controlled_snapshot_refusal(self):
        reader, _ = self.reader()
        pointer = self.store.home / "active.json"
        original = pointer.read_bytes()
        try:
            pointer.write_bytes(b"[]")
            with reader._reads.scope(), self.assertRaises(Invalid):
                reader.finish_snapshot()
        finally:
            pointer.write_bytes(original)

    def test_outside_scope_and_unrelated_scope_still_refuse_immediate_corruption(self):
        for unrelated_scope in (False, True):
            with self.subTest(unrelated_scope=unrelated_scope):
                reader, _ = self.reader()
                path = self.store.run / "events.jsonl"
                original = path.read_bytes()
                reader.read_journal()
                try:
                    path.write_bytes(original + b"broken\n")
                    if unrelated_scope:
                        with VerifiedReads().scope(), self.assertRaises(Invalid):
                            reader.read_journal()
                    else:
                        with self.assertRaises(Invalid):
                            reader.read_journal()
                finally:
                    path.write_bytes(original)

    def test_include_events_upgrade_cannot_adopt_valid_append_into_frozen_generation(self):
        reader, _ = self.reader()
        with reader._reads.scope():
            frozen = reader.read_journal()
            self.state["title"] = "Concurrent committed generation"
            self.store.append(self.state, "fixture-concurrent-append")
            with self.assertRaises(Invalid, msg="include_events adopted a journal newer than the frozen generation"):
                reader.read_journal(include_events=True)
        self.assertNotEqual(frozen["sha256"], Journal(self.root)._hash_file(self.store.run / "events.jsonl"))

    def test_final_validation_bypasses_virtual_cursor_and_same_size_mtime(self):
        reader, _ = self.reader()
        path = self.store.run / "events.jsonl"
        old_stat, raw = path.stat(), path.read_bytes()
        changed = raw.replace("Пример".encode(), "Замена".encode())
        self.assertNotEqual(changed, raw)
        self.assertEqual(len(changed), len(raw))
        with reader._reads.scope():
            frozen = reader._journal_cursor(include_events=True)
            path.write_bytes(changed)
            os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
            # A cached/overridden cursor is not an independent final raw read.
            with patch.object(reader, "_journal_cursor", return_value=frozen):
                with self.assertRaises(Invalid, msg="Final generation check trusted its virtual cached cursor"):
                    reader.finish_snapshot()

    def test_final_validation_refuses_append_replace_and_active_pointer_drift(self):
        for mutation in ("append", "replace", "active"):
            with self.subTest(mutation=mutation):
                reader, _ = self.reader()
                path, pointer = self.store.run / "events.jsonl", self.store.home / "active.json"
                raw, active = path.read_bytes(), pointer.read_bytes()
                try:
                    with reader._reads.scope():
                        reader.read_journal(include_events=True)
                        if mutation == "append":
                            path.write_bytes(raw + b"invalid appended row\n")
                        elif mutation == "replace":
                            replacement = path.with_name("replacement.jsonl")
                            replacement.write_bytes(raw.replace(b'"init"', b'"edit"'))
                            replacement.replace(path)
                        else:
                            pointer.write_text(json.dumps({"run": "another-generation"}), encoding="utf-8")
                        with self.assertRaises(Invalid):
                            reader.finish_snapshot()
                finally:
                    path.write_bytes(raw)
                    pointer.write_bytes(active)

    def test_journal_event_and_timeline_results_are_detached(self):
        self.state["taskHistory"] = {"T01": [{"id": "fixture-attempt", "testIntentHistory": [{"id": "intent"}]}]}
        self.store.append(self.state, "fixture-history")
        reader, _ = self.reader()
        with reader._reads.scope():
            snapshot = reader.read_journal(include_events=True)
            expected = copy.deepcopy(snapshot)
            snapshot["last"]["state"]["title"] = "poison"
            snapshot["events"].clear()
            pair, _ = reader.event_pair(2)
            pair["state"]["title"] = "pair poison"
            timeline = reader.task_timeline("T01")
            timeline[-1]["state"]["taskHistory"]["T01"][0]["testIntentHistory"].clear()
            self.assertEqual(reader.read_journal(include_events=True), expected)
            self.assertEqual(reader.task_timeline("T01")[-1]["state"]["taskHistory"]["T01"][0]
                             ["testIntentHistory"], [{"id": "intent"}])
            reader.finish_snapshot()

    def test_failed_calculation_does_not_poison_next_fresh_scope(self):
        def broken(reader, state):
            self.assertIs(current_scope(), reader._reads)
            reader.read_journal(include_events=True)
            raise Invalid("fixture projection refusal")
        with self.assertRaisesRegex(Invalid, "fixture projection refusal"):
            self.calculation(broken)
        self.assertIsNone(current_scope())
        first, _ = self.calculation(lambda reader, state: {"title": state["title"], "nested": reader.event_pair(1)[0]})
        first["nested"]["state"]["title"] = "poison"
        second, _ = self.calculation(lambda reader, state: {"title": state["title"], "nested": reader.event_pair(1)[0]})
        self.assertEqual(second["nested"]["state"]["title"], self.state["title"])
        self.assertIsNone(current_scope())

    def test_http_calculation_retries_valid_append_without_returning_old_generation(self):
        calls = []
        def projection(reader, state):
            calls.append(state["title"])
            reader.read_journal(include_events=True)
            if len(calls) == 1:
                self.state["title"] = "Fresh after concurrent append"
                self.store.append(self.state, "fixture-new-generation")
            return {"title": state["title"]}
        view, basis = self.calculation(projection)
        self.assertEqual(calls, ["Пример", "Fresh after concurrent append"])
        self.assertEqual(view["title"], self.state["title"])
        self.assertEqual(basis["journalEvents"], 2)

    def test_postmutation_publish_uses_fresh_strict_reader_and_explicit_scope(self):
        self.flow.decision("Committed before projection")
        original = display.project
        def inspect(reader, state):
            self.assertIsNot(reader, self.store, "CLI publish reused the mutable writer for projection")
            self.assertIsInstance(reader, dashboard_reader.SnapshotJournal)
            self.assertIs(current_scope(), reader._reads, "CLI publish lacks an explicit calculation scope")
            self.assertEqual(digest(state), digest(self.state))
            with patch.object(Journal, "_hash_file", wraps=Journal._hash_file) as hashes:
                for _ in range(8):
                    reader.read_journal(include_events=True)
                    reader.event_pair(1)
                    reader.task_timeline("T01")
            self.assertLessEqual(hashes.call_count, 1, "CLI scope still rehashed every nested journal read")
            return original(reader, state)
        with patch.object(display, "project", side_effect=inspect):
            view = display.publish(self.store, self.state)
        self.assertEqual(view["decisions"][-1]["text"], "Committed before projection")
        self.assertEqual(json.loads((self.store.home / "status.json").read_text(encoding="utf-8")), view)

    def test_publish_state_digest_mismatch_cannot_overwrite_last_valid_status(self):
        display.publish(self.store, self.state)
        status = self.store.home / "status.json"
        previous = status.read_bytes()
        supplied = copy.deepcopy(self.state)
        supplied["title"] = "Not the committed state"
        with self.assertRaises(Invalid, msg="Publication accepted state that was never committed"):
            display.publish(self.store, supplied)
        self.assertEqual(status.read_bytes(), previous)

    def test_publish_final_generation_failure_cannot_write_status(self):
        display.publish(self.store, self.state)
        status = self.store.home / "status.json"
        previous = status.read_bytes()
        original = display.project
        def switch_after_projection(reader, state):
            view = original(reader, state)
            (self.store.home / "active.json").write_text(json.dumps({"run": "switched"}), encoding="utf-8")
            return view
        with patch.object(display, "project", side_effect=switch_after_projection):
            with self.assertRaises(Invalid, msg="CLI publication skipped final active-generation validation"):
                display.publish(self.store, self.state)
        self.assertEqual(status.read_bytes(), previous)


class CliProjectionScopeTests(unittest.TestCase):
    setUp = cli_helpers.CliOutputTests.setUp
    git = cli_helpers.CliOutputTests.git
    call = cli_helpers.CliOutputTests.call
    init = cli_helpers.CliOutputTests.init
    full_response = cli_helpers.CliOutputTests.full_response

    def candidate(self):
        spec = importlib.util.spec_from_file_location("aispa_calculation_cli_fixture", self.cli)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def execute(self, module, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = module.execute(module.parser().parse_args(["--root", str(self.root), *argv]))
        self.assertNotIn("Traceback", stderr.getvalue())
        return code, json.loads(stdout.getvalue())

    def test_batch_mutations_get_independent_postcommit_scopes_and_compact_parity(self):
        self.init()
        candidate, seen, original = self.candidate(), [], display.project
        steps = self.root / ".aispa/steps.json"
        steps.write_text(json.dumps([["decision", "First"], ["decision", "Second"]]), encoding="utf-8")
        def inspect(reader, state):
            self.assertIsInstance(reader, dashboard_reader.SnapshotJournal,
                                  "Batch CLI publication still projects through its writable Journal")
            self.assertIs(current_scope(), reader._reads)
            seen.append((reader, reader._reads, copy.deepcopy(state)))
            return original(reader, state)
        with patch.object(display, "project", side_effect=inspect):
            code, result = self.execute(candidate, ["batch", "--file", str(steps)])
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 2)
        self.assertIsNot(seen[0][0], seen[1][0])
        self.assertIsNot(seen[0][1], seen[1][1])
        self.assertEqual([len(row[2]["notes"]) for row in seen], [1, 2])
        for row in result["steps"]:
            receipt = row["output"]
            full = self.full_response(receipt)
            self.assertEqual(receipt["gates"], full["gates"])
            self.assertEqual(receipt["counts"], full["counts"])
            self.assertEqual(receipt["receipt"], full["receipt"])

    def test_internal_projection_failure_preserves_once_committed_full_and_compact_receipt(self):
        self.init()
        candidate = self.candidate()
        for compact in (False, True):
            with self.subTest(compact=compact):
                reader = Journal(self.root)
                state = reader.load()
                before = reader.read_journal()["count"]
                with patch.object(display, "project", side_effect=Invalid("private projection detail")):
                    code, output = self.execute(candidate, [*(["--compact"] if compact else []), "decision", "Only once"])
                state = reader.load()
                self.assertEqual(code, 0)
                self.assertEqual(reader.read_journal()["count"], before + 1)
                self.assertTrue(output["committed"])
                self.assertTrue(output["doNotRepeat"])
                self.assertEqual(output["viewError"], {"code": "projection_unavailable"})
                self.assertNotIn("gates", output)
                if compact:
                    self.assertEqual(self.full_response(output)["committedEvent"], output["committedEvent"])
                self.assertEqual(len(state["notes"]), 1 + int(compact))

    def test_failed_verify_keeps_exit_two_and_full_compact_failure_evidence(self):
        self.init()
        from aispa_core import preflight
        from aispa_core.workflow import Workflow
        from test_preflight import synthetic_host_snapshot
        fixture_store = Journal(self.root)
        with fixture_store.lock():
            fixture_state = fixture_store.load()
            host = synthetic_host_snapshot(fixture_store, fixture_state, session="calculation-cli-fixture")
            adoption = preflight.adopt(Workflow(fixture_store, fixture_state), host)
            self.assertTrue(adoption["ok"])
        self.assertEqual(Journal(self.root).load()["hostSession"], "calculation-cli-fixture")
        candidate = self.candidate()
        code, output = self.execute(candidate, ["--compact", "verify", "--", sys.executable, "-c",
                                               "import sys; print('scope-failure-evidence'); sys.exit(7)"])
        self.assertEqual(code, 2)
        full = self.full_response(output)
        self.assertFalse(output["ok"])
        self.assertEqual(output["gates"], full["gates"])
        reader = Journal(self.root)
        state = reader.load()
        self.assertEqual(state["verification"]["exitCode"], 7)
        self.assertIn("scope-failure-evidence", reader.text(state["verification"]["output"]))


if __name__ == "__main__":
    unittest.main()
