"""Public handoff requires real tracked docs and saved command execution."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import executions, handoff
from aispa_core.store import Invalid, Journal
from aispa_core.workflow import Workflow

RUNNER = "python" if os.name == "nt" else "python3"


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-handoff-🙂-")
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.write(".gitignore", ".aispa/\n")
        self.write("app.py", "print('fixture')\n")
        self.readme = f"""# Fixture

## Quickstart
Run this check from the project root: `{RUNNER} app.py`.
Standard library only; no install step.
This fixture exists only for a check.
No separate test command in the fixture.

## Architecture
The one-file program has a single entry point, app.py, and writes one line.

## Decisions
Choice: keep one file. Alternatives: add a package. Consequences: setup stays simple.

## Glossary
Fixture means the example command-line program used in this test.
"""
        self.write("README.md", self.readme)
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")
        self.store = Journal(self.root)
        self.state = self.store.create("handoff", "Handoff fixture")
        workflow = Workflow(self.store, self.state)
        workflow.verify([sys.executable, "app.py"], 10)
        self.ref = executions.record(self.store, self.state, kind="project", purpose="check")
        self.store.append(self.state, "execution-recorded")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True).stdout

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def source(self, name):
        return {"path": name, "sha256": hashlib.sha256((self.root / name).read_bytes()).hexdigest()}

    def report(self):
        return {"schema": 1, "profile": "small", "documents": [
            {**self.source("README.md"), "sources": [self.source("app.py")],
             "covers": {"quickstart": "Quickstart", "architecture": "Architecture",
                        "decisions": "Decisions", "glossary": "Glossary"}}],
            "notApplicable": {}, "decisionDetails": {
                "choice": "keep one file", "alternatives": "add a package",
                "consequences": "setup stays simple"},
            "commands": {"install": {"notApplicable": "Standard library only; no install step."},
                         "run": {"notApplicable": "This fixture exists only for a check."},
                         "check": {"receipt": self.ref, "document": "README.md", "heading": "Quickstart",
                                   "display": f"{RUNNER} app.py", "runner": RUNNER},
                         "singleTest": {"notApplicable": "No separate test command in the fixture."}},
            "obligations": []}

    def resolve(self, ref):
        return executions.resolve(self.store, self.state, ref)

    def record(self, report=None):
        result = handoff.record(self.store, self.state, report or self.report(),
                                command_receipt=self.resolve)
        self.store.append(self.state, "handoff-recorded")
        return result

    def test_one_readme_covers_small_project_and_stays_current_after_finish(self):
        before = (self.root / "README.md").read_bytes()
        result = self.record()
        self.assertEqual((self.root / "README.md").read_bytes(), before)
        self.assertTrue(result["artifact"]["file"].startswith("handoff/"))
        self.assertTrue(handoff.valid(self.store, self.state, command_receipt=self.resolve))
        self.state["status"] = "accepted"
        self.store.append(self.state, "test-finished")
        self.assertTrue(handoff.valid(self.store, self.state, command_receipt=self.resolve))

    def test_small_profile_can_justify_inapplicable_sections(self):
        original = (self.root / "README.md").read_text(encoding="utf-8")
        self.write("README.md", original + "\n## Scope\n"
                   "Single short script; entry point is documented in Quickstart.\n"
                   "No design fork was taken for this fixture.\n"
                   "No domain-specific vocabulary beyond Python appears here.\n")
        self.git("add", "README.md")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "small profile")
        Workflow(self.store, self.state).verify([sys.executable, "app.py"], 10)
        self.ref = executions.record(self.store, self.state, kind="project", purpose="check")
        self.store.append(self.state, "execution-recorded")
        report = self.report()
        report["documents"][0].update(self.source("README.md"))
        report["documents"][0]["covers"] = {"quickstart": "Quickstart"}
        report["notApplicable"] = {
            "architecture": "Single short script; entry point is documented in Quickstart.",
            "decisions": "No design fork was taken for this fixture.",
            "glossary": "No domain-specific vocabulary beyond Python appears here."}
        report.pop("decisionDetails")
        self.record(report)
        self.assertTrue(handoff.valid(self.store, self.state, command_receipt=self.resolve))
        report["profile"] = "standard"
        with self.assertRaises(Invalid):
            self.record(report)

    def test_missing_or_fake_command_evidence_is_rejected(self):
        report = self.report()
        report["commands"]["check"] = {"notApplicable": "No test needed."}
        with self.assertRaises(Invalid):
            self.record(report)
        report = self.report()
        report["commands"]["check"]["receipt"] = {"sha256": "0" * 64}
        with self.assertRaises(Invalid):
            self.record(report)
        report = self.report()
        report["commands"]["check"]["display"] = f"{RUNNER} other.py"
        with self.assertRaises(Invalid):
            self.record(report)

    def test_public_docs_reject_common_home_path_spellings(self):
        for private_path in ("/Users/alex/work", "/home/alex/work", "C:/Users/Alex/work",
                             "C:\\Users\\Alex\\work"):
            with self.subTest(private_path=private_path):
                text = (self.root / "README.md").read_text(encoding="utf-8")
                self.write("README.md", text + "\n" + private_path + "\n")
                report = self.report()
                report["documents"][0].update(self.source("README.md"))
                with self.assertRaises(Invalid):
                    self.record(report)
                self.write("README.md", text)

    def test_optional_evidence_mapping_is_hash_bound_and_never_only_source(self):
        report = self.report()
        output = self.state["verification"]["output"]
        report["documents"][0]["sources"].append({"kind": "evidence", "artifact": output})
        self.record(report)
        self.assertTrue(handoff.valid(self.store, self.state, command_receipt=self.resolve))
        changed = self.report()
        changed["documents"][0]["sources"] = [{"kind": "evidence", "artifact": output}]
        with self.assertRaises(Invalid):
            self.record(changed)

    def test_doc_and_code_edits_invalidate_saved_handoff(self):
        self.record()
        self.write("README.md", (self.root / "README.md").read_text() + "\nChanged.\n")
        self.assertFalse(handoff.valid(self.store, self.state, command_receipt=self.resolve))
        self.write("README.md", self.readme)
        self.write("app.py", "print('changed')\n")
        self.assertFalse(handoff.valid(self.store, self.state, command_receipt=self.resolve))

    def test_untracked_private_or_secret_docs_rejected(self):
        report = self.report()
        self.write("docs/new.md", f"# Quickstart\nUse `{RUNNER} app.py` to check this script.\n")
        report["documents"][0].update(self.source("docs/new.md"))
        with self.assertRaises(Invalid):
            self.record(report)
        report = self.report()
        synthetic_credential = "".join(("secretFixture", "123456789"))
        self.write("README.md", f"# Quickstart\naccess_token = '{synthetic_credential}'\n")
        report["documents"][0].update(self.source("README.md"))
        with self.assertRaises(Invalid):
            self.record(report)
        self.write("README.md", f"# Quickstart\nUse `{RUNNER} app.py` to check this script.\n")
        report = self.report()
        report["documents"][0]["path"] = ".aispa/private.md"
        with self.assertRaises(Invalid):
            self.record(report)

    def test_blocked_task_needs_explicit_documented_obligation(self):
        self.state["tasks"] = [{"id": "T01", "status": "blocked"}]
        with self.assertRaises(Invalid):
            self.record()
        self.write("README.md", (self.root / "README.md").read_text()
                   + "\n## Remaining work\nT01 cannot proceed because integration is unavailable.\n")
        report = self.report()
        report["documents"][0].update(self.source("README.md"))
        report["documents"][0]["covers"]["architecture"] = "Architecture"
        report["obligations"] = [{"status": "blocked", "text": "T01 cannot proceed because integration is unavailable.",
                                  "document": "README.md", "heading": "Remaining work", "task": "T01"}]
        # The docs changed after the check, so the command is stale until rerun.
        with self.assertRaises(Invalid):
            self.record(report)
        self.git("add", "README.md")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "document blocker")
        Workflow(self.store, self.state).verify([sys.executable, "app.py"], 10)
        self.ref = executions.record(self.store, self.state, kind="project", purpose="check")
        self.store.append(self.state, "execution-recorded")
        report["commands"]["check"]["receipt"] = self.ref
        self.record(report)
        self.assertTrue(handoff.valid(self.store, self.state, command_receipt=self.resolve))

    def test_symlinked_document_is_rejected(self):
        original = self.root / "README.md"
        target = self.root / "real.md"
        original.rename(target)
        original.symlink_to(target.name)
        with self.assertRaises(Invalid):
            self.record()

    def test_malformed_path_and_heading_fields_raise_invalid(self):
        mutations = (
            lambda r: r["documents"][0].update(path=[]),
            lambda r: r["documents"][0]["sources"][0].update(path=[]),
            lambda r: r["documents"][0]["covers"].update(quickstart=[]),
            lambda r: r["commands"]["check"].update(document=[]),
            lambda r: r["commands"]["check"].update(heading=[]),
            lambda r: r.update(obligations=[{"status": "open", "text": "Needs review.",
                                                 "document": [], "heading": "Quickstart", "task": None}]),
            lambda r: r.update(obligations=[{"status": "open", "text": "Needs review.",
                                                 "document": "README.md", "heading": [], "task": None}]),
            lambda r: r.update(obligations=[{"status": "blocked", "text": "Needs review.",
                                                 "document": "README.md", "heading": "Quickstart", "task": []}]),
        )
        for mutate in mutations:
            report = self.report()
            mutate(report)
            with self.subTest(mutate=mutate), self.assertRaises(Invalid):
                self.record(report)
        report = self.report()
        report["unknown"] = "unsupported"
        with self.assertRaises(Invalid):
            self.record(report)
        report = self.report()
        report["schema"] = True
        with self.assertRaises(Invalid):
            self.record(report)
        report = self.report()
        report["commands"]["check"]["receipt"] = []
        with self.assertRaises(Invalid):
            self.record(report)
        report = self.report()
        report["commands"]["check"]["receipt"] = {"sha256": []}
        with self.assertRaises(Invalid):
            self.record(report)

    def test_powershell_literals_and_posix_argv_keep_boundaries(self):
        argv = [r"C:\Program Files\Tool\tool.exe", "a'b", "$env:HOME", "&", ";",
                "`", "two words", "🙂"]
        self.assertEqual(handoff._ps_join(argv),
                         "& 'C:\\Program Files\\Tool\\tool.exe' 'a''b' '$env:HOME' '&' ';' "
                         "'`' 'two words' '🙂'")
        self.assertEqual(handoff._ps_join(["py", "-3", "--flag=value"]),
                         "py -3 --flag=value")
        for unsupported in ("", 'a"b', "line 1\nline 2", "line 1\rline 2"):
            with self.subTest(unsupported=unsupported), self.assertRaises(Invalid):
                handoff._ps_join(["git", unsupported])
        with patch.object(handoff, "IS_WINDOWS", False):
            self.assertEqual(handoff._display(["git", "", "two words", "a'b"], None),
                             "git '' 'two words' 'a'\"'\"'b'")
        with patch.object(handoff, "IS_WINDOWS", True):
            with self.assertRaises(Invalid):
                handoff._display(["git", "", "two words", "a'b"], None)
            self.assertEqual(handoff._display(["git", "two words", "a'b"], None),
                             "git 'two words' 'a''b'")

    def test_alias_must_resolve_to_current_python_and_absolute_exe_is_rejected(self):
        self.assertEqual(handoff._display([sys.executable, "app.py"], RUNNER), f"{RUNNER} app.py")
        self.assertEqual(handoff._display([RUNNER, "app.py"], RUNNER), f"{RUNNER} app.py")
        with patch.object(handoff.shutil, "which", return_value=None):
            with self.assertRaises(Invalid):
                handoff._display([sys.executable, "app.py"], RUNNER)
        with patch.object(handoff.shutil, "which", return_value=str(self.root / "other-python")):
            with self.assertRaises(Invalid):
                handoff._display([sys.executable, "app.py"], RUNNER)
        for argv in (["/opt/homebrew/bin/python3", "app.py"], ["/usr/bin/git", "status"],
                     [r"C:\Tools\git.exe", "status"], ["../git", "status"]):
            with self.subTest(argv=argv), self.assertRaises(Invalid):
                handoff._display(argv, None)

    def test_py_launcher_requires_real_matching_bounded_probe(self):
        with patch.object(handoff, "IS_WINDOWS", True), \
             patch.object(handoff.shutil, "which", return_value="py"), \
             patch.object(handoff.subprocess, "run", return_value=subprocess.CompletedProcess(
                 ["py"], 0, stdout=json.dumps(sys.executable, ensure_ascii=True) + "\n", stderr="")):
            self.assertTrue(handoff._python_runner("py -3"))
            self.assertEqual(handoff._display([sys.executable, "app.py"], "py -3"), "py -3 app.py")
        with patch.object(handoff, "IS_WINDOWS", True), \
             patch.object(handoff.shutil, "which", return_value="py"), \
             patch.object(handoff.subprocess, "run", return_value=subprocess.CompletedProcess(
                 ["py"], 0, stdout=json.dumps(str(self.root / "other-python"), ensure_ascii=True) + "\n", stderr="")):
            self.assertFalse(handoff._python_runner("py -3"))

    @unittest.skipUnless(os.name == "nt", "Native PowerShell argv check is Windows-only")
    def test_native_powershell_passes_literal_argv(self):
        self.write("echo_args.py", "import json,sys\nprint(json.dumps(sys.argv[1:], ensure_ascii=True))\n")
        args = ["two words", "🙂", "a'b", "$env:HOME", "&", ";", "`", r"one\backslash"]
        display = handoff._display([sys.executable, "echo_args.py", *args], RUNNER)
        shells = ["powershell.exe"] + (["pwsh.exe"] if shutil.which("pwsh.exe") else [])
        for shell in shells:
            self.assertIsNotNone(shutil.which(shell), shell + " must be installed for native Windows verification")
            result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", display],
                                    cwd=self.root, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=20, check=False)
            with self.subTest(shell=shell):
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1].lstrip("\ufeff")), args)


if __name__ == "__main__":
    unittest.main()
