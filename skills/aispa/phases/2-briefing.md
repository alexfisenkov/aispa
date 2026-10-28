# Phase 2 — Briefing

`ap.py stage briefing`. The phase the user is actually needed for. Its job is not to collect wishes: it is to **close what cannot be built as written**, and — where the mode or depth asks — to find out what the brief got wrong while changing it is still free. Every question exists to move a row in the manifest.

**Depth decides how much gets opened up; the mode decides who closes it.** A fork found at `deep` is the same fork in every mode — in `auto` you settle it and label the decision, in `interview` you put it to the user. One shift: in `interview` and `manual` the user has asked to be asked, so at «Сбалансированно» the interview walks every dimension too (the question map below) — what is theirs to answer plainly matters once they want to answer it.

**When the adversarial pass runs** — mode `interview` or `manual` at any depth, or depth `deep` in any mode — read `phases/2-adversarial.md` and run it **before** the first question. Otherwise skip it.

## The interview — in rounds

**Ask a round, not a question.** One message holds the current frontier — questions whose answers do not depend on another unanswered one — up to four at a time, numbered, **one theme per round** (one screen, one feature, one kind of decision). Each carries a recommended answer and a reason in half a line, so the user can reply «ок» to accept every recommendation, or «2 — таблица, остальное ок». The next round is built from the answers: what they opened, what they closed.

```
Тема 1 из 5 — заявки · осталось примерно 35 вопросов

1. Где хранить заявки — в Google-таблице или в базе? Я бы взял таблицу: тебе её видно и не нужен сервер.
2. Кто отвечает клиентам ночью? Я бы принимал заявки круглосуточно и обещал ответ в рабочие часы.
3. ⚑ Отменить заявку клиент может:
   а) в любой момент — проще для клиента, мастер иногда едет зря;
   б) только до выезда мастера — после выезда отмена через звонок тебе.
   Я бы взял «б». Тут выбери букву сам — от этого зависит, что увидит клиент.

Ответь, например: «3б, остальное ок» — или просто «ок», если со всем согласен (кроме вопроса с ⚑: там нужна буква).
```

The progress line «Тема N из M · осталось примерно K» exists only in `interview` and `manual`, where `interview.md` holds the map; «осталось» counts the questions of this round too, and the number matches the opening map the first time. In `important` there is no map and no theme count: the round opens with a plain «Два вопроса, дальше соберу сам» (the real number).

Before each round goes out: `ap.py ask briefing --note "тема «заявки» — хранение, ночь, отмена"`. The answers close it — `answered`, or the next round's `ask`.

- **Every question names its requirement** — internally, «this closes R07». A question that closes no row is dropped — except the scope-edge question (item 7 of «What to ask about») and the `carried.md` question, whose answers become `G##` or `dropped` rows.
- **Look facts up, ask only decisions.** What stack the repo uses, what a file already contains, how a service usually works — facts, found by you. Which payment provider they have an account with, what a client may do — decisions, asked.
- **Blocking unknowns go in the first round** — payment, hosting, which accounts exist, where the data lives, an existing system to fit into. Asked at the end, they cost half the project.
- **Decisions, never secrets.** *Which* provider, *whether* an account exists — yes; the key itself — never.
- **Never answer for the user.** «Не знаю» → the row stays live with the missing fact named in Основание, and the build puts a visible stub there. A fact about their business (a price, an address, a text) gets no invented recommendation — the recommendation is «не знаешь сейчас — поставлю заметную заглушку».
- **Everything that is craft you decide yourself** — error wording, retry policy, naming, code structure — in every mode; asking it is padding. A decision goes to the user, not to you, if it **costs money or ties them to a vendor**, **changes what they see or can do**, **means rebuilding rather than editing to undo**, or **encodes a rule about their business** — prices, deadlines, who may do what, what happens to someone's data. Unsure which side it falls on → in `important`, decide and record; in `interview` and `manual`, ask.
- **Nothing open → say so and go:** «Вопросов нет — в брифе всё однозначно, пишу спецификацию», and `ap.py stage briefing skip --note "вопросов не потребовалось"`.

## How far the interview goes — by mode

| | What reaches the user |
|---|---|
| **important** | only forks whose branches lead to a visibly different product; the rest you settle and record. Usually a round or two; none when the brief is unambiguous |
| **interview · manual** | **the whole brief, systematically** — every requirement walked through the map below, plus everything the adversarial pass opened. It ends when the map is empty, not when a count is reached |

### The question map — interview and manual

The user chose to be asked, and every answer they give is one guess fewer in the build: here the thorough interview is the product, not a cost to trim. Before the first round, walk every manifest row (`R##`, `R##i`) and write down each question it raises, grouped by theme, in `.aispa-work/<dir>/interview.md` — one line each: the theme, the question, the row it closes, and a status (`ждёт` · `спрошен` · `ответ` · `решил сам`). The file is the map of what is left: it survives a compaction, and it is where «осталось примерно» comes from.

For each row, three passes:

