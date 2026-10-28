# What's new in AISpa

## 1.0.1 — 2026-10-28

- Copies of AISpa 0.x installed from a GitHub Release move to 1.0 through their own updater: the next `/aispa` offers «Установить обновление», and after that the copy keeps updating from Releases the same way.
- `tools/aispa.py` now only updates the package (`update-check`, `update-prepare`, `update-apply`, `update-rollback`, `--version`); any 0.x run command answers in one line that AISpa is 1.0 now and starts again with `/aispa`.
- An unfinished 0.x build is not dropped: «продолжи» finishes it with the 0.x copy pinned inside the build (`.aispa/runs/<run>/runtime/`); a new task first asks whether to finish it or start anew; a finished one is not mentioned. `tools/aispa.py legacy-run` reads it, read-only. `.aispa/` is never touched.
- `update-backups` lists the copies an update kept; `update-rollback --confirm` without an id restores the newest one. The answer to an old 0.x command names the folder with the previous copy and any hand edits.
- A copy `npx skills` put straight into an agent's folder (copy mode) is recognised by the lock and left to `npx skills`; a Release copy the updater may not replace (in git) says why instead of advising `npx skills update`.
- Duplicate copies an agent would load twice (an old 0.x copy in a project, `~/.codex/skills/aispa` beside `~/.agents/skills/aispa`) are named once at the start of a build.
- The template search finds copies installed by 0.x for Codex, Cursor, Gemini, Antigravity and OpenClaw.
- `install.py` skips Releases without the package files instead of failing on them.
- Choice of start mode and depth: two separate questions; the dashboard lost the yellow banner.
- Releases carry `aispa.zip`, `aispa-update.json` and `install.py`, built by `python3 tools/собрать-выпуск.py`.

## 1.0.0 — 2026-10-28

- AISpa 1.0: builds an app, site, bot or feature from a dictated idea — requirements, briefing, spec, tasks, subagents, review, blind acceptance — with a live dashboard in the AI Мастерская design.
