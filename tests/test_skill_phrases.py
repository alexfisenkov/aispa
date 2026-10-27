"""SKILL.md keeps the constant route; branch details live in the files it links.

The required phrases were taken from SKILL.md of commit b914fad before the
split (story 118). Each one must survive in SKILL.md or in a file SKILL.md
links to. Rules the 0.5.0 policy replaced are listed in RETIRED with the
phrase that now carries the rule; old runs read their pinned copies instead.
Matching ignores case, line breaks, repeated spaces and ``**`` emphasis.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "skills/aispa"
SKILL = PACKAGE / "SKILL.md"
PHASES = sorted((PACKAGE / "phases").glob("*.md"))
OPENAI = PACKAGE / "agents/openai.yaml"
MAX_SKILL_CHARS = 14_000
LINK = re.compile(r"\]\(([^)\s]+)\)")
MODEL_ID = re.compile(r"(?i)\b(?:claude-(?:opus|sonnet|haiku|fable)-\d|gpt-\d|gemini-\d)")

# Rules of b914fad SKILL.md that stay mandatory (normalized text).
REQUIRED = (
    "Новый запуск подхватывает существующие документы и сохраняет проектный замысел.",
    "После init проверь `project-documents`: повторно не создавай найденные аналоги.",
    "Новый бриф задаёт текущую работу; постоянный замысел не переносит старые задачи.",
    "Полный каталог команд, документов и архивов заранее в контекст не загружай.",
    "audit-only",
    "Он не вызывает update-check, init, Journal, команды проекта, браузер или дашборд",
    "Не расширяй его до полноценного запуска.",
    "статический отчёт не подтверждает runtime, тесты или безопасность всего проекта",
    "Для ремонта нужно исходное поручение владельца на изменение; уже данное поручение не спрашивай повторно.",
    "версия берётся у конкретного установленного пакета или pinned runtime через его `--version`",
    "Не применяй новый FAQ к старому активному прогону.",
    "Ссылки на repo docs не заменяют чтение пакетного JSON.",
    "передавай `context`, `card`, `template` по их `absolutePath` без перепечатки тел.",
    "bundle с формой — только после успешной адресной проверки",
    "Движок заполняет только известные поля формы, а не выводы проверяющего.",
    "RED относится только к автоматической группе",
    "Весь бриф принимается отдельно.",
    "Не загружай целиком `references/contracts.md` ради одного отчёта",
    "продолжай его прежними командами; не пересоздавай прогон ради оптимизации.",
    "Первое успешное `seal` завершает вопросы на весь текущий прогон.",
    "Не объявляй готовым сам продукт.",
    "`seal` запускай отдельно от batch.",
    "дополнение брифа не открывает повторное интервью.",
    "Не подменяй запрет вопросов просьбой «подтвердить план/этап/продолжение».",
    "Доступы и полномочия не возникают автоматически",
    "недоступный шаг отметь блокером, продолжай независимое и вынеси необходимое действие в финал.",
    "`batch --file steps.json`",
    "Не включай шаг, которому нужен результат предыдущего, пока его не увидел.",
    "update-check` из открытого установленного пакета",
    "Не повторяй отсутствующий `python3` либо Store alias.",
    "Поддерживаемые ID: `codex`, `claude-code`, `cursor`, `openclaw`, `gemini`, `antigravity`.",
    "название модели не определяет runtime",
    "Не подменяй отсутствующие независимые контексты именами ролей.",
    "ветка main, tag и draft не считаются выпуском",
    "«Установить обновление» / «Продолжить с текущей версией»",
    "`update-apply --candidate <candidateId> --confirm`",
    "перечитай установленный SKILL.md",
    "При `offline`, `no_release`, `current`, `invalid_release`, ошибке подготовки или защищённом каталоге продолжай текущим пакетом и кратко назови результат.",
    "не устанавливай main вместо Release",
    "`progress begin <phase>`",
    "Она не подтверждает результат и не заменяет реальные отчёты.",
    "Не записывай задним числом",
    "не меняй runtime или журнал ради таймера",
    "Assign фиксирует участника, start — начало реализации и таймер; не заменяй одно другим.",
    "без вопроса «открыть ли»",
    "Сделай это до интервью по существу, разбора брифа, исследования, подготовки документов и запуска исполнителей.",
    "После resume и возвращения к существующему проекту повтори этот порядок.",
    "markDeliverable/markHandoff",
    "Фактическую команду выбирай по реестру инструментов текущей среды; не придумывай API.",
    "`dashboard refresh-view --port <текущий порт>`",
    "Все действия разработки продолжаются pinned runtime.",
    "не меняй журнал вручную ради зелёного экрана",
    "не подтверждают видимую вкладку",
    "Не используй системный `open`/`start`/`xdg-open`/`webbrowser.open` как замену.",
    "Не добавляй к URL `?demo` либо токен.",
    "Существующий прогон не пересоздавай ради дашборда.",
    "прямо назови ограничение и дай живой URL",
    "Это ограничение отображения, а не разрешение отменить требования проекта или ослабить его проверки.",
    "`auto` — самостоятельная работа по исходному брифу",
    "`important` — только важные развилки до фиксации брифа",
    "`interview` — подробный разговор до фиксации брифа",
    "В `auto` не открывай интервью.",
    "Новые слова владельца учитывай сразу. Не проси повторить уже данное разрешение.",
    "Обновление, защищённые поля, оплата, удаление, публикация и сообщения сохраняют свои отдельные границы полномочий.",
    "не путай с глубиной сценариев `--depth balanced`",
    "Если человек уже назвал режим в вызове, второй раз не спрашивай.",
    "`check-mode <режим> --file <файл>`",
    "Смену режима по ходу не предлагай",
    "По ходу работы вопросов не задавай",
    "Сохраняй полный исходный бриф без секретов; дополнения — отдельными `brief`.",
    "сверяющий видит оригинал, а не выводы первого",
    "Исходные источники неизменны. Требования и обязательное ядро не сокращай из-за сложности.",
    "`drop` и `defer` требуют точной цитаты нового решения владельца",
    "Невыполнимое ядро остаётся явно заблокированным, независимую работу продолжай.",
    "Не правь журнал, SHA, receipt или документы context вручную.",
    "`init` не лечит повреждённый журнал.",
    "`journal-recover --sha256 … --confirm`",
    "неизвестные внешние эффекты запрещают возобновление",
    "Нативных агентов вызывает среда, не Python.",
    "ID нельзя выдумывать.",
    "Не запускай и не назначай critic2 заранее.",
    "Любой non-PASS второго критика блокирует задачу.",
    "Reset, replan, новое имя и новый прогон не обнуляют лимит",
    "Не выпрашивай слова посреди работы.",
    "Сомнение или новый риск требует `promote-task`",
    "Не подставляй ID другого провайдера; requested и actual/unknown сохраняй отдельно.",
    "Чувствительные задачи требуют сильного разрешённого участника либо явного блокера возможностей.",
    "После `init` зарегистрируй настоящего координатора один раз.",
    "Независимые сверки, критика и приёмка не получают coordinator alias.",
    "отсутствие автора не означает нулевые токены",
    "Исполнители работают в объявленных зонах и сохраняют чужие изменения.",
    "Координатор пишет Journal последовательно.",
    "Старый успешный запуск из `commands` не является текущим PASS: повтори команду на актуальном коде.",
    "Чистый клон точного Git commit — проверка, не sandbox.",
    "Усечённый вывод команды не даёт PASS.",
    "недоступную проверку нельзя заменить обещанием",
    "Не объявляй готовым то, что не проверено.",
    "Продуктовые условия не становятся исключениями.",
    "Связь с тестовым файлом не доказывает выполнение сценария",
    "Секреты и чужие персональные данные не попадают в бриф, команды, логи, отчёты и репозиторий.",
    "Глобальная установка не нужна для удобства.",
    "каждый обязательный источник в нём прочитай целиком. Обрезанный вывод не равен прочитанному файлу.",
    "повторно используй только текст, который ты сам уже получил полностью",
    "запись «прочитано» и наличие файла не заменяют содержание",
    "Прочитай `.aispa/context/INDEX.md`, `memory.md`",
    "Новый пакет не мигрирует старый активный прогон.",
    "Нельзя пересоздать состояние через `init`, если журнал повреждён.",
    "Не меняй runtime или digest вручную.",
    "продолжай через возвращённый `cli`",
    "Используй нативный инструмент выбора либо три нумерованных варианта.",
    "Локальный preview, зелёные библиотечные тесты и PASS критиков не подтверждают использование полного навыка или выпуск.",
    "Это сокращает справочный материал, но не исходный бриф, код и обязательные проверки роли.",
)

# b914fad rule moved to a linked file in other words -> phrase that carries it there.
REWORDED = {
    "Для heavy исполнитель и независимый критик различны.": "Нельзя подменять критика самопроверкой",
    "Отдельный реестр ролей не удостоверяет модель сам": "запись ID означает происхождение, а не криптографическую проверку модели",
    "при недоступности фиксируй capability gap, без молчаливой замены": "другую модель не подставляй",
    "Сохраняй и неуспешные реальные запуски в истории, но не рекомендуй их как проверенные.":
        "Неуспешный запуск не становится рекомендацией.",
    "продукт можно принять, но нельзя объявить будущий срок выполненным.": "будущую отправку нельзя подтвердить заранее",
    "Прочитай состояние открытой страницы": "Прочитай состояние вкладки",
    "Переходи к работе только после фактического открытия": "Лишь затем продолжай работу.",
    "Если вкладка этого проекта уже открыта, покажи/переиспользуй её.": "Найденную вкладку показывай и переиспользуй",
    "Вопросы `important` и `interview` заканчиваются на `seal`": "Первое успешное `seal` завершает вопросы",
}

# b914fad rule the 0.5.0 policy replaced -> phrase that carries the new rule (must be present).
RETIRED = {
    "используй глобальный `--compact` перед командой": "Краткий ответ — умолчание команд",
    "(параметр обязателен, значение всегда из ответа человека)": "`--check` не передан — режим `balanced`",
    "режим проверки не выводи из размера задачи, риска или бюджета":
        "это совет, режим ставит выбор пользователя",
    "«Быстро — в разы дешевле, без проверок по ходу, проблемы — в итоговом отчёте»":
        "первой рекомендацией идёт «Быстро» с причиной",
    "В новых прогонах классифицируй задачи до исполнения: по умолчанию heavy.":
        "Тяжесть задачи определяет движок; критиков назначает режим.",
    "Проблемы приёмки не чинятся": "Расхождение слепой приёмки чинится",
    "Для Codex используй последнюю разрешённую Terra": "Модель и усилие каждой роли — по [таблице маршрутов]",
    "В `test-intent` (кроме `fast`) зафиксируй": "Тестовый контракт фиксирует отказные границы",
    "немедленно открой точный URL из ответа во встроенном браузере приложения":
        "открой панель одним действием `open-dashboard` из его ответа",
}

FORBIDDEN_EVERYWHERE = ("Режим сам не предлагай", "start-and-open-dashboard", "xhigh",
                        "Проблемы приёмки не чинятся", "провал потока запрещает finish")
FAST_CRITIC = "деньгами, ключами, доступами, личными данными или обязательным пользовательским потоком"


def normalized(text):
    return re.sub(r"\s+", " ", text.replace("**", "")).strip().casefold()


def read(path):
    return path.read_text(encoding="utf-8")


def links(path):
    result = []
    for target in LINK.findall(read(path)):
        if "://" in target or target.startswith("#"):
            continue
        result.append((path.parent / target.split("#", 1)[0]).resolve())
    return result


def scope_text():
    files = [SKILL, *dict.fromkeys(links(SKILL))]
    return {path: normalized(read(path)) for path in files if path.is_file()}


class SkillSplitTests(unittest.TestCase):
    def test_skill_fits_budget(self):
        self.assertLessEqual(len(read(SKILL)), MAX_SKILL_CHARS)

    def test_all_skill_links_resolve(self):
        for target in links(SKILL):
            with self.subTest(target=str(target)):
                self.assertTrue(target.is_file(), "SKILL.md ссылается на несуществующий файл")

    def test_branch_files_are_linked(self):
        linked = set(links(SKILL))
        for name in ("phases/01-start.md", "references/support.md", "references/updates.md",
                     "references/dashboard-opening.md", "references/runtime-repair.md",
                     "references/model-routing.md"):
            with self.subTest(name=name):
                self.assertIn((PACKAGE / name).resolve(), linked)

    def test_required_phrases_survive_in_skill_or_linked_file(self):
        texts = scope_text()
        for phrase in REQUIRED:
            with self.subTest(phrase=phrase):
                self.assertTrue(any(normalized(phrase) in text for text in texts.values()),
                                "Правило потерялось при разделении SKILL.md")

    def test_reworded_rules_survive_in_skill_or_linked_file(self):
        texts = scope_text()
        for old, new in REWORDED.items():
            with self.subTest(old=old):
                self.assertTrue(any(normalized(new) in text for text in texts.values()),
                                "Перенесённое правило потерялось")

    def test_retired_rules_have_their_replacement(self):
        texts = scope_text()
        for old, new in RETIRED.items():
            with self.subTest(old=old):
                for path, text in texts.items():
                    self.assertNotIn(normalized(old), text, f"вытесненное правило осталось в {path.name}")
                self.assertTrue(any(normalized(new) in text for text in texts.values()),
                                "Нет фразы, которая несёт новое правило")


class SkillRuleTests(unittest.TestCase):
    def text(self, name):
        return normalized(read(PACKAGE / name))

    def assert_all(self, name, *phrases):
        text = self.text(name)
        for phrase in phrases:
            with self.subTest(file=name, phrase=phrase):
                self.assertIn(normalized(phrase), text)

    def test_old_runs_read_their_pinned_texts(self):
        self.assert_all("SKILL.md", "если в `state.policy` нет `focusedReview`, SKILL, фазы и промпты читай из "
                        "`.aispa/runs/<run>/runtime/`")

    def test_balanced_default_and_fast_advice_for_small_project(self):
        for name in ("SKILL.md", "phases/01-start.md"):
            self.assert_all(name, "«Сбалансированно» — режим по умолчанию", "до 50 отслеживаемых файлов",
                            "первой рекомендацией идёт «Быстро»", "режим ставит выбор пользователя",
                            "охват и длительность интервью",
                            "нативный инструмент выбора либо три нумерованных варианта")

    def test_fast_mode_text_matches_engine(self):
        # focused_review.critic_roles: in fast a critic goes to sensitive tasks and critical user flows.
        for name in ("SKILL.md", "phases/01-start.md", "phases/03-plan.md"):
            self.assert_all(name, FAST_CRITIC)
            text = self.text(name)
            for short in ("деньгами, ключами, доступами и личными данными",
                          "деньгами, ключами, доступами, личными данными;"):
                with self.subTest(file=name, short=short):
                    self.assertNotIn(normalized(short), text)

    def test_host_context_before_interview_and_read_only_before_seal(self):
        for name in ("SKILL.md", "phases/01-start.md"):
            self.assert_all(name, "`host-context`", "до интервью")
        for name in ("SKILL.md", "phases/02-product.md"):
            self.assert_all(name, "До `seal` только читай", "не ставь программы", "не загружай файлы",
                            "не запускай пробы на внешних серверах", "не пиши вне `.aispa/`")
        self.assert_all("phases/02-product.md", "каждый ответ владельца из чата сохраняй дословно через `brief`",
                        "`interviewBudget.exceeded`", "через `decision`", "предложи зафиксировать бриф")

    def test_codex_keeps_going_wave_after_wave(self):
        phrase = ("в Codex, закрыв волну, в том же ходе открой следующую; ход заканчивай только финалом, "
                  "блокером или вопросом, который разрешает режим")
        for name in ("SKILL.md", "phases/04-build.md", "phases/05-accept.md"):
            self.assert_all(name, phrase)
        prompt = re.search(r'default_prompt:\s*"([^"]*)"', read(OPENAI)).group(1)
        self.assertIn("в том же ходе открой следующую", prompt)
        self.assertIn("ход заканчивай только финалом, блокером или вопросом, который разрешает режим", prompt)

    def test_build_route_uses_composites_and_tests_once(self):
        self.assert_all("phases/04-build.md", "создай участника → `task-open` → передай пути", "`task-close`",
                        "тесты своей зоны", "полный набор — один раз в `clean-verify` финального кандидата",
                        "то, что считается командой или скриптом, не отдаётся модели",
                        "`prechangeExecution`", "до первой критики",
                        "повторный `discover` на тех же входах ничего не записывает",
                        "повторная `task-open` или `task-close` продолжает с упавшего шага")
        self.assert_all("SKILL.md", "то, что считается командой или скриптом, не отдаётся модели",
                        "`task-open`", "`task-close`")

    def test_final_stage_repairs_and_rechecks(self):
        self.assert_all("phases/05-accept.md", "по линзам", "`final-repair`", "`clean-prepare`", "`blind`",
                        "только расходившиеся пункты", "не больше двух циклов", "`delivered_with_remaining`",
                        "«принято» невозможно", "несопоставленные запросы")
        text = self.text("phases/05-accept.md")
        self.assertNotIn("не чинятся", text)
        self.assertNotIn("запрещают `finish`", text)

    def test_targeted_replan_and_engine_texts(self):
        for name in ("phases/02-product.md", "phases/03-plan.md"):
            self.assert_all(name, "сам вернёт в работу задачи, у которых изменились требования, и их потомков",
                            "отдельный `new-attempt` на каждую не нужен")
        self.assert_all("phases/03-plan.md", "`new-attempt` с цитатой владельца")
        self.assert_all("phases/01-start.md", "передай человеку как есть")

    def test_continuation_order(self):
        self.assert_all("phases/06-continuity.md", "`update-check` → `host-context --purpose continue` → `resume`",
                        "`codeChanges`", "`lost`", "повторный `discover` на тех же входах ничего не записывает",
                        "повторная `task-open` или `task-close` продолжает с упавшего шага")

    def test_dashboard_opens_once(self):
        for name in ("SKILL.md", "references/dashboard-opening.md"):
            self.assert_all(name, "`dashboard start`", "`open-dashboard`", "проверь, что вкладки этого проекта нет",
                            "`dashboard refresh-view`", "из установленного пакета")
        self.assert_all("SKILL.md", "В Claude Code — браузер приложения, если он есть, иначе один раз дай ссылку")
        self.assert_all("references/dashboard-opening.md",
                        "Claude Code. Открой URL в браузере приложения (панель Browser), если он есть; иначе "
                        "(терминальный клиент) один раз дай ссылку и прямо скажи, что панель не открыта "
                        "автоматически. Ссылку на каждом шаге не повторяй.",
                        "`snapshot.ageSeconds`", "300 с", "не зависание")
        text = self.text("references/dashboard-opening.md")
        self.assertNotIn(normalized("Если такой панели нет (терминальный клиент), иначе"), text)

    def test_proof_limits_survive(self):
        self.assert_all("SKILL.md", "Локальный preview, зелёные библиотечные тесты и PASS критиков не подтверждают "
                        "использование полного навыка или выпуск.",
                        "Это сокращает справочный материал, но не исходный бриф, код и обязательные проверки роли.")

    def test_plain_language_invariant(self):
        self.assert_all("SKILL.md", "Человеку пиши коротко и простыми словами",
                        "без внутренних терминов", "ID и SHA — ссылкой на файл")

    def test_no_model_ids_or_xhigh_in_route_texts(self):
        for path in (SKILL, *PHASES, OPENAI):
            with self.subTest(file=path.name):
                text = read(path)
                self.assertIsNone(MODEL_ID.search(text), "ID моделей живут только в references/model-routing.md")
                self.assertNotIn("xhigh", text.casefold())
        self.assertIn("references/model-routing.md", read(SKILL))

    def test_retired_wording_is_gone(self):
        texts = {**scope_text(), OPENAI: normalized(read(OPENAI))}
        for path in PHASES:
            texts.setdefault(path, normalized(read(path)))
        for path, text in texts.items():
            for phrase in FORBIDDEN_EVERYWHERE:
                with self.subTest(file=path.name, phrase=phrase):
                    self.assertNotIn(normalized(phrase), text)

    def test_invocation_flags_unchanged(self):
        self.assertIn("disable-model-invocation: true", read(SKILL))
        self.assertIn("allow_implicit_invocation: false", read(OPENAI))


if __name__ == "__main__":
    unittest.main()