1. **Its own words** — every word with two readings («заявка», «клиент», «быстро»), every number not given, every «и т.д.».
2. **The eight dimensions** — the same as the spec phase uses: *первый запуск* (what is there before any data), *пусто* (zero items), *неверный ввод*, *сбой* (network, service), *прерывание* (closed halfway, sent twice), *рост* (ten versus ten thousand), *границы* (who may do this, and who may not), *после* (where the result goes, who learns, can it be undone). At «Сбалансированно» and «Максимум» — all eight for every row; at «Быстро» — pass 1, plus *границы* and *после* where the brief already names roles or outcomes (they are the user's to answer); *неверный ввод* and *сбой* are craft at that depth and are decided in the spec. A dimension the user answers here becomes an `R##.n` story at any depth (`phases/3-spec.md`). A dimension that does not apply is skipped silently, never asked as «не применимо?». Only the part of the answer that is the user's goes on the map; the craft part you decide in the spec.
3. **The list in «What to ask about»** below, as it touches this row.

Then order the map: blocking externals first, the adversarial findings no later than the end of the second round (in the one-at-a-time form: the second group, see below), then themes in the order the brief tells them, scope edges last. Answers open new questions — add them to the map and recount; the count you say is honest and approximate, never a promise.

**Open with the map, in one message, before the first round** — the user sees how much there is and how to steer it — and **end it with the first question: how to ask**. Nothing else is asked in that message: `ap.py ask briefing --note "карта интервью и вопрос, как спрашивать"`, wait for the answer, then `ap.py set questions=one` or `questions=rounds`:

```
Пройду твой бриф по темам, чтобы ничего не потерялось:
1. Заявки · 2. Оплата · 3. Уведомления · 4. Кабинет мастера · 5. Чего не нужно
Это примерно 35 вопросов — у каждого мой вариант.
Отвечай коротко: «ок» — согласен с моими вариантами, «2 — таблица» — поправить один.
Устанешь — скажи «дальше решай сам»: остальное решу сам и перечислю в отчёте.

Сначала один вопрос: как тебе удобнее?
1. Пачками — по 3–4 вопроса в сообщении, так быстрее. Я бы так.
2. По одному — один вопрос за раз, спокойнее читать.
```

The form question is skipped when the form is already known — said in the call, in the answer to the start question, or set earlier in this run (`questions` in `state.js` is `one` or `rounds`; empty after `init` means not asked yet); then the map ends with «Буду спрашивать по одному — скажи «можно пачкой», если надоест» or the rounds equivalent. An answer that names neither → rounds. In `important` the form is asked the same way only when more than four questions are coming; for one round it is not worth a message of its own.

Every round after it opens with one line: «Тема 2 из 5 — оплата · осталось примерно 20 вопросов».

### Real choices, not «ок, ок, ок»

Forty «ок» in a row usually means nothing was decided — the recommendations were taken unread. Two guards:

- **⚑ — the user picks.** A question where the branches give a visibly different product — what the client sees, what it costs, what is hard to undo — is written as a choice: two or three options lettered а/б/в, each with what it means for them in their words, your pick named after them, and «⚑ тут выбери букву сам». «Ок» for the round does not cover it. A reply without a letter → ask it once more, alone, with what the recommended option will mean; a second «как скажешь» → take the recommendation and record «принято по рекомендации» in Основание. At most one ⚑ per round, so the rest stays quick. In `important` every question is such a fork, so pick the one that matters most for the ⚑.
- **Three bare «ок» rounds in a row** → one line before the next round: «Ты соглашаешься со всем подряд — это нормально, если варианты подходят. Хочешь — дальше решу сам и перечислю решения в отчёте». Said once per briefing.

### «Дальше решай сам»

«Дальше решай сам», «хватит вопросов», «остальное на твоё усмотрение» — at any point — stop asking. Close what is left on the map the way `auto` would (below): a decision into Основание as `ASSUMPTION — принято за пользователя: …` plus `ap.py add debt.assumptions "…"`, a fact about the user left as a visible stub; mark those lines `решил сам` in `interview.md`. One line to the user — «Понял, остальное решу сам — решения будут в отчёте» — and on. The mode does not change: manual still shows the spec and the tickets, and the safety questions are still asked.

### One question at a time

On request — «спрашивай по одному», «по одному вопросу», «давай по одному», "one at a time" — the interview goes **one question per message**: the same recommendation and reason in half a line, the same ⚑ for a real choice, «ок» takes the recommendation. The progress line shrinks to the question: «Вопрос 7 · тема «оплата» · осталось примерно 15». **"Round" in the one-at-a-time form** means a group of up to four questions of one theme, not one message: the rules above that count rounds — one ⚑ per group, three bare «ок» (about ten questions in a row), the adversarial findings by the end of the second group, `carried.md` in the first — count groups. Back to rounds on «можно пачкой», «давай раундами», "all at once". It can be asked for in the call («/aispa interview спрашивай по одному …» — said to you, not a sentence about the product), in the answer to the start question («интервью, по одному») or at any moment; it changes the form, never what is asked. Stored in the run: `ap.py init --questions one` or `ap.py set questions=one` / `questions=rounds`, one line of acknowledgement («Хорошо, дальше по одному вопросу»), and `ask briefing` before each question.

Calibration, not a target: on a real project an interview or a manual run lands at **several dozen questions** — roughly five to fifteen per screen or main feature, twenty to sixty in all, over many rounds; a one-page brief, at ten or fifteen; `important`, at a round or two. There is no cap. What makes an interview bad is padding — a question you could have answered yourself, a craft decision dressed up as a choice — never length. Say the size once, in the opening map, and do not apologise for the count as you go.

In interview and manual the thing to guard against is **drift into process**: «какой стек берём?», «нарезать помельче?», «показать спецификацию?» are questions AISpa answers itself.

## What to ask about

In priority order, only what is actually unresolved for this brief:

1. **Blocking externals** — payment, hosting, domain, third-party accounts, data to import.
2. **What the adversarial pass found**, where it ran — the only questions that can change *what* gets built.
3. **Implicit requirements** (`R##i`) — «заявки будут падать в таблицу — тебе нужен ещё экран, чтобы их смотреть, или таблицы хватит?»
4. **Depth only the user can settle** — «клиент отменил через час — деньги возвращаем сами или мастер решает?» At strict («Быстро»), only clarifying what they already said.
5. **Untestable wishes** — «красиво», «удобно»: cheapest turned checkable by asking what it should be **like** (below).
6. **Contradictions inside the brief** — quote both halves, ask which wins.
7. **Scope edges** — what is explicitly *not* needed; the answers become `dropped` rows and save whole tickets.

**What the last run left** — `carried.md` → «Не доделано» and «Известные баги», when the file exists — is **one question in the first round**, whatever else the round holds, and it keeps a round from being skipped: «С прошлой сборки остались: 1) корзина, 2) даты в двух форматах. Взять что-то в эту? Я бы взял 2 — это пять минут». A yes is the user adding a requirement: a `G##` row with their answer and the item it names, and the same words under `## Дополнения`. A no, or no answer to that item, leaves it in the progress file — nothing is dropped for being ignored. In `auto` nothing is offered and nothing taken.

