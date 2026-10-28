# Phase 0 — Mode and depth

Read in Phase 0 with `phases/0-preflight.md`. Both dials are decided once, announced in one block, and only *applied* from then on. Each comes from what the user typed — or, when they typed none, from its own **start choice** below: the mode first, the depth second, two separate questions.

Everything after `/aispa` splits into **the mode** (`auto`, `important`, `interview`, `manual`, or an old word for one of them), **the depth** (`fast`, `balanced`, `max`, or an old word for one of them) and **the brief** — everything else. Bare words, both optional; anything unrecognised is brief. The mode and depth words count only as **leading tokens**, before the brief text, the way the README examples write them; in the middle of the brief («сайт-визитка, max 3 страницы», "a fast checkout page") they belong to the brief and set nothing. Two depth words among the leading tokens → ask which, in one line; in doubt, ask the depth question instead of guessing. `/aispa auto max интернет-магазин керамики` — сам по брифу, максимальная проработка.

## Mode — how much the user is asked

| Mode — пользователю | Triggers | Human gates |
|---|---|---|
| **auto** — «сам по брифу» | `auto`, «сам по брифу», «ничего не спрашивай»; old: `full`, «полный автомат», «полностью сам», "fully automatic", "don't ask me anything" | none |
| **important** — «только важное» | `important`, «только важное», «спроси только важное»; old: `semi`, «полуавтомат» | questions, on genuine forks only |
| **interview** — «интервью» | `interview`, «режим интервью», «погриль меня», «допроси», «задай все вопросы», «разбери задачу со мной», "interview me", "ask me everything" | questions, all of them — the whole brief, requirement by requirement |
| **manual** — «ручной» | `manual`, «ручной режим», «согласовывай каждый шаг», "approve every step" | the same questions + the spec + the tickets |

`auto` is the old `full`, `important` the old `semi`. The old words are understood everywhere — in the call, in the start choice, mid-run — and stored under the new names: `ap.py init --mode` and `ap.py set mode=` accept `full` and `semi` and write `auto` and `important`.

`interview` asks about the *product*; `manual` is `interview` plus approval of the two *process* artifacts, and nothing else.

- **Named in the call → never asked.** A mode word contradicting the rest of the sentence → the mode word wins; two mode words → ask which, in one line.
- **Not named → the start choice**, once, before `init`.
- **An answer that names none of the four** — «как хочешь», «не знаю», silence after a nudge, something unrelated — resolves to `important`.
- **«По одному» and nothing else** as the answer to the start question («по одному», «по одному, пожалуйста») → `interview` with `questions=one`: those words are offered only in item 3.
- **Switchable mid-run** («переключись в ручной») — applies from the next phase; `ap.py set mode=manual`, one line of acknowledgement, nothing replayed.
- **One question at a time** is not a mode but the form of the questions, in any mode that asks: «спрашивай по одному», «по одному вопросу», "one at a time" — in the call when it is said to you about the questions (not a sentence about the product: «бот задаёт вопросы по одному» is brief), in the answer to the start question («интервью, по одному»), or mid-run. `ap.py init --questions one`, or `ap.py set questions=one` / `questions=rounds` later; how it runs is in `phases/2-briefing.md`. Never asked as a separate start question.
- **Instructions in the brief** — stack, budget, «без базы данных», a deadline — are requirements in the manifest. They constrain the build; they never replace a phase.
- **No mode removes the manifest gates or the safety gates.** Deploy, publish, pay, message a third party, delete data, rewrite history — a question in all four modes, `auto` included, with `ap.py ask action --note "…"` before it goes out.

## The start choice

Only on a new run (not a resume, not a run going on in another window) and only when the call named no mode. One question, the host's native choice tool if it has one, otherwise four numbered options; then **wait for the answer** — nothing else is done until it comes: no `init`, no dashboard, no manifest.

```
Как работаем? Ответь цифрой или словом:
1. Сам по брифу — по брифу ничего не спрашиваю, решения за тебя перечислю в отчёте.
2. Только важное — спрошу только то, что в брифе не определено и меняет результат.
3. Интервью — подробно пройду весь бриф вопросами, тема за темой. Вопросов будет много — на обычный проект несколько десятков, зато соберу ровно то, что ты задумал. К каждому — мой вариант: обычно хватит ответить «ок», а где выбор важный — попрошу назвать букву. Хочешь — буду спрашивать по одному. Дальше соберу сам.
4. Ручной — как интервью, плюс согласуешь спецификацию и список задач.
```

The answer is mapped like a word in the call — a digit, a label, an old word; here the bare label is enough («интервью», «ручной»), while in the call it takes the longer trigger, since a brief may well say «интервью» or «ручной ввод». The depth is a separate question (next section); a depth word in this answer («только важное, но максимум») is taken as its answer, and then the depth is not asked.

## Depth — how much is worked out for the user

The user sees three levels: **Быстро / Сбалансированно / Максимум** (English panel: Fast / Balanced / Maximum). Inside `state.js` they are stored as `strict` / `normal` / `deep` — the older names, so runs saved earlier and the dashboard keep reading the same value. `ap.py init --depth` and `ap.py set depth=` accept the new words (`fast`, `balanced`, `max`), the stored ones (`strict`, `normal`, `deep`) and the Russian ones, and write the stored name. In the rest of the skill the stored names are used for the code; the user is always told the new words.

