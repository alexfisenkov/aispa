> Текст автора основы — Nick Vels, Autopilot 2.2.0 (MIT); сохранён без изменений.

# Why AISpa is built this way

A page for people who edit the skill. The agent never reads it: the instructions keep the rule and, where the rule is not obvious, one line of "why". Everything below is the reasoning and the measurements we paid for.

## Key decisions

**A manifest of verbatim quotes and four gates.** A requirement that "stopped being mentioned" by the third retelling is the main way a build from a description breaks. So the brief is cut into quoted rows right away, and G2 and G4 are checked by a subagent that gets neither the manifest nor the spec: the spec's author cannot see what they did not write.

**The orchestrator does not write code.** Its context is one for the whole run and is never refreshed. A diff read on task two is still sitting there on task eight.

**One task, one executor, one fresh context.** Accumulated context is what makes long sessions start breaking things that used to work.

**`interfaces.md` is filled from the spec before the first task.** Otherwise module boundaries are decided by task 01, which saw an eighth of the job, and the rest either obey or quietly build a second version.

**Launch only via `/aispa` (`disable-model-invocation`).** The skill runs `git init`, writes code and commits. The description matched phrases that were too broad, and a globally installed skill could start in someone else's project without a command. The cost: the user has to know the command, and the README shows it.

**A wide change is three steps, not a vertical slice.** Editing a shared thing used by several zones breaks every zone but its own: the task never goes green. "New next to old → migrate zone by zone → delete the old" keeps every step green. The path came from Pocock's `to-tickets` and got lost in the port.

**The brief grows an addenda section (`## Дополнения`).** Both independent checks read the brief and do not read the manifest. A change that reached the manifest but not the brief does not exist for them.

## Measurements behind version 2.0

Taken from eight real runs via Claude Code logs (September 2026, `tools/measure-run.py`). Units are normalized: output ×5, cache write ×1.25, cache read ×0.1. These are ratios between runs, not money. **The absolute numbers below are inflated about 1.8–1.9×**: until 2026-10-12 the script counted log lines, and Claude Code writes one model call as several lines (thinking, text, tool call) with the same usage. Shares and comparisons between runs hold.

| Run | Total | Review + repair | Executors | Orchestrator |
|---|---|---|---|---|
| T3, 13 tasks | 162M | 41% | ~39% | 17% |
| T3, 9 tasks | 108M | 13% | 57% | 26% |
| T2, 7 tasks | 29M | 26% | 32% | 37% |
| T3 (two sessions) | 66 / 46M | 20% / 47% | 44% / 28% | 28% / 18% |
| T3 (two sessions) | 35 / 80M | 29% / 45% | 43% / 37% | 28% / 15% |

What followed from this:

- **A long-lived reviewer is expensive.** Its context grew to 300–600K tokens, and a subagent's cache lives 5 minutes: it went cold between tasks, and the whole context was rewritten at cache-write price (55–68% of the reviewer's spend). Hence: a fresh reviewer per event, targeted review only for the foundation and risky tasks, one review of the whole branch at the end.
- **The orchestrator widened repairs.** Follow-up requests carried 12–15 conditions when the review said "nothing blocking"; 13 of 13 tasks got repairs, 8 hit the cap of two. Hence: repair receives the `BLOCKING` lines verbatim, and the run-cost rules can only be broken by a line to the user.
- **Bookkeeping took hundreds of turns.** Up to 48 `sync.py` calls and manual `state.js` edits per run, each turn re-reading the orchestrator's entire context. Hence: `ap.py`, one command per event, chainable with the check and the commit.
- **The executor context ceiling never fired.** Executors took 100–370 steps; a task handoff happened once in about 60 tasks. The mechanism is gone; tight slicing replaces it.
- **Executors were underloaded.** In the 2.0 control run (T2, 6 tasks) executors ended at 110–190K; four of six stayed under 125K. A new executor costs about 0.25M normalized units — 60–80K of cold start and half a dozen exploring calls, plus the orchestrator's calls to launch it, wake on its return and land it. A call at context X costs 0.1·X in cache reads, so a task's cost grows roughly with the square of its work: modelled on the run's numbers (start 70K, +3.2K per call), work that would take one executor to ~475K costs ~4.2M as one task, ~3.2M as two and ~3.0M as three. The two costs meet near 200–250K. Hence: tiers narrowed to 1–2 / 3–6 / 7–12, a size test that merges neighbours under ~220K and splits only past ~250K, and `measure-run.py` flagging executors that ended under 125K or over 250K. Why not 500–600K: cache reads were already half of the run's spend at 0.1 per token, and a five-minute pause rewrites a 500K context at ×1.25 — more than a fresh executor.
- **The context target was not what executors hit, and the run lost its time elsewhere.** In two later runs on a large existing codebase (VelsAgents, 2026-10-19 and 2026-10-20) all 15 and both measured executors ran past 250K, at 300–420K, each writing ~2.6K lines — a cold start there is well over 100K, not 60–80K. And the «risky» list matched almost everything: anything touching access rights was a point review, so 12 of 15 and 7 of 11 tickets got one, each holding its ticket for 15–40 minutes before the commit, with up to two re-reviews. The same project's next day — 18 edits, ~1.3K lines — went through one plain dialogue in about an hour. Hence: the size is read from the volume — ~3K changed lines per executor, one ticket up to ~3K, which makes a list of edits a single-context build; a point review only for the foundation and own code of sign-in, keys, money and deletion, run after the commit beside the next wave; one re-review; the whole-branch review always. The volume rule costs a little more per ticket than the 200–250K optimum above, and buys fewer cold starts, fewer orchestrator events and a size anyone can estimate from the spec.
- **The refinement mode was never switched on** in eight runs. The mode is gone; the user's reference now goes straight to the executors of UI tasks.
- **Skill text.** The orchestrator's starting context was 188 KB before the edits, about 75 KB after.

