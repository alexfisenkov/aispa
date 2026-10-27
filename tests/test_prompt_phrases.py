"""Key phrases of the role prompts and references changed for the focused-review policy.

Each file is pinned by a few phrases a model needs without opening SKILL.md; a phrase
vanishing in an edit fails here instead of silently weakening a role.
"""
import re
from pathlib import Path
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / "skills/aispa"
MODEL_ID = re.compile(r"gpt-\d|claude-(?:opus|sonnet|haiku|fable)-\d|\b(?:terra|luna)\b", re.IGNORECASE)

PHRASES = {
    "prompts/worker.md": (
        "тесты своей зоны", "один раз — в `clean-verify`", "не отдаётся модели", "`scriptCheck`",
        "`prechangeExecution`", "единственная поздняя ревизия", "`productFlows`", "`ownerDecision`",
        "точечный переплан", "доказательства её прошлой попытки не считаются", "model-routing.md",
    ),
    "prompts/task-critic.md": (
        "`blockingClass`", "`brief_drift`", "`broken_scenario`", "`security`", "`data_loss`", "`failing_tests`",
        "`warning`", "`collectedFindings`", "`criteriaFiles`", "по необходимости", "Целиком читается только `zoneChanges`",
        "`expectedChecks`", "`zoneChangesScope: repair`", "`repairFindingIds`", "точечный переплан",
    ),
    "prompts/integration-reviewer.md": (
        "своя линза", "`lens`", "`collectedFindings`", "`behavior`", "`confirmed`", "`dismissed`", "`triage`",
    ),
    "prompts/acceptance.md": (
        "`placement`", "`persistence`", "`positiveCase`", "обработчика в чистом клоне",
        "тестовым сообщением от каждой роли", "в реальные чаты ничего не отправляй", "`recheckOf`",
        "расходившиеся пункты", "Никогда не даёт «принято»", "`remaining.checks`",
    ),
    "prompts/brief-analyst.md": (
        "Одна проверяемая просьба", "одно требование", "не добавляй требований, которых в брифе нет",
    ),
    "references/native-agents.md": (
        "aispa-*", "встроенного агента", "усилие не применено", "`reasoning_effort`", "при каждом запуске",
    ),
    "references/contracts.md": (
        "`blockingClass`", "collectedFindings", "finalRepairs", "`criteriaFiles`", "`scriptCheck`",
        "`decision TEXT [--requirement ID]", "`interviewBudget`", "codeChanges", "строки-дельты",
    ),
    "references/product-development.md": (
        "`placement`", "`persistence`", "`acceptsInput`", "`positiveCase`", "дописывает их в карточку сам",
        "Непройденный обязательный поток никогда не даёт «принято»",
    ),
    "references/task-tiers.md": ("`focusedReview`", "вычисляет движок", "`scriptCheck`"),
    "references/process.md": ("`focusedReview`", "`task-open", "`task-close`", "Блокирует короткий список"),
    "references/continuity.md": ("`resumeReport`", "`codeChanges`", "`lost`"),
    "references/runtimes.md": ("в том же ходе", "model-routing.md"),
    "references/dashboard-progress.md": ("«Простой и ожидание»", "«усилие не применено»", "«идёт дольше обычного»"),
}
CHECKED = tuple(PHRASES) + ("references/failure-checks.md",)


def read(name):
    return re.sub(r"\s+", " ", (PACKAGE / name).read_text(encoding="utf-8"))


class PromptPhraseTests(unittest.TestCase):
    def test_key_phrases_stay_in_each_file(self):
        for name, phrases in PHRASES.items():
            text = read(name)
            for phrase in phrases:
                with self.subTest(file=name, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_texts_name_no_model_and_no_xhigh(self):
        for name in CHECKED:
            with self.subTest(file=name):
                text = read(name)
                self.assertIsNone(MODEL_ID.search(text), "model IDs live only in references/model-routing.md")
                self.assertNotIn("xhigh", text.casefold())

    def test_role_prompts_with_a_model_link_point_to_the_route_table(self):
        for name in ("prompts/worker.md", "prompts/task-critic.md", "prompts/integration-reviewer.md",
                     "prompts/acceptance.md", "prompts/brief-analyst.md"):
            with self.subTest(file=name):
                self.assertIn("../references/model-routing.md", read(name))
                self.assertTrue((PACKAGE / "references/model-routing.md").is_file())


if __name__ == "__main__":
    unittest.main()
