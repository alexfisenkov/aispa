"""CLI-to-host dashboard instructions; no native browser or model runs here."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.request import ProxyHandler, build_opener

CLI = Path(__file__).resolve().parents[1] / "skills/aispa/tools/aispa.py"
sys.path.insert(0, str(CLI.parent))
from aispa_core import dashboard_host, dashboard_server
from aispa_core.store import Invalid, Journal
from test_preflight import synthetic_host_snapshot


class DashboardHostTests(unittest.TestCase):
    def test_first_browser_action_has_targeted_identity_adoption_before_reserve_and_open(self):
        try:
            action, = dashboard_host.start_request(managed=True)
        except TypeError:
            self.fail("The new init must give a managed browser contract before full project preflight")
        contract = action["resourceContract"]
        self.assertEqual(contract["identityPurpose"], "browser")
        self.assertIn("reserve", contract["steps"])
        self.assertNotIn("sealed", contract.get("prerequisites", []))
        self.assertNotIn("browserOpened", action)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-dashboard-host-")
        self.root = Path(self.temp.name) / "проект с пробелами 🙂"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.cli = CLI
        self.store = Journal(self.root)

    def tearDown(self):
        try:
            if (self.store.home / "service/dashboard.json").exists():
                dashboard_server.stop(self.store)
        finally:
            self.temp.cleanup()

    def call(self, *args, compact=False, expected=0):
        if args and args[0] in ("resume", "dashboard") and (self.store.home / "active.json").exists():
            state = self.store.load()
            if state.get("policy", {}).get("managedExecution"):
                host = synthetic_host_snapshot(self.store, state)
                if host is not None:
                    host_file = self.root / ".aispa/synthetic-host.json"
                    host_file.write_text(json.dumps(host), encoding="utf-8")
                    if args[0] == "resume":
                        args = (*args, "--host", str(host_file))
                    elif len(args) > 1 and args[1] == "start" and not state.get("hostSession"):
                        self.call("host-context", "--file", str(host_file))
        flags = ["--compact"] if compact else []
        result = subprocess.run([sys.executable, "-B", str(self.cli), "--root", str(self.root), *flags, *args],
                                cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        return result if expected else json.loads(result.stdout)

    def init(self, compact=False):
        result = self.call("init", "--slug", "dashboard-host", "--title", "Dashboard fixture",
                           "--mode", "auto", "--runtime", "codex", "--check", "balanced", compact=compact)
        self.cli = self.store.home / result["runDirectory"] / "runtime/tools/aispa.py"
        return result

    def assert_start_request(self, result):
        action, = result["requiredHostActions"]
        plain = {k: v for k, v in action.items() if k != "resourceContract"}
        self.assertEqual(plain, {
            "action": "start-dashboard", "runner": "pinned-aispa-cli",
            "cliArguments": ["dashboard", "start"], "verification": "required",
            "instruction": plain["instruction"],
        })
        self.assertNotIn("surface", action)
        self.assertNotIn("visible", action)
        self.assertIn("open-dashboard", plain["instruction"])
        self.assertEqual(action["resourceContract"]["kind"], "browsers")
        self.assertEqual(action["resourceContract"]["scope"], "project")
        self.assertIn("reserve", action["resourceContract"]["steps"])
        self.assertNotIn("opened", json.dumps(result))
        self.assertNotIn("browserOpened", result)
        self.assertFalse((self.store.home / "service/dashboard.json").exists())

    def test_init_full_and_compact_return_mandatory_action_without_starting_server(self):
        initial = self.init()
        self.assert_start_request(initial)
        self.call("close", "cancelled", "--note", "Synthetic fixture finished")
        self.cli = CLI
        compact = self.init(compact=True)
        self.assert_start_request(compact)
        full = json.loads(Path(compact["absolutePath"]).read_text(encoding="utf-8"))
        self.assertEqual(compact["requiredHostActions"], full["requiredHostActions"])
        self.assertTrue(self.cli.is_file())
        self.assertFalse(dashboard_server.inspect(self.store)["running"])

    def test_resume_full_and_compact_keep_host_action_and_failed_resume_has_no_request(self):
        self.init()
        for compact in (False, True):
            with self.subTest(compact=compact):
                self.call("close", "paused", "--note", "Synthetic pause")
                resumed = self.call("resume", compact=compact)
                self.assertEqual(resumed["status"], "running")
                self.assert_start_request(resumed)
                if compact:
                    full = json.loads(Path(resumed["absolutePath"]).read_text(encoding="utf-8"))
                    self.assertEqual(resumed["requiredHostActions"], full["requiredHostActions"])
        continued = self.call("resume")
        self.assertEqual(continued["status"], "running")
        self.assertFalse(continued["autonomy"]["mayAsk"])

    def test_dashboard_start_returns_exact_live_url_reuses_service_and_does_not_claim_browser_opened(self):
        self.init()
        first = self.call("dashboard", "start")
        try:
            self.assertTrue(first["running"])
            action, = first["requiredHostActions"]
            self.assertEqual({k: v for k, v in action.items() if k != "resourceContract"}, {
                "action": "open-dashboard", "url": first["url"], "surface": "embedded",
                "visible": True, "verification": "required",
            })
            self.assertEqual(action["resourceContract"]["kind"], "browsers")
            self.assertNotIn("opened", json.dumps(first))
            metadata = json.loads((self.store.home / "service/dashboard.json").read_text(encoding="utf-8"))
            self.assertNotIn("token", first)
            self.assertNotIn(metadata["token"], json.dumps(first))
            opener = build_opener(ProxyHandler({}))
            with opener.open(first["url"], timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"AISpa", response.read())
            again = self.call("dashboard", "start", compact=True)
            self.assertEqual(again["url"], first["url"])
            self.assertEqual(again["instance"], first["instance"])
            self.assertEqual(again["requiredHostActions"], first["requiredHostActions"])
            inspected = self.call("dashboard", "status")
            self.assertEqual(inspected["url"], first["url"])
            self.assertNotIn("requiredHostActions", inspected)
            self.assertNotIn(metadata["token"], json.dumps(again))
        finally:
            stopped = self.call("dashboard", "stop")
            self.assertTrue(stopped["stopped"])
            self.assertNotIn("requiredHostActions", stopped)
        self.assertFalse(self.call("dashboard", "status")["running"])

    def test_batch_resume_is_rejected_before_an_earlier_mutating_step(self):
        self.init()
        state = self.store.load()
        journal = self.store.run / "events.jsonl"
        before = journal.read_bytes()
        steps = self.store.home / "host-action-batch.json"
        steps.write_text(json.dumps([["decision", "Must never be recorded"], ["resume"]]), encoding="utf-8")
        rejected = self.call("batch", "--file", str(steps), expected=2)
        self.assertIn("шаг недопустим в пакете: resume", rejected.stderr)
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(self.store.load(), state)
        self.assertEqual(rejected.stdout, "")

    def test_read_only_status_never_starts_service_or_reissues_host_action(self):
        self.init()
        for compact in (False, True):
            result = self.call("status", compact=compact)
            self.assertNotIn("requiredHostActions", result)
        result = self.call("dashboard", "status")
        self.assertFalse(result["running"])
        self.assertIsNone(result["url"])
        self.assertNotIn("requiredHostActions", result)
        self.assertFalse((self.store.home / "service/dashboard.json").exists())
        self.assertFalse((self.store.home / "service/last-port.json").exists())

    def test_open_request_rejects_unverified_or_noncanonical_public_urls(self):
        valid = {"running": True, "port": 12345, "url": "http://127.0.0.1:12345/dashboard.html", "unverified": False}
        variants = ({"running": False}, {"unverified": True}, {"port": True}, {"port": 0},
                    {"url": "https://example.invalid/dashboard.html"}, {"url": valid["url"] + "?token=private"},
                    {"url": "http://localhost:12345/dashboard.html"}, {"url": "http://127.0.0.1:12346/dashboard.html"})
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(Invalid):
                dashboard_host.open_request({**valid, **variant})
        request = dashboard_host.open_request({**valid, "token": "not-to-be-returned"})
        self.assertNotIn("not-to-be-returned", json.dumps(request))
        self.assertEqual(request[0]["verification"], "required")


if __name__ == "__main__":
    unittest.main()
