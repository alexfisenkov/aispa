"""Тесты skills/aispa/tools/ap.py — учёта прогона AISpa.

Запуск из корня репозитория:  python3 -m unittest discover -s tests
Каждый тест поднимает временный git-репозиторий с .aispa-work/ и зовёт ap.py
как оркестратор — отдельным процессом, без сервера (--no-serve).
"""

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

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

BRIEF = """# Изначальная задача

Бот принимает заявки и складывает в таблицу, плюс SMS, в цветах студии.

## Дополнения

- 2026-10-01 — «SMS не надо, только телега»
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
        self.data = tempfile.mkdtemp(prefix="ap-data-")      # каталог данных: не настоящий домашний
        self.history = os.path.join(self.data, "aispa", "history.jsonl")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.root] + list(args), capture_output=True, text=True)

    def ap(self, *args):
        env = dict(os.environ, AISPA_NO_UPDATE_CHECK="1", XDG_DATA_HOME=self.data, LOCALAPPDATA=self.data)
        r = subprocess.run([sys.executable, os.path.join(self.a, "ap.py")] + list(args) + ["--no-serve"],
                           capture_output=True, text=True, env=env, cwd=self.root)
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

    def test_gitignore_holds_only_the_serve_files_once(self):
        self.init()
        self.ap("finish", "--result", "готово")
        code, out = self.ap("init", "--slug", "second")
        self.assertEqual(code, 0, out)
        text = read(os.path.join(self.root, ".gitignore"))
        self.assertEqual([line for line in text.splitlines() if line], [".aispa-work/serve.*"], text)

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
        self.write("2026-10-01-brief.md", BRIEF)
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
        self.assertIn("в волне 2, но его зависимости позволяют не раньше 3", out)
        self.assertIn("таски 02 и 03 в одной волне пишут в src/bot/sheets/", out)
        self.assertIn("таск 04 ни к чему не привязан", out)
        self.assertIn("у таска 04 нет зоны", out)

    def test_check_plan_catches_a_requirement_in_no_ticket(self):
        self.plan()
        self.write("manifest.md", MANIFEST + "| G01 | «отмена» | in-spec | — | spec §5 |\n")
        code, out = self.ap("check-plan")
        self.assertIn("требование G01", out)

    def dropped_lines(self, out):
        return [line for line in out.splitlines() if line.lstrip().startswith("!") and "R03" in line]

    def test_check_plan_accepts_a_drop_quoted_from_any_brief_file(self):
        self.plan()
        self.write("2026-10-01-brief.md", "# Изначальная задача\n\nБот для заявок, SMS тоже.\n")
        self.write("2026-10-02-brief.md", "# Изначальная задача\n\nА SMS — не надо, только телега.\n")
        code, out = self.ap("check-plan")
        self.assertEqual(self.dropped_lines(out), [], out)
        self.assertIn("G3: трасса сходится", out)

    def test_check_plan_flags_a_drop_whose_quote_is_not_in_the_brief(self):
        self.plan()
        self.write("2026-10-01-brief.md", "# Изначальная задача\n\nБот для заявок.\n\n## Дополнения\n\n- 2026-10-01 — «телега»\n")
        code, out = self.ap("check-plan")
        self.assertEqual(len(self.dropped_lines(out)), 1, out)
        self.assertFalse(self.state()["gates"]["G3"]["ok"])

    def test_check_plan_flags_a_drop_without_a_quote(self):
        self.plan()
        self.write("manifest.md", MANIFEST.replace("| dropped | «не надо» |", "| dropped | пользователь отказался |"))
        code, out = self.ap("check-plan")
        self.assertEqual(len(self.dropped_lines(out)), 1, out)

    def test_done_closes_rows_only_when_all_their_tickets_are_done(self):
        self.plan()
        self.ap("tickets")
        self.ap("ticket", "01", "start")
        self.ap("ticket", "01", "done", "--tests", "3/0", "--commit", "1111111aaaa", "--placeholder", "R05")
        m = self.manifest()
        self.assertRegex(m, r"\| R01 \|[^\n]*\| in-ticket \|")     # ещё и в таске 02
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
        self.write("manifest.md", MANIFEST + "| D01 | схема не держит два адреса | in-spec | таск 02 | spec §3 |\n")
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

    # ── история времени ────────────────────────────────────────────────────
    def read_history(self):
        with open(self.history, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_finish_appends_one_anonymous_history_row(self):
        self.plan()
        self.ap("tickets")
        self.ap("set", "tier=T2")
        self.ap("stage", "build")
        self.ap("ticket", "01", "02", "start")
        self.ap("ticket", "01", "02", "done")
        self.ap("stage", "review")
        self.ap("stage", "final")
        self.ap("finish", "--result", "Бот принимает заявки")
        rows = self.read_history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(rows[0]), ["finalMin", "reviewMin", "ticketMin", "tickets", "tier"])
        self.assertEqual(rows[0]["tier"], "T2")
        self.assertEqual(rows[0]["tickets"], 2)
        self.assertEqual(len(rows[0]["ticketMin"]), 2)
        self.assertTrue(all(isinstance(x, int) and x >= 1 for x in rows[0]["ticketMin"]))
        raw = read(self.history)
        for private in ("repair-bot", "Бот", "Каркас", "Приём", "wip", self.root, "R01", "src/"):
            self.assertNotIn(private, raw)

    def test_finish_history_leaves_skipped_stages_empty(self):
        self.init()
        self.ap("finish", "--result", "готово")
        row = self.read_history()[0]
        self.assertEqual((row["tickets"], row["ticketMin"], row["reviewMin"], row["finalMin"]),
                         (0, [], None, None))

    def history_rows(self, rows, raw=None):
        os.makedirs(os.path.dirname(self.history), exist_ok=True)
        with open(self.history, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else "".join(json.dumps(r) + "\n" for r in rows))

    def test_init_puts_history_medians_in_state(self):
        row = lambda t, r, f: {"tier": "T2", "tickets": len(t), "ticketMin": t, "reviewMin": r, "finalMin": f}
        self.history_rows([row([10, 20, 30], 12, 4), row([40], None, 6), row([], 20, None)])
        self.init()
        h = self.state()["history"]
        self.assertEqual(h["runs"], 3)
        self.assertEqual(h["ticket"], {"median": 30, "lo": 10, "hi": 40})      # 10 20 30 40
        self.assertEqual(h["review"], {"median": 20, "lo": 12, "hi": 20})      # 12 20
        self.assertEqual(h["final"], {"median": 6, "lo": 4, "hi": 6})          # 4 6

    def test_init_without_usable_history_adds_no_field(self):
        self.init()                                                  # файла нет
        self.assertNotIn("history", self.state())
        self.ap("finish", "--result", "готово")
        os.remove(self.history)
        for raw in ("", "\n\n", "{оборван\n\x00\xff не json\n[1, 2]\n"):
            self.history_rows(None, raw)
            code, out = self.ap("init", "--slug", "again", "--force")
            self.assertEqual(code, 0, out)
            self.assertNotIn("history", self.state(), repr(raw))
            self.ap("finish", "--result", "готово")
            os.remove(self.history)

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
        self.assertIn("коммиты сданных тасков на месте", out)
        self.git("reset", "-q", "--hard", kept)
        code, out = self.ap("commits")
        self.assertEqual(code, 0, out)
        self.assertIn("! таск 02: коммита %s нет в истории" % lost, out)
        self.assertNotIn("таск 01", out)
        self.assertEqual(self.state()["tickets"][1]["status"], "done")   # сбрасывает оркестратор
        self.assertEqual(len(self.state()["log"]), events)               # проверка — не событие


# ── честное время на панели: поведение шаблона через node ──────────────────
NODE = shutil.which("node")
ETA_TOOL = os.path.join(os.path.dirname(HERE), "tools", "eta-honesty.cjs")
HARNESS = """
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const block = html.match(/\\/\\*ETA-BEGIN\\*\\/([\\s\\S]*?)\\/\\*ETA-END\\*\\//)[1];
const cap = +html.match(/const IDLE_CAP_MS = (\\d+)/)[1];
const etaRange = new Function('IDLE_CAP_MS', block + '; return etaRange;')(cap);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(etaRange(input.state, input.now)));
"""
HIST = {"runs": 3, "ticket": {"median": 20, "lo": 10, "hi": 40},
        "review": {"median": 15, "lo": 10, "hi": 30}, "final": {"median": 5, "lo": 3, "hi": 8}}


def at(h, m=0):
    return "2026-10-01T%02d:%02d:00+00:00" % (h + m // 60, m % 60)


def eta_state(tickets, history=None, stage="build"):
    t0 = at(10)
    st = [{"id": "build", "status": "active", "startedAt": t0}] if stage == "build" else [
        {"id": "build", "status": "done", "startedAt": t0, "finishedAt": at(10, 30)},
        {"id": stage, "status": "active", "startedAt": at(10, 30)}]
    s = {"startedAt": t0, "updatedAt": t0, "finishedAt": None, "stages": st, "tickets": tickets,
         "waits": [], "waiting": None, "beats": [t0]}
    if history:
        s["history"] = history
    return s


def tk(tid, status, start=None, finish=None, blocked=()):
    t = {"id": tid, "status": status, "blockedBy": list(blocked)}
    if start:
        t["startedAt"] = start
    if finish:
        t["finishedAt"] = finish
    return t


@unittest.skipUnless(NODE, "нужен node")
class Eta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ap-eta-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.harness = os.path.join(self.tmp, "harness.cjs")
        with open(self.harness, "w", encoding="utf-8") as f:
            f.write(HARNESS)

    def eta(self, state, minute):
        now = int(__import__("datetime").datetime.fromisoformat(at(10, minute)).timestamp() * 1000)
        r = subprocess.run([NODE, self.harness, TEMPLATE], input=json.dumps({"state": state, "now": now}),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_no_measurements_says_dont_know_yet(self):
        r = self.eta(eta_state([tk("01", "pending"), tk("02", "pending", blocked=["01"])]), 5)
        self.assertTrue(r["unknown"])
        self.assertNotIn("lo", r)

    def test_history_gives_the_range(self):
        # цепочка из двух шагов: 10+10 и 40+40, потом проверка 10..30 и приёмка 3..8
        r = self.eta(eta_state([tk("01", "pending"), tk("02", "pending", blocked=["01"])], HIST), 5)
        self.assertEqual((r["lo"], r["hi"], r["late"]), (33, 118, False))

    def test_own_measurements_beat_history(self):
        done = [tk("01", "done", at(10), at(10, 10)), tk("02", "done", at(10, 10), at(10, 30)),
                tk("03", "pending")]
        r = self.eta(eta_state(done, HIST), 30)
        self.assertEqual((r["lo"], r["hi"]), (10 + 10 + 3, 20 + 30 + 8))      # 10 20: свой разброс; хвост из истории

    def test_range_is_unknown_when_the_tail_has_no_history(self):
        own = {"runs": 1, "ticket": {"median": 20, "lo": 10, "hi": 40}}
        r = self.eta(eta_state([tk("01", "pending")], own), 5)
        self.assertTrue(r["unknown"])

    def test_a_ticket_over_one_and_a_half_medians_is_slow(self):
        tickets = [tk("01", "in-progress", at(10)), tk("02", "in-progress", at(10, 20))]
        r = self.eta(eta_state(tickets, HIST), 40)        # 40 мин против 30; 20 мин против 30
        self.assertEqual(r["slow"], ["01"])
        r = self.eta(eta_state(tickets, HIST), 30)        # ровно 30 — ещё не дольше
        self.assertEqual(r["slow"], [])

    def test_review_stage_counts_only_what_is_left(self):
        s = eta_state([tk("01", "done", at(10), at(10, 30))], HIST, stage="review")
        r = self.eta(s, 40)                               # проверка идёт 10 мин
        self.assertEqual((r["lo"], r["hi"], r["late"]), (0 + 3, 20 + 8, True))

    def test_the_formula_holds_no_invented_numbers(self):
        html = read(TEMPLATE)
        block = html[html.index("/*ETA-BEGIN*/"):html.index("/*ETA-END*/")]
        self.assertEqual(re.findall(r"\|\| *10|0\.[0-9]+ *\*|\* *0\.[0-9]|\+ *8\b|1\.4|0\.75", block), [])
        self.assertIn("2700000", html)
        self.assertEqual(len(re.findall(r"token|токен", html, re.I)), 0)

    def finished_run(self, history):
        s = {"startedAt": at(10), "updatedAt": at(10, 50), "finishedAt": at(10, 50), "waits": [], "waiting": None,
             "beats": [at(10), at(10, 15), at(10, 30), at(10, 45), at(10, 50)],
             "stages": [{"id": "build", "status": "done", "startedAt": at(10), "finishedAt": at(10, 30)},
                        {"id": "review", "status": "done", "startedAt": at(10, 30), "finishedAt": at(10, 45)},
                        {"id": "final", "status": "done", "startedAt": at(10, 45), "finishedAt": at(10, 50)}],
             "tickets": [tk("01", "done", at(10), at(10, 15)), tk("02", "done", at(10, 15), at(10, 30), ["01"])],
             "slug": "private-slug", "title": "Private title"}
        if history:
            s["history"] = {"runs": 3, "ticket": {"median": 15, "lo": 10, "hi": 20},
                            "review": {"median": 10, "lo": 8, "hi": 12}, "final": {"median": 5, "lo": 3, "hi": 7}}
        path = os.path.join(self.tmp, ("with" if history else "without") + "-history-state.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write("window.STATE =\n" + json.dumps(s, ensure_ascii=False) + "\n")
        return path

    def test_honesty_tool_prints_only_counts(self):
        # Четыре момента с историей: 31..59 при 50 (внутри), 21..39 при 35 (внутри),
        # 11..19 при 20 (мимо), 3..7 при 5 (внутри). Без истории — «пока не знаю» везде.
        r = subprocess.run([NODE, ETA_TOOL, self.finished_run(True), self.finished_run(False)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "точек с диапазоном: 4\nпопало внутрь: 3\nпока не знаю: 4\n")
        for private in ("private", "Private", self.tmp, "state.js"):
            self.assertNotIn(private, r.stdout)


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

    def test_update_line_names_aispa_and_asks_for_a_yes(self):
        skill = tempfile.mkdtemp(prefix="ap-skill-")
        self.addCleanup(shutil.rmtree, skill, True)
        with open(os.path.join(skill, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write('---\nname: aispa\nmetadata:\n  version: "1.0.0"\n---\n')
        remote = io.BytesIO(b'---\nname: aispa\nmetadata:\n  version: "1.2.0"\n---\n')
        asked = []

        def online(url, timeout=None):
            asked.append(url)
            return remote

        def offline(url, timeout=None):
            raise urllib.error.URLError("нет сети")

        env = {k: v for k, v in os.environ.items() if k != "AISPA_NO_UPDATE_CHECK"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(self.m.urllib.request, "urlopen", online):
                line = self.m.check_update({"skillDir": skill})
            with mock.patch.object(self.m.urllib.request, "urlopen", offline):
                silent = self.m.check_update({"skillDir": skill})
        self.assertEqual(line, "вышла AISpa 1.2.0 (у тебя 1.0.0) — скажи «обнови AISpa», поставлю после твоего «да»")
        self.assertEqual(asked, ["https://raw.githubusercontent.com/alexfisenkov/aispa/main/skills/aispa/SKILL.md"])
        self.assertIsNone(silent)


if __name__ == "__main__":
    unittest.main()