## Dashboard

- **One source of truth (`state.js`), one view (the page), zero build.** The page carries a snapshot of the state inside itself (`ap.py` writes it in), so it opens with data on a double-click, from the panel as `data:`, a month after the run.
- **The Claude panel opens `file://` as `data:`**: from a null origin there is no way to reach `state.js` (verified 2026-08-13 and 2026-08-19). Hence a server on `127.0.0.1` and navigation over http; `preview_start` before `navigate`, `localhost` instead of `127.0.0.1`.
- **Reload is disabled in the panel** (`location.reload()` and `meta refresh` do nothing), so every 10 seconds the page re-fetches `state.js?t=…` instead of reloading.
- **The server is shut down with a 12-second delay** from a separate process: the tool call runs before the message text, and killing it instantly left "in progress" on screen forever.
- **Our own server is recognized only by `-m http.server` and the exact `--directory`.** A broad check once killed the user's dev server (`http-server` from npm), and a shared pid file killed a neighboring run's server. On Windows, command lines come from CIM: there is no `ps`, and without this every call spawned a new server.
- **The clock counts work, not calendar time**: a gap between beats longer than 45 minutes counts as 45. Idle gaps on real runs were 444, 4207 and 7814 minutes; working gaps fit within an hour.
- **Event beats are written by `ap.py`, not the page** (the `beats` log in `state.js`). Previously `updatedAt` was overwritten, and the page knew about past events only if it had seen them itself: if the screen slept or the server died, the gaps between the few remaining beats were cut as idle, and working time was lost (feedback from Codex, 2026-10-02).
- **The server comes back on the same port.** A killed server leaves the port in TIME_WAIT; a check without `SO_REUSEADDR` saw it as busy, the server moved to a new port, and the open tab never came back to life. Now a tab polling the old address picks up the restarted server by itself; two failed http fetches in a row show a "connection lost" banner.
- **The arrow at the top is "refresh now", not a toggle.** People clicked the auto-refresh toggle as "refresh"; it silently turned polling off and remembered that in `localStorage`.
- **Color signals state, not decoration (2.1).** The first 2.1 draft copied the platform's calm style, and everything blended together: "no accents, you can't see what's going on" (user feedback, 2026-10-08). Now each state has one color: blue is work in progress, orange is waiting for the human or a task being written, green is delivered, teal is checking, purple is repair, red is failed.
- **Waiting for the user is its own event (`ask`), not idle time.** Without it the clock kept ticking while the person thought over the questions, and after 45 minutes showed a "pause" that looked like a breakdown. `waits` intervals are subtracted from working time in full.
- **The feed, gates and quotes are written by `ap.py`, not the agent.** Every extra command for the agent is a turn that re-reads its whole context; anything derivable from existing events is derived by the script.
- **The narrow panel is the main screen.** The dashboard is most often viewed in the panel next to the chat (380–900 px), so compact mode kicks in from 900 px, not only on phones.
- **The entrance animation runs only on first render.** The page rewrites `#app` on every event; animating every cycle would flicker.

## Project memory

- **`AGENTS.md` plus a one-line `CLAUDE.md` with `@AGENTS.md`.** Claude Code reads `AGENTS.md` on its own since v2.1.277, but only when there is no `CLAUDE.md` in the folder or above, and not in every session. An import never reads a file twice.
- **The user's own file is left alone**: only a proposal at the end. Never create `AGENTS.md` next to someone else's `CLAUDE.md`: Claude Code will not read it.
- **Memory is pointers, not a retelling.** Every line of that file is paid for by every later session.

## What not to do when editing

- Do not keep in the agent's instructions what only a human needs: put it here, in `docs/`.
- Do not put in text what code can do: everything mechanical goes into `ap.py`.
- Do not read `dashboard-template.html` into context or hand-edit it during a run.
- Do not add a rule without testing it on a run: `tools/measure-run.py` shows both spend by role and where a rule did not fire.
