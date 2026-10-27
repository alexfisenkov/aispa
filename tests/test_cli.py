import json
import copy
import io
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
from contextlib import redirect_stderr

PACKAGE = Path(__file__).resolve().parents[1] / "skills/aispa"
CLI = PACKAGE / "tools/aispa.py"
sys.path.insert(0, str(PACKAGE / "tools"))


def route_args(role, runtime="codex", route_class=None):
    """--model and --reasoning of a role from the engine's route table (modelRoutes runs refuse empty ones)."""
    from aispa_core import model_routes
    if role == "coordinator":
        return ()
    row = model_routes.ROUTES[route_class or model_routes.ROLE_CLASS[role]][runtime]
    return ("--model", row["model"], "--reasoning", row["effort"])


class InterviewSettingCliTests(unittest.TestCase):
    def test_new_init_persists_interview_without_changing_auto_or_max(self):
        from aispa_core.store import Journal
        with tempfile.TemporaryDirectory(prefix="aispa interview axes 🙂 ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            result = subprocess.run([sys.executable, "-B", str(CLI), "--root", str(root), "init",
                "--slug", "axes", "--title", "Independent interview axes", "--mode", "auto",
                "--runtime", "codex", "--check", "max", "--depth", "strict",
                "--interview-scope", "deep", "--interview-minutes", "25"],
                capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            state = Journal(root).load()
            self.assertEqual(state["interview"], {"scope": "deep", "durationMinutes": 25})
            self.assertEqual((state["mode"], state["checkMode"], state["depth"]), ("auto", "max", "strict"))
            self.assertTrue(state["policy"]["managedExecution"])
            self.assertFalse(json.loads(result.stdout)["autonomy"]["mayAsk"])

    def test_interview_axes_parse_independently_of_autonomy_check_and_depth(self):
        import aispa
        for mode in ("auto", "important", "interview"):
            for check in ("fast", "balanced", "max"):
                for scope in ("compact", "standard", "deep"):
                    for depth in ("strict", "balanced", "deep"):
                        with self.subTest(mode=mode, check=check, scope=scope, depth=depth), redirect_stderr(io.StringIO()):
                            try:
                                args = aispa.parser().parse_args(["init", "--slug", "axes", "--title", "Axes",
                                    "--mode", mode, "--runtime", "codex", "--check", check,
                                    "--depth", depth, "--interview-scope", scope, "--interview-minutes", "25"])
                            except SystemExit:
                                self.fail("Interview scope/duration must be accepted independently of all existing axes")
                            self.assertEqual((args.mode, args.check, args.depth, args.interview_scope,
                                              args.interview_minutes), (mode, check, depth, scope, 25))

    def test_invalid_duration_does_not_create_run_or_project_files(self):
        import aispa
        from aispa_core.store import Invalid
        with tempfile.TemporaryDirectory(prefix="aispa-invalid-interview-") as tmp:
            root = Path(tmp)
            args = aispa.parser().parse_args(["--root", str(root), "init", "--slug", "axes", "--title", "Axes",
                "--mode", "auto", "--runtime", "codex", "--check", "max"])
            args.interview_scope, args.interview_minutes = "standard", -1
            with self.assertRaises(Invalid):
                aispa.execute(args)
            self.assertEqual(list(root.iterdir()), [])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-cli-")
        self.root = Path(self.temp.name) / "проект with spaces 🙂"
        self.root.mkdir()
        self.command_env = dict(os.environ)
        runner_directory = Path(sys.executable).parent
        if os.name != "nt":
            # A directly selected python3.11 need not be the host's default
            # python3. Model an activated interpreter without changing the host.
            runner_directory = Path(self.temp.name) / "selected-python"
            runner_directory.mkdir()
            (runner_directory / "python3").symlink_to(Path(sys.executable).resolve())
        self.command_env["PATH"] = str(runner_directory) + os.pathsep + os.environ.get("PATH", "")
        self.git("init", "-q")
        (self.root / "app.py").write_text("print('ready')\n", encoding="utf-8", newline="\n")
        (self.root / "INTENT.md").write_text("# Project intent\nA local command with observable output.\n", encoding="utf-8", newline="\n")
        self.git("add", "app.py", "INTENT.md")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def call(self, *args, expected=0, cli=None, timeout=None):
        cli = cli or (CLI if args and args[0] == "init" else getattr(self, "cli", CLI))
        if args and args[0] in ("seal", "resume"):
            from aispa_core.store import Journal
            from test_preflight import synthetic_host_snapshot
            fixture_store = Journal(self.root)
            fixture_state = fixture_store.load()
            if fixture_state.get("policy", {}).get("managedExecution"):
                host = synthetic_host_snapshot(fixture_store, fixture_state)
                if host is not None:
                    host_file = self.root / ".aispa/inbox/synthetic-host.json"
                    host_file.parent.mkdir(parents=True, exist_ok=True)
                    host_file.write_text(json.dumps(host), encoding="utf-8")
                    if args[0] == "resume":
                        args = (*args, "--host", str(host_file))
                    else:
                        self.call("host-context", "--file", str(host_file), cli=cli)
        # Fixtures read full answers; --full keeps the previous default contract of these tests.
        output = [] if args and args[0].startswith("-") else ["--full"]
        r = subprocess.run([sys.executable, "-B", str(cli), "--root", str(self.root), *output, *args],
                           text=True, capture_output=True,
                           timeout=getattr(self, "cli_timeout", 20) if timeout is None else timeout,
                           encoding="utf-8", env=self.command_env,
                           cwd=getattr(self, "command_cwd", None))
        self.assertEqual(r.returncode, expected, r.stderr + r.stdout)
        if expected:
            self.assertNotIn("Traceback", r.stderr)
            return r
        result = json.loads(r.stdout)
        if args and args[0] == "init":
            self.cli = self.root / ".aispa" / result["runDirectory"] / "runtime/tools/aispa.py"
        return result

    def init(self, cli=CLI):
        state = self.call("init", "--slug", "example", "--title", "Пример", "--mode", "auto", "--runtime", "codex", "--check", "balanced", cli=cli)
        self.run = self.root / ".aispa" / state["runDirectory"]
        self.cli = self.run / "runtime/tools/aispa.py"
        self.inbox = self.root / ".aispa/inbox"
        self.inbox.mkdir()
        (self.root / "AGENTS.md").write_text((self.root / "AGENTS.md").read_text(encoding="utf-8") + "\nSynthetic fixture permits independent delegation and test processes.\n", encoding="utf-8", newline="\n")
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

    @contextmanager
    def status_response(self, url):
        """Observe one exact generation; preserve ready bytes and HTTP errors."""
        requested, generation, deadline = url, None, time.monotonic() + 10
        while time.monotonic() < deadline:
            with urllib.request.urlopen(requested, timeout=max(.001, deadline - time.monotonic())) as response:
                raw = response.read()
                value = json.loads(raw)
                if response.code == 200:
                    self.assertEqual(value["calculation"]["state"], "ready")
                    with io.BytesIO(raw) as body:
                        yield body
                    return
                self.assertEqual(response.code, 202)
                self.assertEqual(value["calculation"]["state"], "calculating")
                identifier = value["calculation"]["id"]
                self.assertRegex(identifier, r"^[0-9a-f]{32}$")
                self.assertTrue(generation is None or generation == identifier)
                generation = identifier
                requested = url + "?calculation=" + identifier
            time.sleep(.01)
        self.fail("The addressed status generation did not settle within 10 seconds")

    def test_mode_is_explicit(self):
        self.call("init", "--slug", "example", "--title", "Example", "--runtime", "codex", "--check", "balanced", expected=2)
        self.assertFalse((self.root / ".aispa/active.json").exists())

    def test_runtime_is_explicit(self):
        self.call("init", "--slug", "example", "--title", "Example", "--mode", "auto", "--check", "balanced", expected=2)
        self.assertFalse((self.root / ".aispa/active.json").exists())

    def test_init_does_not_write_through_ignore_symlink(self):
        external = Path(self.temp.name) / "external-ignore"
        external.write_text("keep this unchanged\n", encoding="utf-8", newline="\n")
        (self.root / ".gitignore").symlink_to(external)
        self.call("init", "--slug", "example", "--title", "Example", "--mode", "auto", "--runtime", "codex", "--check", "balanced", expected=2)
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
        self.call("init", "--slug", "example", "--title", "Example", "--mode", "auto", "--runtime", "codex", "--check", "balanced", expected=2)
        self.assertFalse((self.root / ".aispa/active.json").exists())

    def test_carry_command_records_real_disposition_across_runs(self):
        self.init()
        self.call("brief", "--file", self.input("Команда выводит ready"))
        requirement = {"id": "R01", "text": "Команда", "brief": "B001", "quote": "Команда выводит ready",
                       "core": True, "acceptance": ["Выводит ready"]}
        self.call("requirements", "--file", self.input([requirement]))
        self.call("block", "T99", "--note", "unknown", expected=2)
        self.call("close", "cancelled", "--note", "Synthetic owner stopped this fixture")
        state = self.call("init", "--slug", "second", "--title", "Next", "--mode", "auto", "--runtime", "codex", "--check", "balanced")
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

    def deliver_one_task(self):
        """Brief to a closed final task T01 through the CLI; returns the report builder and the participant helper."""
        self.command_cwd = Path(self.temp.name) / "посторонний каталог 🙂"
        self.command_cwd.mkdir()
        self.init()
        from tests import alpha10_fixtures as fixtures
        from aispa_core.store import Journal
        runner = "python" if os.name == "nt" else "python3"
        from aispa_core.store import file_hash
        owner_brief = "Команда выводит ready. Используй README.md как образец для команды."
        def participant(role):
            return self.call("participant", "--role", role, "--runtime", "codex", *route_args(role),
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
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests/check_output.py").write_text(
            "import os, subprocess, sys\n"
            "assert subprocess.check_output([sys.executable, 'app.py'], text=True).strip() == 'ready'\n"
            "read_fd, write_fd = os.pipe()\n"
            "os.close(read_fd)\n"
            "try:\n"
            "    result = subprocess.run([sys.executable, 'app.py'], stdout=write_fd, stderr=subprocess.PIPE)\n"
            "    assert result.returncode != 0, 'closed stdout must not silently succeed'\n"
            "finally:\n"
            "    os.close(write_fd)\n"
            "print('ready and closed-output checks passed')\n", encoding="utf-8", newline="\n")
        self.git("add", "README.md", "tests/check_output.py")
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
        # The single coordinator genuinely authors these phases; no phantom actors.
        self.idea()
        self.call("seal")
        self.call("spec", "--file", self.input("Команда выводит ready через стандартный вывод."), expected=2)
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
        tasks = [{"id": "T01", "title": "Команда", "requirements": ["R01"],
                  "dependsOn": [], "zones": ["app.py"], "readZones": ["."], "criteria": ["Выводит ready"]}]
        self.call("plan", "--file", self.input(tasks), expected=2)
        self.plan_with_review(tasks)
        ctx = self.context("task", "--task", "T01")
        self.call("start", "T01", "--context", ctx["basis"])
        for role in ("worker", "critic1"):
            self.call("assign", "T01", "--role", role, "--runtime", "codex", *route_args(role),
                      "--native-id", "synthetic-" + role, "--session", "test-session")
        # New runs are lean: the passing critic closes the task, no task acceptor.
        self.call("assign", "T01", "--role", "acceptor", "--runtime", "codex", *route_args("acceptor"),
                  "--native-id", "synthetic-acceptor",
                  "--session", "test-session", expected=2)
        task_context = self.context("task", "--task", "T01")
        self.call("usage", "--file", self.input({"actor": task_context["actors"]["worker"],
            "callId": "synthetic-worker-call", "source": "synthetic native usage fixture",
            "inputTokens": 30, "outputTokens": 8, "cachedInputTokens": 5}, "usage-worker.json"))
        self.assertEqual(self.call("usage")["byRole"]["worker"]["measuredCalls"], 1)
        intent = fixtures.existing_behavior_intent(task_context, self.root)
        intent["failureChecks"] = {
            "cases": [{"id": "E01", "boundary": "output", "scenario": "Closed stdout has no readers",
                       "expected": "Actual child process must report failure rather than silently succeed",
                       "criterion": "Выводит ready", "test": {"path": "tests/check_output.py",
                       "sha256": file_hash(self.root / "tests/check_output.py")}}],
            "notApplicable": [{"boundaries": ["input", "storage", "protocol", "dependency"],
                               "reason": "The declared one-statement fixture has no inputs, persistence, protocol or external dependencies.",
                               "source": {"path": "README.md", "sha256": file_hash(self.root / "README.md")}}]}
        self.call("test-intent", "--task", "T01", "--file",
                  self.input(intent, "test-intent-v1.json"))
        revised_intent = copy.deepcopy(intent)
        revised_intent["notApplicable"][0]["reason"] += " The source hash is frozen before submission."
        self.call("test-intent", "--task", "T01", "--file",
                  self.input(revised_intent, "test-intent-v2.json"))
        self.assertEqual(self.call("test-intent", "--task", "T01")["revision"], 2)
        self.call("verify", "--task", "T01", "--", sys.executable, "tests/check_output.py")

        def proof(kind, rid, extra=()):
            role = extra[extra.index("--role") + 1] if "--role" in extra else None
            if role:
                bundle = self.context(kind, *extra, "--write", "--with-template")
                self.assertEqual(bundle["role"], role)
                ctx = json.loads(Path(bundle["context"]["absolutePath"]).read_text(encoding="utf-8"))
                for key, section in ctx.pop("sections", {}).items():
                    ctx[key] = json.loads(Path(section["absolutePath"]).read_text(encoding="utf-8"))
                draft_path = Path(bundle["template"]["absolutePath"])
                self.assertEqual(draft_path, (self.root / bundle["template"]["path"]).resolve())
                self.assertFalse((self.command_cwd / bundle["template"]["path"]).exists())
                self.assertEqual(file_hash(draft_path), bundle["template"]["sha256"])
                draft = json.loads(draft_path.read_text(encoding="utf-8"))
                self.assertEqual(draft["basis"], ctx["basis"])
                self.assertEqual(draft["reviewer"], ctx["actors"][role])
                self.assertTrue(all(row["status"] == "not_tested" for row in draft["checks"]))
                self.assertIsNone(draft["unmapped"])
            else:
                ctx = self.context(kind, *extra)
            checks = ctx.get("expectedChecks", [{"requirement": rid, "criterion": "Выводит ready"}])
            report = {"basis": ctx["basis"], "reviewer": ctx["actors"][role] if role else
                      getattr(self, "blind_actor", "synthetic") if kind == "acceptance" else "synthetic", "independent": True,
                      "unmapped": [], "checks": [dict(c, status="passed", artifact=ctx["verification"]["output"]["file"], note="Проверено тестовой командой") for c in checks]}
            if role and role != "worker":
                report["testSourceChanges"] = []
                report["verdict"] = "PASS"
            if role in ("critic1", "critic2"):
                report["findings"] = []
                self.assertEqual(len(draft["trace"]), len(ctx["traceRequirements"]))
                # Focused: the final task's critic reads its attempt; the whole branch goes to the lens reviewers.
                self.assertEqual(ctx["zoneChangesScope"], "attempt")
                self.assertEqual(ctx["projectReviewLenses"], [["behavior", "data_security"],
                                                              ["integration", "maintainability", "test_quality"]])
                self.assertNotIn("branchReview", draft)
                self.assertFalse([c for c in checks if c["criterion"].startswith("Вся ветка")])
                report.update(inheritedProofs=ctx["inheritedProofs"],
                    trace=[{"requirement": r["id"], "brief": r["brief"], "quote": r["quote"], "result": "aligned",
                            "note": "Сопоставлено с брифом", "artifact": ctx["verification"]["output"]["file"]} for r in ctx["traceRequirements"]])
            if role == "critic2":
                report.update(previousReport=ctx["rounds"][0]["report"]["sha256"], repairReceipt=None, resolutions=[])
            if role == "acceptor":
                report.update(previousReport=ctx["rounds"][-1]["report"]["sha256"], inheritedProofs=ctx["inheritedProofs"],
                    trace=[{"requirement": r["id"], "brief": r["brief"], "quote": r["quote"], "result": "aligned",
                            "note": "Сопоставлено с брифом", "artifact": ctx["verification"]["output"]["file"]} for r in ctx["traceRequirements"]])
            if kind == "acceptance":
                report['productFlowReview'] = fixtures.final_flows(ctx, self.run, report['checks'])
                report["cleanrun"] = (ctx.get("cleanrun") or {}).get("artifact", {}).get("sha256")
                report["blindReport"] = ctx["blindReview"]["report"]["sha256"]
                # Focused: blindMapping indexes the packet's list (a recheck replaces the diverged item's rows).
                report["blindMapping"] = [{"observation": index, "requirement": "R01"}
                                          for index, _row in enumerate(ctx.get("blindObservations") or [None])]
            return report

        self.call("submit", "T01", "--file", self.input(proof("task", "T01", ("--task", "T01", "--role", "worker"))))
        self.call("critic", "T01", "--round", "1", "--file", self.input(proof("task", "T01", ("--task", "T01", "--role", "critic1"))))
        self.call("assign", "T01", "--role", "critic2", "--runtime", "codex", *route_args("critic2"),
                  "--native-id", "must-not-run", "--session", "test-session", expected=2)
        task_view = next(t for t in self.call("status")["tasks"] if t["id"] == "T01")
        self.assertEqual((task_view["status"], task_view["completionKind"]), ("completed", "critic_acceptance"))
        return proof, participant, coordinator

    def handoff(self):
        """Run and check receipts of the current code, then the tracked handoff documents that cite them."""
        from aispa_core.store import file_hash
        runner = "python" if os.name == "nt" else "python3"
        run = self.call("verify", "--purpose", "run", "--", sys.executable, "app.py")["receipt"]
        check = self.call("verify", "--purpose", "check", "--", sys.executable, "app.py")["receipt"]
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
            "obligations": []}, "handoff.json"))
        return run, check

    def branch_review(self, warnings=None):
        """Focused: every lens of the mode by its own fresh integration reviewer through the CLI.

        warnings maps a lens number to the warning findings its reviewer raises (confirmed at once)."""
        from aispa_core.store import Journal
        evidence = Journal(self.root).load()["verification"]["output"]["file"]
        pending = self.call("integration-review")["pendingLenses"]
        for number, lens in enumerate(pending, 1):
            actor = self.call("participant", "--role", "integration-reviewer", "--runtime", "codex",
                              *route_args("integration-reviewer"), "--native-id", "synthetic-lens-%d" % number,
                              "--session", "test-session")["receipt"]["actor"]
            ctx = self.call("integration-review", "--actor", actor)
            self.assertEqual(ctx["pendingLenses"][0], lens)
            report = ctx["template"]
            self.assertEqual((report["lens"], report["reviewer"]), (lens, actor))
            branch = report["branchReview"]
            self.assertEqual([row["axis"] for row in branch["axes"]], lens)
            branch.update(note="Synthetic structural lens review of the whole branch diff.", evidence=evidence,
                          axes=[dict(row, status="passed", note="Synthetic fixture inspected " + row["axis"],
                                     evidence=evidence) for row in branch["axes"]])
            report["findings"] = list((warnings or {}).get(number, []))
            report["verdict"] = "PASS"
            if "triage" in report:
                report["triage"] = [dict(row, status="confirmed", note="Synthetic: a real issue") for row in report["triage"]]
            self.call("integration-review", "--actor", actor, "--file", self.input(report, "lens-%d.json" % number))

    def test_full_cli_scenario(self):
        from aispa_core.store import Journal, file_hash
        proof, participant, coordinator = self.deliver_one_task()
        runner = "python" if os.name == "nt" else "python3"
        run = self.call("verify", "--purpose", "run", "--", sys.executable, "app.py")["receipt"]
        check = self.call("verify", "--purpose", "check", "--", sys.executable, "app.py")["receipt"]
        commands = self.call("commands")
        self.assertEqual(commands["count"], 3)
        self.assertEqual(self.call("commands", "--receipt", run["sha256"])["purpose"], "run")
        # No wave is checked: the brief is checked at the start and by the blind acceptance from a clean clone.
        refused = self.call("wave", "1", "--file", self.input({}), expected=2)
        self.assertIn("волны не сверяются", refused.stderr)
        self.call("context", "--kind", "acceptance", expected=2)
        self.call("acceptance", "--file", self.input({}), expected=2)
        refused = self.call("clean-prepare", expected=2)
        self.assertIn("проверка всей ветки", refused.stderr)
        self.branch_review()
        self.call("clean-prepare", expected=2)  # the handoff documents come first
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
        clean = self.call("clean-verify", "--", sys.executable, "-B", "tests/check_output.py")["receipt"]
        self.call("context", "--kind", "acceptance", expected=2)
        blind_actor = participant("blind")
        self.blind_actor = blind_actor
        blind = self.context("blind")
        self.call("blind", "--file", self.input({"basis": blind["basis"], "reviewer": blind_actor, "independent": True,
                  "observations": [{"brief": "B001", "quote": "Команда выводит ready", "status": "passed",
                                    "note": "Read original brief first; synthetic clean clone assertion passed", "artifact": clean["output"]["file"]}]}))
        self.call("acceptance", "--file", self.input(proof("acceptance", "R01")))
        self.call("finish", expected=2)
        writer = self.call("knowledge-assign", "--runtime", "codex", *route_args("knowledge-writer"),
                           "--native-id", "synthetic-knowledge",
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
        if os.name == "nt":
            finish_started = time.monotonic()
            try:
                state = self.call("finish", timeout=60)
            finally:
                print(f"Windows final finish elapsed={time.monotonic() - finish_started:.3f}s; caller timeout=60s", flush=True)
        else:
            state = self.call("finish")
        self.assertEqual(state["status"], "accepted")
        self.assertTrue(state["gates"]["acceptance"])
        saved_store = Journal(self.root)
        saved = saved_store.load()
        attempt = saved["taskHistory"]["T01"][-1]
        self.assertEqual(len(attempt["rounds"]), 1)
        self.assertNotIn("critic2", attempt["actors"])
        self.assertEqual(len(saved["actors"]), 2)  # worker and critic1; lean has no task acceptor
        from aispa_core import phase_receipts
        for phase in ("idea", "discovery", "planner"):
            self.assertEqual(phase_receipts.current(saved_store, saved, phase)["actor"], coordinator)
        self.assertFalse(self.call("usage")["coverage"]["missingPhases"])
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
            with self.status_response(url.replace("dashboard.html", "status.json")) as response:
                self.assertEqual(json.load(response)["status"], "stale")
        (self.root / "app.py").write_bytes(original)
        new = self.call("init", "--slug", "next", "--title", "Next iteration", "--mode", "auto", "--runtime", "codex", "--check", "balanced")
        self.assertNotEqual(new["runDirectory"], state["runDirectory"])
        self.assertEqual(new["status"], "running")
        history = self.call("knowledge", "--path", "app.py", "--archive")
        self.assertEqual(len(history["entries"]), 1)
        self.assertTrue(history["historicalOnly"])
        self.call("knowledge-compact")
        self.assertEqual(len(self.call("knowledge", "--path", "app.py")["entries"]), 1)
        self.call("finish", expected=2)

    def final_repair(self, kind, tag, change):
        """One final repair through the CLI: open, fresh worker, change, submit, fresh checker reading the diff."""
        opened = self.call("final-repair", "open", "--file",
                           self.input({"kind": kind, "zones": ["USAGE.md"]}, "repair-open-%s.json" % tag))["receipt"]
        repair = opened["repair"]
        self.assertEqual(repair["kind"], kind)
        people = {}
        for role in ("worker", "checker"):
            if role == "checker":
                (self.root / "USAGE.md").write_text(change, encoding="utf-8", newline="\n")
                self.git("add", "USAGE.md")
                self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "repair " + tag)
                submitted = self.call("final-repair", "submit", "--file", self.input({
                    "repair": repair["id"], "worker": people["worker"]["actor"], "summary": "Synthetic repair " + tag,
                    "items": [{"id": i, "note": "Synthetic: changed the usage note"} for i in repair["items"]]},
                    "repair-submit-%s.json" % tag))["receipt"]
                self.assertEqual(submitted["changes"], ["USAGE.md"])
            people[role] = self.call("final-repair", "assign", "--role", role, "--runtime", "codex",
                                     *route_args("worker" if role == "worker" else "integration-reviewer"),
                                     "--native-id", "synthetic-repair-%s-%s" % (role, tag),
                                     "--session", "test-session")["receipt"]
        packet = people["checker"]["packet"]
        self.assertEqual([row["path"] for row in packet["changes"]], ["USAGE.md"])  # the checker reads the diff only
        checked = self.call("final-repair", "check", "--file", self.input({
            "repair": repair["id"], "checker": people["checker"]["actor"], "independent": True,
            "candidate": packet["candidate"], "verdict": "PASS",
            "items": [{"id": i, "status": "fixed", "note": "Synthetic: the diff fixes it"} for i in repair["items"]]},
            "repair-check-%s.json" % tag))["receipt"]
        self.assertEqual((checked["verdict"], checked["status"]), ("PASS", "passed"))
        return repair

    def progress(self):
        from aispa_core.store import Journal
        return Journal(self.root).load().get("workProgress") or {}

    def test_focused_finale_repairs_findings_and_rechecks_the_blind_divergence(self):
        """Lenses of the branch, one findings repair, a failed blind acceptance, its repair and a fresh recheck."""
        from aispa_core.store import Journal, file_hash
        self.command_env["AISPA_CALIBRATION_FILE"] = str(Path(self.temp.name) / "calibration.json")
        (self.root / "USAGE.md").write_text("Usage: run app.py\n", encoding="utf-8", newline="\n")
        self.git("add", "USAGE.md")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "usage note")
        proof, participant, _coordinator = self.deliver_one_task()
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        # The engine starts the measured branch-review stage at the first branch context.
        self.assertEqual(self.call("integration-review")["pendingLenses"],
                         [["behavior", "data_security"], ["integration", "maintainability", "test_quality"]])
        self.assertEqual((self.progress()["phase"], self.progress().get("finishedAt")), ("integration", None))
        warning = {"id": "N1", "severity": "warning", "criterion": "Выводит ready",
                   "text": "The usage note does not say what the command prints"}
        self.branch_review({1: [warning]})
        journal = Journal(self.root)
        journal.load()
        events = [row["event"] for row in journal.read_journal(include_events=True)["events"]]
        self.assertEqual(events.count("project-integration-lens-reviewed"), 1)  # the stage outlives the first lens
        self.assertEqual(events.count("project-integration-reviewed"), 1)
        self.assertTrue(self.progress()["finishedAt"])
        # A context call after the last lens neither reopens the closed stage nor writes an event.
        finished = self.progress()
        self.assertEqual(self.call("integration-review")["pendingLenses"], [])
        self.assertEqual(self.progress(), finished)
        journal.load()
        self.assertEqual(len(journal.read_journal(include_events=True)["events"]), len(events))
        self.assertEqual([(row["id"], row["status"]) for row in self.call("findings")["findings"]], [("W001", "confirmed")])
        refused = self.call("clean-prepare", expected=2)
        self.assertIn("final-repair", refused.stderr)
        self.final_repair("findings", "findings", "Usage: run app.py; it prints ready\n")
        self.assertEqual(self.call("findings", "--status", "fixed")["count"], 1)
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        self.handoff()
        self.call("clean-prepare")
        self.assertEqual((self.progress()["phase"], self.progress().get("finishedAt")), ("blind", None))
        clean = self.call("clean-verify", "--", sys.executable, "-B", "tests/check_output.py")["receipt"]
        self.blind_actor = participant("blind")
        blind = self.context("blind")
        self.assertNotIn("recheck", blind)
        self.call("blind", "--file", self.input({"basis": blind["basis"], "reviewer": self.blind_actor, "independent": True,
                  "observations": [{"brief": "B001", "quote": "Команда выводит ready", "status": "failed",
                                    "note": "Synthetic: the documented output was not observed"}]}, "blind-1.json"))
        self.assertTrue(self.progress()["finishedAt"])
        first_blind = Journal(self.root).load()["blindReview"]["report"]
        failed = self.call("acceptance", "--file", self.input(proof("acceptance", "R01"), "acceptance-1.json"), expected=2)
        self.assertFalse(json.loads(failed.stdout)["ok"])
        refused = self.call("finish", expected=2)  # a repairable divergence is repaired before delivery
        self.assertIn("Обязательное ядро", refused.stderr)
        repair = self.final_repair("blind", "blind", "Usage: run app.py; it prints ready to stdout\n")
        self.assertEqual((repair["items"], repair["recheckOf"]), (["R01"], first_blind))
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        self.handoff()
        self.call("clean-prepare")
        clean = self.call("clean-verify", "--", sys.executable, "-B", "tests/check_output.py")["receipt"]
        recheck_actor = self.call("participant", "--role", "blind", "--runtime", "codex", *route_args("blind"),
                                  "--native-id", "synthetic-blind-recheck", "--session", "test-session")["receipt"]["actor"]
        blind = self.context("blind")
        self.assertEqual((blind["recheck"]["items"], blind["recheck"]["recheckOf"]),
                         (["R01"], {k: first_blind[k] for k in ("file", "sha256")}))
        observation = {"brief": "B001", "quote": "Команда выводит ready", "status": "passed",
                       "note": "Synthetic recheck in a new clean clone", "artifact": clean["output"]["file"]}
        full = {"basis": blind["basis"], "reviewer": recheck_actor, "independent": True, "observations": [observation]}
        self.call("blind", "--file", self.input(full, "blind-full.json"), expected=2)  # only a recheck after the repair
        self.call("blind", "--file", self.input(dict(full, recheckOf=blind["recheck"]["recheckOf"], items=["R01"]),
                                                "blind-2.json"))
        self.blind_actor = recheck_actor
        self.assertTrue(self.call("acceptance", "--file", self.input(proof("acceptance", "R01"), "acceptance-2.json"))["ok"])
        writer = self.call("knowledge-assign", "--runtime", "codex", *route_args("knowledge-writer"),
                           "--native-id", "synthetic-knowledge", "--session", "fresh-knowledge-session")["receipt"]["actor"]
        learned = self.context("knowledge")
        run_receipt = self.call("verify", "--purpose", "run", "--", sys.executable, "app.py")["receipt"]
        self.call("knowledge-record", "--file", self.input({"writer": writer["id"], "basis": learned["basis"],
            "entries": [{"key": "cli.ready", "kind": "command", "text": "Synthetic: stdout check of this CLI.",
                         "sources": [{"path": "app.py", "sha256": file_hash(self.root / "app.py")}],
                         "command": [sys.executable, "app.py"],
                         "verification": {"kind": "execution", "sha256": run_receipt["sha256"]}}],
            "uncertainties": {k: "Synthetic fixture did not establish additional knowledge in this category."
                              for k in ("trap", "decision", "glossary")}}))
        state = self.call("finish", timeout=60)
        self.assertEqual(state["status"], "accepted")
        saved = Journal(self.root).load()
        self.assertEqual([(r["id"], r["kind"], r["status"]) for r in saved["finalRepairs"]],
                         [("F01", "findings", "passed"), ("F02", "blind", "passed")])
        self.assertEqual([row["source"] for row in saved["candidateChain"]], ["branch", "repair", "repair"])
        self.assertEqual(saved["remaining"]["findings"], [])
        self.assertEqual(saved["blindReview"]["repair"], "F02")
        stages = {row["stage"] for row in saved.get("stageSamples", [])}
        self.assertTrue({"branchReview", "blind", "finalRepair"} <= stages, stages)

    def focused_to_blind(self, status):
        """Standard 0.5.0 policy through the CLI up to the first blind acceptance with the given status."""
        (self.root / "USAGE.md").write_text("Usage: run app.py\n", encoding="utf-8", newline="\n")
        self.git("add", "USAGE.md")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "usage note")
        proof, participant, _coordinator = self.deliver_one_task()
        from aispa_core.store import Journal
        policy = Journal(self.root).load()["policy"]
        self.assertTrue(policy.get("focusedReview") and policy.get("criticalProductFlows") is True)
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        self.branch_review()
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        self.handoff()
        self.call("clean-prepare")
        clean = self.call("clean-verify", "--", sys.executable, "-B", "tests/check_output.py")["receipt"]
        self.blind_actor = participant("blind")
        blind = self.context("blind")
        observation = {"brief": "B001", "quote": "Команда выводит ready", "status": status,
                       "note": "Synthetic first blind observation"}
        if status == "passed":
            observation["artifact"] = clean["output"]["file"]
        self.call("blind", "--file", self.input({"basis": blind["basis"], "reviewer": self.blind_actor, "independent": True,
                  "observations": [observation]}, "blind-1.json"))
        return proof

    def finish_with_knowledge(self, tag):
        from aispa_core.store import file_hash
        writer = self.call("knowledge-assign", "--runtime", "codex", *route_args("knowledge-writer"),
                           "--native-id", "synthetic-knowledge-" + tag, "--session", "fresh-knowledge-" + tag)["receipt"]["actor"]
        learned = self.context("knowledge")
        run_receipt = self.call("verify", "--purpose", "run", "--", sys.executable, "app.py")["receipt"]
        self.call("knowledge-record", "--file", self.input({"writer": writer["id"], "basis": learned["basis"],
            "entries": [{"key": "cli.ready", "kind": "command", "text": "Synthetic: stdout check of this CLI.",
                         "sources": [{"path": "app.py", "sha256": file_hash(self.root / "app.py")}],
                         "command": [sys.executable, "app.py"],
                         "verification": {"kind": "execution", "sha256": run_receipt["sha256"]}}],
            "uncertainties": {k: "Synthetic fixture did not establish additional knowledge in this category."
                              for k in ("trap", "decision", "glossary")}}, "knowledge-%s.json" % tag))
        self.assertEqual(self.call("finish", timeout=60)["status"], "delivered_with_remaining")
        from aispa_core.store import Journal
        return Journal(self.root).load()

    def test_focused_unmapped_request_is_delivered_with_the_product_flow_source_in_the_remainder(self):
        """Standard policy: only an unmapped request fails; nothing to repair, so finish delivers the list."""
        proof = self.focused_to_blind("passed")
        report = proof("acceptance", "R01")
        report["unmapped"] = ["Экспорт в PDF"]
        failed = self.call("acceptance", "--file", self.input(report, "acceptance-unmapped.json"), expected=2)
        self.assertFalse(json.loads(failed.stdout)["ok"])
        refused = self.call("final-repair", "open", "--file",
                            self.input({"kind": "blind", "zones": ["USAGE.md"]}, "repair-open-none.json"), expected=2)
        self.assertIn("finish", refused.stderr)
        state = self.finish_with_knowledge("unmapped")
        self.assertEqual(state["status"], "delivered_with_remaining")
        self.assertEqual(state["remaining"]["unmapped"], ["Экспорт в PDF"])
        self.assertIn(("product-flow-source", "product-flow"),
                      [(row.get("criterion"), row.get("kind")) for row in state["remaining"]["checks"]])

    def test_focused_flow_failure_after_both_blind_cycles_is_delivered_never_accepted(self):
        """Standard policy: both blind repair cycles spent, the R01 flow still diverges; finish delivers the remainder."""
        from aispa_core.store import Journal
        proof = self.focused_to_blind("failed")
        for cycle in (1, 2):
            failed = self.call("acceptance", "--file", self.input(proof("acceptance", "R01"), "acceptance-%d.json" % cycle),
                               expected=2)
            self.assertFalse(json.loads(failed.stdout)["ok"])
            refused = self.call("finish", expected=2)  # a repair cycle is left: repair first
            self.assertIn("Обязательное ядро", refused.stderr)
            first_blind = Journal(self.root).load()["blindReview"]["report"]
            self.final_repair("blind", "blind-%d" % cycle, "Usage: run app.py; attempt %d\n" % cycle)
            self.call("verify", "--", sys.executable, "tests/check_output.py")
            self.handoff()
            self.call("clean-prepare")
            self.call("clean-verify", "--", sys.executable, "-B", "tests/check_output.py")
            actor = self.call("participant", "--role", "blind", "--runtime", "codex", *route_args("blind"),
                              "--native-id", "synthetic-blind-recheck-%d" % cycle, "--session", "test-session")["receipt"]["actor"]
            blind = self.context("blind")
            self.assertEqual(blind["recheck"]["recheckOf"], {k: first_blind[k] for k in ("file", "sha256")})
            self.call("blind", "--file", self.input({"basis": blind["basis"], "reviewer": actor, "independent": True,
                      "recheckOf": blind["recheck"]["recheckOf"], "items": ["R01"],
                      "observations": [{"brief": "B001", "quote": "Команда выводит ready", "status": "failed",
                                        "note": "Synthetic recheck: still not observed"}]}, "blind-recheck-%d.json" % cycle))
            self.blind_actor = actor
        failed = self.call("acceptance", "--file", self.input(proof("acceptance", "R01"), "acceptance-3.json"), expected=2)
        self.assertFalse(json.loads(failed.stdout)["ok"])
        refused = self.call("final-repair", "open", "--file",
                            self.input({"kind": "blind", "zones": ["USAGE.md"]}, "repair-open-third.json"), expected=2)
        self.assertIn("третий не открывается", refused.stderr)
        state = self.finish_with_knowledge("exhausted")
        self.assertEqual(state["status"], "delivered_with_remaining")
        flows = [row for row in state["remaining"]["checks"] if row.get("kind") == "product-flow"]
        self.assertTrue(flows and all(row["requirement"] == "R01" for row in flows), state["remaining"]["checks"])

    def test_local_dashboard_serves_real_projection(self):
        self.init()
        with self.server() as url:
            with urllib.request.urlopen(url, timeout=5) as response:
                self.assertIn("AISpa", response.read().decode())
            with self.status_response(url.replace("dashboard.html", "status.json")) as response:
                self.assertEqual(json.load(response)["status"], "running")
            journal = self.run / "events.jsonl"
            journal.write_text(journal.read_text(encoding="utf-8").replace('"running"', '"accepted"'), encoding="utf-8", newline="\n")
            with self.assertRaises(urllib.error.HTTPError) as error:
                with self.status_response(url.replace("dashboard.html", "status.json")):
                    pass
            self.assertEqual(error.exception.code, 409)
            error.exception.close()

    def test_dashboard_reads_while_verification_holds_writer_lock(self):
        self.init()
        from aispa_core.store import Journal
        from test_preflight import synthetic_host_snapshot
        store = Journal(self.root)
        host = synthetic_host_snapshot(store, store.load(), session="cli-writer-lock-fixture")
        self.call("host-context", "--file", self.input(host, "writer-host.json"))
        self.assertEqual(store.load()["hostSession"], "cli-writer-lock-fixture")
        entered, release = self.inbox / "verification-entered", self.inbox / "verification-release"
        program = ("from pathlib import Path\nimport sys,time\n"
                   "entered,release=map(Path,sys.argv[1:])\n"
                   "entered.write_text('started',encoding='utf-8')\n"
                   "deadline=time.monotonic()+12\n"
                   "while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                   "assert release.exists(), 'fixture release was not observed'\n"
                   "print('checked')\n")
        with self.server() as url:
            process = subprocess.Popen([sys.executable, "-B", str(self.cli), "--root", str(self.root),
                                        "verify", "--", sys.executable, "-c", program, str(entered), str(release)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                       env=self.command_env)
            try:
                deadline = time.monotonic() + 5
                while not entered.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(entered.exists(), "The verification child was not admitted")
                seen = False
                for _ in range(40):
                    with self.status_response(url.replace("dashboard.html", "status.json")) as response:
                        data = json.load(response)
                    if (data.get("activity") or {}).get("kind") == "verification":
                        seen = True
                        self.assertIsNone(process.poll())
                        break
                    time.sleep(.025)
                self.assertTrue(seen, "The active verification was not visible")
                release.write_text("release", encoding="utf-8")
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stdout + stderr)
            finally:
                release.write_text("release", encoding="utf-8")
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)


