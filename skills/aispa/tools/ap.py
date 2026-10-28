#!/usr/bin/env python3
"""Учёт прогона AISpa: состояние, дашборд, сервер — одной командой на событие.

Агент не правит .aispa-work/state.js руками. Каждое событие прогона — один вызов:

    python3 .aispa-work/ap.py init --slug S --title "…" --mode important --depth balanced \\
                                  --memory-file AGENTS.md --memory-owner aispa --skill-dir /abs
    python3 .aispa-work/ap.py stage spec                 # открыть этап (прошлые закроются сами)
    python3 .aispa-work/ap.py stage briefing skip --note "сам по брифу — самобрифинг"
    python3 .aispa-work/ap.py set tier=T2 baseCommit=a1b2c3d
    python3 .aispa-work/ap.py set mode=manual            # режим сменили по ходу (full/semi тоже понимаются)
    python3 .aispa-work/ap.py set questions=one          # «спрашивай по одному»; обратно — questions=rounds
    python3 .aispa-work/ap.py tickets                    # опубликовать задачи из <dir>/tickets/*.md
    python3 .aispa-work/ap.py check-plan                 # гейт G3: трасса манифест ↔ задачи
    python3 .aispa-work/ap.py ticket 02 03 start         # волна ушла
    python3 .aispa-work/ap.py ticket 02 review
    python3 .aispa-work/ap.py ticket 02 repair --note "пустой адрес проходит — R01.1"
    python3 .aispa-work/ap.py ticket 02 done --tests 34/0 --commit a1b2c3d
    python3 .aispa-work/ap.py ticket 03 retry | fail --note "…" | reset
    python3 .aispa-work/ap.py stage build done | fail --note "…"
    python3 .aispa-work/ap.py add concerns "src/notify.ts:40 — два формата даты"
    python3 .aispa-work/ap.py add debt.emptyEnv TELEGRAM_BOT_TOKEN
    python3 .aispa-work/ap.py coverage found=2 fixed=1 deferred=1 --item "R07 — отложено: …"
    python3 .aispa-work/ap.py add report "Два формата даты в уведомлениях — оставлено"
    python3 .aispa-work/ap.py blind checked=12 matched=11 --mismatch "R07 — статус не виден"
    python3 .aispa-work/ap.py tests 34/0                 # последний полный прогон
    python3 .aispa-work/ap.py ask briefing --note "три вопроса в чате"   # ждём пользователя
    python3 .aispa-work/ap.py answered                   # ответил (stage/ticket закрывают сами)
    python3 .aispa-work/ap.py finish --result "Бот принимает заявки и пишет их в таблицу"
    python3 .aispa-work/ap.py reopen                     # второй бриф в сданном прогоне
    python3 .aispa-work/ap.py commits                    # коммиты сданных задач ещё в истории?
    python3 .aispa-work/ap.py                            # просто синхронизировать

После каждой команды: updatedAt = сейчас (и метка в журнале beats), строка в ленте log,
счётчики и список требований — из manifest.md, гейты G1–G4 — из событий, которые их решают,
пройденные этапы закрыты, снимок вписан в dashboard.html, сервер жив. Время
ставит сам скрипт, с секундами и поясом, — агент его не пишет никогда.

Флаги: --check-update (сверить версию навыка с GitHub), --no-serve, --stop.
Работает на macOS, Linux и Windows (в том числе из Git Bash).
"""

import datetime as dt
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):                       # cp1251-консоль Windows
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

A = os.path.dirname(os.path.abspath(__file__))          # .aispa-work этого проекта
ROOT = os.path.dirname(A)
STATE = os.path.join(A, "state.js")
PAGE = os.path.join(A, "dashboard.html")
README = os.path.join(A, "README.md")
PIDF = os.path.join(A, "serve.pid")
LOG = os.path.join(A, "serve.log")
BEGIN, END = "/*STATE-BEGIN*/", "/*STATE-END*/"
WIN = os.name == "nt"
REPO = "alexfisenkov/aispa"
REMOTE_SKILL = "https://raw.githubusercontent.com/%s/main/skills/aispa/SKILL.md" % REPO
STOP_DELAY = 12
BEATS_KEPT = 2000
LOG_KEPT = 300
QUOTE_MAX = 240
WAIT_KINDS = ("briefing", "spec", "plan", "action", "stop")
# Команды, которые значат «работа пошла дальше»: ожидание пользователя ими закрывается,
# даже если `answered` забыли.
MOVES_ON = {"stage", "ticket", "tickets", "check-plan", "coverage", "blind", "tests", "finish", "reopen"}
# Режимы — имена AISpa 0.5 плюс ручной; старые слова (full, semi) понимаются и хранятся новыми.
MODES = ("auto", "important", "interview", "manual")
OLD_MODES = {"full": "auto", "semi": "important"}
# Глубина хранится как strict | normal | deep (так читают старые прогоны и панель); пользователю —
# «Быстро / Сбалансированно / Максимум». Новые и старые слова, русские в том числе, сводятся к этим трём.
DEPTHS = ("strict", "normal", "deep")
DEPTH_WORDS = {
    "fast": "strict", "быстро": "strict", "быстрая": "strict", "строго": "strict", "строго по брифу": "strict",
    "balanced": "normal", "сбалансированно": "normal", "сбалансированная": "normal",
    "сбалансированный": "normal", "обычная": "normal", "обычно": "normal",
    "max": "deep", "maximum": "deep", "максимум": "deep", "максимальная": "deep", "максимально": "deep",
    "по максимуму": "deep", "проработка по максимуму": "deep",
    "глубоко": "deep", "глубокая": "deep", "глубокий": "deep",
}
# Как задаются вопросы брифинга: rounds — раундами до четырёх, one — по одному в сообщении.
# Хранится в state.js, чтобы выбор пользователя пережил сжатие контекста и продолжение.
QUESTION_FORMS = ("rounds", "one")
QUESTION_WORDS = {
    "round": "rounds", "раундами": "rounds", "пачкой": "rounds", "пачками": "rounds", "по несколько": "rounds",
    "по одному": "one", "по-одному": "one", "одному": "one", "one at a time": "one",
}
ORDER = ["preflight", "manifest", "briefing", "spec", "plan", "build", "review", "final"]
# Этап, чей результат лежит на диске, пройден — даже если его забыли отметить.
ARTIFACT = {"manifest": "manifest.md", "spec": "spec.md", "plan": "tickets/*.md"}
TICKET_STATUSES = {"start": "in-progress", "review": "review", "repair": "repair",
                   "done": "done", "fail": "failed", "retry": "in-progress", "reset": "pending"}
