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
        state = self.call("init", "--slug", "example", "--title", "Пример", "--mode", "auto", "--runtime", "codex", cli=cli)
        self.run = self.root / ".aispa" / state["runDirectory"]
        self.inbox = self.root / ".aispa/inbox"
        self.inbox.mkdir()
        self.git("add", ".gitignore", "AGENTS.md")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "ignore local records")

    def input(self, value):
        p = self.inbox / "input"
        p.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        return str(p)

    def context(self, kind, *extra):
        return self.call("context", "--kind", kind, *extra)

    def discover(self):
        from test_discovery import synthetic_report
        receipt = self.call("discover")["receipt"]
        evidence = json.loads(Path(receipt["absolutePath"]).read_text())
        self.call("discover", "--file", self.input(synthetic_report(evidence)))

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
        self.call("init", "--slug", "example", "--title", "Example", "--runtime", "codex", expected=2)
        self.assertFalse((self.root / ".aispa/active.json").exists())

    def test_runtime_is_explicit(self):
        self.call("init", "--slug", "example", "--title", "Example", "--mode", "auto", expected=2)
        self.assertFalse((self.root / ".aispa/active.json").exists())

    def test_init_does_not_write_through_ignore_symlink(self):
        external = Path(self.temp.name) / "external-ignore"
        external.write_text("keep this unchanged\n")
        (self.root / ".gitignore").symlink_to(external)
        self.call("init", "--slug", "example", "--title", "Example", "--mode", "auto", "--runtime", "codex", expected=2)
        self.assertEqual(external.read_text(), "keep this unchanged\n")
        self.assertFalse((self.root / ".aispa/active.json").exists())

    def test_context_receipts_and_derived_memory_are_current(self):
        self.init()
        result = self.call("decision", "Use standard library")
        self.assertNotIn("requirements", result)
        self.assertIn("Use standard library", (self.root / ".aispa/context/decisions.md").read_text())
        result = self.call("context", "--kind", "coverage", "--write")
        self.assertNotIn("briefs", result)
        packet = json.loads(Path(result["absolutePath"]).read_text())
        self.assertEqual(packet["basis"], result["basis"])
        self.assertLessEqual(len((self.root / ".aispa/context/memory.md").read_text()), 8000)

    def test_init_rejects_ignore_directory_before_switching_active_run(self):
        (self.root / ".gitignore").mkdir()
        self.call("init", "--slug", "example", "--title", "Example", "--mode", "auto", "--runtime", "codex", expected=2)
        self.assertFalse((self.root / ".aispa/active.json").exists())

    def test_carry_command_records_real_disposition_across_runs(self):
        self.init()
        self.call("brief", "--file", self.input("Команда выводит ready"))
        requirement = {"id": "R01", "text": "Команда", "brief": "B001", "quote": "Команда выводит ready",
                       "core": True, "acceptance": ["Выводит ready"]}
        self.call("requirements", "--file", self.input([requirement]))
        self.call("block", "T99", "--note", "unknown", expected=2)
        self.call("close", "cancelled", "--note", "Synthetic owner stopped this fixture")
        state = self.call("init", "--slug", "second", "--title", "Next", "--mode", "auto", "--runtime", "codex")
        self.run = self.root / ".aispa" / state["runDirectory"]
        ledger = self.call("memory")
        item = ledger["items"][0]
        self.assertEqual(item["status"], "outstanding")
        self.call("brief", "--file", self.input("Теперь продолжи команду"))
        requirement["quote"] = "Теперь продолжи команду"
        self.call("requirements", "--file", self.input([requirement]))
        receipt = self.call("carry", item["id"], "--action", "map", "--requirement", "R01",
                            "--brief", "B001", "--quote", "Теперь продолжи команду")["receipt"]
        self.assertEqual(receipt["action"], "map")
        self.assertEqual(self.call("memory")["items"][0]["status"], "mapped")
        self.call("brief", "--file", self.input("Эту команду больше не нужно делать"))
        self.call("carry", item["id"], "--action", "drop", "--brief", "B002", "--quote", "Эту команду больше не нужно делать")
        self.assertEqual(self.call("memory")["items"][0]["status"], "dropped")
        self.assertIn("Команда", (self.root / ".aispa/context/carryover.md").read_text())

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
        self.call("decision", "Новая запись", cli=pinned)
        state = self.call("status", cli=pinned)
        self.assertEqual(state["decisions"][0]["text"], "Новая запись")

    def test_full_cli_scenario(self):
        self.init()
        self.call("brief", "--file", self.input("Команда выводит ready"))
        self.call("requirements", "--file", self.input([{"id": "R01", "text": "Команда",
            "brief": "B001", "quote": "Команда выводит ready", "core": True, "acceptance": ["Выводит ready"]}]))
        self.call("seal")
        self.call("spec", "--file", self.input("Команда выводит ready через стандартный вывод."), expected=2)
        self.discover()
        self.call("spec", "--file", self.input("Команда выводит ready через стандартный вывод."))
        ctx = self.context("coverage")
        self.call("coverage", "--file", self.input({"basis": ctx["basis"], "reviewer": "synthetic",
            "independent": True, "unmapped": [], "checks": [{"requirement": "R01", "status": "covered", "specQuote": "Команда выводит ready"}]}))
        self.call("plan", "--file", self.input([{"id": "T01", "title": "Команда", "requirements": ["R01"],
            "dependsOn": [], "zones": ["app.py"], "criteria": ["Выводит ready"]}]))
        ctx = self.context("task", "--task", "T01")
        self.call("start", "T01", "--context", ctx["basis"])
        for role in ("worker", "critic1", "critic2", "acceptor"):
            self.call("assign", "T01", "--role", role, "--runtime", "codex", "--native-id", "synthetic-" + role, "--session", "test-session")
        self.call("verify", "--task", "T01", "--", sys.executable, "-c",
                  "import subprocess,sys; assert subprocess.check_output([sys.executable,'app.py'],text=True).strip()=='ready'; print('checked')")

        def proof(kind, rid, extra=()):
            ctx = self.context(kind, *extra)
            role = extra[extra.index("--role") + 1] if "--role" in extra else None
            checks = ctx.get("expectedChecks", [{"requirement": rid, "criterion": "Выводит ready"}])
            report = {"basis": ctx["basis"], "reviewer": ctx["actors"][role] if role else "synthetic", "independent": True,
                      "unmapped": [], "checks": [dict(c, status="passed", artifact=ctx["verification"]["output"]["file"], note="Проверено тестовой командой") for c in checks]}
            if role in ("critic1", "critic2"):
                report["findings"] = []
            if role == "critic2":
                report.update(previousReport=ctx["rounds"][0]["report"]["sha256"], repairReceipt=None, resolutions=[])
            if role == "acceptor":
                report.update(previousReport=ctx["rounds"][1]["report"]["sha256"], inheritedProofs=ctx["inheritedProofs"],
                    trace=[{"requirement": r["id"], "brief": r["brief"], "quote": r["quote"], "result": "aligned",
                            "note": "Сопоставлено с брифом", "artifact": ctx["verification"]["output"]["file"]} for r in ctx["traceRequirements"]])
            if kind == "acceptance":
                report["cleanrun"] = (ctx.get("cleanrun") or {}).get("artifact", {}).get("sha256")
                report["blindReport"] = ctx["blindReview"]["report"]["sha256"]
                report["blindMapping"] = [{"observation": 0, "requirement": "R01"}]
            return report

        self.call("submit", "T01", "--file", self.input(proof("task", "T01", ("--task", "T01", "--role", "worker"))))
        self.call("critic", "T01", "--round", "1", "--file", self.input(proof("task", "T01", ("--task", "T01", "--role", "critic1"))))
        self.call("critic", "T01", "--round", "2", "--file", self.input(proof("task", "T01", ("--task", "T01", "--role", "critic2"))))
        self.call("complete", "T01", "--file", self.input(proof("task", "T01", ("--task", "T01", "--role", "acceptor"))))
        self.call("verify", "--", sys.executable, "app.py")
        self.call("wave", "1", "--file", self.input(proof("wave", "R01")))
        self.call("context", "--kind", "acceptance", expected=2)
        self.call("acceptance", "--file", self.input({}), expected=2)
        self.call("clean-prepare")
        blind = self.context("blind")
        self.assertNotIn("requirements", blind)
        self.assertNotIn("spec", blind)
        self.assertNotEqual(Path(blind["clone"]["path"]), self.root)
        clean = self.call("clean-verify", "--", sys.executable, "-B", "-c",
                  "import subprocess,sys; assert subprocess.check_output([sys.executable,'app.py'],text=True).strip()=='ready'; print('fresh clone checked')")["receipt"]
        self.call("context", "--kind", "acceptance", expected=2)
        blind = self.context("blind")
        self.call("blind", "--file", self.input({"basis": blind["basis"], "reviewer": "synthetic", "independent": True,
                  "observations": [{"brief": "B001", "quote": "Команда выводит ready", "status": "passed",
                                    "note": "Read original brief first; synthetic clean clone assertion passed", "artifact": clean["output"]["file"]}]}))
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