## Эталон — what it should be like

Whatever the user hands you that the result could be measured against — reference sites, a competitor, screenshots, a text whose tone they like, «как в приложении банка» — goes into `.aispa-work/<dir>/reference.md`, one line each, verbatim after redaction, with where it came from. It travels to **the executors of tickets that build a surface** (`phases/5-subagents.md`), so the first version already looks like what they meant.

- **Only what the user gave.** A comparable you chose is your taste with a citation. Nothing named → no file.
- **A comparable is not a requirement** — it never becomes a row and never gates anything. «Тёмная тема обязательно» is a requirement and goes to the manifest.
- **One question, only where there is a surface**, folded into a round. In `auto`, `reference.md` holds only what the brief itself carries.

## Recording answers

After each round, update the manifest rows at once, and the statuses in `interview.md` when there is one: a decision into Основание; a cancellation → `dropped` with the user's words; something new → a `G##` row with their phrasing; «не знаю» → the missing fact into Основание. Every answer that **cancels, adds or reverses** is also appended to the brief's `## Дополнения`, dated and verbatim.

## When the brief changes later

«Убери SMS» at ticket four, «а добавь ещё экспорт» during a review — ordinary, and one procedure wherever it arrives:

1. **The brief file first** — their words under `## Дополнения`. First because it is the step that gets skipped.
2. **The manifest** — `dropped` with the quote, or a new `G##`.
3. **The plan** — a new `G##` becomes a ticket, a `deferred` row, or a line in the report. Say which and what it does to the rest: «Беру, но лендинг тогда сдвигается». A ticket means the whole path: a story in the spec, the row `in-spec`, a ticket file, `ap.py tickets` and `ap.py check-plan` — a row that skips the spec is invisible to both.

`G##` is the user's words, `A##` your idea, `D##` what the build proved — never blur them.

## Mode `auto` — the self-briefing

No interview: `ap.py stage briefing skip --note "сам по брифу — самобрифинг"`. Run the same list against yourself and write the answers into the manifest.

- **Decisions are yours** — stack, structure, provider, data model, layout. Pick what runs on the user's machine **without a third-party account and without money**, and record `ASSUMPTION — принято за пользователя: …` in Основание, plus `ap.py add debt.assumptions "…"` for the dashboard; every one is a line in the report.
- **Facts about the user are not** — prices, texts, addresses, business rules, brand colours: the missing fact named in Основание, a visibly labelled stub in the code (`[ЦЕНА — впиши]`, never `4990 ₽`), a line in the report.
- **A paid or account-bound service becomes an adapter** — one interface, a local stub behind it, the credential an empty name in `.env.example`.

## Closing

Gate **G1**: every row has a status; nothing `open` without a recorded reason; in `interview` and `manual`, no line of `interview.md` left at `ждёт` or `спрошен`. Then one line — «Понял. Пишу спецификацию» — and Phase 3. Do not summarise the interview back to the user.
