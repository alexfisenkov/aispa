"""Filesystem assessment contracts; fixtures are not executed projects."""
import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "skills/aispa"
sys.path.insert(0, str(PACKAGE / "tools"))
import aispa
from aispa_core.store import Invalid


def tree_bytes(root):
    rows = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            metadata = path.lstat()
            payload = path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None
            rows[path.relative_to(root).as_posix()] = (stat.S_IFMT(metadata.st_mode), payload)
    return rows


@contextlib.contextmanager
def no_effects(forbidden=()):
    """Fail on a write, launch, or read of an explicitly excluded fixture."""
    ordinary_open, ordinary_io, ordinary_fd = open, io.open, os.open
    def check_read(file):
        if isinstance(file, (str, bytes, os.PathLike)):
            path = os.path.abspath(os.fsdecode(file))
            if any(path == str(p) or path.startswith(str(p) + os.sep) for p in forbidden):
                raise AssertionError("Excluded source was read: " + path)
    def checked_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            raise AssertionError("Assessment attempted a write")
        check_read(file)
        return ordinary_open(file, mode, *args, **kwargs)
    def checked_io(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            raise AssertionError("Assessment attempted an io write")
        check_read(file)
        return ordinary_io(file, mode, *args, **kwargs)
    def checked_fd(file, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise AssertionError("Assessment attempted an fd write")
        check_read(file)
        return ordinary_fd(file, flags, *args, **kwargs)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("builtins.open", side_effect=checked_open))
        stack.enter_context(patch("io.open", side_effect=checked_io))
        stack.enter_context(patch("os.open", side_effect=checked_fd))
        for name in ("mkdir", "makedirs", "remove", "unlink", "rename", "replace", "rmdir", "system"):
            stack.enter_context(patch.object(os, name, side_effect=AssertionError("Assessment attempted " + name)))
        for name in ("run", "Popen", "call", "check_call", "check_output"):
            stack.enter_context(patch.object(subprocess, name, side_effect=AssertionError("Assessment launched a command")))
        yield


class AssessmentTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("aispa_core.assessment"),
            "T05 requires an explicit bounded filesystem-only assessment capability")
        module = importlib.import_module("aispa_core.assessment")
        self.assertTrue(callable(getattr(module, "assess", None)), "Assessment must provide its real read-only collector")
        return module

    def test_plain_project_reports_source_bound_facts_commands_and_repair_without_effects(self):
        module = self.module()
        with tempfile.TemporaryDirectory(prefix="audit project 🙂 ") as tmp:
            root = Path(tmp)
            readme = b"# Existing bot\nUse the existing service architecture.\n"
            (root / "README.md").write_bytes(readme)
            (root / "package.json").write_text(json.dumps({"scripts": {"test": "node test.js", "danger": "touch NEVER"}}))
            (root / "main.py").write_text("def broken(:\n", encoding="utf-8")
            before = tree_bytes(root)
            with no_effects():
                report = module.assess(root)
            self.assertEqual(tree_bytes(root), before)
            self.assertEqual(report["mode"], "audit-only")
            facts = {row["path"]: row for row in report["facts"]}
            self.assertEqual(facts["README.md"]["sha256"], hashlib.sha256(readme).hexdigest())
            self.assertTrue(any(row["command"] == "node test.js" and row["executed"] is False for row in report["commands"]))
            self.assertEqual(report["executedCommands"], [])
            self.assertTrue(report["unknowns"], "Static filesystem inspection must retain runtime unknowns")
            self.assertTrue(any(row["path"] == "main.py" for row in report["suspicions"]))
            self.assertTrue(any(row["path"] == "main.py" for row in report["proposedRepairs"]))
            self.assertFalse(report["repair"]["authorized"])
            self.assertTrue(report["repair"]["requiresOwnerRepairRequest"])
            self.assertNotIn("verified_completion", json.dumps(report))

    def test_empty_root_and_compact_cli_do_not_initialize_lock_save_or_check_updates(self):
        self.module()
        for compact in (False, True):
            with self.subTest(compact=compact), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                argv = ["--root", str(root), *(["--compact"] if compact else []), "assess"]
                with contextlib.redirect_stderr(io.StringIO()):
                    try:
                        args = aispa.parser().parse_args(argv)
                    except SystemExit:
                        self.fail("T05 requires an assess CLI route before Journal/init/update/dashboard")
                output = io.StringIO()
                with patch.object(aispa, "Journal", side_effect=AssertionError("Audit reached Journal")), \
                     patch.object(aispa, "emit", side_effect=AssertionError("Audit persisted compact output")), \
                     patch("aispa_core.updates.check", side_effect=AssertionError("Audit performed update-check")), \
                     patch("aispa_core.dashboard_server.start", side_effect=AssertionError("Audit started dashboard")), \
                     no_effects(), contextlib.redirect_stdout(output):
                    self.assertEqual(aispa.execute(args), 0)
                report = json.loads(output.getvalue())
                self.assertEqual(report["mode"], "audit-only")
                self.assertEqual(tree_bytes(root), {})

    def test_private_hidden_link_and_special_files_are_never_read(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = Path(tmp) / "project", Path(tmp) / "outside"
            root.mkdir(); outside.mkdir()
            (root / "README.md").write_text("# Safe\n")
            denied = []
            for name in (".aispa", ".git", "private", "data", "vendor", "node_modules", "generated", "media"):
                directory = root / name
                directory.mkdir()
                (directory / "README.md").write_text("SECRET_SENTINEL")
                denied.append(directory)
            secret = root / ".env"
            secret.write_text("SECRET_SENTINEL")
            denied.append(secret)
            (outside / "README.md").write_text("SECRET_SENTINEL")
            denied.append(outside)
            try:
                (root / "linked").symlink_to(outside, target_is_directory=True)
            except OSError:
                pass  # The real Windows junction/link checks remain separate where supported.
            if hasattr(os, "mkfifo"):
                os.mkfifo(root / "pipe")
            before = tree_bytes(root)
            with no_effects(denied):
                report = module.assess(root)
            self.assertEqual(tree_bytes(root), before)
            self.assertNotIn("SECRET_SENTINEL", json.dumps(report))
            self.assertEqual({row["path"] for row in report["facts"]}, {"README.md"})
            self.assertTrue(report["omissions"])
            self.assertFalse(report["complete"], "Excluded inputs are an explicit limit, not evidence of no errors")

    def test_budgets_refuse_invalid_values_and_report_partial_traversal(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number in range(12):
                directory = root / str(number)
                directory.mkdir()
                (directory / "README.md").write_text("large " * 100)
            for option, value in (("max_entries", 0), ("max_depth", -1), ("max_file_bytes", True),
                                  ("max_total_bytes", 0), ("max_excerpt_chars", -2),
                                  ("timeout", float("nan")), ("timeout", float("inf"))):
                with self.subTest(option=option, value=value), self.assertRaises(Invalid), no_effects():
                    module.assess(root, **{option: value})
            for options in ({"max_entries": 2}, {"max_depth": 0}, {"max_file_bytes": 20},
                            {"max_total_bytes": 20}, {"max_excerpt_chars": 8}):
                with self.subTest(options=options), no_effects():
                    report = module.assess(root, **options)
                self.assertFalse(report["complete"])
                self.assertTrue(report["omissions"])
            with patch.object(module.time, "monotonic", side_effect=[0.0] + [2.0] * 100), no_effects():
                timed = module.assess(root, timeout=1)
            self.assertFalse(timed["complete"])
            self.assertTrue(any("deadline" in row["reason"].lower() for row in timed["omissions"]))

    def test_permission_and_invalid_utf8_are_explicit_limits_not_empty_pass(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_bytes(b"# unreadable")
            (root / "main.py").write_bytes(b"\xff\xfe\x00")
            original = io.open
            def denied(path, *args, **kwargs):
                if str(path).endswith("README.md"):
                    raise PermissionError("fixture denial")
                return original(path, *args, **kwargs)
            with patch("io.open", side_effect=denied):
                report = module.assess(root)
            self.assertFalse(report["complete"])
            self.assertTrue(report["omissions"])
            self.assertTrue(report["unknowns"])

    def test_inline_secret_canary_is_omitted_from_excerpts_commands_and_findings(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canary = "sk-" + "SYNTHETIC_AUDIT_CANARY_1234567890"
            source = ("API_KEY = " + repr(canary) + "\n").encode("utf-8")
            (root / "main.py").write_bytes(source)
            manifest = json.dumps({"scripts": {"test": "API_KEY=" + canary + " node test.js"}}).encode()
            (root / "package.json").write_bytes(manifest)
            with no_effects():
                report = module.assess(root)
            facts = {row["path"]: row for row in report["facts"]}
            self.assertEqual(facts["main.py"]["sha256"], hashlib.sha256(source).hexdigest())
            self.assertEqual(facts["package.json"]["sha256"], hashlib.sha256(manifest).hexdigest())
            self.assertNotIn(canary, json.dumps(report, ensure_ascii=False))
            self.assertFalse(report["complete"])
            self.assertTrue(any("secret" in row["reason"].lower() for row in report["omissions"]))
            self.assertEqual(report["commands"], [])


class AssessmentEntryTests(unittest.TestCase):
    def test_real_cli_on_non_git_unicode_root_is_zero_write_without_python_B(self):
        with tempfile.TemporaryDirectory(prefix="audit CLI 🙂 ") as tmp:
            workspace = Path(tmp)
            root = workspace / "existing project"
            root.mkdir()
            package = workspace / "installed aispa"
            shutil.copytree(PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (root / "README.md").write_text("# Existing project\n")
            before = tree_bytes(workspace)
            environment = dict(os.environ)
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            result = subprocess.run([sys.executable, str(package / "tools/aispa.py"),
                "--root", str(root), "assess", "--max-entries", "20", "--max-depth", "2"],
                capture_output=True, text=True, encoding="utf-8", timeout=15,
                env=environment)
            self.assertEqual(result.returncode, 0, "Audit-only CLI is unavailable: " + result.stderr)
            self.assertEqual(json.loads(result.stdout)["mode"], "audit-only")
            self.assertEqual(tree_bytes(workspace), before)

    def test_all_host_instructions_exempt_explicit_audit_before_mutating_start(self):
        for name in ("SKILL.md", "phases/01-start.md", "references/native-agents.md", "references/runtimes.md", "agents/openai.yaml"):
            with self.subTest(name=name):
                text = (PACKAGE / name).read_text(encoding="utf-8")
                self.assertIn("audit-only", text, "Every real host entrypoint needs the explicit no-init audit route")
                self.assertIn("assess", text)
        self.assertIn("allow_implicit_invocation: false", (PACKAGE / "agents/openai.yaml").read_text())
