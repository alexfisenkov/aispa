import json
import io
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import dashboard_server as dashboard
from aispa_core.dashboard_reader import DisplayJournal, SUPPORTED_VERSIONS
from aispa_core.store import Invalid, Journal, atomic_json


@contextmanager
def ready_status(url, *, timeout=10):
    """Observe the same generation until ready; never create work by polling."""
    requested, generation, deadline = url, None, time.monotonic() + timeout
    while time.monotonic() < deadline:
        stream = urlopen(requested, timeout=max(.1, deadline - time.monotonic()))
        if stream.code == 200:
            with stream:
                raw = stream.read()
            assert json.loads(raw)["calculation"]["state"] == "ready"
            with io.BytesIO(raw) as body:
                yield body
            return
        with stream:
            assert stream.code == 202
            pending = json.load(stream)["calculation"]
        assert pending["state"] == "calculating"
        identifier = pending["id"]
        assert isinstance(identifier, str) and len(identifier) == 32 and all(c in "0123456789abcdef" for c in identifier)
        assert generation is None or generation == identifier
        generation = identifier
        requested = url + "?calculation=" + generation
        time.sleep(.01)
    raise AssertionError("Addressed dashboard generation did not become ready within the deadline")


class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa dashboard 🧪 ")
        self.store = Journal(self.temp.name)
        self.state = self.store.create("panel", "Panel fixture")

    def tearDown(self):
        try:
            dashboard.stop(self.store)
        finally:
            self.temp.cleanup()

    @contextmanager
    def inline_server(self, projection):
        """A real HTTP fixture with an addressable deterministic writer race."""
        with patch.object(dashboard, "project", side_effect=projection):
            thread = threading.Thread(target=dashboard.serve, args=(self.store,),
                                      kwargs={"managed": True, "announce": False}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    metadata = dashboard._read_meta(self.store)
                    if metadata is not None and dashboard._verified(self.store, metadata):
                        break
                    time.sleep(.01)
                else:
                    self.fail("Inline dashboard fixture did not start")
                yield dashboard._url(metadata["port"], "status.json")
            finally:
                dashboard.stop(self.store)
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())

    def test_status_retries_exact_append_race_with_fresh_reader(self):
        original = dashboard.project
        readers = []

        def projection(reader, state):
            readers.append(reader)
            if len(readers) == 1:
                writer = Journal(self.store.root)
                current = writer.load()
                current["notes"].append({"text": "fresh concurrent event", "at": current["updatedAt"]})
                writer.append(current, "agent-decision-recorded")
            return original(reader, state)

        with self.inline_server(projection) as url, ready_status(url) as response:
            view = json.load(response)
        self.assertEqual(len(readers), 2)
        self.assertIsNot(readers[0], readers[1])
        self.assertEqual(view["decisions"][-1]["text"], "fresh concurrent event")
        self.assertEqual(view["updatedAt"], Journal(self.store.root).load()["updatedAt"])

    def test_status_detects_append_after_projection_before_publishing(self):
        original = dashboard.project
        calls = []

        def projection(reader, state):
            calls.append(reader)
            view = original(reader, state)
            if len(calls) == 1:
                writer = Journal(self.store.root)
                current = writer.load()
                current["notes"].append({"text": "last-moment event", "at": current["updatedAt"]})
                writer.append(current, "agent-decision-recorded")
            return view

        with self.inline_server(projection) as url, ready_status(url) as response:
            view = json.load(response)
        self.assertEqual(len(calls), 2)
        self.assertEqual(view["decisions"][-1]["text"], "last-moment event")

    def test_status_append_retry_is_bounded_and_other_invalid_errors_do_not_retry(self):
        for message, expected_calls in (("Журнал изменился после проверки", 3),
                                        ("Повреждён журнал, запись 1", 1)):
            with self.subTest(message=message):
                calls = []

                def projection(reader, state):
                    calls.append(reader)
                    raise Invalid(message)

                with self.inline_server(projection) as url, self.assertRaises(HTTPError) as error:
                    with ready_status(url):
                        pass
                self.assertEqual(error.exception.code, 409)
                failure = json.load(error.exception)
                self.assertEqual(failure["error"], {"code": "snapshot_source_error"})
                self.assertEqual(failure["calculation"]["state"], "source_error")
                error.exception.close()
                self.assertEqual(len(calls), expected_calls)
                self.assertEqual(len({id(reader) for reader in calls}), expected_calls)

    def test_start_status_stop_and_port_reuse(self):
        first = dashboard.start(self.store)
        self.assertTrue(first["running"])
        child = dashboard._CHILDREN[first["instance"]]
        self.assertTrue(child.stdout.closed, "A live dashboard must not keep an unread diagnostic pipe")
        self.assertEqual(dashboard.start(self.store)["instance"], first["instance"])
        self.assertEqual(dashboard.inspect(self.store)["port"], first["port"])
        with urlopen(first["url"], timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"AISpa", response.read())
        with ready_status(first["url"].replace("dashboard.html", "status.json")) as response:
            self.assertEqual(json.load(response)["status"], "running")
        metadata = self.store.home / "service/dashboard.json"
        self.assertNotIn("token", json.dumps(first))
        self.assertTrue(metadata.is_file())
        if os.name != "nt":
            self.assertEqual(metadata.stat().st_mode & 0o777, 0o600)
        result = dashboard.stop(self.store)
        self.assertTrue(result["stopped"])
        self.assertIsNotNone(child.poll(), "The exact spawned process must exit after authenticated stop")
        self.assertFalse(dashboard.inspect(self.store)["running"])
        again = dashboard.start(self.store)
        self.assertTrue(again["running"])
        self.assertEqual(again["port"], first["port"])
        self.assertNotEqual(again["instance"], first["instance"])

    def test_new_run_restarts_authenticated_service_and_keeps_previous_journal(self):
        first = dashboard.start(self.store)
        old_child = dashboard._CHILDREN[first["instance"]]
        self.state["status"] = "cancelled"
        self.store.append(self.state, "run-cancelled")
        previous_journal = self.store.run / "events.jsonl"
        before = previous_journal.read_bytes()
        next_state = self.store.create("next", "Next fixture")
        stale = dashboard.inspect(self.store)
        self.assertFalse(stale["running"])
        self.assertTrue(stale["serviceRunning"])
        self.assertFalse(stale["contextCurrent"])
        self.assertIsNone(stale["url"])
        self.assertEqual(stale["context"]["run"], self.state["run"])
        with self.assertRaises(HTTPError) as error:
            with ready_status(first["url"].replace("dashboard.html", "status.json")):
                pass
        failure = json.load(error.exception)
        self.assertEqual(failure["error"], {"code": "dashboard_context_changed"})
        self.assertEqual(failure["calculation"]["state"], "source_error")
        error.exception.close()
        second = dashboard.start(self.store)
        self.assertNotEqual(second["instance"], first["instance"])
        self.assertEqual(second["port"], first["port"])
        self.assertIsNotNone(old_child.poll())
        self.assertEqual(second["context"]["run"], next_state["run"])
        with ready_status(second["url"].replace("dashboard.html", "status.json")) as response:
            self.assertEqual(json.load(response)["run"], next_state["run"])
        self.assertEqual(previous_journal.read_bytes(), before)

    def test_replaced_asset_is_not_served_as_current_until_restart(self):
        first = dashboard.start(self.store)
        asset = self.store.home / "dashboard.html"
        replacement = b"<!doctype html><title>AISpa replacement fixture</title>"
        asset.write_bytes(replacement)
        old = dashboard._read_meta(self.store)
        health = dashboard._health(self.store, old)
        self.assertEqual(health["assetSha256"], first["context"]["assetSha256"])
        self.assertFalse(dashboard.inspect(self.store)["running"])
        second = dashboard.start(self.store)
        self.assertNotEqual(second["instance"], first["instance"])
        self.assertNotEqual(second["context"]["assetSha256"], first["context"]["assetSha256"])
        with urlopen(second["url"], timeout=3) as response:
            self.assertEqual(response.read(), replacement)

    def test_new_reader_version_restarts_old_daemon_without_migrating_old_run(self):
        first = dashboard.start(self.store)
        old_child = dashboard._CHILDREN[first["instance"]]
        self.state["status"] = "cancelled"
        self.store.append(self.state, "run-cancelled")
        previous_journal = self.store.run / "events.jsonl"
        before = previous_journal.read_bytes()
        next_version = dashboard.VERSION + "-reader-fixture"
        next_state = self.store.create("new-version", "New version fixture",
                                      initializer=lambda state: state.update(version=next_version))
        with tempfile.TemporaryDirectory(prefix="aispa-next-reader-") as folder:
            package = Path(folder) / "aispa"
            shutil.copytree(dashboard.TOOLS_DIR.parent, package,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            version_file = package / "tools/aispa_core/__init__.py"
            version_file.write_text(version_file.read_text(encoding="utf-8").replace(
                'VERSION = "' + dashboard.VERSION + '"', 'VERSION = "' + next_version + '"'), encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", str(package / "tools/aispa.py"),
                                     "--root", str(self.store.root), "dashboard", "start"],
                                    text=True, encoding="utf-8", capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            second = json.loads(result.stdout)
            try:
                old_child.wait(timeout=3)
                self.assertNotEqual(second["instance"], first["instance"])
                self.assertEqual(second["context"]["version"], next_version)
                self.assertNotEqual(second["context"]["runtimeDigest"], first["context"]["runtimeDigest"])
                with ready_status(second["url"].replace("dashboard.html", "status.json")) as response:
                    self.assertEqual(json.load(response)["run"], next_state["run"])
                self.assertEqual(previous_journal.read_bytes(), before)
            finally:
                self.assertTrue(dashboard.stop(self.store)["stopped"])

    def test_wrong_runtime_cannot_replace_active_pinned_service(self):
        first = dashboard.start(self.store)
        metadata = dashboard._read_meta(self.store)
        self.state["runtimeDigest"] = "0" * 64
        self.store.append(self.state, "fixture-runtime-pin")
        with self.assertRaisesRegex(Invalid, "закреплённый runtime"):
            dashboard.start(self.store)
        self.assertTrue(dashboard._verified(self.store, metadata))
        self.assertEqual(dashboard._read_meta(self.store), metadata)
        self.assertIsNone(dashboard._CHILDREN[first["instance"]].poll())

    def test_legacy_owned_health_requires_restart_not_reuse(self):
        legacy = {"marker": dashboard.MARKER, "rootHash": dashboard._root_hash(self.store),
                  "instance": "legacy-fixture", "pid": os.getpid(), "token": "x" * 48}
        stopped = []

        class LegacyHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path != "/_control/health" or self.headers.get(dashboard.TOKEN_HEADER) != legacy["token"]:
                    self.send_error(404)
                    return
                raw = json.dumps({key: legacy[key] for key in ("marker", "rootHash", "instance", "pid")}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                if self.path != "/_control/shutdown" or self.headers.get(dashboard.TOKEN_HEADER) != legacy["token"]:
                    self.send_error(404)
                    return
                stopped.append(True)
                self.send_response(204)
                self.end_headers()

                def shutdown():
                    self.server.shutdown()
                    self.server.server_close()
                threading.Thread(target=shutdown, daemon=True).start()

        server = ThreadingHTTPServer((dashboard.HOST, 0), LegacyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            legacy["port"] = server.server_port
            metadata = self.store.home / "service/dashboard.json"
            metadata.parent.mkdir(parents=True)
            atomic_json(metadata, legacy)
            current = dashboard.inspect(self.store)
            self.assertFalse(current["running"])
            self.assertTrue(current["serviceRunning"])
            fresh = dashboard.start(self.store)
            self.assertTrue(stopped)
            self.assertTrue(fresh["running"])
            self.assertNotEqual(fresh["instance"], legacy["instance"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_status_error_is_safe_json_and_unchanged_journal_is_not_new_work(self):
        info = dashboard.start(self.store)
        url = info["url"].replace("dashboard.html", "status.json")
        with ready_status(url) as response:
            first = json.load(response)
        with ready_status(url) as response:
            second = json.load(response)
        self.assertEqual(first["updatedAt"], second["updatedAt"])
        self.assertEqual(first["events"], second["events"])
        self.assertIsNone(second.get("activity"))
        journal = self.store.run / "events.jsonl"
        before = journal.read_bytes()
        try:
            journal.write_bytes(b"private-error-fixture-not-public\n")
            with self.assertRaises(HTTPError) as error:
                with ready_status(url):
                    pass
            self.assertEqual(error.exception.code, 409)
            failure = json.load(error.exception)
            self.assertEqual(failure["error"], {"code": "snapshot_source_error"})
            self.assertEqual(failure["calculation"]["state"], "source_error")
            self.assertEqual(failure["lastSnapshot"]["freshness"], "historical")
            self.assertEqual(failure["lastSnapshot"]["capturedAt"], second["snapshot"]["capturedAt"])
            self.assertNotIn("private-error-fixture", json.dumps(failure))
            error.exception.close()
        finally:
            journal.write_bytes(before)

    def test_refresh_view_reads_old_active_run_without_changing_journal_or_pin(self):
        old_service = dashboard.start(self.store)
        old_child = dashboard._CHILDREN[old_service["instance"]]
        # Model a valid active stable journal produced by its own pinned engine.
        self.state.update(version="0.3.0", runtimeDigest="a" * 64)
        self.store.append(self.state, "fixture-old-runtime")
        pinned = self.store.run / "runtime"
        pinned.mkdir()
        (pinned / "SKILL.md").write_bytes(b"original pinned runtime, never replaced")
        before = {path.relative_to(self.store.run): path.read_bytes()
                  for path in self.store.run.rglob("*") if path.is_file()}
        active_before = (self.store.home / "active.json").read_bytes()
        asset = self.store.home / "dashboard.html"
        old_asset = b"<!doctype html><title>AISpa legacy viewer</title>"
        asset.write_bytes(old_asset)
        with self.assertRaises(Invalid):
            Journal(self.store.root).load()
        with self.assertRaises(Invalid):
            dashboard.start(self.store)
        refreshed = dashboard.start(self.store, refresh_view=True)
        self.assertTrue(refreshed["running"])
        self.assertIsNotNone(old_child.poll())
        self.assertEqual(refreshed["port"], old_service["port"])
        self.assertEqual(refreshed["context"]["version"], "0.3.0")
        self.assertEqual(refreshed["context"]["dashboardVersion"], dashboard.VERSION)
        self.assertEqual(refreshed["context"]["readerMode"], "compatible-view")
        self.assertEqual(asset.read_bytes(), dashboard.ASSET.read_bytes())
        backups = list((self.store.home / "service/asset-backups").glob("*.html"))
        self.assertEqual([path.read_bytes() for path in backups], [old_asset])
        with ready_status(refreshed["url"].replace("dashboard.html", "status.json")) as response:
            view = json.load(response)
        self.assertEqual(view["version"], "0.3.0")
        self.assertEqual(view["dashboardVersion"], dashboard.VERSION)
        self.assertEqual(view["viewerMode"], "compatible-view")
        self.assertEqual(view["run"], self.state["run"])
        self.assertTrue(dashboard.inspect(self.store)["running"])
        self.assertEqual(dashboard.start(self.store, refresh_view=True)["instance"], refreshed["instance"])
        with self.assertRaises(Invalid):
            Journal(self.store.root).load()
        self.assertEqual({path.relative_to(self.store.run): path.read_bytes()
                          for path in self.store.run.rglob("*") if path.is_file()}, before)
        self.assertEqual((self.store.home / "active.json").read_bytes(), active_before)

    def test_compatible_reader_has_explicit_version_allowlist_and_blocks_writes(self):
        reader = DisplayJournal(self.store.root)
        for version in SUPPORTED_VERSIONS:
            self.state["version"] = version
            self.store.append(self.state, "fixture-version")
            self.assertEqual(reader.load()["version"], version)
        before = (self.store.run / "events.jsonl").read_bytes()
        for operation in (
                lambda: reader.lock(), lambda: reader.create("other", "Other"),
                lambda: reader.append(self.state, "never"),
                lambda: reader.artifact("never", b"never", ".txt"),
                lambda: reader.apply_recovery("0" * 64, confirmed=True),
                lambda: reader.git("reset", "--hard")):
            with self.subTest(operation=operation), self.assertRaises(Invalid):
                operation()
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), before)
        for version in ("99.0.0", "0.3.2-unknown", "0.1.0-alpha.10"):
            self.state["version"] = version
            self.store.append(self.state, "fixture-unsupported-version")
            with self.subTest(version=version), self.assertRaises(Invalid):
                reader.load()

    def test_refresh_view_refuses_unsupported_or_damaged_state_before_asset_change(self):
        asset = self.store.home / "dashboard.html"
        asset.write_bytes(b"keep original")
        self.state["version"] = "99.0.0"
        self.store.append(self.state, "fixture-future-version")
        with self.assertRaises(Invalid):
            dashboard.start(self.store, refresh_view=True)
        self.assertEqual(asset.read_bytes(), b"keep original")
        self.assertFalse((self.store.home / "service").exists())
        self.state["version"] = "0.3.0"
        self.store.append(self.state, "fixture-stable-version")
        journal = self.store.run / "events.jsonl"
        journal.write_bytes(journal.read_bytes() + b"broken tail\n")
        with self.assertRaises(Invalid):
            dashboard.start(self.store, refresh_view=True)
        self.assertEqual(asset.read_bytes(), b"keep original")
        self.assertFalse((self.store.home / "service").exists())

    def test_refresh_view_rejects_asset_backup_link_without_overwrite(self):
        asset = self.store.home / "dashboard.html"
        asset.write_bytes(b"keep original")
        with tempfile.TemporaryDirectory(prefix="aispa-outside-backups-") as folder:
            backup_dir = self.store.home / "service/asset-backups"
            backup_dir.parent.mkdir()
            try:
                backup_dir.symlink_to(folder, target_is_directory=True)
            except OSError:
                self.skipTest("Native symlink capability unavailable")
            with self.assertRaises(Invalid):
                dashboard.start(self.store, refresh_view=True)
            self.assertEqual(asset.read_bytes(), b"keep original")
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_only_two_public_paths_and_host_validation(self):
        info = dashboard.start(self.store)
        base = info["url"].replace("dashboard.html", "")
        for path in ("active.json", "service/dashboard.json", ".aispa/active.json",
                     "_control/health", "dashboard.html?x=1", ""):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                urlopen(base + path, timeout=3)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        request = Request(base + "status.json", headers={"Host": "attacker.invalid"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 421)
        error.exception.close()
        request = Request(base + "_control/shutdown", method="POST")
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        self.assertTrue(dashboard.inspect(self.store)["running"])

    def test_unrelated_listener_is_never_stopped_or_port_stolen(self):
        class OtherHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"other")

            def log_message(self, *_args):
                pass

        other = ThreadingHTTPServer(("127.0.0.1", 0), OtherHandler)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            fake = {"marker": dashboard.MARKER, "rootHash": dashboard._root_hash(self.store),
                    "instance": "old-instance", "token": "x" * 48,
                    "pid": os.getpid(), "port": other.server_port}
            metadata = self.store.home / "service/dashboard.json"
            metadata.parent.mkdir(parents=True)
            atomic_json(metadata, fake)
            result = dashboard.stop(self.store)
            self.assertFalse(result["stopped"])
            self.assertFalse(result["staleMetadataRemoved"])
            with urlopen(f"http://127.0.0.1:{other.server_port}/", timeout=3) as response:
                self.assertEqual(response.read(), b"other")
            ours = dashboard.start(self.store)
            self.assertTrue(ours["running"])
            self.assertNotEqual(ours["port"], other.server_port)
            with urlopen(f"http://127.0.0.1:{other.server_port}/", timeout=3) as response:
                self.assertEqual(response.read(), b"other")
        finally:
            other.shutdown()
            other.server_close()
            thread.join(timeout=3)

    def test_private_health_and_shutdown_ignore_host_proxy_configuration(self):
        # Host-side proxy settings must never intercept control traffic or
        # receive the private token. Public browser requests are outside this.
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1",
                                     "HTTPS_PROXY": "http://127.0.0.1:1",
                                     "ALL_PROXY": "http://127.0.0.1:1"}):
            service = dashboard.start(self.store)
            self.assertTrue(service["running"])
            self.assertTrue(dashboard.inspect(self.store)["running"])
            self.assertTrue(dashboard.stop(self.store)["stopped"])

    def test_numeric_loopback_bind_never_uses_reverse_dns(self):
        with patch("socket.getfqdn", side_effect=AssertionError("reverse DNS is not needed")):
            with dashboard.LoopbackServer((dashboard.HOST, 0), BaseHTTPRequestHandler) as server:
                self.assertEqual(server.server_name, dashboard.HOST)
                self.assertGreater(server.server_port, 0)

    def test_startup_failure_reports_probe_class_without_token(self):
        with patch.object(dashboard, "_private_request", return_value=(None, "http-421")):
            with self.assertRaisesRegex(Exception, "health-http-421") as failure:
                dashboard.start(self.store)
        self.assertNotIn("token", str(failure.exception).lower())
        self.assertFalse(dashboard.inspect(self.store)["running"])


if __name__ == "__main__":
    unittest.main()
