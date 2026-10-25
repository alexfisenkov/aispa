"""Real projection reads must share and finally verify project-document bytes."""
from contextlib import contextmanager
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import dashboard_server, project_documents, scale
from aispa_core.store import Invalid
import test_dashboard_server as server_fixture
import test_workflow as workflow_fixture


MODES = ("legacy-plain", "scalable-plain", "scalable-manifest")


def settled_response(url):
    """Support both historical plain ready and the addressed async protocol."""
    requested, generation, deadline = url, None, time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            stream = urlopen(requested, timeout=max(.1, deadline - time.monotonic()))
        except HTTPError as error:
            stream = error
        with stream:
            code, value = stream.code, json.load(stream)
        if code != 202:
            return code, value
        pending = value["calculation"]
        assert pending["state"] == "calculating"
        identifier = pending["id"]
        assert isinstance(identifier, str) and len(identifier) == 32
        assert generation is None or identifier == generation
        generation = identifier
        requested = url + "?calculation=" + identifier
        time.sleep(.01)
    raise AssertionError("The exact document projection generation did not settle")


class DashboardDocumentIntegrityTests(unittest.TestCase):
    inline_server = server_fixture.DashboardServerTests.inline_server

    @contextmanager
    def fixture(self, mode, *, accepted=True, intent=None):
        fx = workflow_fixture.WorkflowTests("test_full_delivery_roundtrip")
        fx.setUp()
        self.store = fx.store
        try:
            scalable = mode != "legacy-plain"
            fx.state["policy"] = {"projectDocuments": True, "scalableInputs": scalable}
            fx.store.scale_enabled = scalable
            raw = intent or b"Safe purpose for the document projection fixture.\n"
            (fx.root / "INTENT.md").write_bytes(raw)
            binding = project_documents.bind(fx.store, fx.state, "intent", "INTENT.md",
                                             hashlib.sha256(raw).hexdigest(), "Synthetic source binding")
            paths = {"snapshot": fx.store.run / binding["snapshot"]["file"]}
            if mode == "scalable-manifest":
                target = fx.store.run / "project-documents/snapshot-transport.md"
                transport = scale.write_manifest(target, raw, kind="text", basis=hashlib.sha256(raw).hexdigest(), chunk_bytes=16)
                binding["snapshot"] = {"file": target.relative_to(fx.store.run).as_posix(),
                                       "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
                paths["snapshot"] = target
                for index, ref in enumerate(transport["chunks"]):
                    paths["snapshot-chunk-" + str(index)] = target.parent / ref["file"]
            binding_raw = json.dumps({"schema": 1, "binding": {k: v for k, v in binding.items() if k != "manifest"}},
                                     ensure_ascii=False, sort_keys=True).encode()
            if mode == "scalable-manifest":
                target = fx.store.run / "project-documents/binding-transport.json"
                transport = scale.write_manifest(target, binding_raw, kind="json",
                                                  basis=hashlib.sha256(binding_raw).hexdigest(), chunk_bytes=128)
                binding["manifest"] = {"file": target.relative_to(fx.store.run).as_posix(),
                                       "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
                for index, ref in enumerate(transport["chunks"]):
                    paths["binding-chunk-" + str(index)] = target.parent / ref["file"]
                fx.state["projectDocuments"]["bindings"]["intent"] = copy.deepcopy(binding)
                fx.state["projectDocuments"]["history"][0]["binding"] = copy.deepcopy(binding)
            paths["binding"] = fx.store.run / binding["manifest"]["file"]
            if accepted:
                fx.accepted()
            yield fx, binding, raw, binding_raw, paths
        finally:
            fx.tearDown()

    def test_actual_projection_reuses_each_document_physical_read_and_parsed_json(self):
        original_project = dashboard_server.project
        for mode in MODES:
            with self.subTest(mode=mode), self.fixture(mode) as (fx, binding, raw, binding_raw, paths):
                identifiers = {(path.stat().st_dev, path.stat().st_ino): name for name, path in paths.items()}
                reads, parses, observed = [], [], {}
                original_open, original_loads = io.open, json.loads

                def opened(*args, **kwargs):
                    stream = original_open(*args, **kwargs)
                    stat = os.fstat(stream.fileno())
                    name = identifiers.get((stat.st_dev, stat.st_ino))
                    if name is not None:
                        reads.append(name)
                    return stream

                def loads(value, *args, **kwargs):
                    if value == binding_raw or value == binding_raw.decode():
                        parses.append(True)
                    return original_loads(value, *args, **kwargs)

                def projection(reader, state):
                    read_json = getattr(reader, "json", lambda ref: json.loads(reader.text(ref)))
                    first = read_json(binding["manifest"])
                    first["binding"]["reason"] = "Caller must not mutate the verified cache"
                    self.assertEqual(read_json(binding["manifest"])["binding"]["reason"], binding["reason"])
                    view = original_project(reader, state)
                    self.assertTrue(view["gates"]["acceptance"], "The fixture must reach actual accepted projection")
                    observed["reads"] = {name: reads.count(name) for name in paths}
                    observed["parses"] = len(parses)
                    scope = getattr(reader, "_reads", None)
                    observed["tracked"] = set(getattr(scope, "hashes", {}))
                    return view

                with self.inline_server(projection) as url, patch.object(io, "open", opened), patch.object(json, "loads", loads):
                    code, value = settled_response(url)
                self.assertEqual(code, 200)
                self.assertTrue(value["gates"]["acceptance"])
                for name, count in observed["reads"].items():
                    self.assertEqual(count, 1, "Project document was reread inside one projection: " + mode + "/" + name)
                self.assertEqual(observed["parses"], 1, "Immutable binding JSON was reparsed inside one projection")
                self.assertTrue(set(paths.values()) <= observed["tracked"], "Consumed document evidence escaped final tracking")

    def test_late_snapshot_binding_and_chunk_changes_never_publish_current_acceptance(self):
        original_project = dashboard_server.project
        for mode in MODES:
            targets = ["snapshot", "binding"]
            if mode == "scalable-manifest":
                targets += ["snapshot-chunk-0", "binding-chunk-0"]
            for name in targets:
                with self.subTest(mode=mode, changed=name), self.fixture(mode) as (fx, binding, raw, binding_raw, paths):
                    mutate = []
                    before_state = copy.deepcopy(fx.state)
                    before_journal = (fx.store.run / "events.jsonl").read_bytes()

                    def projection(reader, state):
                        view = original_project(reader, state)
                        self.assertTrue(view["gates"]["acceptance"])
                        if mutate:
                            path = paths[name]
                            path.write_bytes(path.read_bytes() + b"\nlate disposable mutation")
                        return view

                    with self.inline_server(projection) as url:
                        code, first = settled_response(url)
                        self.assertEqual(code, 200)
                        self.assertTrue(first["gates"]["acceptance"])
                        mutate.append(True)
                        code, failed = settled_response(url)
                    self.assertEqual(code, 409, "Changed consumed immutable document still published current acceptance")
                    self.assertNotIn("gates", failed)
                    self.assertEqual(failed["calculation"]["state"], "source_error")
                    self.assertNotEqual(failed.get("snapshot", {}).get("freshness"), "current")
                    self.assertEqual(failed["lastSnapshot"]["freshness"], "historical")
                    self.assertEqual(failed["lastSnapshot"]["capturedAt"], first["snapshot"]["capturedAt"])
                    self.assertEqual(fx.state, before_state)
                    self.assertEqual((fx.store.run / "events.jsonl").read_bytes(), before_journal)

    def test_legacy_plain_text_is_not_reinterpreted_as_transport(self):
        raw = b'{"aispaManifest":1,"ordinaryLegacyOwnerText":true}'
        with self.fixture("legacy-plain", accepted=False, intent=raw) as (fx, binding, *_):
            self.assertEqual(project_documents._validate_binding(fx.store.run, binding, "intent", scalable=False), raw)

    def test_legacy_binding_still_rejects_manifest_transport(self):
        with self.fixture("scalable-manifest", accepted=False) as (fx, binding, *_):
            with self.assertRaises(Invalid):
                project_documents._validate_binding(fx.store.run, binding, "intent", scalable=False)

    def test_warm_document_cache_never_bypasses_binding_path_or_expected_sha(self):
        original_project = dashboard_server.project
        for mode in MODES:
            with self.subTest(mode=mode), self.fixture(mode) as (fx, binding, *_):
                def projection(reader, state):
                    view = original_project(reader, state)
                    for change in ({"sha256": "0" * 64}, {"file": "project-documents/../../outside.json"}):
                        wrong = copy.deepcopy(binding)
                        wrong["manifest"].update(change)
                        with self.assertRaises(Invalid):
                            project_documents._validate_binding(reader.run, wrong, "intent", scalable=mode != "legacy-plain")
                    return view

                with self.inline_server(projection) as url:
                    code, value = settled_response(url)
                self.assertEqual(code, 200)
                self.assertTrue(value["gates"]["acceptance"])


if __name__ == "__main__":
    unittest.main()
