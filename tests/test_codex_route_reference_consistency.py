"""Current agent-facing routing references must preserve host and evidence boundaries."""
import re
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/aispa/tools"))
from aispa_core import participants
from tests import test_codex_terra_policy as previous_regressions


class CodexRouteReferenceConsistencyTests(unittest.TestCase):
    def test_references_point_to_current_policy_without_claiming_actual_model(self):
        for name in ("continuity.md", "process.md"):
            with self.subTest(reference=name):
                source = (ROOT / "skills/aispa/references" / name).read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"сильная модель\s*—\s*только", source, re.IGNORECASE),
                                  f"{name}: unconditional sensitive-only model rule conflicts with Codex")
                self.assertIsNone(re.search(r"Assign --model (?:записывает фактический ID|сохраняет фактически выбранный ID)", source),
                                  f"{name}: request is not proof of actual serving model")
                self.assertRegex(source, r"(?i)codex")
                self.assertRegex(source, r"(?i)terra")
                self.assertRegex(source, r"(?i)(unknown|неизвестн)")
                self.assertRegex(source, r"(?i)(capability gap|пробел возможностей)")
        continuity = (ROOT / "skills/aispa/references/continuity.md").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"сохраняют текущую модель", continuity),
                          "Unavailable Terra must not silently fall back to current model")

    def test_other_hosts_and_legacy_model_classification_remain_available(self):
        self.assertEqual(participants.model_advice("claude-code", "complex")["preferredFamilies"], ["opus"])
        self.assertEqual(participants.model_advice("claude-code", "mechanical")["preferredFamilies"], ["sonnet"])
        self.assertEqual(participants.model_advice("gemini", "complex")["preferredFamilies"], ["argon", "pro"])
        self.assertEqual(participants.MODEL_CATALOGUE["openai"]["gpt-6-sol"], ("sol", True))
        self.assertEqual(participants.MODEL_CATALOGUE["openai"]["gpt-6-luna"], ("luna", False))
        self.assertEqual(participants.MODEL_CATALOGUE["openai"]["gpt-6.1-terra"], ("terra", True))
        self.assertFalse(participants._strong_model("codex", "openai", "gpt-7-terra"))


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    previous = loader.loadTestsFromModule(previous_regressions)
    suite.addTests(previous)
    suite.addTests(tests)
    return suite


if __name__ == "__main__":
    unittest.main()