LISTS = {"concerns", "additions", "report", "debt.placeholders", "debt.assumptions", "debt.emptyEnv"}


def now():
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def die(msg):
    print("ошибка: " + msg)
    sys.exit(1)


# ── состояние ───────────────────────────────────────────────────────────────

def read_state():
    try:
        raw = open(STATE, encoding="utf-8").read()
    except FileNotFoundError:
        return None
    body = raw.split("=", 1)[1] if "=" in raw.split("\n", 1)[0] else raw
    try:
        return json.loads(body.strip().rstrip(";"))
    except json.JSONDecodeError as e:
        die("state.js не разбирается (строка %d: %s) — перезапиши его командой init "
            "или поправь строку" % (e.lineno, e.msg))


def save(state):
    state["rev"] = (state.get("rev") or 0) + 1        # дашборд перерисовывается по нему
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("window.STATE =\n" + json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, STATE)


def beat(state):
    """Журнал пульса: каждое событие оставляет метку. updatedAt перезаписывается,
    и без журнала дашборд знал о прошлых событиях, только если сам их видел, —
    уснул экран или упал сервер, и рабочее время между редкими метками
    обрезалось как простой."""
    beats = state.setdefault("beats", [])
    if state["updatedAt"] not in beats:
        beats.append(state["updatedAt"])
    del beats[:-BEATS_KEPT]


def log(state, event, **kw):
    """Лента событий для дашборда: что случилось, словами страницы, а не агента.
    Пишет скрипт сам, по команде, — от агента ничего сверх обычного вызова не нужно."""
    entry = {"t": state["updatedAt"], "k": event}
    entry.update({k: v for k, v in kw.items() if v not in (None, "", [])})
    state.setdefault("log", []).append(entry)
    del state["log"][:-LOG_KEPT]


def set_gate(state, g, ok, **kw):
    """Гейт решается событием, которое его проверяет: G1 — открытием спецификации,
    G2 — coverage, G3 — check-plan, G4 — blind. Повторная проверка перезаписывает."""
    rec = {"ok": bool(ok), "at": state["updatedAt"]}
    rec.update({k: v for k, v in kw.items() if v is not None})
    state.setdefault("gates", {})[g] = rec
    log(state, "gate", g=g, ok=bool(ok), **kw)


def close_wait(state):
    """Ожидание пользователя кончилось: интервал уходит в waits — часы дашборда
    его не считают, это не работа сборки."""
    w = state.get("waiting")
    if not w:
        return
    state.setdefault("waits", []).append({"kind": w.get("kind"), "from": w.get("since"),
                                          "to": state["updatedAt"]})
    state["waiting"] = None
    log(state, "answered", kind=w.get("kind"))


def run_dir(state):
    return os.path.join(A, state.get("dir") or "")


def norm_mode(v):
    """auto|important|interview|manual; full → auto, semi → important; иначе None."""
    v = str(v or "").strip().lower()
    v = OLD_MODES.get(v, v)
    return v if v in MODES else None


def mode_of(v):
    m = norm_mode(v)
    if m is None:
        die("режим: %s (старые full → auto, semi → important), а не «%s»"
            % (" | ".join(MODES), v))
    return m


def depth_of(v):
    """strict|normal|deep; fast → strict, balanced → normal, max → deep (и русские слова)."""
    d = str(v or "").strip().lower()
    d = DEPTH_WORDS.get(d, d)
    if d not in DEPTHS:
        die("глубина: fast | balanced | max (хранится как %s), а не «%s»" % (" | ".join(DEPTHS), v))
    return d


def questions_of(v):
    """rounds|one; «по одному», "one at a time" → one, «раундами», «пачкой» → rounds."""
    q = str(v or "").strip().lower()
    q = QUESTION_WORDS.get(q, q)
    if q not in QUESTION_FORMS:
        die("вопросы: rounds (раундами) | one (по одному), а не «%s»" % v)
    return q


def fresh_state(a):
    t = now()
    return {
        "version": 2,
        "slug": a["slug"], "dir": "%s-%s--wip" % (t[:10], a["slug"]),
        "title": a.get("title") or a["slug"],
        "mode": mode_of(a.get("mode") or "important"), "depth": depth_of(a.get("depth") or "normal"),
        "questions": questions_of(a["questions"]) if a.get("questions") else None,  # None — форму ещё не спрашивали
        "tier": None, "briefFile": a.get("brief-file"),
        "memoryFile": a.get("memory-file") or "AGENTS.md",
        "memoryOwner": a.get("memory-owner") or "aispa",
        "skillDir": a.get("skill-dir"), "skillVersion": skill_version(a.get("skill-dir")),
        "baseCommit": None,
        "startedAt": t, "updatedAt": t, "finishedAt": None,
        "stages": [{"id": "preflight", "status": "active", "startedAt": t}]
                  + [{"id": s, "status": "pending"} for s in ORDER[1:]],
        "requirements": {"total": 0, "done": 0, "inTicket": 0, "inSpec": 0,
                         "placeholder": 0, "deferred": 0, "dropped": 0},
        "tickets": [], "tests": None,
        "debt": {"placeholders": [], "assumptions": [], "emptyEnv": []},
        "additions": [], "coverage": None, "concerns": [], "blind": None, "beats": [t],
        "rev": 0, "waiting": None, "waits": [], "log": [], "reqList": [], "gates": {}, "result": None,
    }


# ── манифест и задачи ────────────────────────────────────────────────────────

ROW_ID = re.compile(r"^[RGD]\d+[a-z]?$")
PIPE = re.compile(r"(?<!\\)\|")          # «\|» внутри цитаты — не граница колонки


def cells_of(line):
    return PIPE.split(line.strip().strip("|"))
STATUS_KEY = {"done": "done", "in-ticket": "inTicket", "in-spec": "inSpec",
              "placeholder": "placeholder", "deferred": "deferred", "dropped": "dropped"}


def plain(cell):
    """Ячейка манифеста как текст для страницы: без разметки, «\\|» — обратно в «|»."""
    t = re.sub(r"\*+|`", "", cell.replace("\\|", "|")).strip()
    return t if len(t) <= QUOTE_MAX else t[:QUOTE_MAX - 1].rstrip() + "…"


