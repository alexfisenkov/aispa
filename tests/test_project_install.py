"""A skill installed inside the learner's project must not block the clean-clone finale.

The package copied into <project>/.claude/skills/aispa (and its Claude Code agent
files) is not the learner's code. When Git does not track it, init ignores it
next to .aispa/, so the finale's clean clone sees no untracked files. A tracked
install and an install outside the project leave .gitignore alone.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_cli  # module import only: its test classes are not collected here

PACKAGE = test_cli.PACKAGE
AGENT_FILES = sorted((PACKAGE / "agents/claude-code").glob("aispa-*.md"))
INIT = ("init", "--slug", "example", "--title", "Пример", "--mode", "auto", "--runtime", "claude-code")


class ProjectInstallTests(test_cli.CliTests):
    """The CLI fixture of test_cli, with the package installed into the project itself."""

    def setUp(self):
        super().setUp()
        self.installed = self.root / ".claude/skills/aispa"
        shutil.copytree(PACKAGE, self.installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        agents = self.root / ".claude/agents"
        agents.mkdir()
        for path in AGENT_FILES:
            shutil.copyfile(path, agents / path.name)
        self.project_cli = self.installed / "tools/aispa.py"

    def commit(self, *paths, message="fixture"):
        self.git("add", *paths)
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", message)

    def status(self):
        return subprocess.run(["git", "-C", str(self.root), "status", "--porcelain", "--untracked-files=all"],
                              check=True, capture_output=True, text=True, encoding="utf-8").stdout

    def ignore_lines(self):
        return (self.root / ".gitignore").read_text(encoding="utf-8").splitlines()

    def init(self, cli=None):
        """CliTests.init through the project's own copy of the skill."""
        return test_cli.CliTests.init(self, cli or self.project_cli)

    def test_untracked_install_is_ignored_and_the_clean_clone_finale_passes(self):
        self.assertIn("?? .claude/", self.status())
        self.deliver_one_task()  # init from .claude/skills/aispa, then one closed final task
        lines = self.ignore_lines()
        self.assertIn(".aispa/", lines)
        self.assertIn(".claude/skills/aispa/", lines)
        self.assertIn(".claude/agents/aispa-*.md", lines)
        self.assertNotIn(".claude/", self.status())
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        self.branch_review()
        self.call("verify", "--", sys.executable, "tests/check_output.py")
        self.handoff()
        receipt = self.call("clean-prepare")["receipt"]
        self.assertIsNot(receipt.get("passed"), False, receipt)

    def test_init_answer_names_the_ignored_install_in_plain_words(self):
        (self.root / ".gitignore").write_text("node_modules/\n# мои правила\n", encoding="utf-8", newline="\n")
        self.commit(".gitignore")
        # The default (compact) answer keeps the note too.
        result = subprocess.run([sys.executable, "-B", str(self.project_cli), "--root", str(self.root), *INIT],
                                capture_output=True, text=True, encoding="utf-8", env=self.command_env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        answer = json.loads(result.stdout)
        self.assertTrue(answer.get("compact"), answer)
        note = answer["projectIgnore"]
        self.assertEqual(note["added"], [".claude/skills/aispa/", ".claude/agents/aispa-*.md"])
        self.assertIn(".gitignore", note["message"])
        self.assertIn("чистого клона", note["message"])
        self.cli = self.root / ".aispa" / answer["runDirectory"] / "runtime/tools/aispa.py"
        self.call("close", "cancelled", "--note", "Synthetic owner stopped this fixture")
        again = self.call(*INIT, cli=self.project_cli)
        self.assertNotIn("projectIgnore", again, "Нечего добавлять — нечего и сообщать")
        lines = self.ignore_lines()
        self.assertEqual(lines[:2], ["node_modules/", "# мои правила"], "Чужие строки .gitignore не трогаются")
        self.assertEqual(lines.count(".claude/skills/aispa/"), 1, "Второй init не дублирует строку")
        self.assertEqual(lines.count(".claude/agents/aispa-*.md"), 1)

    def test_tracked_install_leaves_gitignore_without_new_lines(self):
        self.commit(".claude", message="skill committed with the project")
        self.init()
        self.assertEqual(self.ignore_lines(), [".aispa/"])
        self.assertNotIn(".claude/", self.status())

    def test_install_outside_the_project_leaves_gitignore_alone(self):
        shutil.rmtree(self.root / ".claude")
        self.init(test_cli.CLI)
        self.assertEqual(self.ignore_lines(), [".aispa/"])


# Inherited scenarios of test_cli run in test_cli itself, not a second time here.
for _name in [name for name in dir(test_cli.CliTests) if name.startswith("test_")]:
    if _name not in vars(ProjectInstallTests):
        setattr(ProjectInstallTests, _name, None)


if __name__ == "__main__":
    unittest.main()
