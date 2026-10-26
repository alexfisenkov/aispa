"""Positive validity memo stays inside one authenticated read calculation."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import contracts, display, mixed_evidence, scale, team
from aispa_core.dashboard_reader import SnapshotJournal
from aispa_core.store import Invalid, Journal
from aispa_core.verified_reads import ResourceLimit, VerifiedReads
import test_workflow as helpers


class VerdictReadScopeTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    git = helpers.WorkflowTests.git
    commit = helpers.WorkflowTests.commit
    file = helpers.WorkflowTests.file

    def tearDown(self):
        # Restore fixture directory substitutions before its owning temp root.
        self.doCleanups()
        helpers.WorkflowTests.tearDown(self)

    def reader(self, root=None):
        reader = SnapshotJournal(root or self.root)
        reader.load()
        return reader

    def record(self):
        output = self.store.artifact("evidence", b"Synthetic immutable evidence\n", ".txt")
        report = self.store.artifact("reports", b'{"basis":"fixture-basis","reviewer":"fixture-actor"}', ".json")
        return team._seal(self.store, {"basis": "fixture-basis", "actor": "fixture-actor",
             "report": report, "artifacts": [output], "verification": {
                 "exitCode": 0, "changedCode": False, "candidate": "fixture-basis", "output": output}})

    def replace_parent_with_identical_symlink(self, parent):
        external = tempfile.TemporaryDirectory(prefix="aispa-identical-parent-")
        self.addCleanup(external.cleanup)
        target = Path(external.name) / "identical"
        shutil.copytree(parent, target)
        backup = parent.with_name(parent.name + "-original")
        parent.rename(backup)
        try:
            parent.symlink_to(target, target_is_directory=True)
        except OSError as error:
            backup.rename(parent)
            if os.name == "nt" and getattr(error, "winerror", None) in (5, 1314):
                self.skipTest("Native Windows account cannot create this directory symlink")
            raise
        def restore():
            parent.unlink()
            backup.rename(parent)
        self.addCleanup(restore)

    def test_identical_artifact_reference_is_confined_once_per_calculation(self):
        ref = self.store.artifact("reports", b'{"fixture":"valid"}', ".json")
        reader = self.reader()
        with patch.object(contracts, "confined", wraps=contracts.confined) as checks:
            with reader._reads.scope():
                for _ in range(20):
                    self.assertTrue(contracts.artifact_valid(reader, ref))
                self.assertEqual(checks.call_count, 1,
                                 "Identical artifact reference repeated its full predicate within one read scope")
                reader.finish_snapshot()

    def test_identical_sealed_record_runs_mixed_validation_once(self):
        record, reader = self.record(), self.reader()
        with patch.object(mixed_evidence, "record_valid", wraps=mixed_evidence.record_valid) as checks:
            with reader._reads.scope():
                for _ in range(15):
                    self.assertTrue(team._record_valid(reader, copy.deepcopy(record)))
                self.assertEqual(checks.call_count, 1,
                                 "Identical sealed record repeated its full mixed-evidence predicate")
                reader.finish_snapshot()

    def test_writer_outside_unrelated_and_foreign_reader_scopes_do_not_reuse_verdicts(self):
        record, reader, foreign = self.record(), self.reader(), self.reader()
        cases = [(self.store, None), (reader, None), (reader, VerifiedReads()), (foreign, reader._reads)]
        for store, scope in cases:
            with self.subTest(store=type(store).__name__, scope=scope is not None):
                with patch.object(mixed_evidence, "record_valid", wraps=mixed_evidence.record_valid) as checks:
                    if scope is None:
                        for _ in range(2):
                            self.assertTrue(team._record_valid(store, record))
                    else:
                        with scope.scope():
                            for _ in range(2):
                                self.assertTrue(team._record_valid(store, record))
                    self.assertEqual(checks.call_count, 2)

    def test_a_later_scope_or_load_does_not_inherit_previous_verdict(self):
        record, reader = self.record(), self.reader()
        with patch.object(mixed_evidence, "record_valid", wraps=mixed_evidence.record_valid) as checks:
            for _ in range(2):
                with reader._reads.scope():
                    self.assertTrue(team._record_valid(reader, record))
                    self.assertTrue(team._record_valid(reader, record))
            reader.load()
            with reader._reads.scope():
                self.assertTrue(team._record_valid(reader, record))
            self.assertEqual(checks.call_count, 3, "Validity was reused outside its calculation lifetime")

    def test_full_reference_and_record_content_are_keys_including_mutable_objects(self):
        record, reader = self.record(), self.reader()
        with reader._reads.scope():
            self.assertTrue(team._record_valid(reader, record))
            changed = copy.deepcopy(record)
            for field, value in (("basis", "foreign"), ("actor", "foreign"), ("passed", True)):
                changed.clear(); changed.update(copy.deepcopy(record)); changed[field] = value
                self.assertFalse(team._record_valid(reader, changed), field)
            ref = copy.deepcopy(record["report"])
            self.assertTrue(contracts.artifact_valid(reader, ref))
            ref["sha256"] = "0" * 64
            self.assertFalse(contracts.artifact_valid(reader, ref))
            ref.update(record["report"])
            ref["file"] = "../" + ref["file"]
            self.assertFalse(contracts.artifact_valid(reader, ref))

    def test_non_json_key_values_preserve_original_predicate_semantics(self):
        record, reader = self.record(), self.reader()
        opaque = object()
        with reader._reads.scope():
            ref = {**record["report"], "ignoredMetadata": opaque}
            self.assertTrue(contracts.artifact_valid(reader, ref))
            self.assertFalse(team._record_valid(reader, {**record, "unsealedMetadata": opaque}))

    def sealed_metadata(self, metadata):
        body = {key: value for key, value in self.record().items() if key != "record"}
        return team._seal(self.store, {**body, "metadata": metadata})

    def test_nested_tuple_cannot_borrow_a_positive_list_record_verdict(self):
        record = self.sealed_metadata({"nested": [["value"]]})
        changed = copy.deepcopy(record)
        changed["metadata"]["nested"][0] = ("value",)
        self.assertFalse(team._record_valid(self.store, changed))
        reader = self.reader()
        with reader._reads.scope():
            self.assertTrue(team._record_valid(reader, record))
            self.assertFalse(team._record_valid(reader, changed),
                             "Tuple/list JSON normalization reused a different Python predicate")
            reader.finish_snapshot()

    def test_non_string_dict_key_cannot_borrow_a_string_key_verdict(self):
        record = self.sealed_metadata({"1": "value"})
        changed = copy.deepcopy(record)
        changed["metadata"] = {1: "value"}
        self.assertFalse(team._record_valid(self.store, changed))
        reader = self.reader()
        with reader._reads.scope():
            self.assertTrue(team._record_valid(reader, record))
            self.assertFalse(team._record_valid(reader, changed),
                             "Non-string dict key borrowed the JSON-normalized positive verdict")

    def test_json_type_subclasses_keep_original_predicate_without_memo(self):
        class ListValue(list):
            pass
        class DictValue(dict):
            pass
        class StringValue(str):
            pass
        class IntValue(int):
            pass
        class FloatValue(float):
            pass
        values = [ListValue(["value"]), DictValue(field="value"), StringValue("value"),
                  IntValue(7), FloatValue(1.5), {StringValue("key"): "value"}]
        for value in values:
            with self.subTest(kind=type(value).__name__):
                record = self.sealed_metadata(value)
                self.assertTrue(team._record_valid(self.store, record))
                reader = self.reader()
                with patch.object(mixed_evidence, "record_valid", wraps=mixed_evidence.record_valid) as checks:
                    with reader._reads.scope():
                        for _ in range(2):
                            self.assertTrue(team._record_valid(reader, record))
                    self.assertEqual(checks.call_count, 2, "JSON subclass unexpectedly entered the positive memo")

    def test_nonfinite_values_keep_uncached_predicate_semantics(self):
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=repr(value)):
                record = self.sealed_metadata(value)
                expected = team._record_valid(self.store, record)
                reader = self.reader()
                with patch.object(mixed_evidence, "record_valid", wraps=mixed_evidence.record_valid) as checks:
                    with reader._reads.scope():
                        for _ in range(2):
                            self.assertEqual(team._record_valid(reader, record), expected)
                    if expected:
                        self.assertEqual(checks.call_count, 2, "Non-finite number unexpectedly entered the positive memo")

    def test_cyclic_unsupported_value_falls_back_without_a_type_walk_loop(self):
        record = self.sealed_metadata([None])
        cycle = [None]
        cycle[0] = cycle
        changed = copy.deepcopy(record)
        changed["metadata"] = cycle
        expected = team._record_valid(self.store, changed)
        reader = self.reader()
        with reader._reads.scope():
            self.assertTrue(team._record_valid(reader, record))
            self.assertEqual(team._record_valid(reader, changed), expected)

    def test_shared_json_native_subtree_is_not_mistaken_for_a_cycle(self):
        shared = {"value": ["same"]}
        record = self.sealed_metadata([shared, shared])
        reader = self.reader()
        with patch.object(mixed_evidence, "record_valid", wraps=mixed_evidence.record_valid) as checks:
            with reader._reads.scope():
                for _ in range(2):
                    self.assertTrue(team._record_valid(reader, record))
            self.assertEqual(checks.call_count, 1)

    def test_literal_json_with_a_reserved_marker_stays_literal_after_parse(self):
        raw = b'{"aispaManifest":1,"ordinary":"literal source, not transport"}'
        ref = self.store.artifact("evidence", raw, ".json")
        reader = self.reader()
        path = self.store.run / ref["file"]
        with reader._reads.scope():
            reader._reads.parse(raw)
            try:
                actual = reader._reads.artifact(path, ref["sha256"], literal=True)
            except (Invalid, KeyError, TypeError) as error:
                self.fail("Literal JSON was reinterpreted as transport: " + type(error).__name__)
            self.assertEqual(actual, raw)
            reader.finish_snapshot()

    def test_foreign_root_and_run_cannot_borrow_a_valid_record(self):
        record, reader = self.record(), self.reader()
        other = tempfile.TemporaryDirectory(prefix="aispa-foreign-verdict-")
        self.addCleanup(other.cleanup)
        foreign = Journal(other.name)
        foreign.create("other", "Foreign fixture")
        other_reader = self.reader(other.name)
        with reader._reads.scope():
            self.assertTrue(team._record_valid(reader, record))
            self.assertFalse(team._record_valid(other_reader, record))
        self.state["status"] = "cancelled"
        self.store.append(self.state, "fixture-cancelled")
        self.store.create("later", "Later run")
        next_reader = self.reader()
        with next_reader._reads.scope():
            self.assertFalse(team._record_valid(next_reader, record))

    def test_same_size_same_mtime_change_is_caught_by_fresh_final_validation(self):
        ref = self.store.artifact("reports", b'{"value":"first"}', ".json")
        path = self.store.run / ref["file"]
        before, reader = path.stat(), self.reader()
        with reader._reads.scope():
            self.assertTrue(contracts.artifact_valid(reader, ref))
            path.write_bytes(b'{"value":"other"}')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(path.stat().st_size, before.st_size)
            with self.assertRaises(Invalid):
                reader.finish_snapshot()

    def test_identical_bytes_under_replaced_parent_are_not_a_confined_generation(self):
        ref = self.store.artifact("reports", b'{"value":"same"}', ".json")
        path, reader = self.store.run / ref["file"], self.reader()
        with reader._reads.scope():
            self.assertTrue(contracts.artifact_valid(reader, ref))
            self.replace_parent_with_identical_symlink(path.parent)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), ref["sha256"])
            with self.assertRaises(Invalid, msg="Final validation lost the original logical parent boundary"):
                reader.finish_snapshot()

    def test_manifest_chunks_keep_their_own_original_logical_parent_boundaries(self):
        path = self.store.run / "reports/manifest.json"
        path.parent.mkdir()
        raw = b'{"checks":["one","two","three"]}'
        manifest = scale.write_manifest(path, raw, kind="json", basis=hashlib.sha256(raw).hexdigest(), chunk_bytes=8)
        for index, ref in enumerate(manifest["chunks"]):
            original = path.parent / ref["file"]
            moved = path.parent / ("first" if index % 2 == 0 else "second") / original.name
            moved.parent.mkdir(exist_ok=True)
            original.rename(moved)
            ref["file"] = moved.relative_to(path.parent).as_posix()
        path.write_text(json.dumps(manifest), encoding="utf-8")
        ref = {"file": path.relative_to(self.store.run).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        reader = self.reader()
        with reader._reads.scope():
            self.assertTrue(contracts.artifact_valid(reader, ref))
            self.replace_parent_with_identical_symlink(path.parent / "second")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), ref["sha256"])
            with self.assertRaises(Invalid, msg="Manifest child parent substitution survived final validation"):
                reader.finish_snapshot()

    def test_resource_failure_is_sticky_even_after_a_positive_record_verdict(self):
        record, reader = self.record(), self.reader()
        large = self.store.artifact("evidence", b"x" * 20000, ".txt")
        with reader._reads.scope():
            self.assertTrue(team._record_valid(reader, record))
            with patch.dict(os.environ, {"AISPA_MAX_MEMORY_BYTES": "4096"}):
                with self.assertRaises(ResourceLimit):
                    reader.artifact_bytes(large)
            with self.assertRaises(ResourceLimit):
                team._record_valid(reader, record)
            with self.assertRaises(ResourceLimit):
                reader.finish_snapshot()

    def test_failed_final_confinement_cannot_publish_a_partial_status(self):
        ref = self.store.artifact("reports", b'{"value":"same"}', ".json")
        path = self.store.run / ref["file"]
        display.publish(self.store, self.state)
        status = self.store.home / "status.json"
        before, original = status.read_bytes(), display.project
        def substitute(reader, state):
            self.assertTrue(contracts.artifact_valid(reader, ref))
            result = original(reader, state)
            self.replace_parent_with_identical_symlink(path.parent)
            return result
        with patch.object(display, "project", side_effect=substitute):
            with self.assertRaises(Invalid, msg="An unconfined cached verdict was published as ready"):
                display.publish(self.store, self.state)
        self.assertEqual(status.read_bytes(), before)

    def test_swallowed_resource_failure_cannot_publish_after_a_cached_positive(self):
        record = self.record()
        large = self.store.artifact("evidence", b"x" * 20000, ".txt")
        display.publish(self.store, self.state)
        status = self.store.home / "status.json"
        before, original = status.read_bytes(), display.project
        def exhausted(reader, state):
            result = original(reader, state)
            self.assertTrue(team._record_valid(reader, record))
            with patch.dict(os.environ, {"AISPA_MAX_MEMORY_BYTES": "4096"}):
                try:
                    reader.artifact_bytes(large)
                except ResourceLimit:
                    pass
            return result
        with patch.object(display, "project", side_effect=exhausted):
            with self.assertRaises(ResourceLimit):
                display.publish(self.store, self.state)
        self.assertEqual(status.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