def manifest_table(state):
    """Строки manifest.md: ID в первой колонке, цитата во второй, статус в третьей,
    основание в четвёртой."""
    try:
        text = open(os.path.join(run_dir(state), "manifest.md"), encoding="utf-8").read()
    except OSError:
        return None
    rows = []
    for line in text.splitlines():
        raw = cells_of(line)
        cells = [c.strip().strip("`*") for c in raw]
        if len(cells) >= 3 and ROW_ID.match(cells[0]):
            rows.append({"id": cells[0], "text": plain(raw[1]) if len(raw) > 1 else "",
                         "status": cells[2].split()[0].lower() if cells[2] else "open",
                         "why": plain(raw[3]) if len(raw) > 3 else ""})
    return rows


def manifest_rows(state):
    """[(id, status)] из таблицы manifest.md."""
    rows = manifest_table(state)
    return None if rows is None else [(r["id"], r["status"]) for r in rows]


def recount(state):
    table = manifest_table(state)
    if table is None:
        return
    # Список для страницы — слова пользователя и что с ними стало. Основание нужно
    # только там, где требование не сделано как написано.
    state["reqList"] = [dict({"id": r["id"], "text": r["text"], "status": r["status"]},
                             **({"why": r["why"]} if r["status"] in ("placeholder", "deferred", "dropped")
                                and r["why"] not in ("", "—", "-") else {}))
                        for r in table if not r["id"].startswith("D")]
    rows = [(r["id"], r["status"]) for r in table]
    rows = [(i, st) for i, st in rows if not i.startswith("D")]   # D## — ограничение, не требование
    r = {"total": len(rows), "done": 0, "inTicket": 0, "inSpec": 0,
         "placeholder": 0, "deferred": 0, "dropped": 0}
    for _, st in rows:
        if st in STATUS_KEY:
            r[STATUS_KEY[st]] += 1
    state["requirements"] = r


def manifest_update(state, changes):
    """Переписывает статус (третья колонка) и «Где» (пятая) у строк манифеста.
    changes: {id: (статус, добавка к «Где»)}. Возвращает список изменённых id."""
    path = os.path.join(run_dir(state), "manifest.md")
    try:
        lines = open(path, encoding="utf-8").read().split("\n")
    except OSError:
        return []
    touched = []
    for n, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        cells = cells_of(line)
        rid = cells[0].strip().strip("`*") if cells else ""
        if rid not in changes or len(cells) < 3:
            continue
        status, where = changes[rid]
        cells[2] = " %s " % status
        if where and len(cells) >= 5:
            cur = cells[4].strip()
            cells[4] = " %s " % (where if cur in ("", "—", "-") else
                                 cur if where in cur else "%s → %s" % (cur, where))
        lines[n] = "|" + "|".join(cells) + "|"
        touched.append(rid)
    if touched:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    return touched


FIELD = re.compile(r"^\*\*([^*:]+):?\*\*:?\s*(.*)$")


def parse_ticket(path):
    text = open(path, encoding="utf-8").read()
    base = os.path.basename(path)
    tid = re.match(r"(\w+?)[-_.]", base)
    tid = tid.group(1) if tid else base[:2]
    title = ""
    f = {}
    for line in text.splitlines():
        s = line.strip()
        if not title and s.startswith("# "):
            title = re.sub(r"^#\s*\w+\s*[—–-]\s*", "", s).strip() or s[2:]
        m = FIELD.match(s)
        if m:
            f[m.group(1).strip().lower()] = m.group(2).strip()

    def ids(v):
        return [x for x in re.findall(r"[A-Za-z]*\d+[a-z]?(?:\.\d+)?", v or "")]

    def get(*names):
        for n in names:
            if n in f:
                return f[n]
        return ""
    wave = re.search(r"\d+", get("волна", "wave"))
    return {
        "id": tid, "title": title,
        "requirements": ids(get("требования", "requirements")),
        "blockedBy": [x.zfill(2) for x in re.findall(r"\d+", get("зависит от", "blocked by"))],
        "wave": int(wave.group()) if wave else None,
        "zone": re.findall(r"`([^`]+)`", get("зона", "zone")),
        "review": get("ревью", "review") or None,
        "model": get("модель", "model") or None,
    }


def publish_tickets(state):
    files = sorted(glob.glob(os.path.join(run_dir(state), "tickets", "*.md")))
    if not files:
        return "в %s/tickets/ нет файлов" % state.get("dir")
    old = {t["id"]: t for t in state.get("tickets") or []}
    out = []
    for p in files:
        t = parse_ticket(p)
        prev = old.get(t["id"], {})
        for k in ("status", "startedAt", "finishedAt", "retries", "repairs", "repairFindings",
                  "tests", "commit", "concerns"):
            if k in prev:
                t[k] = prev[k]
        t.setdefault("status", "pending")
        t.setdefault("retries", 0)
        t.setdefault("repairs", 0)
        out.append(t)
    state["tickets"] = out
    rows = dict(manifest_rows(state) or [])
    where = {}
    for t in out:
        for r in {x.split(".")[0] for x in t["requirements"]}:
            where.setdefault(r, []).append("T" + t["id"])
    moved = manifest_update(state, {r: ("in-ticket", ", ".join(w)) for r, w in where.items()
                                    if rows.get(r) == "in-spec"})
    return "задач опубликовано: %d%s" % (len(out), (" · в манифесте in-ticket: %d" % len(moved)) if moved else "")


