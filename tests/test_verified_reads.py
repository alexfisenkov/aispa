"""Scoped integrity reuse and bounded document materialization regressions."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import docmeta, project_documents, scale
from aispa_core.contracts import artifact_valid
from aispa_core.dashboard_reader import SnapshotJournal
from aispa_core.store import Invalid
import test_workflow as workflow_tests


class VerifiedReadTests(unittest.TestCase):
    def setUp(self):
        self.fx = workflow_tests.WorkflowTests("test_empty_run_cannot_finish")
        self.fx.setUp()

    def tearDown(self):
        self.fx.tearDown()

    def reader(self):
        reader = SnapshotJournal(self.fx.root)
        reader.load()
        return reader

    def test_validation_text_and_json_share_one_physical_read_and_parse(self):
        raw = b'{"checks":[{"criterion":"complete","status":"passed"}]}'
        ref = self.fx.store.artifact("reports", raw, ".json")
        target = (self.fx.store.run / ref["file"]).stat()
        reader = self.reader()
        opened, parsed = [], []
        original_open, original_loads = io.open, json.loads

        def tracked_open(*args, **kwargs):
            stream = original_open(*args, **kwargs)
            stat = os.fstat(stream.fileno())
            if (stat.st_dev, stat.st_ino) == (target.st_dev, target.st_ino):
                opened.append(stream.name)
            return stream

        def tracked_loads(value, *args, **kwargs):
            if value == raw or value == raw.decode():
                parsed.append(value)
            return original_loads(value, *args, **kwargs)

        # Fall back only to the currently supported API so baseline failures
        # reproduce duplicate I/O/parse, rather than a missing method import.
        read_json = getattr(reader, "json", lambda artifact: json.loads(reader.text(artifact)))
        with patch.object(io, "open", tracked_open), patch.object(json, "loads", tracked_loads):
            for _ in range(3):
                self.assertTrue(artifact_valid(reader, ref))
                self.assertEqual(reader.text(ref), raw.decode())
                self.assertEqual(read_json(ref)["checks"][0]["status"], "passed")
        self.assertEqual(len(opened), 1, "Artifact validation and consumption reread the same physical file")
        self.assertEqual(len(parsed), 1, "Validated JSON was parsed repeatedly within one calculation")

    def test_new_calculation_rechecks_same_size_same_mtime_and_conflicting_sha(self):
        raw = b'{"value":"first"}'
        ref = self.fx.store.artifact("reports", raw, ".json")
        path = self.fx.store.run / ref["file"]
        reader = self.reader()
        self.assertTrue(artifact_valid(reader, ref))
        read_json = getattr(reader, "json", lambda artifact: json.loads(reader.text(artifact)))
        first = read_json(ref)
        first["value"] = "caller-mutated"
        self.assertEqual(read_json(ref)["value"], "first", "JSON cache aliased caller data")
        self.assertFalse(artifact_valid(reader, {**ref, "sha256": "0" * 64}))
        before = path.stat()
        path.write_bytes(b'{"value":"other"}')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        next_reader = self.reader()
        self.assertFalse(artifact_valid(next_reader, ref))
        self.assertEqual(path.stat().st_size, before.st_size)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_new_calculation_rechecks_manifest_chunks_with_unchanged_outer_manifest(self):
        path = self.fx.store.run / "reports/manifest.json"
        path.parent.mkdir()
        raw = b'{"checks":["one","two"]}'
        manifest = scale.write_manifest(path, raw, kind="json", basis=hashlib.sha256(raw).hexdigest(), chunk_bytes=8)
        ref = {"file": path.relative_to(self.fx.store.run).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        self.assertTrue(artifact_valid(self.reader(), ref))
        outer = path.read_bytes()
        chunk = path.parent / manifest["chunks"][-1]["file"]
        value = json.loads(chunk.read_bytes())
        value["index"] += 1
        chunk.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(path.read_bytes(), outer)
        self.assertFalse(artifact_valid(self.reader(), ref))

    def test_immutable_document_binding_obeys_materialization_memory_budget(self):
        fx = self.fx
        fx.state["policy"] = {"scalableInputs": True}
        fx.store.scale_enabled = True
        raw = b"Safe source paragraph.\n" * 1000
        path = fx.root / "INTENT.md"
        path.write_bytes(raw)
        with patch.dict(os.environ, {"AISPA_MAX_MEMORY_BYTES": "1048576"}):
            binding = project_documents.bind(fx.store, fx.state, "intent", "INTENT.md", hashlib.sha256(raw).hexdigest(), "Scoped budget fixture")
        state_before = copy.deepcopy(fx.state)
        journal_before = (fx.store.run / "events.jsonl").read_bytes()
        with patch.dict(os.environ, {"AISPA_MAX_MEMORY_BYTES": "4096"}):
            with self.assertRaisesRegex(Invalid, r"RESOURCE_LIMIT.*maxMemoryBytes"):
                project_documents._validate_binding(fx.store.run, binding, "intent", scalable=True)
        self.assertEqual(fx.state, state_before)
        self.assertEqual((fx.store.run / "events.jsonl").read_bytes(), journal_before)
        self.assertEqual(path.read_bytes(), raw)
        with patch.dict(os.environ, {"AISPA_MAX_MEMORY_BYTES": "1048576"}):
            self.assertEqual(project_documents._validate_binding(fx.store.run, binding, "intent", scalable=True), raw)

    def test_metadata_post_stat_growth_is_refused_before_large_allocation(self):
        fx = self.fx
        fx.state["policy"] = {"scalableInputs": True}
        fx.store.scale_enabled = True
        name = "docs/growing.md"
        source = fx.root / name
        source.parent.mkdir()
        source.write_bytes(b"Safe initial.\n")
        initial = source.stat()
        grown = b"Safe grown paragraph.\n" * 1000
        reads = []
        original = os.fdopen
        state_before = copy.deepcopy(fx.state)
        journal_before = (fx.store.run / "events.jsonl").read_bytes()

        class GrowingReader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def fileno(self):
                return self.stream.fileno()

            def read(self, size=-1):
                source.write_bytes(grown)
                part = self.stream.read(size)
                reads.append((size, len(part)))
                return part

        def fdopen(fd, *args, **kwargs):
            stream = original(fd, *args, **kwargs)
            stat = os.fstat(fd)
            return GrowingReader(stream) if (stat.st_dev, stat.st_ino) == (initial.st_dev, initial.st_ino) else stream

        with patch.dict(os.environ, {"AISPA_MAX_MEMORY_BYTES": "4096"}), patch.object(os, "fdopen", fdopen):
            with self.assertRaisesRegex(Invalid, r"RESOURCE_LIMIT.*maxMemoryBytes"):
                docmeta.register(fx.store, fx.state, name)
        self.assertTrue(reads)
        self.assertTrue(all(0 < requested <= 1025 for requested, _ in reads))
        self.assertLessEqual(sum(count for _, count in reads), 1025)
        self.assertEqual(fx.state, state_before)
        self.assertEqual((fx.store.run / "events.jsonl").read_bytes(), journal_before)
        with patch.dict(os.environ, {"AISPA_MAX_MEMORY_BYTES": "131072"}):
            result = docmeta.register(fx.store, fx.state, name)
            saved = docmeta._read(fx.store, fx.state, name, result["artifact"])
        self.assertEqual(saved["sourceSha256"], hashlib.sha256(grown).hexdigest())


if __name__ == "__main__":
    unittest.main()