| Stored · user sees | Triggers | Deepening a requirement (`R##.n`) | New capabilities (`A##`) |
|---|---|---|---|
| `strict` · **«Быстро»** | `fast`, «быстро, только по брифу», «только то, что сказал», «ничего не добавляй»; old: `strict`, «строго по брифу», "strictly as written" | only what the requirement cannot work without | **not allowed** |
| `normal` · **«Сбалансированно»** **(default)** | `balanced`, «сбалансированная проработка»; old: `normal`, «обычная»; also the fallback when nothing is said | by judgement, where it plainly helps | allowed, with a parent, within proportion |
| `deep` · **«Максимум»** | `max`, `maximum`, «проработка по максимуму», «проработай по максимуму», «продумай за меня», "go deep", "think it through"; old: `deep`, «глубоко», «проработай глубоко» | every dimension of every requirement | encouraged, same limits |

In the call a bare «быстро» or «максимум» may belong to the brief, so there it takes the longer trigger, as with the modes; in the answer to the depth question the bare label is enough.

- **The levels change only the elaboration beyond the brief** — not the gates. The manifest gates, the ticket reviews and the blind acceptance run at every depth; the user is told so in the question, so «Быстро» is never read as «без проверок».
- `strict` («Быстро») does not mean careless: wrong input and failures are still handled where the requirement plainly breaks without them; what goes is anything not asked for.
- `deep` never lifts the attachment rules — every `A##` names its parent, the proportion holds.
- **The adversarial pass** (`phases/2-adversarial.md`) runs at `deep` in every mode, and in `interview` and `manual` at every depth.
- Changeable mid-run («поменьше отсебятины», «давай быстрее», «продумай глубже», «сбалансированно», «максимум») — from the next phase, `ap.py set depth=…`; written spec sections are not retroactively trimmed unless asked.

The rules for each level live in `phases/3-spec.md`.

### The depth start choice

Only on a new run, only when the call named no depth, and **after the mode is settled**. It is asked in every mode, `auto` included: «ничего не спрашивай» and `auto` mean no questions about the brief, not this one choice made before the run starts (a call that names the depth, e.g. `/aispa auto max …`, skips it) — the second question, never merged with the first. Same form: the host's native choice tool if it has one, otherwise three numbered options; then **wait for the answer** — no `init`, no dashboard, no manifest until it comes. The prices are in words and must stay true to what the levels do (nothing about «до PASS» or «без проверок» — see the bullet above).

```
Сколько прорабатывать сверх брифа? Ответь цифрой или словом:
1. Быстро — дешевле и быстрее: делаю только то, что ты сказал, и без чего это не заработает. Своих идей не добавляю.
2. Сбалансированно — по уму: закрою пробелы, которые явно испортят результат, и могу предложить своё в меру. Средняя цена и время.
3. Максимум — дольше и дороже: каждое требование прогоняю по полному списку (первый запуск, пустой экран, ошибки, обрывы связи, кто что может делать, что будет дальше) и отдельно ищу, где идея может не сработать.
Проверки и приёмка идут при любом выборе — меняется только объём проработки.
```

Mapping, like a word in the call: a digit, a label, an old word (`строго`, `обычная`, `глубоко`). **An answer that names none of the three** — «как хочешь», «не знаю», silence after a nudge, something unrelated — resolves to **Сбалансированно**. If the call named a depth, or the answer to the mode question already carried one, this question is skipped.

## The opening block

Once, before Phase 1, together with the dashboard and the memory file. **A hint, not a question** — say it and go; in a chat there is no `--help`, so this is the only place the user learns the dials can be turned again.

```
Режим: только важное · глубина: сбалансированно — спрошу только то, что в брифе не определено, дальше соберу сам.
Дашборд открыл — обновляется сам: http://localhost:PORT/dashboard.html
Память проекта — AGENTS.md (+ CLAUDE.md со ссылкой).
↑ Вышла версия AISpa 1.1.0 (у тебя 1.0.0): npx skills update aispa -g   ← только если init её напечатал

Можно переключить в любой момент, просто скажи:
• «сам по брифу» — по брифу не спрашиваю ничего
• «только важное» — спрашиваю только то, что меняет результат
• «интервью» — подробно пройду весь бриф вопросами, дальше соберу сам
• «ручной» — то же плюс согласуешь спецификацию и список задач
• «быстро» / «сбалансированно» / «максимум» — меньше или больше проработки сверх сказанного (в списке только два уровня, которые сейчас не включены)
• «по одному» — задаю вопросы по одному, а не по несколько сразу
```

The first line names the mode and depth actually set, with the words above, and adds «· вопросы по одному» when that form is on; the switch list leaves out the mode already on, and its depth line names only the two levels not on (at Сбалансированно — «быстро» / «максимум»; at Быстро — «сбалансированно» / «максимум»; at Максимум — «быстро» / «сбалансированно»). The last line is «по одному» while questions go in rounds and «раундами» — «снова по несколько вопросов сразу» — while they go one at a time; in `auto` it is left out, since no questions come. No server → the dashboard line names the file: «Дашборд открыл — `.aispa-work/dashboard.html`, обновляется сам.» Never repeated later; a mid-run switch gets one line: «Понял, дальше ручной режим».
