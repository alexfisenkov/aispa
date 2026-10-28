# Phase 0 — Mode and depth

Read in Phase 0 with `phases/0-preflight.md`. Both dials are decided once, announced in one block, and only *applied* from then on. The mode comes from what the user typed — or, when they typed none, from **the start choice** below; the depth comes only from their words.

Everything after `/aispa` splits into **the mode** (`auto`, `important`, `interview`, `manual`, or an old word for one of them), **the depth** (`strict`, `deep`) and **the brief** — everything else. Bare words, any order, both optional; anything unrecognised is brief. `/aispa auto deep интернет-магазин керамики` — сам по брифу, глубокая проработка.

## Mode — how much the user is asked

| Mode — пользователю | Triggers | Human gates |
|---|---|---|
| **auto** — «сам по брифу» | `auto`, «сам по брифу», «ничего не спрашивай»; old: `full`, «полный автомат», «полностью сам», "fully automatic", "don't ask me anything" | none |
| **important** — «только важное» | `important`, «только важное», «спроси только важное»; old: `semi`, «полуавтомат» | questions, on genuine forks only |
| **interview** — «интервью» | `interview`, «режим интервью», «погриль меня», «допроси», «задай все вопросы», «разбери задачу со мной», "interview me", "ask me everything" | questions, all of them |
| **manual** — «ручной» | `manual`, «ручной режим», «согласовывай каждый шаг», "approve every step" | the same questions + the spec + the tickets |

`auto` works as Autopilot's old `full`, `important` as its old `semi`. The old words are understood everywhere — in the call, in the start choice, mid-run — and stored under the new names: `ap.py init --mode` and `ap.py set mode=` accept `full` and `semi` and write `auto` and `important`.

`interview` asks about the *product*; `manual` is `interview` plus approval of the two *process* artifacts, and nothing else.

- **Named in the call → never asked.** A mode word contradicting the rest of the sentence → the mode word wins; two mode words → ask which, in one line.
- **Not named → the start choice**, once, before `init`.
- **An answer that names none of the four** — «как хочешь», «не знаю», silence after a nudge, something unrelated — resolves to `important`.
- **Switchable mid-run** («переключись в ручной») — applies from the next phase; `ap.py set mode=manual`, one line of acknowledgement, nothing replayed.
- **Instructions in the brief** — stack, budget, «без базы данных», a deadline — are requirements in the manifest. They constrain the build; they never replace a phase.
- **No mode removes the manifest gates or the safety gates.** Deploy, publish, pay, message a third party, delete data, rewrite history — a question in all four modes, `auto` included, with `ap.py ask action --note "…"` before it goes out.

## The start choice

Only on a new run (not a resume, not a run going on in another window) and only when the call named no mode. One question, the host's native choice tool if it has one, otherwise four numbered options; then **wait for the answer** — nothing else is done until it comes: no `init`, no dashboard, no manifest.

```
Как работаем? Ответь цифрой или словом:
1. Сам по брифу — ничего не спрашиваю, решения за тебя перечислю в отчёте.
2. Только важное — спрошу только то, что в брифе не определено и меняет результат.
3. Интервью — разберу бриф вопросами до конца, дальше соберу сам.
4. Ручной — как интервью, плюс согласуешь спецификацию и список задач.
```

The answer is mapped like a word in the call — a digit, a label, an old word; here the bare label is enough («интервью», «ручной»), while in the call it takes the longer trigger, since a brief may well say «интервью» or «ручной ввод». The depth is not part of this question; a depth word in the answer («только важное, но проработай глубоко») is taken as well.

## Depth — how much is worked out for the user

| Depth — пользователю | Triggers | Deepening a requirement (`R##.n`) | New capabilities (`A##`) |
|---|---|---|---|
| **strict** — «строго по брифу» | `strict`, «строго по брифу», «только то, что сказал», «ничего не добавляй», "strictly as written" | only what the requirement cannot work without | **not allowed** |
| **normal** — «обычная» **(default)** | nothing said | by judgement, where it plainly helps | allowed, with a parent, within proportion |
| **deep** — «глубоко» | `deep`, «глубоко», «проработай глубоко», «продумай за меня», "go deep", "think it through" | every dimension of every requirement | encouraged, same limits |

- `strict` does not mean careless: errors and empty states are still handled; what goes is anything not asked for.
- `deep` never lifts the attachment rules — every `A##` names its parent, the proportion holds.
- **The adversarial pass** (`phases/2-adversarial.md`) runs at `deep` in every mode, and in `interview` and `manual` at every depth.
- Changeable mid-run («поменьше отсебятины», «продумай глубже») — from the next phase, `ap.py set depth=…`; written spec sections are not retroactively trimmed unless asked.

The rules for each level live in `phases/3-spec.md`.

## The opening block

Once, before Phase 1, together with the dashboard and the memory file. **A hint, not a question** — say it and go; in a chat there is no `--help`, so this is the only place the user learns the dials can be turned again.

```
Режим: только важное · глубина: обычная — спрошу только то, что в брифе не определено, дальше соберу сам.
Дашборд открыл — обновляется сам: http://localhost:PORT/dashboard.html
Память проекта — AGENTS.md (+ CLAUDE.md со ссылкой).
↑ Вышла версия AISpa 1.1.0 (у тебя 1.0.0): npx skills update aispa -g   ← только если init её напечатал

Можно переключить в любой момент, просто скажи:
• «сам по брифу» — не спрашиваю вообще ничего
• «только важное» — спрашиваю только то, что меняет результат
• «интервью» — разберу бриф вопросами до конца, дальше соберу сам
• «ручной» — то же плюс согласуешь спецификацию и список задач
• «строго по брифу» / «глубоко» — меньше или больше проработки сверх сказанного
```

The first line names the mode and depth actually set, with the words above; the switch list leaves out the mode already on. No server → the dashboard line names the file: «Дашборд открыл — `.aispa-work/dashboard.html`, обновляется сам.» Never repeated later; a mid-run switch gets one line: «Понял, дальше ручной режим».
