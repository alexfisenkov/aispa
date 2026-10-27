"""The one route table: decision 9 of the 0.5.0 specification, and its reference."""
from pathlib import Path
import re
import sys
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / "skills/aispa"
sys.path.insert(0, str(PACKAGE / "tools"))

from aispa_core import model_routes, participants  # noqa: E402

REFERENCE = PACKAGE / "references/model-routing.md"

# Copied by hand from the owner-approved table (spec, decision 9); never from the code under test.
APPROVED = {
    "judge": {"claude-code": ("claude-opus-5-5", "high"), "codex": ("gpt-6.1-sol", "high")},
    "sensitive_worker": {"claude-code": ("claude-opus-5-5", "high"), "codex": ("gpt-6.1-sol", "high")},
    "worker": {"claude-code": ("claude-sonnet-5-5", "medium"), "codex": ("gpt-6.1-terra", "medium")},
    "mechanical_scripted": {"claude-code": ("claude-haiku-5-5", "low"), "codex": ("gpt-6-luna", "low")},
    "mechanical": {"claude-code": ("claude-sonnet-5-5", "low"), "codex": ("gpt-6.1-terra", "low")},
    "research": {"claude-code": ("claude-opus-5-5", "medium"), "codex": ("gpt-6.1-terra", "high")},
}
JUDGE_ROLES = {"critic1", "critic2", "acceptor", "blind", "integration-reviewer", "branch-reviewer",
               "brief-analyst", "brief-reviewer", "coverage", "idea", "planner", "recovery-reviewer"}
RESEARCH_ROLES = {"discovery", "source-reader", "knowledge-writer"}
WORKER_CLASSES = {"sensitive_worker", "worker", "mechanical_scripted", "mechanical"}


def state(runtime="claude-code", requirements=(), tasks=()):
    return {"runtime": runtime, "requirements": list(requirements), "tasks": list(tasks)}


class RouteTableTests(unittest.TestCase):
    def test_classes_and_table_equal_the_approved_decision_without_substitutes(self):
        self.assertEqual(model_routes.CLASSES, ("coordinator", "judge", "sensitive_worker", "worker",
                                                "mechanical_scripted", "mechanical", "research"))
        table = {cls: {runtime: (row["model"], row["effort"]) for runtime, row in runtimes.items()}
                 for cls, runtimes in model_routes.ROUTES.items()}
        self.assertEqual(table, APPROVED)
        for runtimes in model_routes.ROUTES.values():
            for row in runtimes.values():
                self.assertEqual(set(row), {"model", "effort"}, "A route has one model and one effort, no fallbacks")

    def test_codex_luna_only_for_scripted_mechanics_and_claude_haiku_likewise(self):
        lunas = {cls for cls, rows in model_routes.ROUTES.items() if "luna" in rows["codex"]["model"]}
        haikus = {cls for cls, rows in model_routes.ROUTES.items() if "haiku" in rows["claude-code"]["model"]}
        self.assertEqual(lunas, {"mechanical_scripted"})
        self.assertEqual(haikus, {"mechanical_scripted"})
        for rows in model_routes.ROUTES.values():
            for row in rows.values():
                self.assertNotRegex(row["model"], "fable|astra")
                self.assertIn(row["effort"], ("low", "medium", "high"))
                self.assertNotEqual(row["model"], "gpt-6-sol")

    def test_coordinator_advice_is_advice_with_high_then_medium(self):
        self.assertEqual(model_routes.COORDINATOR_ADVICE, {
            "claude-code": {"model": "claude-opus-5-5", "effortBriefPlan": "high", "effortLater": "medium"},
            "codex": {"model": "gpt-6.1-sol", "effortBriefPlan": "high", "effortLater": "medium"},
        })
        self.assertNotIn("coordinator", model_routes.ROUTES)

    def test_claude_aliases_are_a_closed_map(self):
        self.assertEqual(model_routes.CLAUDE_ALIASES, {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                                                       "haiku": "claude-haiku-5-5"})

    def test_agent_files_name_one_claude_agent_per_subagent_class(self):
        self.assertEqual(model_routes.CLAUDE_AGENT_FILES, {
            "judge": "aispa-judge", "sensitive_worker": "aispa-sensitive-worker", "worker": "aispa-worker",
            "mechanical_scripted": "aispa-mechanic-scripted", "mechanical": "aispa-mechanic",
            "research": "aispa-researcher"})


