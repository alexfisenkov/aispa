import json
import copy
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
sys.path.insert(0, str(PACKAGE / "tools"))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-cli-")
        self.root = Path(self.temp.name) / "проект with spaces 🙂"
        self.root.mkdir()
        self.git("init", "-q")
        (self.root / "app.py").write_text("print('ready')\n", encoding="utf-8", newline="\n")
        self.git("add", "app.py")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def call(self, *args, expected=0, cli=None):
        cli = cli or (CLI if args and args[0] == "init" else getattr(self, "cli", CLI))
        r = subprocess.run([sys.executable, "-B", str(cli), "--root", str(self.root), *args],
                           text=True, capture_output=True, timeout=20, encoding="utf-8")
        self.assertEqual(r.returncode, expected, r.stderr + r.stdout)
        if expected:
            self.assertNotIn("Traceback", r.stderr)
            return r
        result = json.loads(r.stdout)
        if args and args[0] == "init":
            self.cli = self.root / ".aispa" / result["runDirectory"] / "runtime/tools/aispa.py"
        return result

    def init(self, cli=CLI):
        state = self.call("init", "--slug", "example", "--title", "Пример", "--mode", "auto", "--runtime", "codex", cli=cli)
        self.run = self.root / ".aispa" / state["runDirectory"]
        self.cli = self.run / "runtime/tools/aispa.py"
        self.inbox = self.root / ".aispa/inbox"
        self.inbox.mkdir()
        self.git("add", ".gitignore", "AGENTS.md")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "ignore local records")

    def input(self, value, name="input"):
        p = self.inbox / name
        p.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), encoding="utf-8", newline="\n")
        return str(p)

    def context(self, kind, *extra):
        return self.call("context", "--kind", kind, *extra)

    def assert_artifact_tamper_stales_status(self, label, ref):
        path = self.run / ref["file"]
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"\nfixture-tamper")
            self.assertEqual(self.call("status")["status"], "stale", label)
        finally:
            path.write_bytes(original)
        self.assertEqual(self.call("status")["status"], "accepted", label + " after restore")

    def discover(self):
        from tests.test_discovery import synthetic_report
        receipt = self.call("discover")["receipt"]
        evidence = json.loads(Path(receipt["absolutePath"]).read_text(encoding="utf-8"))
        self.call("discover", "--file", self.input(synthetic_report(evidence)))

    def idea(self):
        from tests.test_ideas import synthetic_report
        from aispa_core.store import Journal
        store = Journal(self.root); state = store.load()
        self.call("idea", "--file", self.input(synthetic_report(store, state)))

    def plan_with_review(self, tasks):
        from tests.test_product_delivery_repairs import delivery_report
        draft = self.input(tasks, "draft.json")
        check = self.call("plan-check", "--file", draft)
        review = {"basis": check["basis"], "draft": tasks,
                  "groups": [{"task": t["id"], "from": [t["id"]], "reason": "Synthetic fixture: one coherent scenario"} for t in tasks],
                  "separate": [{"suggestion": s["id"], "reason": "Synthetic fixture preserves this boundary"} for s in check["suggestions"]],
                  "delivery": delivery_report(tasks)}
        self.call("plan", "--file", draft, "--review", self.input(review, "size-review.json"))

    @contextmanager
    def server(self):
        process = subprocess.Popen([sys.executable, "-B", str(getattr(self, "cli", CLI)), "--root", str(self.root), "serve"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
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
        external.write_text("keep this unchanged\n", encoding="utf-8", newline="\n")
        (self.root / ".gitignore").symlink_to(external)
        self.call("init", "--slug", "example", "--title", "Example", "--mode", "auto", "--runtime", "codex", expected=2)
        self.assertEqual(external.read_text(encoding="utf-8"), "keep this unchanged\n")
        self.assertFalse((self.root / ".aispa/active.json").exists())

    def test_context_receipts_and_derived_memory_are_current(self):
        self.init()
        result = self.call("decision", "Use standard library")
        self.assertNotIn("requirements", result)
        self.assertIn("Use standard library", (self.root / ".aispa/context/decisions.md").read_text(encoding="utf-8"))
        result = self.call("context", "--kind", "coverage", "--write")
        self.assertNotIn("briefs", result)
        packet = json.loads(Path(result["absolutePath"]).read_text(encoding="utf-8"))
        self.assertEqual(packet["basis"], result["basis"])
        self.assertLessEqual(len((self.root / ".aispa/context/memory.md").read_text(encoding="utf-8")), 8000)

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
        self.assertIn("Команда", (self.root / ".aispa/context/carryover.md").read_text(encoding="utf-8"))

    def test_empty_finish_fails_with_nonzero_exit(self):
        self.init()
        self.call("finish", expected=2)
        self.assertEqual(self.call("status")["status"], "running")

    def test_pinned_runtime_survives_package_edits(self):
        copy = Path(self.temp.name) / "package"
        shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        cli = copy / "tools/aispa.py"
        self.init(cli)
        (copy / "assets/dashboard.html").write_text("changed template", encoding="utf-8", newline="\n")
        self.call("decision", "Новая запись", cli=cli, expected=2)
        pinned = self.run / "runtime/tools/aispa.py"
        self.call("decision", "Новая запись", cli=pinned)
        state = self.call("status", cli=pinned)
        self.assertEqual(state["decisions"][0]["text"], "Новая запись")

    def test_full_cli_scenario(self):
        self.init()
        from tests import alpha10_fixtures as fixtures
        from aispa_core.store import Journal
        runner = "python" if os.name == "nt" else "python3"
        from aispa_core.store import file_hash
        owner_brief = "Команда выводит ready. Используй README.md как образец для команды."
        def participant(role):
            return self.call("participant", "--role", role, "--runtime", "codex",
                             "--native-id", "synthetic-" + role, "--session", "test-session")["receipt"]["actor"]

        coordinator = participant("coordinator")
        analyst = participant("brief-analyst")
        reviewer = participant("brief-reviewer")
        self.call("usage", "--file", self.input({"actor": coordinator, "callId": "synthetic-coordinator-call",
            "source": "synthetic native usage fixture", "inputTokens": 20,
            "outputTokens": 5, "cachedInputTokens": 3}, "usage-coordinator.json"))
        self.call("usage", "--file", self.input({"actor": analyst, "callId": "synthetic-brief-call",
            "source": "synthetic native usage fixture", "inputTokens": 12,
            "outputTokens": 3, "cachedInputTokens": 1}, "usage-brief.json"))
        self.assertEqual(set(self.call("usage")["byRole"]), {"coordinator", "brief-analyst"})
        (self.root / "README.md").write_text(
            "# CLI fixture\n\n## Quickstart\nPython 3.11+; run `python3 app.py` on macOS or `python app.py` on Windows.\n"
            "The program prints ready. Python standard library only.\n"
            "The whole one-statement program is the check.\n"
            "One print statement has no separate architecture.\n"
            "No material technical alternative in this synthetic fixture.\n"
            "No domain terminology beyond standard output.\n", encoding="utf-8", newline="\n")
        self.git("add", "README.md")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "document synthetic CLI")
        self.call("brief", "--file", self.input(owner_brief))
        self.call("requirements", "--file", self.input([{"id": "R01", "text": "Команда",
            "brief": "B001", "quote": owner_brief, "core": True, "acceptance": ["Выводит ready"]}]))
        self.call("reference", "--file", self.input({"path": "README.md", "sha256": file_hash(self.root / "README.md"),
                  "brief": "B001", "quote": "Используй README.md как образец для команды.",
                  "requirements": ["R01"], "use": "Use the owner's local README quickstart as a concrete reference for this fixture."},
                  "owner-reference.json"))
        self.call("seal", expected=2)
        original = self.call("brief-source")
        self.call("brief-extract", "--file", self.input(fixtures.source_report(original, analyst), "source-analysis.json"))
        independent_source = self.call("brief-source")
        self.assertNotIn(analyst, json.dumps(independent_source))
        self.call("brief-review", "--file", self.input(fixtures.source_report(independent_source, reviewer), "source-review.json"))
        mapping = self.call("brief-map-context")
        self.call("brief-map", "--file", self.input(fixtures.source_map_report(mapping, reviewer), "source-map.json"))
        self.assertTrue(self.call("preparation")["brief"]["valid"])
        participant("idea")
        self.idea()
        self.call("seal")
        self.call("spec", "--file", self.input("Команда выводит ready через стандартный вывод."), expected=2)
        participant("discovery")
        self.discover()
        challenge = self.call("idea", "--full")["report"]["challenge"]
        spec = "Команда выводит ready через стандартный вывод. README.md служит образцом команды.\n" + "\n".join(
            row["decision"] + "\n" + row["validation"] for row in challenge)
        self.call("spec", "--file", self.input(spec))
        coverage_actor = participant("coverage")
        ctx = self.context("coverage")
        self.call("coverage", "--file", self.input({"basis": ctx["basis"], "reviewer": coverage_actor,
            "independent": True, "unmapped": [], "checks": [{"requirement": "R01", "status": "covered", "specQuote": "Команда выводит ready"}],
            "ideaChecks": [{"probe": row["probe"], "status": "covered", "specQuote": row["decision"] + "\n" + row["validation"],
                           "note": "Synthetic correspondence; this test is not semantic review."} for row in challenge]}))
        current = Journal(self.root); design_state = current.load()
        self.call("scenarios", "--file", self.input(fixtures.scenarios(design_state,
                  source_quote=owner_brief, spec_quote="Команда выводит ready"), "scenarios.json"))
        design_state = current.load()
        self.call("interfaces", "--file", self.input(fixtures.interfaces(design_state), "interfaces.json"))
        self.assertTrue(self.call("preparation")["design"]["scenariosValid"])
        self.assertTrue(self.call("preparation")["design"]["interfacesValid"])
        participant("planner")
        tasks = [{"id": "T01", "title": "Команда", "requirements": ["R01"],
                  "dependsOn": [], "zones": ["app.py"], "readZones": ["."], "criteria": ["Выводит ready"]}]
        self.call("plan", "--file", self.input(tasks), expected=2)
        self.plan_with_review(tasks)
        ctx = self.context("task", "--task", "T01")
        self.call("start", "T01", "--context", ctx["basis"])
        for role in ("worker", "critic1", "critic2", "acceptor"):
            self.call("assign", "T01", "--role", role, "--runtime", "codex", "--native-id", "synthetic-" + role, "--session", "test-session")
        task_context = self.context("task", "--task", "T01")
        self.call("usage", "--file", self.input({"actor": task_context["actors"]["worker"],
            "callId": "synthetic-worker-call", "source": "synthetic native usage fixture",
            "inputTokens": 30, "outputTokens": 8, "cachedInputTokens": 5}, "usage-worker.json"))
        self.assertEqual(self.call("usage")["byRole"]["worker"]["measuredCalls"], 1)
        intent = fixtures.existing_behavior_intent(task_context, self.root)
        self.call("test-intent", "--task", "T01", "--file",
                  self.input(intent, "test-intent-v1.json"))
        revised_intent = copy.deepcopy(intent)
        revised_intent["notApplicable"][0]["reason"] += " The source hash is frozen before submission."
        self.call("test-intent", "--task", "T01", "--file",
                  self.input(revised_intent, "test-intent-v2.json"))
        self.assertEqual(self.call("test-intent", "--task", "T01")["revision"], 2)
        self.call("verify", "--task", "T01", "--", sys.executable, "-c",
                  "import subprocess,sys; assert subprocess.check_output([sys.executable,'app.py'],text=True).strip()=='ready'; print('checked')")

        def proof(kind, rid, extra=()):
            ctx = self.context(kind, *extra)
            role = extra[extra.index("--role") + 1] if "--role" in extra else None
            checks = ctx.get("expectedChecks", [{"requirement": rid, "criterion": "Выводит ready"}])
            report = {"basis": ctx["basis"], "reviewer": ctx["actors"][role] if role else
                      getattr(self, "blind_actor", "synthetic") if kind == "acceptance" else "synthetic", "independent": True,
                      "unmapped": [], "checks": [dict(c, status="passed", artifact=ctx["verification"]["output"]["file"], note="Проверено тестовой командой") for c in checks]}
            if role and role != "worker":
                report["testSourceChanges"] = []
            if role in ("critic1", "critic2"):
                report["findings"] = []
                report["branchReview"] = fixtures.branch_review(self.root, "T01", ctx["verification"]["output"]["file"])
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
        run = self.call("verify", "--purpose", "run", "--", sys.executable, "app.py")["receipt"]
        check = self.call("verify", "--purpose", "check", "--", sys.executable, "app.py")["receipt"]
        commands = self.call("commands")
        self.assertEqual(commands["count"], 3)
        self.assertEqual(self.call("commands", "--receipt", run["sha256"])["purpose"], "run")
        self.call("wave", "1", "--file", self.input(proof("wave", "R01")))
        self.call("context", "--kind", "acceptance", expected=2)
        self.call("acceptance", "--file", self.input({}), expected=2)
        self.call("clean-prepare", expected=2)
        self.call("handoff", "--file", self.input({"schema": 1, "profile": "small",
            "documents": [{"path": "README.md", "sha256": file_hash(self.root / "README.md"),
                           "sources": [{"path": "app.py", "sha256": file_hash(self.root / "app.py")}],
                           "covers": {"quickstart": "Quickstart"}}],
            "notApplicable": {"architecture": "One print statement has no separate architecture.",
                              "decisions": "No material technical alternative in this synthetic fixture.",
                              "glossary": "No domain terminology beyond standard output."},
            "commands": {"check": {"receipt": check, "document": "README.md", "heading": "Quickstart",
                                  "display": runner + " app.py", "runner": runner},
                         "run": {"receipt": run, "document": "README.md", "heading": "Quickstart",
                                 "display": runner + " app.py", "runner": runner},
                         "install": {"notApplicable": "Python standard library only."},
                         "singleTest": {"notApplicable": "The whole one-statement program is the check."}},
            "obligations": []}))
        self.call("clean-prepare")
        blind = self.context("blind")
        self.assertNotIn("requirements", blind)
        self.assertNotIn("spec", blind)
        self.assertNotEqual(Path(blind["clone"]["path"]), self.root)
        clean = self.call("clean-verify", "--", sys.executable, "-B", "-c",
                  "import subprocess,sys; assert subprocess.check_output([sys.executable,'app.py'],text=True).strip()=='ready'; print('fresh clone checked')")["receipt"]
        self.call("context", "--kind", "acceptance", expected=2)
        blind_actor = participant("blind")
        self.blind_actor = blind_actor
        blind = self.context("blind")
        self.call("blind", "--file", self.input({"basis": blind["basis"], "reviewer": blind_actor, "independent": True,
                  "observations": [{"brief": "B001", "quote": "Команда выводит ready", "status": "passed",
                                    "note": "Read original brief first; synthetic clean clone assertion passed", "artifact": clean["output"]["file"]}]}))
        self.call("acceptance", "--file", self.input(proof("acceptance", "R01")))
        self.call("finish", expected=2)
        writer = self.call("knowledge-assign", "--runtime", "codex", "--native-id", "synthetic-knowledge",
                           "--session", "fresh-knowledge-session")["receipt"]["actor"]
        learned = self.context("knowledge")
        self.assertNotIn("spec", learned); self.assertNotIn("briefs", learned); self.assertNotIn("tasks", learned)
        self.call("knowledge-record", "--file", self.input({"writer": writer["id"], "basis": learned["basis"],
            "entries": [{"key": "cli.ready", "kind": "command", "text": "Synthetic: stdout check of this CLI.",
                         "sources": [{"path": "app.py", "sha256": file_hash(self.root / "app.py")}],
                         "command": [sys.executable, "app.py"],
                         "verification": {"kind": "execution", "sha256": run["sha256"]}}],
            "uncertainties": {k: "Synthetic fixture did not establish additional knowledge in this category." for k in ("trap", "decision", "glossary")}}))
        self.assertEqual(len(self.call("knowledge", "--path", "app.py")["entries"]), 1)
        state = self.call("finish")
        self.assertEqual(state["status"], "accepted")
        self.assertTrue(state["gates"]["acceptance"])
        saved = Journal(self.root).load()
        attempt = saved["taskHistory"]["T01"][-1]
        self.assertEqual(len(attempt["testIntentHistory"]), 2)
        integrity_refs = {
            "test intent first revision": attempt["testIntentHistory"][0],
            "test intent second revision": attempt["testIntentHistory"][1],
            "test baseline": attempt["testBaseline"],
            "brief map": saved["briefMap"],
            "feature scenarios": saved["featureScenarios"],
            "interface contract": saved["interfaceContract"],
            "owner reference snapshot": saved["ownerReferences"][0]["snapshot"],
        }
        for label, ref in integrity_refs.items():
            with self.subTest(artifact=label):
                self.assert_artifact_tamper_stales_status(label, ref)
        original = (self.root / "app.py").read_bytes()
        with self.server() as url:
            (self.root / "app.py").write_text("print('changed after delivery')\n", encoding="utf-8", newline="\n")
            with urllib.request.urlopen(url.replace("dashboard.html", "status.json"), timeout=5) as response:
                self.assertEqual(json.load(response)["status"], "stale")
        (self.root / "app.py").write_bytes(original)
        new = self.call("init", "--slug", "next", "--title", "Next iteration", "--mode", "auto", "--runtime", "codex")
        self.assertNotEqual(new["runDirectory"], state["runDirectory"])
        self.assertEqual(new["status"], "running")
        history = self.call("knowledge", "--path", "app.py", "--archive")
        self.assertEqual(len(history["entries"]), 1)
        self.assertTrue(history["historicalOnly"])
        self.call("knowledge-compact")
        self.assertEqual(len(self.call("knowledge", "--path", "app.py")["entries"]), 1)
        self.call("finish", expected=2)

    def test_local_dashboard_serves_real_projection(self):
        self.init()
        with self.server() as url:
            with urllib.request.urlopen(url, timeout=5) as response:
                self.assertIn("AISpa", response.read().decode())
            with urllib.request.urlopen(url.replace("dashboard.html", "status.json"), timeout=5) as response:
                self.assertEqual(json.load(response)["status"], "running")
            journal = self.run / "events.jsonl"
            journal.write_text(journal.read_text(encoding="utf-8").replace('"running"', '"accepted"'), encoding="utf-8", newline="\n")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url.replace("dashboard.html", "status.json"), timeout=5)
            self.assertEqual(error.exception.code, 409)
            error.exception.close()

    def test_dashboard_reads_while_verification_holds_writer_lock(self):
        self.init()
        with self.server() as url:
            process = subprocess.Popen([sys.executable, "-B", str(CLI), "--root", str(self.root),
                                        "verify", "--", sys.executable, "-c", "import time; time.sleep(2); print('checked')"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
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
