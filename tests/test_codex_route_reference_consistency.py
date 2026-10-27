"""Current agent-facing routing references must preserve host and evidence boundaries."""
import re
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core import participants
from tests import test_codex_terra_policy as previous_regressions


MODEL_ID = re.compile(r"gpt-\d|claude-(?:opus|sonnet|haiku|fable)-\d|\b(?:terra|luna)\b", re.IGNORECASE)
LINKED = ("runtimes.md", "native-agents.md", "continuity.md", "process.md", "contracts.md")


def reference(name):
    return (ROOT / "skills/aispa/references" / name).read_text(encoding="utf-8")


class CodexRouteReferenceConsistencyTests(unittest.TestCase):
    def test_references_point_to_the_route_table_and_name_no_model(self):
        self.assertTrue((ROOT / "skills/aispa/references/model-routing.md").is_file())
        for name in LINKED:
            with self.subTest(reference=name):
                source = reference(name)
                self.assertIn("model-routing.md", source, f"{name}: no link to the route table")
                self.assertIsNone(MODEL_ID.search(source), f"{name}: model IDs live only in model-routing.md")
                self.assertNotIn("xhigh", source.casefold())

    def test_references_keep_evidence_boundaries_without_silent_fallback(self):
        for name in ("continuity.md", "process.md", "runtimes.md"):
            with self.subTest(reference=name):
                source = reference(name)
                self.assertRegex(source, r"(?i)(capability gap|пробел возможностей)")
                self.assertRegex(source, r"(?i)без молчаливой замены")
        for name in ("continuity.md", "runtimes.md"):
            self.assertRegex(reference(name), r"(?i)(unknown|неизвестн)")
        self.assertIsNone(re.search(r"сохраняют текущую модель", reference("continuity.md")))
        for name in ("continuity.md", "process.md"):
            self.assertIsNone(re.search(r"Assign --model (?:записывает фактический ID|сохраняет фактически выбранный ID)",
                                        reference(name)), f"{name}: request is not proof of actual serving model")

    def test_native_agents_describe_claude_agents_fallback_and_codex_parameters(self):
        source = re.sub(r"\s+", " ", reference("native-agents.md"))
        for phrase in ("aispa-*", "`.claude/agents/`", "встроенного агента", "усилие не применено",
                       "reasoningSupport", "reasoning_effort", "при каждом запуске"):
            self.assertIn(phrase, source)

    def test_other_hosts_and_legacy_model_classification_remain_available(self):
        self.assertEqual(participants.model_advice("claude-code", "complex")["preferredFamilies"], ["opus"])
        self.assertEqual(participants.model_advice("claude-code", "mechanical")["preferredFamilies"], ["sonnet"])
        self.assertEqual(participants.model_advice("gemini", "complex")["preferredFamilies"], ["argon", "pro"])
        self.assertEqual(participants.MODEL_CATALOGUE["openai"]["gpt-6-sol"], ("sol", True))
        self.assertEqual(participants.MODEL_CATALOGUE["openai"]["gpt-6-luna"], ("luna", False))
        self.assertEqual(participants.MODEL_CATALOGUE["openai"]["gpt-6.1-terra"], ("terra", True))
        # Known but never for subagents, and Haiku only for scripted mechanics: none earns a sensitive route.
        self.assertEqual(participants.MODEL_CATALOGUE["openai"]["gpt-6-astra"], ("astra", False))
        self.assertEqual(participants.MODEL_CATALOGUE["anthropic"]["claude-fable-5-1"], ("fable", False))
        self.assertEqual(participants.MODEL_CATALOGUE["anthropic"]["claude-haiku-5-5"], ("haiku", False))
        for runtime, provider, future in (("codex", "openai", "gpt-7-terra"), ("claude-code", "anthropic", "claude-opus-6")):
            self.assertNotIn(future, participants.MODEL_CATALOGUE[provider])
            self.assertFalse(participants._strong_model(runtime, provider, future))


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    previous = loader.loadTestsFromModule(previous_regressions)
    suite.addTests(previous)
    suite.addTests(tests)
    return suite


if __name__ == "__main__":
    unittest.main()
