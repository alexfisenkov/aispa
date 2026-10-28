"""Тесты skills/aispa/tools/ap.py — учёта прогона AISpa.

Запуск из корня репозитория:  python3 -m unittest discover -s tests
Каждый тест поднимает временный git-репозиторий с .aispa-work/ и зовёт ap.py
как оркестратор — отдельным процессом, без сервера (--no-serve).
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(os.path.dirname(HERE), "skills", "aispa")
AP = os.path.join(SKILL, "tools", "ap.py")
TEMPLATE = os.path.join(SKILL, "phases", "dashboard-template.html")

MANIFEST = """# Манифест требований

| ID | Из брифа (дословно) | Статус | Основание | Где |
|----|---------------------|--------|-----------|-----|
| R01 | «принимает заявки» | in-spec | — | spec §2 |
| R02 | «складывает в таблицу» | in-spec | — | spec §4 |
| R03 | «SMS» | dropped | «не надо» | — |
| R05 | «цвета студии» | in-spec | — | spec §6 |
"""


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def ticket(tid, title, reqs, deps, zone, wave, review="нет", model="обычная"):
    return ("# %s — %s\n\n**Требования:** %s\n**Зависит от:** %s\n**Зона:** %s\n"
            "**Волна:** %s\n**Ревью:** %s\n**Модель:** %s\n\n## Критерии приёмки\n\n- [ ] что-то\n"
            % (tid, title, reqs, deps, " · ".join("`%s`" % z for z in zone), wave, review, model))


class Run(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ap-test-")
        self.git("init", "-q")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
        self.a = os.path.join(self.root, ".aispa-work")
        os.makedirs(self.a)
        shutil.copy(TEMPLATE, os.path.join(self.a, "dashboard.html"))
        shutil.copy(AP, os.path.join(self.a, "ap.py"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.root] + list(args), capture_output=True, text=True)

    def ap(self, *args):
        env = dict(os.environ, AISPA_NO_UPDATE_CHECK="1")
        r = subprocess.run([sys.executable, os.path.join(self.a, "ap.py")] + list(args) + ["--no-serve"],
                           capture_output=True, text=True, encoding="utf-8", env=env, cwd=self.root)
        return r.returncode, r.stdout

    def state(self):
        raw = read(os.path.join(self.a, "state.js"))
        return json.loads(raw.split("=", 1)[1])

    def stage(self, sid):
        return next(s for s in self.state()["stages"] if s["id"] == sid)

    def init(self):
        code, out = self.ap("init", "--slug", "repair-bot", "--title", "Бот", "--skill-dir", SKILL)
        self.assertEqual(code, 0, out)
        self.dir = os.path.join(self.a, self.state()["dir"])
        return out

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def manifest(self):
        return read(os.path.join(self.dir, "manifest.md"))

    # ── init ───────────────────────────────────────────────────────────────
    def test_init_creates_run(self):
        self.init()
        s = self.state()
        self.assertTrue(re.match(r"\d{4}-\d{2}-\d{2}-repair-bot--wip$", s["dir"]))
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "tickets")))
        self.assertEqual([x["status"] for x in s["stages"]], ["active"] + ["pending"] * 7)
        self.assertRegex(s["startedAt"], r"T\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertIn("в работе", read(os.path.join(self.a, "README.md")))
        self.assertIn(".aispa-work/serve.*", read(os.path.join(self.root, ".gitignore")))
        self.assertIn("window.STATE={", read(os.path.join(self.a, "dashboard.html")))

    def test_init_refuses_on_live_run(self):
        self.init()
        code, out = self.ap("init", "--slug", "other")
        self.assertNotEqual(code, 0)
        self.assertIn("продолжение", out)

    def test_init_archives_finished_run(self):
        self.init()
        first = self.state()["dir"]
        self.ap("finish", "--result", "готово")
        code, out = self.ap("init", "--slug", "second")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.a, first.replace("--wip", ""), "state.js")))
        self.assertTrue(self.state()["dir"].endswith("second--wip"))

    # ── mode, depth, skill version ─────────────────────────────────────────
    def init_with(self, *extra):
        return self.ap("init", "--slug", "repair-bot", "--force", "--skill-dir", SKILL, *extra)

    def test_init_takes_the_four_mode_names(self):
        for mode in ("auto", "important", "interview", "manual"):
            code, out = self.init_with("--mode", mode)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.state()["mode"], mode)

    def test_init_understands_the_old_mode_words(self):
        for old, new in (("full", "auto"), ("semi", "important"), ("FULL", "auto")):
            code, out = self.init_with("--mode", old)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.state()["mode"], new)

    def test_init_without_a_mode_is_important(self):
        self.init()
        self.assertEqual(self.state()["mode"], "important")
        self.assertEqual(self.state()["depth"], "normal")

    def test_init_refuses_an_unknown_mode_or_depth(self):
        code, out = self.init_with("--mode", "turbo")
        self.assertNotEqual(code, 0)
        self.assertIn("auto", out)
        self.assertFalse(os.path.exists(os.path.join(self.a, "state.js")))
        code, out = self.init_with("--depth", "bottomless")
        self.assertNotEqual(code, 0)
        self.assertIn("strict", out)

    def test_a_bad_mode_does_not_archive_the_finished_run(self):
        self.init()
        self.ap("finish", "--result", "готово")
        code, out = self.ap("init", "--slug", "second", "--mode", "turbo")
        self.assertNotEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.a, "state.js")))
        self.assertTrue(self.state()["dir"].endswith("repair-bot"))

    def test_set_mode_and_depth_keep_the_new_names(self):
        self.init()
        for given, kept in (("semi", "important"), ("full", "auto"), ("manual", "manual"),
                            ("interview", "interview"), ("auto", "auto")):
            code, out = self.ap("set", "mode=" + given)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.state()["mode"], kept)
        code, out = self.ap("set", "mode=turbo")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.state()["mode"], "auto")
        self.assertEqual(self.ap("set", "depth=deep")[0], 0)
        self.assertEqual(self.state()["depth"], "deep")
        self.assertNotEqual(self.ap("set", "depth=bottomless")[0], 0)
        self.assertEqual(self.state()["depth"], "deep")

    def test_init_understands_the_new_and_old_depth_words(self):
        for given, kept in (("fast", "strict"), ("balanced", "normal"), ("max", "deep"),
                            ("strict", "strict"), ("normal", "normal"), ("deep", "deep"),
                            ("Быстро", "strict"), ("Сбалансированно", "normal"),
                            ("Максимум", "deep"), ("MAX", "deep"),
                            ("сбалансированная", "normal"), ("максимальная", "deep"),
                            ("быстрая", "strict"), ("по максимуму", "deep")):
            code, out = self.init_with("--depth", given)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.state()["depth"], kept)

    def test_set_depth_takes_the_new_words(self):
        self.init()
        for given, kept in (("fast", "strict"), ("max", "deep"), ("balanced", "normal"),
                            ("быстро", "strict"), ("максимум", "deep")):
            self.assertEqual(self.ap("set", "depth=" + given)[0], 0)
            self.assertEqual(self.state()["depth"], kept)
        self.assertNotEqual(self.ap("set", "depth=bottomless")[0], 0)
        self.assertEqual(self.state()["depth"], "deep")

    def test_questions_go_in_rounds_unless_asked_one_at_a_time(self):
        self.init()
        self.assertIsNone(self.state()["questions"])  # форму ещё не спрашивали — спросит карта интервью
        for given, kept in (("one", "one"), ("rounds", "rounds"), ("по одному", "one"),
                            ("раундами", "rounds"), ("one at a time", "one"), ("Пачкой", "rounds"),
                            ("пачками", "rounds")):
            code, out = self.ap("set", "questions=" + given)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.state()["questions"], kept)
        self.ap("set", "questions=one")
        code, out = self.ap("set", "questions=by-telepathy")
        self.assertNotEqual(code, 0)
        self.assertIn("rounds", out)
        self.assertEqual(self.state()["questions"], "one")

    def test_init_takes_the_question_form_and_refuses_a_bad_one(self):
        code, out = self.init_with("--questions", "по одному")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state()["questions"], "one")
        self.ap("finish", "--result", "готово")
        code, out = self.ap("init", "--slug", "second", "--questions", "telepathy")
        self.assertNotEqual(code, 0)
        self.assertTrue(self.state()["dir"].endswith(self.state()["slug"]))
        self.assertIsNotNone(self.state()["finishedAt"])

    def test_init_records_the_skill_version(self):
        self.init()
        want = re.search(r'^\s*version:\s*"([^"]+)"', read(os.path.join(SKILL, "SKILL.md")), re.M).group(1)
        self.assertEqual(self.state()["skillVersion"], want)

    def test_skill_version_is_null_when_no_skill_md_is_found(self):
        code, out = self.ap("init", "--slug", "repair-bot")
        self.assertEqual(code, 0, out)
        self.assertIn("skillVersion", self.state())
        self.assertIsNone(self.state()["skillVersion"])

    # ── stages ─────────────────────────────────────────────────────────────
    def test_opening_a_stage_closes_the_previous(self):
        self.init()
        self.ap("stage", "manifest")
        self.assertEqual(self.stage("preflight")["status"], "done")
        self.assertEqual(self.stage("manifest")["status"], "active")

    def test_review_closes_build(self):
        self.init()
        self.ap("stage", "build")
        self.ap("stage", "review")
        self.assertEqual(self.stage("build")["status"], "done")

    def test_passed_stage_with_artifact_is_marked_done(self):
        self.init()
        self.write("manifest.md", MANIFEST)
        code, out = self.ap("stage", "spec")
        self.assertEqual(self.stage("manifest")["status"], "done")
        self.assertIn("briefing", out)                     # ни файла, ни отметки — подсказка
        self.assertEqual(self.stage("briefing")["status"], "pending")

    def test_skip_needs_a_reason(self):
        self.init()
        code, _ = self.ap("stage", "briefing", "skip")
        self.assertNotEqual(code, 0)
        self.ap("stage", "briefing", "skip", "--note", "полный автомат — самобрифинг")
        self.assertEqual(self.stage("briefing")["status"], "skipped")

    # ── tickets and the manifest ───────────────────────────────────────────
    def plan(self):
        self.init()
        self.write("manifest.md", MANIFEST)
        self.write("tickets/01-karkas.md", ticket("01", "Каркас", "R01, R05", "—", ["src/core/"], 1, "да — фундамент", "сильная"))
        self.write("tickets/02-priem.md", ticket("02", "Приём", "R01.1, R02, A01", "01", ["src/bot/"], 2))

    def test_tickets_publish_and_move_rows_in_ticket(self):
        self.plan()
        code, out = self.ap("tickets")
        t = {x["id"]: x for x in self.state()["tickets"]}
        self.assertEqual(t["02"]["requirements"], ["R01.1", "R02", "A01"])
        self.assertEqual(t["02"]["blockedBy"], ["01"])
        self.assertEqual(t["01"]["zone"], ["src/core/"])
        self.assertEqual(t["01"]["review"], "да — фундамент")
        self.assertEqual(t["02"]["status"], "pending")
        m = self.manifest()
        self.assertRegex(m, r"\| R01 \|[^\n]*\| in-ticket \|[^\n]*T01, T02")
        self.assertRegex(m, r"\| R03 \|[^\n]*\| dropped \|")
        self.assertEqual(self.state()["requirements"]["inTicket"], 3)

    def test_check_plan_passes_a_sound_plan(self):
        self.plan()
        code, out = self.ap("check-plan")
        self.assertIn("G3: трасса сходится", out)

    def test_check_plan_catches_defects(self):
        self.plan()
        self.write("tickets/03-tablica.md", ticket("03", "Таблица", "R02", "02", ["src/bot/sheets/"], 2))
        self.write("tickets/04-nichei.md", ticket("04", "Ничей", "", "—", [], 1))
        code, out = self.ap("check-plan")
        self.assertIn("в волне 2, но её зависимости позволяют не раньше 3", out)
        self.assertIn("задачи 02 и 03 в одной волне пишут в src/bot/sheets/", out)
        self.assertIn("задача 04 ни к чему не привязана", out)
        self.assertIn("у задачи 04 нет зоны", out)

    def test_check_plan_catches_a_requirement_in_no_ticket(self):
        self.plan()
        self.write("manifest.md", MANIFEST + "| G01 | «отмена» | in-spec | — | spec §5 |\n")
        code, out = self.ap("check-plan")
        self.assertIn("требование G01", out)

    def test_done_closes_rows_only_when_all_their_tickets_are_done(self):
        self.plan()
        self.ap("tickets")
        self.ap("ticket", "01", "start")
        self.ap("ticket", "01", "done", "--tests", "3/0", "--commit", "1111111aaaa", "--placeholder", "R05")
        m = self.manifest()
        self.assertRegex(m, r"\| R01 \|[^\n]*\| in-ticket \|")     # ещё и в задаче 02
        self.assertRegex(m, r"\| R05 \|[^\n]*\| placeholder \|[^\n]*1111111")
        self.ap("ticket", "02", "start")
        self.ap("ticket", "02", "done", "--tests", "5/0", "--commit", "2222222bbbb")
        m = self.manifest()
        self.assertRegex(m, r"\| R01 \|[^\n]*\| done \|")
        self.assertRegex(m, r"\| R02 \|[^\n]*\| done \|")
        s = self.state()
        self.assertEqual(s["requirements"]["done"], 2)
        self.assertEqual(s["requirements"]["placeholder"], 1)
        self.assertEqual(s["tests"], {"passed": 5, "failed": 0})

    def test_ticket_lifecycle_counters(self):
        self.plan()
        self.ap("tickets")
        self.ap("ticket", "01", "02", "start")
        self.ap("ticket", "01", "review")
        self.ap("ticket", "01", "repair", "--note", "ключ схемы — R01")
        self.ap("ticket", "02", "retry")
        t = {x["id"]: x for x in self.state()["tickets"]}
        self.assertEqual(t["01"]["status"], "repair")
        self.assertEqual(t["01"]["repairs"], 1)
        self.assertEqual(t["01"]["repairFindings"], ["ключ схемы — R01"])
        self.assertEqual(t["02"]["retries"], 1)
        self.assertTrue(t["02"]["startedAt"])
        self.ap("ticket", "02", "reset")
        t = {x["id"]: x for x in self.state()["tickets"]}
        self.assertEqual(t["02"]["status"], "pending")
        self.assertNotIn("startedAt", t["02"])

    def test_unknown_ticket_is_an_error(self):
        self.plan()
        self.ap("tickets")
        code, out = self.ap("ticket", "09", "start")
        self.assertNotEqual(code, 0)

    def test_escaped_pipe_in_a_quote(self):
        self.init()
        self.write("manifest.md", MANIFEST.replace("«складывает в таблицу»", "«таблица \\| или база»"))
        self.write("tickets/01-a.md", ticket("01", "A", "R01, R02, R05", "—", ["a/"], 1))
        self.ap("tickets")
        self.assertRegex(self.manifest(), r"\| R02 \| «таблица \\\| или база» \| in-ticket \|")

    def test_d_rows_are_constraints_not_requirements(self):
        self.plan()
        self.write("manifest.md", MANIFEST + "| D01 | схема не держит два адреса | in-spec | задача 02 | spec §3 |\n")
        code, out = self.ap("check-plan")
        self.assertNotIn("D01", out)
        self.assertEqual(self.state()["requirements"]["total"], 4)

    def test_dependencies_accept_a_t_prefix(self):
        self.plan()
        self.write("tickets/03-c.md", ticket("03", "C", "R02", "T1, T02", ["c/"], 3))
        self.ap("tickets")
        t = {x["id"]: x for x in self.state()["tickets"]}
        self.assertEqual(t["03"]["blockedBy"], ["01", "02"])

    def test_empty_tickets_dir_does_not_pass_the_plan(self):
        self.init()
        self.write("manifest.md", MANIFEST)
        self.ap("stage", "build")
        self.assertEqual(self.stage("plan")["status"], "pending")
        self.write("tickets/01-a.md", ticket("01", "A", "R01", "—", ["a/"], 1))
        self.ap()
        self.assertEqual(self.stage("plan")["status"], "done")

    # ── lists and numbers ──────────────────────────────────────────────────
    def test_add_set_and_numbers(self):
        self.init()
        self.ap("add", "concerns", "src/x.ts:4 — два формата даты")
        self.ap("add", "debt.emptyEnv", "TELEGRAM_BOT_TOKEN")
        self.ap("add", "debt.emptyEnv", "TELEGRAM_BOT_TOKEN")      # без дублей
        self.ap("set", "tier=T2", "baseCommit=abc1234")
        self.ap("coverage", "found=2", "fixed=1", "deferred=1", "--item", "R07 — отложено")
        self.ap("add", "report", "два формата даты", "лишний отступ")
        self.ap("blind", "checked=4", "matched=3", "--mismatch", "R02 — не видно")
        s = self.state()
        self.assertEqual(s["concerns"], ["src/x.ts:4 — два формата даты"])
        self.assertEqual(s["debt"]["emptyEnv"], ["TELEGRAM_BOT_TOKEN"])
        self.assertEqual((s["tier"], s["baseCommit"]), ("T2", "abc1234"))
        self.assertEqual(s["coverage"], {"found": 2, "fixed": 1, "deferred": 1, "items": ["R07 — отложено"]})
        self.assertEqual(s["report"], ["два формата даты", "лишний отступ"])
        self.assertEqual(s["blind"], {"checked": 4, "matched": 3, "mismatches": ["R02 — не видно"]})

    def test_every_event_leaves_a_beat(self):
        # updatedAt перезаписывается; журнал хранит каждое событие, чтобы рабочее
        # время считалось и тогда, когда дашборд их не видел (сон, мёртвый сервер)
        self.init()
        first = self.state()["beats"]
        self.assertEqual(first, [self.state()["startedAt"]])
        self.ap("add", "report", "раз")
        self.ap()                                   # синхронизация — не событие
        s = self.state()
        self.assertEqual(s["beats"][-1], s["updatedAt"])
        self.assertLessEqual(len(s["beats"]), 2)
        self.assertEqual(s["beats"], sorted(set(s["beats"])))

    # ── what the dashboard reads ───────────────────────────────────────────
    def test_waiting_for_the_user_is_kept_apart_from_work(self):
        # Ожидание ответа — не работа сборки: дашборд вычитает waits из часов
        self.init()
        self.ap("stage", "briefing")
        self.ap("ask", "briefing", "--note", "три вопроса")
        s = self.state()
        self.assertEqual((s["waiting"]["kind"], s["waiting"]["note"]), ("briefing", "три вопроса"))
        self.assertEqual(s["waiting"]["since"], s["updatedAt"])
        self.ap("answered")
        s = self.state()
        self.assertIsNone(s["waiting"])
        self.assertEqual(len(s["waits"]), 1)
        self.assertEqual(s["waits"][0]["kind"], "briefing")
        self.assertLessEqual(s["waits"][0]["from"], s["waits"][0]["to"])

    def test_moving_on_closes_a_forgotten_wait(self):
        self.init()
        self.ap("ask", "spec")
        self.ap("add", "report", "раз")                    # учёт — не ответ пользователя
        self.assertIsNotNone(self.state()["waiting"])
        self.ap("stage", "plan")
        s = self.state()
        self.assertIsNone(s["waiting"])
        self.assertEqual([w["kind"] for w in s["waits"]], ["spec"])

    def test_ask_needs_a_known_kind(self):
        self.init()
        code, _ = self.ap("ask", "whatever")
        self.assertNotEqual(code, 0)

    def test_log_records_events_not_syncs(self):
        self.plan()
        self.ap("tickets")
        self.ap("ticket", "01", "start")
        self.ap("ticket", "01", "repair", "--note", "пустой адрес")
        self.ap("ticket", "01", "done", "--tests", "7/0", "--commit", "abc")
        self.ap()
        kinds = [(e["k"], e.get("act")) for e in self.state()["log"]]
        self.assertEqual(kinds, [("init", None), ("tickets", None), ("ticket", "start"),
                                 ("ticket", "repair"), ("ticket", "done")])
        last = self.state()["log"][-1]
        self.assertEqual((last["ids"], last["tests"]), (["01"], {"passed": 7, "failed": 0}))
        self.assertEqual(self.state()["log"][3]["note"], "пустой адрес")

    def test_gates_follow_the_events_that_decide_them(self):
        self.plan()
        self.write("manifest.md", MANIFEST.replace("| R02 | «складывает в таблицу» | in-spec",
                                                   "| R02 | «складывает в таблицу» | open"))
        self.ap("stage", "spec")
        g = self.state()["gates"]["G1"]
        self.assertEqual((g["ok"], g["open"], g["total"]), (False, 1, 4))
        self.write("manifest.md", MANIFEST)
        self.ap("stage", "spec", "done")
        self.assertTrue(self.state()["gates"]["G1"]["ok"])
        self.ap("coverage", "found=2", "fixed=1", "deferred=1")
        self.ap("check-plan")
        self.ap("blind", "checked=4", "matched=3", "--mismatch", "R02 — не видно")
        g = self.state()["gates"]
        self.assertTrue(g["G2"]["ok"])
        self.assertTrue(g["G3"]["ok"])
        self.assertEqual((g["G4"]["ok"], g["G4"]["matched"], g["G4"]["checked"]), (False, 3, 4))

    def test_requirements_carry_the_users_words(self):
        self.init()
        self.write("manifest.md", MANIFEST.replace("«цвета студии»", "*(подразумевается)* «таблица \\| база»")
                   + "| D01 | схема | in-spec | — | — |\n")
        self.ap()
        req = {r["id"]: r for r in self.state()["reqList"]}
        self.assertEqual(sorted(req), ["R01", "R02", "R03", "R05"])     # D## — не требование
        self.assertEqual(req["R01"], {"id": "R01", "text": "«принимает заявки»", "status": "in-spec"})
        self.assertEqual(req["R03"]["why"], "«не надо»")
        self.assertEqual(req["R05"]["text"], "(подразумевается) «таблица | база»")

    def test_every_write_bumps_rev(self):
        # Дашборд перерисовывается по rev: синхронизация тоже меняет то, что он показывает
        self.init()
        r = self.state()["rev"]
        self.ap()
        self.assertEqual(self.state()["rev"], r + 1)

    # ── finish ─────────────────────────────────────────────────────────────
    def test_finish_lands_the_run(self):
        self.plan()
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "run")
        code, out = self.ap("finish", "--result", "Бот принимает заявки")
        s = self.state()
        self.assertTrue(s["finishedAt"])
        self.assertFalse(s["dir"].endswith("--wip"))
        self.assertTrue(os.path.isdir(os.path.join(self.a, s["dir"])))
        self.assertTrue(all(x["status"] in ("done", "skipped") for x in s["stages"]))
        self.assertIn("| сдан | Бот принимает заявки |", read(os.path.join(self.a, "README.md")))
        self.assertEqual(s["result"], "Бот принимает заявки")
        self.assertIn("R", self.git("status", "--short").stdout)    # переименование через git mv

    def test_reopen_puts_wip_back(self):
        self.init()
        self.ap("finish", "--result", "готово")
        self.ap("reopen")
        s = self.state()
        self.assertTrue(s["dir"].endswith("--wip"))
        self.assertIsNone(s["finishedAt"])
        self.assertEqual(self.stage("manifest")["status"], "active")
        self.assertEqual(self.stage("final")["status"], "pending")
        self.assertNotIn("finishedAt", self.stage("final"))

    def test_commits_flags_a_done_ticket_whose_commit_left_history(self):
        self.plan()
        self.ap("tickets")
        commit = lambda m: self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", m)
        commit("T01")
        kept = self.git("rev-parse", "--short", "HEAD").stdout.strip()
        commit("T02")
        lost = self.git("rev-parse", "--short", "HEAD").stdout.strip()
        self.ap("ticket", "01", "done", "--commit", kept)
        self.ap("ticket", "02", "done", "--commit", lost)
        events = len(self.state()["log"])
        code, out = self.ap("commits")
        self.assertIn("коммиты сданных задач на месте", out)
        self.git("reset", "-q", "--hard", kept)
        code, out = self.ap("commits")
        self.assertEqual(code, 0, out)
        self.assertIn("! задача 02: коммита %s нет в истории" % lost, out)
        self.assertNotIn("задача 01", out)
        self.assertEqual(self.state()["tickets"][1]["status"], "done")   # сбрасывает оркестратор
        self.assertEqual(len(self.state()["log"]), events)               # проверка — не событие


class Pure(unittest.TestCase):
    """Функции без процесса: опознание своего сервера и версия навыка."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("ap", AP)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def test_is_ours(self):
        a = self.m.A
        self.assertTrue(self.m.is_ours("/usr/bin/python3 -m http.server 8000 --bind 127.0.0.1 --directory %s" % a))
        self.assertFalse(self.m.is_ours("python3 -m http.server 8000 --directory %s2" % a))
        self.assertFalse(self.m.is_ours("node http-server --directory %s" % a))

    def test_version(self):
        self.assertEqual(self.m._version('---\nname: x\nmetadata:\n  version: "2.0.1"\n---\nversion: 9'), (2, 0, 1))
        self.assertIsNone(self.m._version("---\nname: x\n---\nversion: 3\n"))

    def test_skill_declares_a_version(self):
        text = read(os.path.join(SKILL, "SKILL.md"))
        self.assertIsNotNone(self.m._version(text))

    def test_skill_version_next_to_the_packaged_ap_py(self):
        text = read(os.path.join(SKILL, "SKILL.md"))
        want = re.search(r'^\s*version:\s*"([^"]+)"', text, re.M).group(1)
        self.assertEqual(self.m.skill_version(None), want)          # tools/ap.py → ../SKILL.md
        self.assertEqual(self.m.skill_version(SKILL), want)

    def test_mode_words(self):
        n = self.m.norm_mode
        self.assertEqual([n(x) for x in ("auto", "important", "interview", "manual", "full", "semi", " Semi ")],
                         ["auto", "important", "interview", "manual", "auto", "important", "important"])
        self.assertIsNone(n("turbo"))

    def test_no_task_slang_in_what_ap_py_prints(self):
        """«Таск» пользователю не пишется: ни в одной строке, которую ap.py может вывести."""
        import tokenize
        with open(AP, encoding="utf-8") as f:
            toks = list(tokenize.generate_tokens(f.readline))
        bad = [t.start[0] for t in toks
               if t.type == tokenize.STRING and "таск" in t.string.lower()]
        self.assertEqual(bad, [], "строки ap.py со словом «таск»: %s" % bad)


