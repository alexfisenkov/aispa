import importlib.util
from pathlib import Path
import subprocess
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dashboard_compare", ROOT / "tools/preview_dashboard_compare.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


BASELINE = "68225daa2dd2a0619562d0bf874da49f0c3060bf"


class DashboardCompareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.commit = preview.make_server(ROOT, BASELINE)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_baseline_is_git_snapshot_and_candidate_is_working_asset(self):
        before = subprocess.check_output(["git", "-C", str(ROOT), "show", self.commit + ":" + preview.ASSET])
        for path, expected in (("/before/dashboard.html?demo&status=accepted", before),
                               ("/after/dashboard.html?demo", (ROOT / preview.ASSET).read_bytes())):
            with urllib.request.urlopen(self.url + path, timeout=5) as response:
                self.assertEqual(response.read(), expected)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_no_project_state_files_or_write_endpoints(self):
        requests = [urllib.request.Request(self.url + path) for path in
                    ("/status.json", "/AGENTS.md", "/after/status.json", "/after/../AGENTS.md")]
        requests.append(urllib.request.Request(self.url + "/after/dashboard.html", data=b"no-write", method="POST"))
        for request in requests:
            with self.subTest(url=request.full_url, method=request.get_method()):
                try:
                    response = urllib.request.urlopen(request, timeout=5)
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 501 if request.get_method() == "POST" else 404)
                    error.close()
                else:
                    response.close()
                    self.fail("Preview unexpectedly exposed a data/write endpoint")


if __name__ == "__main__":
    unittest.main()
