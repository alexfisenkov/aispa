"""Claude Code agent files shipped with the package match the model route table."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "skills/aispa"
sys.path.insert(0, str(PACKAGE / "tools"))
from aispa_core import model_routes

AGENTS = PACKAGE / "agents/claude-code"
EXPECTED_FILES = {
    "aispa-judge.md", "aispa-sensitive-worker.md", "aispa-worker.md",
    "aispa-mechanic-scripted.md", "aispa-mechanic.md", "aispa-researcher.md",
}


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if match is None:
        raise AssertionError("no frontmatter: " + path.name)
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            raise AssertionError("bad frontmatter line in %s: %r" % (path.name, line))
        if key.strip() in fields:
            raise AssertionError("duplicate key %s in %s" % (key, path.name))
        fields[key.strip()] = value.strip()
    return fields, text[match.end():]


class ClaudeAgentFilesTests(unittest.TestCase):
    def test_package_ships_exactly_the_six_route_agents(self):
        self.assertTrue(AGENTS.is_dir())
        self.assertEqual({p.name for p in AGENTS.iterdir()}, EXPECTED_FILES)
        self.assertEqual({name + ".md" for name in model_routes.CLAUDE_AGENT_FILES.values()}, EXPECTED_FILES)

    def test_model_and_effort_match_claude_code_route_table(self):
        for cls, name in model_routes.CLAUDE_AGENT_FILES.items():
            with self.subTest(cls=cls):
                fields, body = frontmatter(AGENTS / (name + ".md"))
                self.assertEqual(fields["name"], name)
                self.assertRegex(fields["description"], r'\A"[^"]+"\Z')
                self.assertIn(fields["model"], model_routes.CLAUDE_ALIASES)
                route = model_routes.ROUTES[cls]["claude-code"]
                self.assertEqual(model_routes.CLAUDE_ALIASES[fields["model"]], route["model"])
                self.assertEqual(fields["effort"], route["effort"])
                self.assertTrue(body.strip())

    def test_owner_approved_aliases_per_class(self):
        # Spec story 42: Opus for judge, sensitive worker and research; Sonnet for
        # ordinary and unscripted mechanical work; Haiku only for scripted mechanics.
        expected = {"judge": ("opus", "high"), "sensitive_worker": ("opus", "high"),
                    "worker": ("sonnet", "medium"), "mechanical_scripted": ("haiku", "low"),
                    "mechanical": ("sonnet", "low"), "research": ("opus", "medium")}
        for cls, (model, effort) in expected.items():
            with self.subTest(cls=cls):
                fields, _body = frontmatter(AGENTS / (model_routes.CLAUDE_AGENT_FILES[cls] + ".md"))
                self.assertEqual((fields["model"], fields["effort"]), (model, effort))

    def test_no_forbidden_model_or_effort_words(self):
        for path in AGENTS.iterdir():
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8").casefold()
                for word in ("inherit", "fable", "astra", "xhigh", "ultra"):
                    self.assertNotIn(word, text)


class AgentsCommandTests(unittest.TestCase):
    """A 0.5.0 skill updated by the 0.4.0 apply has no agents; the CLI command adds them."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-agents-cli-")
        project = Path(self.temp.name) / "проект"
        self.skill = project / ".claude" / "skills" / "aispa"
        shutil.copytree(PACKAGE, self.skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.agents = project / ".claude" / "agents"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(self.skill / "tools/aispa.py"), *args],
                              capture_output=True, text=True, timeout=60)

    def test_update_agents_needs_confirmation_and_keeps_an_edited_file(self):
        refused = self.run_cli("update-agents")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("подтверждение", refused.stderr)
        self.assertFalse(self.agents.exists())
        self.agents.mkdir()
        edited = b"---\nname: aispa-worker\nmodel: opus\n---\nmy own worker\n"
        (self.agents / "aispa-worker.md").write_bytes(edited)
        done = self.run_cli("update-agents", "--confirm")
        self.assertEqual(done.returncode, 0, done.stderr)
        receipt = json.loads(done.stdout)
        self.assertEqual(receipt["status"], "conflict")
        self.assertEqual(receipt["installed"], sorted(EXPECTED_FILES - {"aispa-worker.md"}))
        self.assertEqual(receipt["skipped"], [{"file": "aispa-worker.md", "reason": "different"}])
        self.assertEqual({p.name for p in self.agents.iterdir()}, EXPECTED_FILES)
        self.assertEqual((self.agents / "aispa-worker.md").read_bytes(), edited)
        for name in EXPECTED_FILES - {"aispa-worker.md"}:
            self.assertEqual((self.agents / name).read_bytes(), (AGENTS / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
