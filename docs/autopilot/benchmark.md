> Текст автора основы — Nick Vels, Autopilot 2.2.0 (MIT); сохранён без изменений.

# Version 2.0 control run

How to check, on the same brief, that the new version is cheaper and no worse than the old one.

## Brief

`~/Documents/VScode/AISpa 2 test/brief.md` is the original brief text of the News Search 2 run (2026-09-30) without the addenda, i.e. what the user dictated before the first questions.

## Baseline: version 1.x, News Search 2

Measured on that session's first pass (T2, 7 tasks, interview mode) with `tools/measure-run.py`, before it continued with tasks 08–11:

| | Norm. units | Share |
|---|---|---|
| **Total** | **29.4M** | |
| Orchestrator | 10.7M | 37% |
| Executors | 9.3M | 32% |
| Review | 6.8M | 23% |
| Repair | 0.9M | 3% |
| Acceptance, memory, ADR, G2 | 1.4M | 5% |

Time: 145 minutes on the clock, about 110 of them active. Repairs on 5 of 7 tasks. Average orchestrator context: 235K tokens.

The comparison is not perfect: the baseline ran in interview mode with user answers, the control run in full. Look at per-role shares and absolute numbers, not just the total.

## The run

In a **new window** opened in the `AISpa 2 test` folder:

```
/aispa full brief.md Это контрольный прогон: платные сервисы (Apify, Exa, Firecrawl) и Telegram — только через адаптеры с заглушками, реальных вызовов и отправок не делать.
```

## What to compare afterwards

```bash
python3 tools/measure-run.py "$HOME/Documents/VScode/AISpa 2 test"
```

1. **Total and per-role shares** against the table above. Expected: review + repair well below 26%, orchestrator below 37%.
2. **Time**, from the run's dashboard (`.aispa-work/dashboard.html`): "in progress" against 110 minutes.
3. **Quality**: the "Blind acceptance" block on the dashboard and the "Open questions" section of the report: how many requirements were confirmed and how many discrepancies.
4. **Did the rules fire**: pre-commit review only for the foundation and risky tasks; follow-up requests carried only blocking items; `state.js` was not hand-edited (no Edit on `state.js` in the orchestrator log); one review of the whole branch at the end.

> **Counting fixed 2026-10-12.** Every number on this page before this note was taken with a `measure-run.py` that counted log lines instead of model calls; Claude Code writes one call as several lines with the same usage. Re-measured: the 2.0 run is **11.26M** (orchestrator 2.70M over 96 calls, not 187), and the whole 1.x News Search 2 session 43.4M instead of 78.3M. Both were inflated about equally (1.9× and 1.8×), so the comparison below holds; the absolute numbers and step counts do not.

## Result: 2026-10-01

The 2.0 run (`full`, T2, 6 tasks + 2 branch-repair tasks) against the first pass of News Search 2 on 1.x (`interview`, T2, 7 tasks). Normalized units from `measure-run.py`.

| | 1.x | 2.0 | |
|---|---|---|---|
| **Total** | 29.4M | **21.1M** | −28% |
| Orchestrator | 10.7M · 301 steps · avg. context 235K | **5.8M** · 187 steps · 189K | −46% |
| Task executors | 9.3M | 6.8M | −27% |
| Review (targeted, branch, re-reviews) | 6.8M | 3.4M | −51% |
| Repair and branch-repair tasks (F1, F2) | 0.9M | 3.0M | more work: more found and fixed |
| Acceptance, G2, memory, ADR | 1.4M | 1.8M | |
| Clock time | 145 min (with interview) | 96 min (excluding waits for the user) | |

What worked as intended: zero manual edits of `state.js` (58 `ap.py` calls); targeted review only on the three flagged tasks; follow-up requests carried the blocking items verbatim; routine work, memory and ADR ran on Sonnet; a plan commit, per-zone commits and a final commit with a list, while the user's `brief.md` stayed uncommitted. Acceptance from a clean clone found three real discrepancies (cron fails in a fresh copy, Python 3.11+ not stated, an inaccurate description of the dry run); task F2 closed them before delivery. 242 tests.

What we tuned afterwards: task F1 (branch repair) went through three re-reviews and a fresh restart, because the reviewer kept finding new sanitizer bypasses; that was 3.2M and ~27 of the 36 minutes in the review stage. Now a task gets at most two re-reviews, and another bypass of the same condition is "not closed", not a new finding. Also, the orchestrator once launched a subagent just to wait; now waiting on a wave ends the turn.

Caveat: one run per version and different modes, so the numbers show direction, not a precise measurement.