class RouteClassTests(unittest.TestCase):
    def test_every_engine_role_has_its_decided_class(self):
        self.assertEqual(set(model_routes.ROLE_CLASS), set(participants.ROLES))
        for role in JUDGE_ROLES:
            self.assertEqual(model_routes.route_class(state(), role), "judge", role)
        for role in RESEARCH_ROLES:
            self.assertEqual(model_routes.route_class(state(), role), "research", role)
        self.assertEqual(model_routes.route_class(state(), "coordinator"), "coordinator")
        self.assertEqual(model_routes.route_class(state(), "worker"), "worker")

    def test_worker_class_follows_the_task(self):
        sensitive = {"id": "T01", "title": "Payment form", "zones": ["src/pay.py"], "requirements": [],
                     "dependsOn": []}
        ordinary = {"id": "T02", "title": "Greeting page", "zones": ["src/page.py"], "requirements": [],
                    "dependsOn": []}
        mechanical = {"id": "T03", "title": "Fix typos", "zones": ["docs/guide.md"], "requirements": [],
                      "dependsOn": [], "risk": "low", "changeKind": "documentation"}
        scripted = {**mechanical, "id": "T04", "scriptCheck": "python3 tools/check_docs.py"}
        run = state(tasks=[sensitive, ordinary, mechanical, scripted])
        self.assertEqual(model_routes.route_class(run, "worker", sensitive), "sensitive_worker")
        self.assertEqual(model_routes.route_class(run, "worker", ordinary), "worker")
        from aispa_core import task_tiers
        self.assertEqual(task_tiers.model_need(run, mechanical), "mechanical", "fixture must be mechanical work")
        self.assertEqual(model_routes.route_class(run, "worker", mechanical), "mechanical")
        self.assertEqual(model_routes.route_class(run, "worker", scripted), "mechanical_scripted")
        self.assertEqual(model_routes.route_class(run, "critic1", sensitive), "judge")

    def test_unknown_role_is_refused(self):
        from aispa_core.store import Invalid
        with self.assertRaises(Invalid):
            model_routes.route_class(state(), "assistant")

    def test_route_returns_the_table_row_with_claude_agent(self):
        self.assertEqual(model_routes.route(state("claude-code"), "critic1"), {
            "class": "judge", "runtime": "claude-code", "model": "claude-opus-5-5", "effort": "high",
            "agent": "aispa-judge", "advisory": False})
        self.assertEqual(model_routes.route(state("codex"), "discovery"), {
            "class": "research", "runtime": "codex", "model": "gpt-6.1-terra", "effort": "high",
            "advisory": False})


class ReferenceTableTests(unittest.TestCase):
    ROW = re.compile(r"^\|\s*`(?P<cls>[a-z_]+)`[^|]*\|(?P<roles>[^|]*)\|(?P<claude>[^|]*)\|(?P<codex>[^|]*)\|(?P<agent>[^|]*)\|\s*$")
    CELL = re.compile(r"^`(?P<model>[^`]+)` · (?P<effort>low|medium|high)$")
    ADVICE = re.compile(r"^`(?P<model>[^`]+)` · (?P<first>low|medium|high) на брифе и плане, дальше (?P<later>low|medium|high)$")
    ALIAS = re.compile(r"^- `(?P<alias>[a-z]+)` → `(?P<model>[^`]+)`$")

    def rows(self, text):
        found = {}
        for line in text.splitlines():
            match = self.ROW.match(line)
            if match:
                self.assertNotIn(match["cls"], found, "Duplicate reference row")
                found[match["cls"]] = {key: match[key].strip() for key in ("roles", "claude", "codex", "agent")}
        return found

    def compare(self, text):
        rows = self.rows(text)
        self.assertEqual(set(rows), set(model_routes.CLASSES))
        for cls, row in rows.items():
            roles = set(re.findall(r"`([a-z0-9-]+)`", row["roles"]))
            expected = {role for role, value in model_routes.ROLE_CLASS.items() if value == cls}
            self.assertEqual(roles, expected | ({"worker"} if cls in model_routes.WORKER_CLASSES else set()), cls)
            if cls == "coordinator":
                for runtime, cell in (("claude-code", row["claude"]), ("codex", row["codex"])):
                    match = self.ADVICE.match(cell)
                    self.assertIsNotNone(match, cell)
                    advice = model_routes.COORDINATOR_ADVICE[runtime]
                    self.assertEqual((match["model"], match["first"], match["later"]),
                                     (advice["model"], advice["effortBriefPlan"], advice["effortLater"]))
                self.assertEqual(row["agent"], "—")
                continue
            for runtime, cell in (("claude-code", row["claude"]), ("codex", row["codex"])):
                match = self.CELL.match(cell)
                self.assertIsNotNone(match, cell)
                self.assertEqual((match["model"], match["effort"]),
                                 (model_routes.ROUTES[cls][runtime]["model"], model_routes.ROUTES[cls][runtime]["effort"]))
            self.assertEqual(row["agent"], "`%s`" % model_routes.CLAUDE_AGENT_FILES[cls])
        aliases = dict((m["alias"], m["model"]) for m in map(self.ALIAS.match, text.splitlines()) if m)
        self.assertEqual(aliases, model_routes.CLAUDE_ALIASES)

    def test_reference_table_equals_code(self):
        text = REFERENCE.read_text(encoding="utf-8")
        self.compare(text)
        self.assertNotIn("xhigh", text.casefold())

    def test_any_divergence_fails(self):
        text = REFERENCE.read_text(encoding="utf-8")
        for old, new in (("`gpt-6.1-terra` · medium", "`gpt-6.1-terra` · low"),
                         ("`claude-haiku-5-5` · low", "`claude-sonnet-5-5` · low"),
                         ("`aispa-judge`", "`aispa-critic`"),
                         ("`opus` → `claude-opus-5-5`", "`opus` → `claude-opus-6`")):
            with self.subTest(old=old):
                self.assertIn(old, text)
                with self.assertRaises(AssertionError):
                    self.compare(text.replace(old, new, 1))


if __name__ == "__main__":
    unittest.main()