class AuditOrdinaryTextCliTests(unittest.TestCase):
    """R12 repair: ordinary bounded text is selected independently of language."""

    def test_real_compact_assess_keeps_mixed_languages_unknown_extension_and_extensionless_text(self):
        import hashlib
        from test_assessment import tree_bytes
        sources = {
            "main.c": b"int main(void) { return 0; }\n",
            "main.cpp": b"int main() { return 0; }\n",
            "Program.cs": b"class Program { static void Main() {} }\n",
            "index.html": b"<!doctype html><p>Existing project</p>\n",
            "styles.css": b"body { color: #123456; }\n",
            "schema.sql": b"CREATE TABLE task (id INTEGER PRIMARY KEY);\n",
            "build.sh": b"#!/bin/sh\ntouch SHOULD_NOT_EXECUTE\n",
            "main.js": b"console.log('control');\n",
            "domain.unregistered-source-format": "процесс заявки -> проверка -> ответ\n".encode("utf-8"),
            "serviceplan": b"service app\n  entry main.cpp\n  port 8080\n",
        }
        with tempfile.TemporaryDirectory(prefix="audit mixed source 🙂 ") as tmp:
            root = Path(tmp)
            for name, data in sources.items():
                (root / name).write_bytes(data)
            before = tree_bytes(root)
            result = subprocess.run([sys.executable, "-B", str(CLI), "--root", str(root), "--compact", "assess"],
                cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["mode"], "audit-only")
            facts = {row["path"]: row for row in report["facts"]}
            self.assertEqual(set(facts), set(sources),
                "Ordinary source/text must not be discarded by a language-extension allowlist")
            for name, data in sources.items():
                self.assertEqual(facts[name]["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(facts[name]["excerpt"], data.decode("utf-8"))
            self.assertEqual(report["executedCommands"], [])
            self.assertTrue(report["unknowns"], "Reading text cannot certify every language/runtime")
            self.assertFalse(report["repair"]["authorized"])
            self.assertEqual(tree_bytes(root), before)

    def test_ordinary_text_selection_preserves_private_media_binary_link_and_special_guards(self):
        from aispa_core import assessment
        from test_assessment import no_effects, tree_bytes
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = Path(tmp) / "project", Path(tmp) / "outside"
            root.mkdir(); outside.mkdir()
            (root / "module.c").write_text("int ready = 1;\n", encoding="utf-8")
            forbidden = []
            for name in ("private", "data", "vendor", "generated", "media", ".git"):
                directory = root / name
                directory.mkdir()
                (directory / "ordinary.customsrc").write_text("PRIVATE_CANARY_DO_NOT_READ")
                forbidden.append(directory)
            for name in (".env", "client-secret.txt", "wallet.pem", "metrics.csv", "snapshot.sqlite", "clip.mp4", "banner.png"):
                path = root / name
                path.write_text("PRIVATE_OR_MEDIA_CANARY_DO_NOT_READ")
                forbidden.append(path)
            binaries = {"payload.unknown-binary": b"\0BINARY_CANARY\x01\x02",
                        "signal.unknown-format": b"\x01\x02\x03" * 20 + b"BINARY_CANARY",
                        "encoded.unknown-source": b"\xff\xfeBINARY_CANARY"}
            for name, data in binaries.items():
                (root / name).write_bytes(data)
            (outside / "source.customsrc").write_text("OUTSIDE_CANARY_DO_NOT_READ")
            forbidden.append(outside)
            try:
                (root / "external.customsrc").symlink_to(outside / "source.customsrc")
            except OSError:
                pass
            if hasattr(os, "mkfifo"):
                os.mkfifo(root / "stream.customsrc")
            before = tree_bytes(root)
            with no_effects(forbidden):
                report = assessment.assess(root)
            facts = {row["path"]: row for row in report["facts"]}
            self.assertIn("module.c", facts, "Useful ordinary source cannot be excluded with unsafe inputs")
            for path in forbidden:
                if path.parent == root:
                    self.assertNotIn(path.name, facts)
            rendered = json.dumps(report)
            for sentinel in ("PRIVATE_CANARY", "PRIVATE_OR_MEDIA_CANARY", "BINARY_CANARY", "OUTSIDE_CANARY"):
                self.assertNotIn(sentinel, rendered)
            for name in binaries:
                self.assertNotIn("excerpt", facts.get(name, {}))
                self.assertTrue(any(row["path"] == name for row in report["omissions"]))
            self.assertTrue(report["omissions"])
            self.assertFalse(report["complete"])
            self.assertEqual(report["executedCommands"], [])
            self.assertEqual(tree_bytes(root), before)

    def test_unknown_text_sources_still_obey_read_budgets_without_launch_or_writes(self):
        from aispa_core import assessment
        from test_assessment import no_effects, tree_bytes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "small.language-not-in-registry").write_text("source text\n", encoding="utf-8")
            large = root / "large.unregistered-source"
            large.write_text("oversized text\n" * 1000, encoding="utf-8")
            before = tree_bytes(root)
            with no_effects([large]):
                report = assessment.assess(root, max_file_bytes=64, max_total_bytes=64)
            self.assertIn("small.language-not-in-registry", {row["path"] for row in report["facts"]},
                "Resource limits must bound ordinary text reads, not select a fixed language list")
            self.assertTrue(any(row["path"] == large.name and "byte budget" in row["reason"] for row in report["omissions"]))
            self.assertLessEqual(report["observed"]["bytes"], 64)
            self.assertEqual(report["executedCommands"], [])
            self.assertEqual(tree_bytes(root), before)


if __name__ == "__main__":
    unittest.main()
