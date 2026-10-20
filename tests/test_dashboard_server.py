import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import dashboard_server as dashboard
from aispa_core.store import Journal, atomic_json


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
        with urlopen(first["url"].replace("dashboard.html", "status.json"), timeout=3) as response:
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

    def test_startup_failure_reports_probe_class_without_token(self):
        with patch.object(dashboard, "_private_request", return_value=(None, "http-421")):
            with self.assertRaisesRegex(Exception, "health-http-421") as failure:
                dashboard.start(self.store)
        self.assertNotIn("token", str(failure.exception).lower())
        self.assertFalse(dashboard.inspect(self.store)["running"])


if __name__ == "__main__":
    unittest.main()