class Words(unittest.TestCase):
    """Слова режимов в SKILL.md и фазах — как в AISpa 0.5, старые понимаются."""

    def test_skill_names_the_four_modes(self):
        text = read(os.path.join(SKILL, "SKILL.md"))
        self.assertIn("auto|important|interview|manual", text)
        for label in ("сам по брифу", "только важное", "интервью", "ручной"):
            self.assertIn(label, text)

    def test_modes_phase_has_the_start_choice_and_old_words(self):
        text = read(os.path.join(SKILL, "phases", "0-modes.md"))
        for label in ("1. Сам по брифу", "2. Только важное", "3. Интервью", "4. Ручной"):
            self.assertIn(label, text)
        for old in ("`full`", "`semi`", "«полный автомат»", "«полуавтомат»"):
            self.assertIn(old, text)

    def test_one_question_at_a_time_is_offered_and_documented(self):
        modes = read(os.path.join(SKILL, "phases", "0-modes.md"))
        self.assertIn("• «по одному»", modes)
        self.assertIn("set questions=one", modes)
        briefing = read(os.path.join(SKILL, "phases", "2-briefing.md"))
        for rule in ("One question at a time", "interview.md", "«Дальше решай сам»", "⚑"):
            self.assertIn(rule, briefing)
        self.assertNotIn("three to six", briefing)
        self.assertIn("«По одному» and nothing else", modes)
        self.assertIn("\"Round\" in the one-at-a-time form", briefing)
        self.assertIn("«3б, остальное ок»", briefing)
        self.assertIn("`interview.md` if the active stage is briefing", read(os.path.join(SKILL, "phases", "0-resume.md")))
        self.assertIn("becomes a story at every depth", read(os.path.join(SKILL, "phases", "3-spec.md")))
        readme = os.path.join(os.path.dirname(SKILL), "..")
        self.assertIn("«по одному»", read(os.path.join(readme, "README.ru.md")))
        self.assertIn("\"one at a time\"", read(os.path.join(readme, "README.md")))

    def test_phases_do_not_name_modes_the_old_way(self):
        old = re.compile(r"\bsemi\b|\bin full\b(?! check)|\bfull mode\b|\bfull:|^\*\*full\b|\| full \|"
                         r"|«полный автомат —|полуавтомат", re.I | re.M)
        for name in sorted(os.listdir(os.path.join(SKILL, "phases"))) + ["../SKILL.md"]:
            if not name.endswith(".md") or name == "0-modes.md":
                continue
            text = read(os.path.join(SKILL, "phases", name))
            text = text.replace("show it in full", "")
            # строка соответствия старых слов новым — единственное место, где они названы
            text = "\n".join(l for l in text.split("\n") if not ("`full`" in l and "`auto`" in l))
            self.assertEqual(old.findall(text), [], name)


if __name__ == "__main__":
    unittest.main()
