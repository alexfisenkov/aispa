import json
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error

PACKAGE = Path(__file__).resolve().parents[1] / "skills/aispa"
CLI = PACKAGE / "tools/aispa.py"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-cli-")
        self.root = Path(self.temp.name) / "project with spaces"
        self.root.mkdir()
        self.git("init", "-q")
        (self.root / "app.py").write_text("print('ready')\n")
        self.git("add", "app.py")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def call(self, *args, expected=0, cli=CLI):
        r = subprocess.run([sys.executable, "-B", str(cli), "--root", str(self.root), *args],
                           text=True, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, expected, r.stderr + r.stdout)
        if expected:
            self.assertNotIn("Traceback", r.stderr)
            return r
        return json.loads(r.stdout)

    def init(self, cli=CLI):
        state = self.call("init", "--slug", "example", "--title", "Пример", "--mode", "auto", cli=cli)
        self.run = self.root / ".aispa" / state["runDirectory"]
        self.inbox = self.root / ".aispa/inbox"
        self.inbox.mkdir()
        self.git("add", ".gitignore")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "ignore local records")

    def input(self, value):
        p = self.inbox / "input"
        p.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        return str(p)

    def context(self, kind, *extra):
        return self.call("context", "--kind", kind, *extra)

    @contextmanager
    def server(self):
        process = subprocess.Popen([sys.executable, "-B", str(CLI), "--root", str(self.root), "serve"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            url = process.stdout.readline().strip()
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)
            yield url
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_mode_is_explicit(self):
        self.call("init", "--slug", "example", "--title", "Example", expected=2)
        self.assertFalse((self.root / ".aispa/active.json").exists())

    def test_empty_finish_fails_with_nonzero_exit(self):
        self.init()
        self.call("finish", expected=2)
        self.assertEqual(self.call("status")["status"], "running")

    def test_pinned_runtime_survives_package_edits(self):
        copy = Path(self.temp.name) / "package"
        shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        cli = copy / "tools/aispa.py"
        self.init(cli)
        (copy / "assets/dashboard.html").write_text("changed template")
        self.call("decision", "Новая запись", cli=cli, expected=2)
        pinned = self.run / "runtime/tools/aispa.py"
        state = self.call("decision", "Новая запись", cli=pinned)
        self.assertEqual(state["decisions"][0]["text"], "Новая запись")

    def test_full_cli_scenario(self):
        self.init()
        self.call("brief", "--file", self.input("Команда выводит ready"))
        self.call("requirements", "--file", self.input([{"id": "R01", "text": "Команда",
            "brief": "B001", "quote": "Команда выводит ready", "core": True, "acceptance": ["Выводит ready"]}]))
        self.call("seal")
        self.call("spec", "--file", self.input("Команда выводит ready через стандартный вывод."))
        ctx = self.context("coverage")
        self.call("coverage", "--file", self.input({"basis": ctx["basis"], "reviewer": "synthetic",
            "independent": True, "unmapped": [], "checks": [{"requirement": "R01", "status": "covered", "specQuote": "Команда выводит ready"}]}))
        self.call("plan", "--file", self.input([{"id": "T01", "title": "Команда", "requirements": ["R01"],
            "dependsOn": [], "zones": ["app.py"], "criteria": ["Выводит ready"]}]))
        ctx = self.context("task", "--task", "T01")
        self.call("start", "T01", "--context", ctx["basis"])
        self.call("verify", "--", sys.executable, "-c",
                  "import subprocess,sys; assert subprocess.check_output([sys.executable,'app.py'],text=True).strip()=='ready'; print('checked')")

        def proof(kind, rid, extra=()):
            ctx = self.context(kind, *extra)
            return {"basis": ctx["basis"], "reviewer": "synthetic", "independent": True,
                    "unmapped": [], "checks": [{"requirement": rid, "criterion": "Выводит ready",
                    "status": "passed", "artifact": ctx["verification"]["output"]["file"], "note": "Проверено тестовой командой"}]}

        self.call("complete", "T01", "--file", self.input(proof("task", "T01", ("--task", "T01"))))
        self.call("wave", "1", "--file", self.input(proof("wave", "R01")))
        self.call("acceptance", "--file", self.input(proof("acceptance", "R01")))
        state = self.call("finish")
        self.assertEqual(state["status"], "accepted")
        self.assertTrue(state["gates"]["acceptance"])
        with self.server() as url:
            (self.root / "app.py").write_text("print('changed after delivery')\n")
            with urllib.request.urlopen(url.replace("dashboard.html", "status.json"), timeout=5) as response:
                self.assertEqual(json.load(response)["status"], "stale")

    def test_local_dashboard_serves_real_projection(self):
        self.init()
        with self.server() as url:
            with urllib.request.urlopen(url, timeout=5) as response:
                self.assertIn("AISpa", response.read().decode())
            with urllib.request.urlopen(url.replace("dashboard.html", "status.json"), timeout=5) as response:
                self.assertEqual(json.load(response)["status"], "running")
            journal = self.run / "events.jsonl"
            journal.write_text(journal.read_text().replace('"running"', '"accepted"'))
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url.replace("dashboard.html", "status.json"), timeout=5)
            self.assertEqual(error.exception.code, 409)
            error.exception.close()

    def test_dashboard_reads_while_verification_holds_writer_lock(self):
        self.init()
        with self.server() as url:
            process = subprocess.Popen([sys.executable, "-B", str(CLI), "--root", str(self.root),
                                        "verify", "--", sys.executable, "-c", "import time; time.sleep(2); print('checked')"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                seen = False
                for _ in range(40):
                    with urllib.request.urlopen(url.replace("dashboard.html", "status.json"), timeout=5) as response:
                        data = json.load(response)
                    if (data.get("activity") or {}).get("kind") == "verification":
                        seen = True
                        self.assertIsNone(process.poll())
                        break
                    time.sleep(.025)
                self.assertTrue(seen, "The active verification was not visible")
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stdout + stderr)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
