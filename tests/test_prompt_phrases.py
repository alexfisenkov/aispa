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

# The worker and task critic run on every task: their texts must not outgrow the plan base 5604ac2.
MAX_BYTES = {"prompts/worker.md": 13_846, "prompts/task-critic.md": 14_403}
# Old-policy branches; a new run is always focused and old runs read their pinned runtime copy.
OLD_POLICY = re.compile(r"two_rounds|adaptive|legacy|reviewPolicy: lean|Прогоны с `focusedReview`", re.IGNORECASE)
# Wording that contradicted the focused engine -> phrase that carries the rule now (must be present).
RETIRED = {
    "prompts/worker.md": {
        "В режиме проверки `fast` RED и `test-intent` не нужны":
            "Тестовый контракт и RED нужны heavy-задаче в любом режиме",
        "Сохрани связь каждого обязательного `blocking` и `warning` ID":
            "`addresses` перечисляет все блокирующие ID первого отчёта",
        "кандидат переходит отдельному приёмщику": "Отдельного приёмщика задачи нет",
        "в `fast` общего обзора версии нет": "в `fast` ветку смотрит один ревьюер",
        "до общего обзора версии": "до проверки ветки",
        # The engine writes criticRoles to the attempt and the task packet, not to the role card.
        "видно в карточке (`criticRoles`)": "видно в пакете (`criticRoles`)",
        # A first PASS of a heavy task in max hands over to critic2 (secondRound).
        "PASS критика сразу закрывает задачу": "PASS критика закрывает задачу, кроме тяжёлой в `max`",
    },
    "prompts/task-critic.md": {
        "как `blocking`, исключает PASS": "Отчёт с одними warning получает PASS",
        "заполни `branchReview`": "`branchReview` в отчёт не добавляй",
        "критерии всех задач замыкания": "Критерии принятых предков проверяй только те, что есть в твоих `expectedChecks`",
        "Эта роль назначается только heavy-задаче": "Роли задачи выбирает движок",
        "`zoneChangesScope: branch`": "читает свою попытку",
        "в общем обзоре": "ветку смотрят ревьюеры по линзам",
        "видны в карточке (`criticRoles`)": "видны в пакете (`criticRoles`)",
        "твой PASS закрывает задачу.": "твой PASS закрывает задачу, кроме PASS critic1 тяжёлой задачи в `max`",
    },
}


def read(name):
    return re.sub(r"\s+", " ", (PACKAGE / name).read_text(encoding="utf-8"))


class PromptPhraseTests(unittest.TestCase):
    def test_key_phrases_stay_in_each_file(self):
        for name, phrases in PHRASES.items():
            text = read(name)
            for phrase in phrases:
                with self.subTest(file=name, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_worker_and_critic_fit_the_base_size(self):
        for name, limit in MAX_BYTES.items():
            with self.subTest(file=name):
                self.assertLessEqual(len((PACKAGE / name).read_bytes()), limit)

    def test_worker_and_critic_carry_only_the_focused_policy(self):
        for name in MAX_BYTES:
            with self.subTest(file=name):
                found = OLD_POLICY.search(read(name))
                self.assertIsNone(found, found and found.group(0))

    def test_retired_wording_has_its_replacement(self):
        for name, rules in RETIRED.items():
            text = read(name)
            for old, new in rules.items():
                with self.subTest(file=name, old=old):
                    self.assertNotIn(old, text)
                    self.assertIn(new, text)

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
