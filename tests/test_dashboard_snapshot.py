"""Generation-scoped HTTP behavior, using actual loopback requests and sources."""
from concurrent.futures import ThreadPoolExecutor, wait
import json
import os
import sys
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

import test_dashboard_server as server_tests
from aispa_core import dashboard_server as dashboard
from aispa_core.store import Invalid, Journal, atomic_json
from aispa_core.contracts import artifact_valid
from aispa_core.dashboard_reader import SnapshotJournal


def response(url):
    try:
        stream = urlopen(url, timeout=4)
    except HTTPError as error:
        stream = error
    with stream:
        raw = stream.read()
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"_nonJsonResponse": True}
        return stream.code, data


class DashboardSnapshotTests(unittest.TestCase):
    setUp = server_tests.DashboardServerTests.setUp
    tearDown = server_tests.DashboardServerTests.tearDown
    inline_server = server_tests.DashboardServerTests.inline_server

    def await_ready(self, url, first):
        generation = first["calculation"]["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            code, data = response(url + "?calculation=" + generation)
            if data.get("calculation", {}).get("state") != "calculating":
                return code, data
            time.sleep(.01)
        self.fail("The addressed calculation did not settle")

    def test_simultaneous_http_requests_join_one_observable_calculation(self):
        entered, release = threading.Event(), threading.Event()
        all_arrived = threading.Event()
        calls, lock = [], threading.Lock()
        arrivals = []
        original, original_get = dashboard.project, dashboard.SnapshotCoordinator.get

        def slow(reader, state):
            with lock:
                calls.append(reader)
            entered.set()
            if not release.wait(3):
                raise Invalid("fixture calculation was not released")
            return original(reader, state)

        def observed_get(coordinator, generation=None):
            if generation is None:
                with lock:
                    arrivals.append(threading.get_ident())
                    if len(arrivals) == 6:
                        all_arrived.set()
            return original_get(coordinator, generation)

        with patch.object(dashboard.SnapshotCoordinator, "get", observed_get), self.inline_server(slow) as url, ThreadPoolExecutor(max_workers=6) as pool:
            start = threading.Barrier(6)

            def fetch():
                start.wait(timeout=2)
                return response(url)

            requests = [pool.submit(fetch) for _ in range(6)]
            try:
                self.assertTrue(entered.wait(2))
                # Client dispatch does not prove server admission. Keep the job
                # blocked until all six real handlers have reached get(), then
                # retain the bounded pending-response assertion while it runs.
                self.assertTrue(all_arrived.wait(2), "HTTP clients did not all reach snapshot admission")
                completed, _ = wait(requests, timeout=.6)
                self.assertEqual(len(completed), 6, "Calculating responses waited for full projection")
                replies = [future.result(timeout=3) for future in requests]
                self.assertFalse(release.is_set(), "Pending replies were checked only after calculation release")
                self.assertEqual(len(arrivals), 6)
                self.assertEqual(len(calls), 1, "Concurrent HTTP clients duplicated projection")
                self.assertEqual({code for code, _ in replies}, {202})
                self.assertEqual({row["calculation"]["state"] for _, row in replies}, {"calculating"})
                self.assertEqual(len({row["calculation"]["id"] for _, row in replies}), 1)
                self.assertTrue(all(row.get("lastSnapshot") is None for _, row in replies))
            finally:
                release.set()
            code, ready = self.await_ready(url, replies[0][1])
            self.assertEqual(code, 200)
            self.assertEqual(ready["run"], self.state["run"])
            self.assertEqual(ready["snapshot"]["freshness"], "current")
            self.assertEqual(len(calls), 1, "Polling a generation started another calculation")

    def test_source_error_is_distinct_and_keeps_original_last_snapshot_time(self):
        original = dashboard.project
        fail = threading.Event()

        def projection(reader, state):
            if fail.is_set():
                raise Invalid("private/local/source diagnostics must not escape")
            return original(reader, state)

        with self.inline_server(projection) as url:
            code, initial = response(url)
            if code == 202:
                code, initial = self.await_ready(url, initial)
            self.assertEqual(code, 200)
            fail.set()
            code, failed = response(url)
            if code == 202:
                code, failed = self.await_ready(url, failed)
            self.assertEqual(failed.get("calculation", {}).get("state"), "source_error")
            self.assertEqual(code, 409)
            self.assertEqual(failed["error"]["code"], "snapshot_source_error")
            self.assertNotIn("private/local", json.dumps(failed))
            self.assertEqual(failed["lastSnapshot"]["capturedAt"], initial["snapshot"]["capturedAt"])
            self.assertEqual(failed["lastSnapshot"]["freshness"], "historical")
            self.assertNotIn("gates", failed, "An error envelope must not present last PASS as current")
            fail.clear()
            code, recovered = response(url)
            if code == 202:
                code, recovered = self.await_ready(url, recovered)
            self.assertEqual(code, 200)
            self.assertNotEqual(recovered["calculation"]["id"], initial["calculation"]["id"])
            self.assertEqual(recovered["snapshot"]["freshness"], "current")

    def test_active_run_change_after_projection_cannot_publish_old_run_as_current(self):
        original = dashboard.project
        switched = []

        def projection(reader, state):
            view = original(reader, state)
            if not switched:
                switched.append(True)
                # Changing the pointer is independent of the old event file.
                # No old-journal append exists to accidentally catch this race.
                atomic_json(reader.home / "active.json", {"run": "different-active-run"})
            return view

        with self.inline_server(projection) as url:
            code, data = response(url)
            if code == 202:
                code, data = self.await_ready(url, data)
            self.assertEqual(code, 409, "A switched active pointer was published as current")
            self.assertEqual(data["error"]["code"], "dashboard_context_changed")

    def test_missing_generation_is_explicit_and_does_not_start_projection(self):
        calls = []
        original = dashboard.project

        def projection(reader, state):
            calls.append(True)
            return original(reader, state)

        with self.inline_server(projection) as url:
            code, data = response(url + "?calculation=" + "0" * 32)
        self.assertEqual(code, 410)
        self.assertEqual(data["error"]["code"], "snapshot_generation_expired")
        self.assertEqual(calls, [])

    def test_swallowed_resource_limit_cannot_publish_partial_ready_and_job_recovers(self):
        ref = self.store.artifact("reports", b'{"rows":["' + b"x" * 20000 + b'"]}', ".json")
        original = dashboard.project
        constrained = threading.Event()

        def projection(reader, state):
            if constrained.is_set():
                with patch.dict(os.environ, {"AISPA_MAX_MEMORY_BYTES": "4096"}):
                    # This existing public validator intentionally catches
                    # Invalid, so the calculation must retain resource failure.
                    artifact_valid(reader, ref)
            return original(reader, state)

        with self.inline_server(projection) as url:
            constrained.set()
            code, first = response(url)
            if code == 202:
                code, first = self.await_ready(url, first)
            self.assertEqual(code, 409, "Swallowed budget failure became ready")
            self.assertEqual(first["error"]["code"], "snapshot_resource_limit")
            self.assertEqual(first["calculation"]["state"], "source_error")
            self.assertIsNone(first.get("lastSnapshot"))
            constrained.clear()
            code, ready = response(url)
            if code == 202:
                code, ready = self.await_ready(url, ready)
            self.assertEqual(code, 200)
            constrained.set()
            code, failed = response(url)
            if code == 202:
                code, failed = self.await_ready(url, failed)
            self.assertEqual(code, 409)
            self.assertEqual(failed["error"]["code"], "snapshot_resource_limit")
            self.assertEqual(failed["lastSnapshot"]["capturedAt"], ready["snapshot"]["capturedAt"])
            self.assertEqual(failed["lastSnapshot"]["freshness"], "historical")

    def test_expiry_after_finish_before_context_check_never_publishes_ready(self):
        original = SnapshotJournal.finish_snapshot
        expire = threading.Event()

        def finish(reader):
            basis = original(reader)
            if expire.is_set():
                reader._reads._stream_budget().deadline = -1.0
            return basis

        with patch.object(SnapshotJournal, "finish_snapshot", finish), self.inline_server(dashboard.project) as url:
            expire.set()
            code, failed = response(url)
            if code == 202:
                code, failed = self.await_ready(url, failed)
            self.assertEqual(code, 409)
            self.assertEqual(failed["error"]["code"], "snapshot_resource_limit")
            self.assertIsNone(failed.get("lastSnapshot"))
            expire.clear()
            code, ready = response(url)
            if code == 202:
                code, ready = self.await_ready(url, ready)
            self.assertEqual(code, 200)
            self.assertEqual(ready["snapshot"]["freshness"], "current")

    def test_superseded_or_expired_ready_generation_cannot_advertise_current_pass(self):
        original = dashboard.project
        calls, delayed = [], threading.Event()
        release = threading.Event()

        def projection(reader, state):
            calls.append(reader)
            if delayed.is_set() and not release.wait(3):
                raise Invalid("fixture calculation was not released")
            return original(reader, state)

        with self.inline_server(projection) as url:
            code, first = response(url)
            if code == 202:
                code, first = self.await_ready(url, first)
            self.assertEqual(code, 200)
            self.assertEqual(first.get("calculation", {}).get("state"), "ready")
            old_id = first["calculation"]["id"]
            delayed.set()
            try:
                code, pending = response(url)
                self.assertEqual(code, 202)
                self.assertNotEqual(pending["calculation"]["id"], old_id)
                expired_code, expired = response(url + "?calculation=" + old_id)
            finally:
                release.set()
            code, second = self.await_ready(url, pending)
            self.assertEqual(code, 200)
            self.assertEqual(expired_code, 410, "Superseded ready generation advertised current PASS")
            self.assertEqual(expired["error"]["code"], "snapshot_generation_expired")
            self.assertEqual(len(calls), 2, "Polling an expired generation launched new work")
            # The server's monotonic clock seam expires a finished generation
            # deterministically, without sleeps or another ordinary refresh.
            module = sys.modules["aispa_core.dashboard_snapshot"]
            now = module.monotonic()
            with patch.object(module, "monotonic", return_value=now + 3600):
                expired_code, expired = response(url + "?calculation=" + second["calculation"]["id"])
            self.assertEqual(expired_code, 410, "Finished generations must have bounded retention")
            self.assertEqual(expired["error"]["code"], "snapshot_generation_expired")
            self.assertEqual(len(calls), 2)

    def test_unchanged_journal_serves_the_same_snapshot_and_an_append_calculates_again(self):
        original = dashboard.project
        calls = []

        def projection(reader, state):
            calls.append(reader)
            return original(reader, state)

        with self.inline_server(projection, reuse=True) as url:
            code, first = response(url)
            if code == 202:
                code, first = self.await_ready(url, first)
            self.assertEqual(code, 200)
            for _ in range(5):
                code, again = response(url)
                self.assertEqual(code, 200, "Reuse must not turn a poll into a pending calculation")
                self.assertEqual(again["calculation"]["id"], first["calculation"]["id"])
                self.assertEqual(again["snapshot"]["capturedAt"], first["snapshot"]["capturedAt"])
            self.assertEqual(len(calls), 1, "Polling an unchanged journal calculated again")
            writer = Journal(self.store.root)
            current = writer.load()
            current["notes"].append({"text": "decision after the first snapshot", "at": current["updatedAt"]})
            writer.append(current, "agent-decision-recorded")
            code, fresh = response(url)
            if code == 202:
                code, fresh = self.await_ready(url, fresh)
            self.assertEqual(code, 200)
            self.assertEqual(fresh["decisions"][-1]["text"], "decision after the first snapshot")
            self.assertEqual(len(calls), 2)

    def test_reuse_ends_after_the_limit_even_when_files_are_untouched(self):
        original = dashboard.project
        calls = []

        def projection(reader, state):
            calls.append(reader)
            return original(reader, state)

        with self.inline_server(projection, reuse=True) as url:
            code, first = response(url)
            if code == 202:
                code, first = self.await_ready(url, first)
            module = sys.modules["aispa_core.dashboard_snapshot"]
            now = module.monotonic()
            with patch.object(module, "monotonic", return_value=now + module.REUSE_SECONDS + 1):
                code, later = response(url)
                if code == 202:
                    code, later = self.await_ready(url, later)
            self.assertEqual(code, 200)
            self.assertNotEqual(later["calculation"]["id"], first["calculation"]["id"])
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
