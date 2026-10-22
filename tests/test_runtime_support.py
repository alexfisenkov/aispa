"""Runtime adapters: deterministic contracts, not native-model execution evidence."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
import aispa
from aispa_core import project_context as context, task_tiers
from aispa_core.store import Invalid, Journal
import test_team

CLI = Path(aispa.__file__).resolve()
HOOKS = {
    "codex": ["AGENTS.md"],
    "claude-code": ["AGENTS.md", "CLAUDE.md"],
    "cursor": ["AGENTS.md", ".cursor/rules/aispa.mdc"],
    "openclaw": ["AGENTS.md"],
    "gemini": ["AGENTS.md", "GEMINI.md"],
    "antigravity": ["AGENTS.md", "GEMINI.md", ".agents/rules/aispa.md"],
}


class RuntimeSupportTests(unittest.TestCase):
    def call(self, cli, root, *args, expected=0):
        result = subprocess.run([sys.executable, "-B", str(cli), "--root", str(root), *args],
                                cwd=root, text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result if expected else json.loads(result.stdout)

    def test_six_runtime_cli_init_context_participant_and_knowledge(self):
        self.assertEqual(set(HOOKS), set(context.RUNTIMES))
        with tempfile.TemporaryDirectory() as tmp:
            for runtime, hooks in HOOKS.items():
                with self.subTest(runtime=runtime):
                    root = Path(tmp) / (runtime + " проект с пробелами 🙂")
                    root.mkdir()
                    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
                    started = self.call(CLI, root, "init", "--slug", "runtime-fixture", "--title", "Fixture",
                                        "--mode", "auto", "--check", "balanced", "--runtime", runtime)
                    pinned = root / ".aispa" / started["runDirectory"] / "runtime/tools/aispa.py"
                    store = Journal(root)
                    state = store.load()
                    self.assertEqual(state["runtime"], runtime)
                    self.assertEqual(state["projectContext"]["instructionFiles"], hooks)
                    before = {name: (root / name).read_bytes() for name in hooks}
                    receipt = self.call(pinned, root, "project-context", "--runtime", runtime)["receipt"]
                    self.assertEqual(receipt["changedInstructionFiles"], [])
                    self.assertEqual(receipt["instructionFiles"], hooks)
                    actor = self.call(pinned, root, "participant", "--role", "coordinator", "--runtime", runtime,
                                      "--native-id", "synthetic-coordinator", "--session", "fixture-session")["receipt"]["actor"]
                    self.assertEqual(store.load()["participants"][actor]["runtime"], runtime)
                    writer = self.call(pinned, root, "knowledge-assign", "--runtime", runtime,
                                       "--native-id", "synthetic-writer", "--session", "fixture-session")["receipt"]["actor"]
                    self.assertEqual(writer["runtime"], runtime)
                    self.assertEqual(store.load()["knowledge"]["writer"]["id"], writer["id"])
                    # A new host must hit the same unsealed-brief gate, not bypass it.
                    denied = self.call(pinned, root, "assign", "T01", "--role", "worker", "--runtime", runtime,
                                       "--native-id", "synthetic-worker", "--session", "fixture-session", expected=2)
                    self.assertIn("Бриф изменён", denied.stderr)
                    wrong = "gemini" if runtime != "gemini" else "codex"
                    denied = self.call(pinned, root, "project-context", "--runtime", wrong, expected=2)
                    self.assertIn("смена среды", denied.stderr)
                    self.call(pinned, root, "context-refresh")
                    self.assertEqual({name: (root / name).read_bytes() for name in hooks}, before)
                    self.assertTrue(context.info(store, store.load())["fresh"])

    def test_runtime_parser_rejects_unknown_for_every_entrypoint(self):
        commands = [
            ["init", "--slug", "x", "--title", "x", "--mode", "auto", "--check", "balanced"],
            ["project-context"],
            ["participant", "--role", "coordinator", "--native-id", "x", "--session", "x"],
            ["assign", "T01", "--role", "worker", "--native-id", "x", "--session", "x"],
            ["knowledge-assign", "--native-id", "x", "--session", "x"],
        ]
        for command in commands:
            for runtime in context.RUNTIMES:
                with self.subTest(command=command[0], runtime=runtime):
                    self.assertEqual(aispa.parser().parse_args([*command, "--runtime", runtime]).runtime, runtime)
            with self.subTest(command=command[0]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                aispa.parser().parse_args([*command, "--runtime", "unknown-agent"])
            self.assertEqual(error.exception.code, 2)

    def test_new_hooks_preserve_bytes_backup_and_refresh_idempotence(self):
        for runtime in ("openclaw", "gemini", "antigravity"):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = Journal(root)
                state = store.create("hooks", "Hooks fixture")
                originals = {"AGENTS.md": b"# Owner\r\nKeep this byte: \xff"}
                if runtime != "openclaw":
                    originals["GEMINI.md"] = b"# Gemini owner\r\nNo network.\r\n"
                for name, before in originals.items():
                    (root / name).write_bytes(before)
                result = context.initialize(store, state, runtime)
                store.append(state, "synthetic-adapter-initialized")
                context.refresh(store, state)
                for name, before in originals.items():
                    self.assertTrue((root / name).read_bytes().startswith(before))
                    self.assertEqual(next((root / ".aispa/backups").glob(name + ".*.bak")).read_bytes(), before)
                if runtime == "antigravity":
                    self.assertEqual((root / ".agents/rules/aispa.md").read_bytes(), context.ANTIGRAVITY_RULE)
                    self.assertIn(b"trigger: always_on", context.ANTIGRAVITY_RULE)
                self.assertEqual(result["instructionFiles"], HOOKS[runtime])
                snapshots = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns)
                             for p in root.rglob("*") if p.is_file()}
                self.assertEqual(context.initialize(store, state, runtime)["changedInstructionFiles"], [])
                context.refresh(store, state)
                self.assertEqual(snapshots, {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns)
                                            for p in root.rglob("*") if p.is_file()})
                self.assertFalse((root / ".openclaw").exists())
                self.assertFalse((root / ".gemini").exists())

    def test_antigravity_conflicts_leave_all_rules_and_state_unchanged(self):
        cases = ("foreign-rule", "modified-block", "parent-file", "context-directory")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = Journal(root)
                state = store.create("conflict", "Conflict fixture")
                originals = {"AGENTS.md": b"Owner agents\n", "GEMINI.md": b"Owner gemini\n"}
                for name, before in originals.items():
                    (root / name).write_bytes(before)
                if case == "foreign-rule":
                    path = root / ".agents/rules/aispa.md"
                    path.parent.mkdir(parents=True)
                    path.write_bytes(b"Foreign owner's rule")
                elif case == "modified-block":
                    (root / "GEMINI.md").write_bytes(context.POINTER.replace(b"source-map.json", b"modified-map.json"))
                elif case == "parent-file":
                    (root / ".agents").write_bytes(b"Owner file")
                else:
                    (root / ".aispa/context/INDEX.md").mkdir(parents=True)
                before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                with self.assertRaises(Invalid):
                    context.initialize(store, state, "antigravity")
                self.assertNotIn("projectContext", state)
                self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_new_hook_symlinks_are_refused_before_rule_edits(self):
        for runtime, name in (("gemini", "GEMINI.md"), ("antigravity", ".agents"),
                              ("antigravity", ".agents/rules/aispa.md")):
            with self.subTest(runtime=runtime, name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "project"
                root.mkdir()
                outside = Path(tmp) / "outside"
                is_directory = name == ".agents"
                if is_directory:
                    outside.mkdir()
                else:
                    outside.write_bytes(b"External instructions")
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(outside, target_is_directory=is_directory)
                store = Journal(root)
                state = store.create("links", "Links fixture")
                with self.assertRaises(Invalid):
                    context.initialize(store, state, runtime)
                self.assertFalse((root / "AGENTS.md").exists())
                if is_directory:
                    self.assertEqual(list(outside.iterdir()), [])
                else:
                    self.assertEqual(outside.read_bytes(), b"External instructions")

    def test_runtime_rule_and_configuration_paths_require_heavy(self):
        for name in ("GEMINI.md", "AGENTS.md", ".agents/rules/aispa.md", ".agents/skills/aispa/SKILL.md",
                     ".gemini/settings.json", ".gemini/skills/aispa/SKILL.md", ".gemini/config/skills/aispa/SKILL.md",
                     ".gemini/antigravity/brain/config.md", ".openclaw/config.json", ".openclaw/workspace/SOUL.md",
                     ".antigravity/config.md", "docs/antigravity/policy.md"):
            with self.subTest(name=name):
                self.assertFalse(task_tiers.safe_zone(name, "documentation"))
                self.assertFalse(task_tiers.standard_zone(name))
                self.assertTrue(task_tiers.sensitive_path(name))


class RuntimeTaskBindingTests(unittest.TestCase):
    setUp = test_team.TeamTests.setUp
    tearDown = test_team.TeamTests.tearDown
    task = test_team.TeamTests.task

    def test_every_runtime_binds_worker_and_reviewer_to_real_role_cards(self):
        # Synthetic host IDs validate the storage/routing contract, not authentication.
        for runtime in context.RUNTIMES:
            with self.subTest(runtime=runtime):
                for role in ("worker", "critic1"):
                    aid = self.team.assign("T01", role, runtime, runtime + "-" + role, "fixture-session")
                    actor = self.state["actors"][aid]
                    self.assertEqual(actor["runtime"], runtime)
                    self.assertEqual(actor["role"], role)
                    self.assertEqual(self.state["taskHistory"]["T01"][-1]["actors"][role], aid)
                    self.assertTrue((self.store.run / actor["record"]["file"]).is_file())


if __name__ == "__main__":
    unittest.main()