def check_plan(state):
    """G3 механически: каждое живое требование — в задаче, каждая задача — от требования,
    у каждой задачи зона и волна, в одной волне зоны не пересекаются."""
    problems = []
    rows = manifest_rows(state) or []
    publish_tickets(state)
    tickets = state.get("tickets") or []
    named = {r for t in tickets for r in t["requirements"]}
    base = {r.split(".")[0] for r in named}
    for rid, st in rows:
        if st in ("in-spec", "in-ticket", "placeholder") and not rid.startswith("D") and rid not in base:
            problems.append("требование %s (%s) не попало ни в одну задачу" % (rid, st))
    known = {rid for rid, _ in rows}
    ids = {t["id"] for t in tickets}
    for t in tickets:
        own = [r for r in t["requirements"] if r.split(".")[0][:1] in "RGD"]
        if not t["requirements"]:
            problems.append("задача %s ни к чему не привязана — работа, которую никто не заказывал" % t["id"])
        for r in own:
            if r.split(".")[0] not in known:
                problems.append("задача %s ссылается на %s, которого нет в манифесте" % (t["id"], r))
        if not t["zone"]:
            problems.append("у задачи %s нет зоны" % t["id"])
        if t["wave"] is None:
            problems.append("у задачи %s нет волны" % t["id"])
        for b in t["blockedBy"]:
            if b not in ids:
                problems.append("задача %s зависит от несуществующей задачи %s" % (t["id"], b))
    wave_of = {t["id"]: t["wave"] for t in tickets}
    for t in tickets:
        need = 1 + max([wave_of.get(b) or 0 for b in t["blockedBy"]] or [0])
        if t["wave"] is not None and t["wave"] < need:
            problems.append("задача %s в волне %s, но её зависимости позволяют не раньше %d"
                            % (t["id"], t["wave"], need))
    for i, a in enumerate(tickets):
        for b in tickets[i + 1:]:
            if a["wave"] is None or a["wave"] != b["wave"]:
                continue
            for za in a["zone"]:
                for zb in b["zone"]:
                    x, y = za.rstrip("/") + "/", zb.rstrip("/") + "/"
                    if x.startswith(y) or y.startswith(x):
                        problems.append("задачи %s и %s в одной волне пишут в %s"
                                        % (a["id"], b["id"], za if len(za) >= len(zb) else zb))
    return problems


# ── этапы ───────────────────────────────────────────────────────────────────

def stage(state, sid):
    for s in state.get("stages") or []:
        if s.get("id") == sid:
            return s
    s = {"id": sid, "status": "pending"}
    state.setdefault("stages", []).append(s)
    return s


def close_passed(state):
    """Инвариант: раньше открытого этапа не бывает другого открытого. Прошлый
    закрывается временем открытия ближайшего следующего. Этап, который прогон
    проскочил, не отметив, закрывается, если его файл лежит на диске.
    skipped и failed — осознанные состояния, их не трогаем."""
    rank = {v: i for i, v in enumerate(ORDER)}
    stages = [s for s in state.get("stages") or [] if s.get("id") in rank]
    live = [s for s in stages if s.get("status") == "active"]
    closed = []
    for s in live:
        later = sorted(o["startedAt"] for o in live
                       if rank[o["id"]] > rank[s["id"]] and o.get("startedAt"))
        if later:
            s["status"], s["finishedAt"] = "done", later[0]
            closed.append("%s закрыт (%s)" % (s["id"], later[0][11:19]))
    reached = [rank[s["id"]] for s in stages if s.get("status") in ("active", "done")]
    if not reached:
        return closed
    edge = max(reached)
    for s in stages:
        art = ARTIFACT.get(s["id"])
        if s.get("status") != "pending" or rank[s["id"]] >= edge or not art or not state.get("dir"):
            continue
        if glob.glob(os.path.join(run_dir(state), art)):
            nxt = [o["startedAt"] for o in stages if rank[o["id"]] > rank[s["id"]] and o.get("startedAt")]
            when = min(nxt) if nxt else state.get("updatedAt")
            s["status"], s["startedAt"], s["finishedAt"] = "done", s.get("startedAt") or when, when
            closed.append("%s отмечен пройденным по файлу %s" % (s["id"], art))
    return closed


def audit(state):
    out = []
    rank = {v: i for i, v in enumerate(ORDER)}
    stages = state.get("stages") or []
    live = [rank[s["id"]] for s in stages if s.get("status") == "active" and s.get("id") in rank]
    if live:
        for s in stages:
            if s.get("status") == "pending" and rank.get(s.get("id"), 99) < max(live):
                out.append("этап %s пропущен в учёте: был — `stage %s done`, не было — `stage %s skip "
                           "--note причина`. Это бухгалтерия, не расследуй" % ((s["id"],) * 3))
    return out


# ── страница и сервер ───────────────────────────────────────────────────────

def write_snapshot(state):
    try:
        page = open(PAGE, encoding="utf-8").read()
    except FileNotFoundError:
        return "страницы нет — перекопируй dashboard.html из навыка"
    i, j = page.find(BEGIN), page.find(END)
    if i < 0 or j < 0:
        return "страница без маркеров снимка — перекопируй dashboard.html из навыка"
    payload = "window.STATE=" + json.dumps(state, ensure_ascii=False).replace("</", "<\\/") + ";"
    new = page[: i + len(BEGIN)] + payload + page[j:]
    if new != page:
        tmp = PAGE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(tmp, PAGE)
    return "дашборд обновлён"


def _run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10,
                              encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _ps(script):
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])


