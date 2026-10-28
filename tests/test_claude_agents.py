"""Agent files, openai.yaml and the model table in SKILL.md stay consistent (AISpa 1.0)."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "skills/aispa"
AGENTS = PACKAGE / "agents/claude-code"
SKILL = (PACKAGE / "SKILL.md").read_text(encoding="utf-8")

NAMES = ["aispa-judge", "aispa-sensitive-worker", "aispa-worker",
         "aispa-mechanic-scripted", "aispa-mechanic", "aispa-researcher"]
FIRST_LINE = "Если в промпте есть путь к пакету роли — прочитай его целиком первым."
FORBIDDEN = re.compile(r"\b(fable|astra|inherit|xhigh|max|ultra)\b", re.I)
SECTION_TITLE = "## Subagent models"


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
        fields[key.strip()] = value.strip()
    return fields, text[match.end():]


def model_section():
    start = SKILL.index(SECTION_TITLE)
    end = SKILL.index("\n## ", start + 1)
    return SKILL[start:end]


def table_routes():
    """agent name -> (model, effort) read from the table in SKILL.md."""
    routes = {}
    for row in model_section().splitlines():
        if not row.startswith("|"):
            continue
        claude_cell = row.split("|")[2]
        found = re.search(r"(Opus|Sonnet|Haiku) · (low|medium|high)", claude_cell)
        if found:
            for name in re.findall(r"`(aispa-[a-z-]+)`", claude_cell):
                routes[name] = (found.group(1).lower(), found.group(2))
    return routes


class AgentFilesTests(unittest.TestCase):
    def test_package_ships_exactly_the_six_agents(self):
        self.assertEqual(sorted(p.name for p in AGENTS.iterdir()), sorted(n + ".md" for n in NAMES))
        for name in NAMES:
            fields, _body = frontmatter(AGENTS / (name + ".md"))
            self.assertEqual(fields["name"], name)

    def test_model_and_effort_are_allowed_and_match_the_table(self):
        routes = table_routes()
        self.assertEqual(sorted(routes), sorted(set(NAMES) - {"aispa-researcher"}))
        for name in NAMES:
            with self.subTest(agent=name):
                fields, _body = frontmatter(AGENTS / (name + ".md"))
                self.assertIn(fields["model"], {"opus", "sonnet", "haiku"})
                self.assertIn(fields["effort"], {"low", "medium", "high"})
                if name in routes:
                    self.assertEqual((fields["model"], fields["effort"]), routes[name])

    def test_table_values_chosen_by_the_owner(self):
        self.assertEqual(table_routes(), {
            "aispa-judge": ("opus", "high"), "aispa-sensitive-worker": ("opus", "high"),
            "aispa-worker": ("sonnet", "medium"), "aispa-mechanic-scripted": ("haiku", "low"),
            "aispa-mechanic": ("sonnet", "low")})

    def test_body_starts_with_the_role_package_line(self):
        for name in NAMES:
            with self.subTest(agent=name):
                _fields, body = frontmatter(AGENTS / (name + ".md"))
                self.assertEqual(body.strip().splitlines()[0], FIRST_LINE)


class ForbiddenWordsTests(unittest.TestCase):
    def hits(self, path):
        text = path.read_text(encoding="utf-8").replace("max(", "(")
        return [m.group(0) for m in FORBIDDEN.finditer(text)]

    def test_agents_yaml_phases_and_prompts_have_none(self):
        paths = list(AGENTS.iterdir()) + [PACKAGE / "agents/openai.yaml"]
        paths += sorted((PACKAGE / "phases").glob("*.md"))
        paths += sorted((PACKAGE / "prompts").glob("*.md"))
        for path in paths:
            with self.subTest(file=path.name):
                self.assertTrue(path.is_file())
                self.assertEqual(self.hits(path), [])

    def test_skill_md_has_them_only_in_the_model_rules(self):
        section = model_section()
        outside = SKILL.replace(section, "")
        self.assertEqual(FORBIDDEN.findall(outside), [])
        rule_lines = [l for l in section.splitlines() if l.startswith("- ")]
        listed = [l for l in section.splitlines() if FORBIDDEN.search(l)]
        self.assertTrue(listed)
        for line in listed:
            self.assertIn(line, rule_lines)

    def test_phase_files_no_longer_inherit_the_chat_model(self):
        text = (PACKAGE / "phases/5-subagents.md").read_text(encoding="utf-8")
        self.assertNotIn("session's model", text)
        self.assertNotIn("ignores this silently", text)
        self.assertIn("SKILL.md", text)


class OpenaiYamlTests(unittest.TestCase):
    def setUp(self):
        self.text = (PACKAGE / "agents/openai.yaml").read_text(encoding="utf-8")

    def test_implicit_invocation_stays_off(self):
        self.assertIn("allow_implicit_invocation: false", self.text)

    def test_default_prompt_follows_the_new_route(self):
        self.assertIn("$aispa", self.text)
        self.assertIn("SKILL.md", self.text)
        for old in ("assess", "seal", "update-check", "dashboard start"):
            self.assertNotIn(old, self.text)


if __name__ == "__main__":
    unittest.main()
