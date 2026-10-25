"""Native immutable evidence must belong to the actual projection generation."""
from contextlib import contextmanager
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
from aispa_core import blind, carryover, cleanroom, dashboard_server, executions, handoff
from aispa_core.workflow import Workflow
import test_dashboard_server as server_fixture
import test_handoff as handoff_fixture
import test_workflow as workflow_fixture
from test_dashboard_document_integrity import settled_response


class DashboardRemainingEvidenceTests(unittest.TestCase):
    inline_server = server_fixture.DashboardServerTests.inline_server

    @contextmanager
    def fixture(self, kind):
        fx = workflow_fixture.WorkflowTests("test_full_delivery_roundtrip")
        fx.setUp()
        self.store = fx.store
        try:
            paths = {}
            if kind == "carryover":
                fx.prepare(secondary=True)
                fx.completed()
                fx.flow.block("T02", "Synthetic unavailable export service")
                fx.flow.wave(1, fx.file(fx.assessment("wave", {"R02": "blocked"})))
                self.assertTrue(fx.flow.acceptance(fx.file(fx.assessment(failed={"R02": "blocked"}))))
                fx.flow.finish()
                payload = carryover.capture(fx.store, fx.state)
                fx.state = fx.store.create("continued", "Synthetic resumed project")
                carryover.attach(fx.store, fx.state, payload)
                fx.store.append(fx.state, "fixture-carryover-attached")
                outstanding = next(row for row in payload["items"] if row["status"] == "outstanding")
                carryover.decide(fx.store, fx.state, outstanding["id"], "keep")
                fx.store.append(fx.state, "fixture-disposition-recorded")
                fx.flow = Workflow(fx.store, fx.state)
                fx.accepted()
                paths = {"inherited-handoff": fx.state["carryover"]["handoff"],
                         "disposition": fx.state["carryover"]["decisions"][0]}
            elif kind == "delivery":
                # Reuse the public tracked-document contract, with genuine saved executions.
                docs = handoff_fixture.HandoffTests("test_one_readme_covers_small_project_and_stays_current_after_finish")
                docs.root = fx.root
                docs.write("README.md", """# Fixture

## Quickstart
Run this check from the project root: `RUNNER app.py`.
Standard library only; no install step.
This fixture exists only for a check.
No separate test command in the fixture.

## Architecture
The one-file program has a single entry point, app.py, and writes one line.

## Decisions
Choice: keep one file. Alternatives: add a package. Consequences: setup stays simple.

## Glossary
Fixture means the example command-line program used in this test.
""".replace("RUNNER", handoff_fixture.RUNNER))
                fx.commit()
                fx.state["policy"] = {"deliveryDocs": True}
                fx.prepare()
                fx.completed()
                refs = []
                for _ in range(executions.HOT_LIMIT + 1):
                    self.assertTrue(fx.flow.verify([sys.executable, "app.py"], 10))
                    refs.append(executions.record(fx.store, fx.state, kind="project", purpose="check"))
                    fx.store.append(fx.state, "execution-recorded")
                docs.ref = refs[0]
                handoff.record(fx.store, fx.state, docs.report(),
                               command_receipt=lambda ref: executions.resolve(fx.store, fx.state, ref))
                fx.store.append(fx.state, "handoff-recorded")
                self.assertTrue(fx.flow.wave(1, fx.file(fx.assessment("wave"))))
                self.assertTrue(fx.flow.acceptance(fx.file(fx.assessment())))
                paths = {"handoff-manifest": fx.state["handoff"]["artifact"],
                         "execution-record": refs[0], "execution-archive": fx.state["executions"]["archive"]}
            elif kind == "clean":
                fx.state["policy"] = {"cleanClone": True}
                fx.prepare()
                fx.completed()
                fx.state["cleanroom"] = cleanroom.prepare(fx.store, fx.state)
                fx.store.append(fx.state, "cleanroom-prepared")
                fx.state["cleanrun"] = cleanroom.execute(fx.store, fx.state, [sys.executable, "app.py"], 10)
                fx.store.append(fx.state, "clean-verification-recorded")
                # A separate real project check supplies blind observations. Clean output
                # must therefore be tracked by cleanroom itself, not incidentally by blind.
                report = {"basis": blind.basis(fx.store, fx.state), "reviewer": "native-final-acceptor",
                          "independent": True, "observations": [{"brief": "B001",
                          "quote": "Команда выводит ready", "status": "passed",
                          "note": "Observed the synthetic command output from the original brief",
                          "artifact": fx.state["verification"]["output"]["file"]}]}
                blind.record(fx.store, fx.state, report)
                fx.store.append(fx.state, "blind-review-recorded")
                self.assertTrue(fx.flow.wave(1, fx.file(fx.assessment("wave"))))
                final = fx.assessment()
                final.update(reviewer="native-final-acceptor", cleanrun=fx.state["cleanrun"]["artifact"]["sha256"],
                             blindReport=fx.state["blindReview"]["report"]["sha256"],
                             blindMapping=[{"observation": 0, "requirement": "R01"}])
                self.assertTrue(fx.flow.acceptance(fx.file(final)))
                paths = {"clean-prepared": fx.state["cleanroom"]["artifact"],
                         "clean-snapshot": fx.state["cleanroom"]["snapshot"],
                         "clean-run": fx.state["cleanrun"]["artifact"],
                         "clean-output": fx.state["cleanrun"]["output"]}
            else:
                raise AssertionError("Unknown fixture")
            yield fx, {name: fx.store.run / ref["file"] for name, ref in paths.items()}
        finally:
            fx.tearDown()

    def test_native_projection_reads_and_parses_once_and_refreshes_next_generation(self):
        original_project = dashboard_server.project
        for kind in ("carryover", "delivery", "clean"):
            with self.subTest(kind=kind), self.fixture(kind) as (fx, paths):
                identifiers = {(p.stat().st_dev, p.stat().st_ino): name for name, p in paths.items()}
                raw_json = {name: p.read_bytes() for name, p in paths.items() if name != "clean-output"}
                reads, parses, observed = [], [], []
                original_open, original_loads = io.open, json.loads

                def opened(*args, **kwargs):
                    stream = original_open(*args, **kwargs)
                    stat = os.fstat(stream.fileno())
                    name = identifiers.get((stat.st_dev, stat.st_ino))
                    if name is not None:
                        reads.append(name)
                    return stream

                def loads(value, *args, **kwargs):
                    for name, raw in raw_json.items():
                        if value == raw or value == raw.decode("utf-8"):
                            parses.append(name)
                    return original_loads(value, *args, **kwargs)

                def projection(reader, state):
                    view = original_project(reader, state)
                    self.assertTrue(view["gates"]["acceptance"], "Fixture must reach actual current acceptance")
                    # Repeated real consumers still share the calculation, including detached
                    # parsed JSON returned through their existing native helper APIs.
                    if kind == "carryover":
                        ref = state["carryover"]["handoff"]
                        first = carryover._load(reader, ref)
                        first["schema"] = "caller mutation"
                        self.assertEqual(carryover._load(reader, ref)["schema"], 1)
                    elif kind == "delivery":
                        ref = state["executions"]["archive"]
                        first = executions._read(reader, ref, "archive")
                        first["schema"] = "caller mutation"
                        self.assertEqual(executions._read(reader, ref, "archive")["schema"], 1)
                    else:
                        ref = state["cleanroom"]["snapshot"]
                        first = cleanroom._artifact_value(reader, ref)
                        expected = copy.deepcopy(first)
                        first["caller mutation"] = True
                        self.assertEqual(cleanroom._artifact_value(reader, ref), expected)
                    scope = getattr(reader, "_reads", None)
                    observed.append(({name: reads.count(name) for name in paths},
                                     {name: parses.count(name) for name in raw_json},
                                     set(getattr(scope, "hashes", {}))))
                    return view

                with self.inline_server(projection) as url, patch.object(io, "open", opened), patch.object(json, "loads", loads):
                    for _ in range(2):
                        reads.clear()
                        parses.clear()
                        code, value = settled_response(url)
                        self.assertEqual(code, 200)
                        self.assertTrue(value["gates"]["acceptance"])
                self.assertEqual(len(observed), 2, "An ordinary refresh must calculate a fresh generation")
                for counts, parsed, tracked in observed:
                    for name, count in counts.items():
                        self.assertEqual(count, 1, "Native immutable evidence reread in one projection: " + name)
                    for name, count in parsed.items():
                        self.assertEqual(count, 1, "Native immutable JSON reparsed in one projection: " + name)
                    self.assertTrue(set(paths.values()) <= tracked, "Consumed native evidence escaped final tracking")

    def test_late_native_evidence_changes_cannot_publish_current_acceptance(self):
        original_project = dashboard_server.project
        for kind in ("carryover", "delivery", "clean"):
            with self.subTest(kind=kind), self.fixture(kind) as (fx, paths):
                changing = []
                before_state = copy.deepcopy(fx.state)
                before_journal = (fx.store.run / "events.jsonl").read_bytes()

                def projection(reader, state):
                    view = original_project(reader, state)
                    self.assertTrue(view["gates"]["acceptance"])
                    if changing:
                        path = changing[0]
                        path.write_bytes(path.read_bytes() + b"\nlate disposable evidence mutation")
                    return view

                with self.inline_server(projection) as url:
                    for name, path in paths.items():
                        with self.subTest(changed=name):
                            code, first = settled_response(url)
                            self.assertEqual(code, 200)
                            self.assertTrue(first["gates"]["acceptance"])
                            raw = path.read_bytes()
                            changing.append(path)
                            try:
                                code, failed = settled_response(url)
                            finally:
                                changing.clear()
                                path.write_bytes(raw)
                            self.assertEqual(code, 409, "Changed native evidence still published current acceptance: " + name)
                            self.assertNotIn("gates", failed)
                            self.assertEqual(failed["calculation"]["state"], "source_error")
                            self.assertEqual(failed["lastSnapshot"]["freshness"], "historical")
                            self.assertEqual(failed["lastSnapshot"]["capturedAt"], first["snapshot"]["capturedAt"])
                self.assertEqual(fx.state, before_state)
                self.assertEqual((fx.store.run / "events.jsonl").read_bytes(), before_journal)


if __name__ == "__main__":
    unittest.main()