def cmdline(pid):
    if WIN:
        return _ps("(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % int(pid)).strip()
    return _run(["ps", "-p", str(pid), "-o", "command="]).strip()


def processes():
    out = []
    if WIN:
        text = _ps("Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | "
                   "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        for line in text.splitlines():
            num, _, cmd = line.partition("\t")
            if num.strip().isdigit():
                out.append((int(num), cmd))
    else:
        for line in _run(["ps", "-Ao", "pid=,command="]).splitlines():
            num, _, cmd = line.strip().partition(" ")
            if num.isdigit():
                out.append((int(num), cmd))
    return out


def _norm(path):
    return os.path.normcase(os.path.normpath(path)).replace("\\", "/")


_OURS = re.compile(r"--directory\s+\"?" + re.escape(_norm(A)) + r"\"?(?=\s|$)")


def is_ours(cmd):
    """Сервер этого проекта — и только он: `-m http.server` и ровно наш --directory.
    Узко намеренно: широкая проверка уже убивала чужие серверы."""
    c = cmd.replace("\\", "/")
    c = c.lower() if WIN else c
    return bool(re.search(r"-m\s+http\.server\b", c)) and bool(_OURS.search(c))


def kill(pid):
    try:
        os.kill(int(pid), 15)
    except (OSError, ValueError):
        pass


def recorded():
    try:
        port, pid = open(PIDF, encoding="utf-8").read().split()
        return int(port), int(pid)
    except (OSError, ValueError):
        return None, None


def http_ok(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/dashboard.html" % port, timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def free_port(prefer):
    """Прежний порт — в первую очередь: открытая вкладка опрашивает именно его и
    оживает сама, когда сервер вернулся. Убитый сервер оставляет порт в TIME_WAIT,
    и без SO_REUSEADDR проверка считала его занятым — сервер уезжал на новый порт,
    а вкладка оставалась мёртвой. http.server сам ставит этот флаг, так что порт
    ему достанется. На Windows флаг значит другое (захват чужого порта) — там без него."""
    for p in ([prefer] if prefer else []) + [0]:
        s = socket.socket()
        if not WIN:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    return None


def kill_orphans(keep=None):
    n = 0
    for pid, cmd in processes():
        if pid not in (keep, os.getpid()) and is_ours(cmd):
            kill(pid)
            n += 1
    return n


def _detached():
    if WIN:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def serve(state):
    port, pid = recorded()
    if state.get("finishedAt"):
        kill_orphans(keep=pid)
        return "прогон закрыт"
    if os.environ.get("SSH_CONNECTION") or os.environ.get("CI"):
        return "удалённая сессия — без сервера, дашборд: %s" % PAGE
    if port and pid and http_ok(port) and is_ours(cmdline(pid)):
        return "http://localhost:%d/dashboard.html" % port
    kill_orphans()
    port = free_port(port)
    if not port:
        return "порт не нашёлся — дашборд открывается файлом: %s" % PAGE
    try:
        with open(LOG, "a", encoding="utf-8") as log:
            srv = subprocess.Popen([sys.executable, "-m", "http.server", str(port),
                                    "--bind", "127.0.0.1", "--directory", A],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=log, **_detached())
    except OSError as e:
        return "сервер не запустился (%s) — дашборд открывается файлом: %s" % (e, PAGE)
    for _ in range(10):
        if http_ok(port):
            with open(PIDF, "w", encoding="utf-8") as f:
                f.write("%d %d\n" % (port, srv.pid))
            return "сервер поднят: http://localhost:%d/dashboard.html" % port
        try:
            srv.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            continue
    srv.terminate()
    return "сервер не ответил — дашборд открывается файлом: %s" % PAGE


def stop_now():
    time.sleep(STOP_DELAY)
    port, pid = recorded()
    if pid and is_ours(cmdline(pid)):
        kill(pid)
    kill_orphans()
    for f in (PIDF, LOG):
        try:
            os.remove(f)
        except OSError:
            pass


def stop_later():
    """Гасит сервер через STOP_DELAY секунд из отдельного процесса: страница
    опрашивает state.js раз в 10 секунд и должна успеть забрать финал."""
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--stop-now"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **_detached())
    return "сервер погаснет через %d с" % STOP_DELAY


# ── версия навыка ───────────────────────────────────────────────────────────

_VER = re.compile(r"^\s*version:\s*[\"']?(\d+(?:\.\d+)*)", re.M)


def _version_str(text):
    """metadata.version из шапки SKILL.md строкой, как написано: «1.0.0»."""
    m = _VER.search(text.split("\n---", 1)[0] if text.startswith("---") else "")
    return m.group(1) if m else None


def _version(text):
    v = _version_str(text)
    return tuple(int(x) for x in v.split(".")) if v else None


def skill_version(skill_dir):
    """Версия навыка для state.skillVersion: SKILL.md из --skill-dir, иначе рядом
    с ap.py — в пакете ap.py лежит в tools/, SKILL.md уровнем выше. Копия ap.py в
    .aispa-work/ без --skill-dir версии не знает — null, а не выдумка."""
    here = os.path.dirname(os.path.abspath(__file__))
    places = [skill_dir] if skill_dir else []
    places.append(here)
    if os.path.basename(here) == "tools":
        places.append(os.path.dirname(here))
    for d in places:
        try:
            with open(os.path.join(d, "SKILL.md"), encoding="utf-8") as f:
                v = _version_str(f.read())
        except OSError:
            continue
        if v:
            return v
    return None


def release_check(skill):
    """Ответ обновителя пакета (`tools/aispa.py update-check`, JSON) или None.
    Он сам знает, кто ставил копию: «release» — выпуск GitHub, обновляется им же;
    «skills-cli» — `npx skills`, тогда сеть он не трогает."""
    tool = os.path.join(skill, "tools", "aispa.py")
    if not os.path.isfile(tool):
        return None
    try:
        r = subprocess.run([sys.executable, "-B", tool, "update-check", "--root", ROOT], capture_output=True,
                           text=True, encoding="utf-8", timeout=15, stdin=subprocess.DEVNULL)
        value = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return value if isinstance(value, dict) else None


HELD = {"git_checkout": "папка навыка лежит в git-репозитории",
        "git_check_failed": "не удалось проверить, лежит ли папка навыка в git",
        "git_unavailable": "папка навыка лежит в git, а git не запускается",
        "contains_runs": "внутри папки навыка лежат прогоны",
        "cwd_inside_package": "терминал открыт внутри папки навыка",
        "pinned_runtime": "это закреплённая копия прогона"}


def release_note(found):
    """Строка ↑ для копии из выпуска GitHub: обновляет агент после сборки, по слову пользователя.
    Копия, которую обновитель менять не вправе, получает причину и путь, а не чужую команду."""
    if found.get("status") != "available":
        return None
    release = found.get("release") or {}
    head = "вышла версия AISpa %s (у тебя %s)" % (release.get("version"), found.get("installedVersion"))
    tail = " · что нового — %s" % (release.get("url") or "github.com/%s/releases" % REPO)
    if found.get("installAllowed"):
        return head + ": обновлю после сборки, если скажешь «обнови AISpa»" + tail
    why = HELD.get(found.get("installReason"), "эту папку обновитель не меняет (%s)" % found.get("installReason"))
    return (head + ": сама эта копия не обновится — %s; обнови её там, откуда она взялась, "
            "или поставь заново: npx skills add %s --skill aispa -g" % (why, REPO) + tail)


def check_update(state):
    """Строка, если навык новее. Без сети — молчит. AISPA_NO_UPDATE_CHECK=1 выключает.
    Копия из выпуска GitHub сверяется с выпусками; остальные (npx skills, клон) — с main."""
    if os.environ.get("AISPA_NO_UPDATE_CHECK"):
        return None
    skill = state.get("skillDir") or ""
    found = release_check(skill) if skill else None
    _FOUND[:] = [found or {}]
    if found and found.get("manager") == "release":
        return release_note(found)   # копия из выпуска сверяется только с выпусками, не с main
    return main_note(skill)


_FOUND = []   # ответ update-check этого вызова, чтобы не спрашивать GitHub дважды


def copies_note():
    """Строка `!`, если агент увидит рядом вторую AISpa: копию 0.x или лишнюю ~/.codex/skills/aispa.
    Берёт ответ update-check, полученный в check_update."""
    found = _FOUND[0] if _FOUND else {}
    copies = [c for c in found.get("otherCopies") or [] if isinstance(c, dict) and c.get("path")]
    if not copies:
        return None
    return ("! рядом лежит ещё одна AISpa — агент может увидеть навык дважды: %s; "
            "убрать её — решение пользователя"
            % "; ".join("%s (%s)" % (c["path"], c.get("version") or "?") for c in copies))


def main_note(skill):
    """Копия из npx skills или клон: сверка версии с SKILL.md в main."""
    try:
        local = _version(open(os.path.join(skill, "SKILL.md"), encoding="utf-8").read())
    except OSError:
        return None
    try:
        with urllib.request.urlopen(REMOTE_SKILL, timeout=3) as r:
            remote = _version(r.read(65536).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not remote or (local and remote <= local):
        return None
    home = _norm(os.path.expanduser("~"))
    flag = " -g" if _norm(skill).startswith(home + "/.") else ""
    v = lambda t: ".".join(map(str, t))
    return ("вышла версия AISpa %s (у тебя %s): npx skills update aispa%s · "
            "что нового — github.com/%s/blob/main/CHANGELOG.md"
            % (v(remote), v(local) if local else "без номера", flag, REPO))


# ── реестр прогонов (.aispa-work/README.md) ──────────────────────────────────

README_TEXT = """# Как читать эту папку

- `dashboard.html` — открывается сам в начале сборки; можно и двойным кликом.
  Этапы, прогресс, время, что осталось. Обновляется сам, пока сборка идёт.
  `index.html` рядом — тот же файл под другим именем, открывать его не нужно.
- `<дата>-<проект>/` — папка одной сборки. Дата — день, когда сборка началась.
  Суффикс `--wip` значит «ещё делается»; когда сборка сдана, он снимается. Внутри:
  - `<дата>-brief.md` — твоя задача слово в слово и «Дополнения» — всё, что ты сказал позже.
  - `manifest.md` — список требований и что с каждым стало.
  - `spec.md` — спецификация. `tickets/` — задачи, на которые разбита сборка.
- `PROGRESS.md` — что осталось после последней сборки: не доделано, известные баги,
  о чём договорились. Мелкие правки без AISpa дописываются сюда же.

Если сборка прервалась — скажи агенту «продолжи AISpa», он поднимет состояние отсюда.

## Прогоны

| Начат | Папка | Статус | Итог |
|---|---|---|---|
"""


def register_row(state, status, result):
    d = state.get("dir") or ""
    base = re.sub(r"--wip$", "", d)
    try:
        text = open(README, encoding="utf-8").read()
    except OSError:
        text = README_TEXT
    if "## Прогоны" not in text:
        text = text.rstrip("\n") + "\n\n## Прогоны\n\n| Начат | Папка | Статус | Итог |\n|---|---|---|---|\n"
    row = "| %s | `%s` | %s | %s |" % (d[:10], d, status, result or "—")
    lines = text.splitlines()
    hit = [i for i, l in enumerate(lines) if l.startswith("|") and ("`%s`" % base in l or "`%s--wip`" % base in l)]
    if hit:
        lines[hit[-1]] = row
    else:
        lines.append(row)
    with open(README, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def git_mv(src, dst):
    if subprocess.run(["git", "-C", A, "mv", src, dst], capture_output=True).returncode == 0:
        return True
    try:
        os.rename(os.path.join(A, src), os.path.join(A, dst))
        return True
    except OSError:
        return False


def ensure_gitignore():
    g = os.path.join(ROOT, ".gitignore")
    try:
        text = open(g, encoding="utf-8").read()
    except OSError:
        text = ""
    if re.search(r"^\.aispa-work/serve\.\*", text, re.M):
        return
    with open(g, "a", encoding="utf-8") as f:
        f.write(("" if not text or text.endswith("\n") else "\n") + ".aispa-work/serve.*\n")


# ── команды ─────────────────────────────────────────────────────────────────

def parse_args(argv):
    pos, opt, multi = [], {}, {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key = a[2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opt[key] = argv[i + 1]
                multi.setdefault(key, []).append(argv[i + 1])
                i += 2
                continue
            opt[key] = True
        else:
            pos.append(a)
        i += 1
    return pos, opt, multi


def tests_pair(v):
    m = re.match(r"^\s*(\d+)\s*(?:/\s*(\d+))?\s*$", str(v))
    if not m:
        die("тесты пишутся как ПРОШЛО/УПАЛО, например 34/0")
    return {"passed": int(m.group(1)), "failed": int(m.group(2) or 0)}


def cmd_init(opt):
    if not opt.get("slug"):
        die("init требует --slug")
    mode_of(opt.get("mode") or "important")       # неверный режим — отказ до архивации прошлого прогона
    depth_of(opt.get("depth") or "normal")
    if opt.get("questions"):
        questions_of(opt["questions"])
    old = read_state()
    notes = []
    if old and not old.get("finishedAt") and not opt.get("force"):
        die("идёт прогон %s — это продолжение, а не новый прогон (или --force)" % old.get("dir"))
    if old and old.get("dir") and os.path.isdir(os.path.join(A, old["dir"])):
        dst = os.path.join(old["dir"], "state.js")
        if subprocess.run(["git", "-C", A, "mv", "-f", "state.js", dst], capture_output=True).returncode:
            os.replace(STATE, os.path.join(A, dst))
        notes.append("прошлый прогон убран в архив: %s" % dst)
    state = fresh_state(opt)
    log(state, "init")
    os.makedirs(os.path.join(run_dir(state), "tickets"), exist_ok=True)
    save(state)
    ensure_gitignore()
    register_row(state, "в работе", None)
    notes.append("папка прогона: .aispa-work/%s" % state["dir"])
    line = None if old else legacy_note(state)   # после первой сборки 1.x выбор уже сделан
    if line:
        notes.append(line)
    return state, notes


def legacy_note(state):
    """Строка `!` только про незаконченный прогон AISpa 0.x; законченная история молчит.
    Читает его обновитель пакета (`tools/aispa.py legacy-run`) — он знает формат 0.x."""
    if not os.path.isdir(os.path.join(ROOT, ".aispa")):
        return None
    tool = os.path.join(state.get("skillDir") or "", "tools", "aispa.py")
    old = None
    if os.path.isfile(tool):
        try:
            r = subprocess.run([sys.executable, "-B", tool, "legacy-run", "--root", ROOT], capture_output=True,
                               text=True, encoding="utf-8", timeout=15, stdin=subprocess.DEVNULL)
            old = (json.loads(r.stdout) or {}).get("oldRun") if r.returncode == 0 else None
        except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
            old = None
    if isinstance(old, dict) and old.get("finished") is True:
        return None
    if isinstance(old, dict) and old.get("skill"):
        return ("! в проекте есть %s прогон AISpa 0.x (%s): доделывает его только его версия — "
                "%s; папку .aispa/ не трогать"
                % ("незаконченный" if old.get("finished") is False else "возможно, незаконченный",
                   old.get("title") or old.get("run"), old["skill"]))
    return "! в проекте есть .aispa/ — прогон AISpa 0.x, его состояние не прочитать; папку не трогать"


def cmd_stage(state, pos, opt):
    if not pos:
        die("stage <id> [done|skip|fail] [--note …]")
    sid, action = pos[0], (pos[1] if len(pos) > 1 else "start")
    if sid not in ORDER:
        die("этапы: " + ", ".join(ORDER))
    s = stage(state, sid)
    t = now()
    if action == "start":
        s["status"] = "active"
        s.setdefault("startedAt", t)
        s.pop("finishedAt", None)
    elif action == "done":
        s["status"] = "done"
        s.setdefault("startedAt", t)
        s["finishedAt"] = t
    elif action in ("skip", "fail"):
        if not opt.get("note"):
            die("%s требует --note с причиной" % action)
        s["status"] = "skipped" if action == "skip" else "failed"
        s.setdefault("startedAt", t)
        s["finishedAt"] = t
    else:
        die("действие этапа: start | done | skip | fail")
    if isinstance(opt.get("note"), str):
        s["note"] = opt["note"]
    if sid == "spec" and action in ("start", "done"):
        rows = [r for r in manifest_table(state) or [] if not r["id"].startswith("D")]
        if rows:
            n = sum(1 for r in rows if r["status"] == "open")
            set_gate(state, "G1", n == 0, open=n, total=len(rows))
    log(state, "stage", id=sid, act=action,
        note=opt["note"] if action in ("skip", "fail") and isinstance(opt.get("note"), str) else None)


def close_requirements(state, tk, opt, t):
    """Требование закрыто, когда закрыты все задачи, где оно названо. --placeholder R05
    помечает его заглушкой вместо done: там стоит видимая метка вместо данных пользователя."""
    ph = set(re.findall(r"[RGD]\d+[a-z]?", str(opt.get("placeholder") or "")))
    rows = dict(manifest_rows(state) or [])
    tickets = state.get("tickets") or []
    changes = {}
    for r in {x.split(".")[0] for x in tk.get("requirements", [])}:
        if rows.get(r) not in ("in-ticket", "in-spec", "open"):
            continue
        holders = [x for x in tickets if r in {y.split(".")[0] for y in x.get("requirements", [])}]
        if all(x is tk or x.get("status") == "done" for x in holders):
            changes[r] = ("placeholder" if r in ph else "done",
                          (opt.get("commit")[:7] if isinstance(opt.get("commit"), str) else None))
    manifest_update(state, changes)


def cmd_ticket(state, pos, opt):
    acts = [p for p in pos if p in TICKET_STATUSES]
    ids = [p for p in pos if p not in TICKET_STATUSES]
    if len(acts) != 1 or not ids:
        die("ticket <id…> start|review|repair|done|fail|retry|reset")
    act = acts[0]
    known = {t["id"]: t for t in state.get("tickets") or []}
    t = now()
    for i in ids:
        tk = known.get(i)
        if tk is None:
            die("задачи %s нет — сначала `ap.py tickets`" % i)
        tk["status"] = TICKET_STATUSES[act]
        if act == "start":
            tk.setdefault("startedAt", t)
            tk.pop("finishedAt", None)
        elif act == "retry":
            tk["retries"] = tk.get("retries", 0) + 1
            tk["startedAt"] = tk.get("startedAt") or t
        elif act == "repair":
            tk["repairs"] = tk.get("repairs", 0) + 1
            if isinstance(opt.get("note"), str):
                tk.setdefault("repairFindings", []).append(opt["note"])
        elif act in ("done", "fail"):
            tk.setdefault("startedAt", t)
            tk["finishedAt"] = t
            if act == "fail" and isinstance(opt.get("note"), str):
                tk.setdefault("concerns", []).append(opt["note"])
        elif act == "reset":
            for k in ("startedAt", "finishedAt"):
                tk.pop(k, None)
        if act == "done":
            close_requirements(state, tk, opt, t)
        if opt.get("tests"):
            tk["tests"] = tests_pair(opt["tests"])
            state["tests"] = tk["tests"]
        if isinstance(opt.get("commit"), str):
            tk["commit"] = opt["commit"][:12]
    log(state, "ticket", ids=ids, act=act, tests=tests_pair(opt["tests"]) if opt.get("tests") else None,
        note=opt["note"] if act in ("repair", "fail") and isinstance(opt.get("note"), str) else None)


def cmd_set(state, pos):
    for p in pos:
        k, _, v = p.partition("=")
        if not _ or k in ("stages", "tickets", "requirements"):
            die("set key=value (скалярные поля верхнего уровня)")
        if k == "mode":
            v = mode_of(v)
        elif k == "depth":
            v = depth_of(v)
        elif k == "questions":
            v = questions_of(v)
        state[k] = None if v in ("", "null") else v


def cmd_add(state, pos):
    if len(pos) < 2 or pos[0] not in LISTS:
        die("add <%s> \"текст\"" % "|".join(sorted(LISTS)))
    key, vals = pos[0], pos[1:]
    if key.startswith("debt."):
        tgt = state.setdefault("debt", {}).setdefault(key[5:], [])
    else:
        tgt = state.setdefault(key, [])
    fresh = [v for v in vals if v not in tgt]
    tgt.extend(dict.fromkeys(fresh))
    if fresh:
        log(state, "add", key=key, n=len(fresh))


def cmd_numbers(state, name, pos, multi):
    d = {}
    for p in pos:
        k, _, v = p.partition("=")
        d[k] = int(v) if v.isdigit() else v
    if name == "blind":
        d["mismatches"] = multi.get("mismatch", [])
        state[name] = d
        set_gate(state, "G4", not d["mismatches"], checked=d.get("checked"), matched=d.get("matched"))
    else:
        d["items"] = multi.get("item", [])
        state[name] = d
        num = lambda k: d.get(k) if isinstance(d.get(k), int) else 0
        set_gate(state, "G2", num("found") <= num("fixed") + num("deferred"),
                 found=num("found"), fixed=num("fixed"), deferred=num("deferred"))


def cmd_ask(state, pos, opt):
    """Сборка ждёт пользователя: раунд брифинга, ручной гейт, вопрос перед необратимым."""
    kind = pos[0] if pos else ""
    if kind not in WAIT_KINDS:
        die("ask <%s> --note \"что ждём, одной строкой\"" % "|".join(WAIT_KINDS))
    w = state.get("waiting")
    if w and w.get("kind") != kind:
        close_wait(state)
        w = None
    note = opt.get("note") if isinstance(opt.get("note"), str) else None
    state["waiting"] = {"kind": kind, "since": w["since"] if w else state["updatedAt"], "note": note}
    log(state, "ask", kind=kind, note=note)


def cmd_finish(state, opt):
    t = now()
    notes = []
    for s in state.get("stages") or []:
        if s.get("status") in ("active", "pending") and s.get("id") == "final":
            s["status"], s["finishedAt"] = "done", t
            s.setdefault("startedAt", t)
        elif s.get("status") == "active":
            s["status"], s["finishedAt"] = "done", t
        elif s.get("status") == "pending":
            s["status"], s["note"], s["finishedAt"] = "skipped", s.get("note") or "не понадобился", t
    for tk in state.get("tickets") or []:
        if tk.get("status") in ("in-progress", "review", "repair", "pending"):
            notes.append("! задача %s закрыта незавершённой (%s)" % (tk["id"], tk["status"]))
    d = state.get("dir") or ""
    if d.endswith("--wip"):
        new = d[: -len("--wip")]
        if os.path.exists(os.path.join(A, new)):
            notes.append("! папка %s уже есть — оставляю %s" % (new, d))
        elif git_mv(d, new):
            state["dir"] = new
            notes.append("папка прогона: .aispa-work/%s" % new)
        else:
            notes.append("! не смог переименовать %s — оставляю как есть" % d)
    state["finishedAt"] = t
    state["result"] = opt.get("result") if isinstance(opt.get("result"), str) else None
    register_row(state, "сдан", state["result"])
    log(state, "finish", result=state["result"])
    return notes


def cmd_reopen(state):
    """Второй бриф в сданном прогоне: фазы 1–8 идут заново, прежние задачи остаются."""
    d = state.get("dir") or ""
    if not d.endswith("--wip") and git_mv(d, d + "--wip"):
        state["dir"] = d + "--wip"
    state["finishedAt"] = None
    state["blind"] = None
    for s in state.get("stages") or []:
        if s.get("id") != "preflight":
            for k in ("startedAt", "finishedAt", "note"):
                s.pop(k, None)
            s["status"] = "pending"
    stage(state, "manifest").update(status="active", startedAt=now())
    state["result"] = None
    state["gates"] = {}
    register_row(state, "в работе", None)
    log(state, "reopen")


def cmd_commits(state):
    """Возобновление верит state.js, а история могла уйти из-под него: reset, rebase,
    другая ветка. Сданная задача, чьего коммита нет в HEAD, — строка «!»; сбрасывает
    его оркестратор, потому что код после rebase может лежать на месте под другим sha."""
    notes = []
    for tk in state.get("tickets") or []:
        c = tk.get("commit")
        if tk.get("status") != "done" or not c:
            continue
        r = subprocess.run(["git", "-C", ROOT, "merge-base", "--is-ancestor", c, "HEAD"],
                           capture_output=True)
        if r.returncode:
            notes.append("! задача %s: коммита %s нет в истории — `ap.py ticket %s reset`"
                         % (tk["id"], c, tk["id"]))
    return notes or ["коммиты сданных задач на месте"]


def main():
    argv = sys.argv[1:]
    if "--stop-now" in argv:
        stop_now()
        return
    pos, opt, multi = parse_args(argv)
    cmd = pos[0] if pos else ""
    notes = []
    if cmd == "init":
        state, notes = cmd_init(opt)
    else:
        state = read_state()
        if state is None:
            die("state.js ещё нет — начни с `ap.py init`")
        rest = pos[1:]
        if cmd not in ("", "sync", "commits"):
            # Время события ставится до команды: ей самой и ленте нужна одна метка.
            state["updatedAt"] = now()
            beat(state)
        if cmd in MOVES_ON:
            close_wait(state)
        if cmd == "stage":
            cmd_stage(state, rest, opt)
        elif cmd == "ticket":
            cmd_ticket(state, rest, opt)
        elif cmd == "tickets":
            notes.append(publish_tickets(state))
            log(state, "tickets", n=len(state.get("tickets") or []))
        elif cmd == "check-plan":
            problems = check_plan(state)
            notes += ["! " + p for p in problems] or ["G3: трасса сходится"]
            set_gate(state, "G3", not problems, problems=len(problems))
        elif cmd == "set":
            cmd_set(state, rest)
            if any(p.startswith("tier=") for p in rest):
                log(state, "tier", v=state.get("tier"))
        elif cmd == "add":
            cmd_add(state, rest)
        elif cmd in ("coverage", "blind"):
            cmd_numbers(state, cmd, rest, multi)
        elif cmd == "tests":
            state["tests"] = tests_pair(rest[0] if rest else "")
            log(state, "tests", **state["tests"])
        elif cmd == "ask":
            cmd_ask(state, rest, opt)
        elif cmd == "answered":
            close_wait(state)
        elif cmd == "finish":
            notes = cmd_finish(state, opt)
        elif cmd == "reopen":
            cmd_reopen(state)
        elif cmd == "commits":
            notes = cmd_commits(state)
        elif cmd not in ("", "sync"):
            die("неизвестная команда %s — список в начале ap.py" % cmd)
    recount(state)
    notes = close_passed(state) + notes
    save(state)
    snap = write_snapshot(state)
    if cmd == "finish" or opt.get("stop"):
        srv = stop_later()
    elif opt.get("no-serve"):
        srv = "сервер не проверялся"
    else:
        srv = serve(state)
    r = state.get("requirements") or {}
    print("%s · %s · требований %s, готово %s" % (snap, srv, r.get("total", 0), r.get("done", 0)))
    for line in notes:
        print("  " + (line if line.startswith("!") else "· " + line))
    for line in audit(state)[:5]:
        print("  ! " + line)
    if opt.get("check-update"):
        note = check_update(state)
        if note:
            print("  ↑ " + note)
        if cmd == "init":
            note = copies_note()
            if note:
                print("  " + note)


if __name__ == "__main__":
    main()
