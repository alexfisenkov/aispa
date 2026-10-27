#!/usr/bin/env python3
"""Replay the remaining-time estimate over finished AISpa journals and count how often the range held.

    python3 tools/eta_honesty.py --runs DIR [DIR ...] [--write-calibration]

DIR is a project (with .aispa/runs), a runs directory or one run directory.
Journals are only read, through the package reader, in both formats. The report
carries counts and seconds only: no project names, titles or paths.
--write-calibration adds the finished tasks and stages to the calibration file
(AISPA_CALIBRATION_FILE or ~/.aispa/calibration.json), skipping what is there.

For each journal event of a finished run the estimate is recomputed from that
event's state, with the calibration of the runs finished before it. A point is
"ranged" when the tasks row had both bounds. It is "inside" when the real work
time from that event to the last finished task fell within the bounds.
Stage rows are not judged: old journals hold no stage timings.
"""
import argparse
import copy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "aispa" / "tools"))
from aispa_core import calibration, measured_estimate, timing, work_activity  # noqa: E402
from aispa_core.dashboard_reader import DisplayJournal  # noqa: E402

FINISHED = ("accepted", "delivered_with_remaining")
MAX_MISSES = 20


def discover(dirs):
    """(run directories, number of requested paths that do not exist)."""
    runs, missing = [], 0
    for item in dirs:
        base = Path(item)
        if not base.exists():
            print("Каталог не найден: один из переданных путей")
            missing += 1
        elif (base / "events.jsonl").is_file():
            runs.append(base.resolve())
        else:
            for parent in (base, base / ".aispa" / "runs"):
                if parent.is_dir():
                    runs += sorted(path.parent.resolve() for path in parent.glob("*/events.jsonl"))
    return list(dict.fromkeys(runs)), missing


def read_states(run):
    """[(event time, full state)] of a run, or None when the journal cannot be read."""
    if run.parent.name != "runs" or run.parent.parent.name != ".aispa":
        return None
    try:
        reader = DisplayJournal(run.parent.parent.parent)
        reader.run = run
        events = reader.read_journal(include_events=True)["events"]
        return [(event["at"], event["state"]) for event in events]
    except Exception:
        return None


def _owner_wait(state, at):
    waits = state.get("waitTiming") or {}
    return sum(timing.elapsed(waits.get(kind, {}), at) or 0.0 for kind in measured_estimate.OWNER_WAITS)


def _actual(final, state, at, last_finish):
    wall = (work_activity.parse(last_finish) - work_activity.parse(at)).total_seconds()
    waited = max(0.0, _owner_wait(final, final.get("finishedAt") or at) - _owner_wait(state, at))
    return max(0.0, wall - min(waited, wall))


def replay_run(label, states, rows, report):
    """Replay one finished run; returns the calibration rows it adds for later runs."""
    final = states[-1][1]
    finishes = [t["finishedAt"] for t in final.get("tasks", []) if t.get("status") == "completed" and t.get("finishedAt")]
    carried = []
    for at, original in states:
        state = copy.deepcopy(original)
        state["stageSamples"] = list(carried)
        found = measured_estimate.progress_stage_sample(state, at)
        if found:
            carried.append({"key": found[0], "stage": found[1], "seconds": found[2], "finishedAt": at})
            state["stageSamples"] = list(carried)
        if not finishes or state.get("status") != "running" or all(t.get("status") == "completed" for t in state.get("tasks", [])):
            continue
        report["points"] += 1
        row = measured_estimate.estimate(state, at, calibration=rows)["rows"][0]
        if row["lowerSeconds"] is None or row["upperSeconds"] is None:
            continue
        report["ranged"] += 1
        actual = _actual(final, state, at, max(finishes))
        if row["lowerSeconds"] <= actual <= row["upperSeconds"]:
            report["inside"] += 1
        elif len(report["misses"]) < MAX_MISSES:
            report["misses"].append({"run": label, "shownLower": row["lowerSeconds"],
                                     "shownUpper": row["upperSeconds"], "actual": round(actual)})
    final["stageSamples"] = carried
    return measured_estimate.run_samples(final)


def analyse(dirs, *, collect=None):
    runs, missing = discover(dirs)
    report = {"runsRead": len(runs), "runsUsed": 0, "runsSkipped": 0, "points": 0, "ranged": 0, "inside": 0,
              "share": None, "misses": [], "pathsMissing": missing}
    loaded = []
    for run in runs:
        states = read_states(run)
        if states and states[-1][1].get("status") in FINISHED:
            loaded.append(states)
        else:
            report["runsSkipped"] += 1
    loaded.sort(key=lambda states: states[0][1].get("startedAt") or states[0][0])
    rows = []
    for number, states in enumerate(loaded, 1):
        report["runsUsed"] += 1
        added = replay_run(number, states, rows, report)
        rows += added
        if collect is not None:
            collect.extend(added)
    report["share"] = report["inside"] / report["ranged"] if report["ranged"] else None
    return report


def render(report):
    lines = ["Проверка честности оценки времени",
             "Прогонов прочитано: %d, учтено: %d, пропущено: %d" % (report["runsRead"], report["runsUsed"], report["runsSkipped"]),
             "Точек оценки: %d" % report["points"],
             "С диапазоном по задачам: %d" % report["ranged"]]
    if report["share"] is None:
        lines.append("Внутри диапазона: нет точек с диапазоном")
    else:
        lines.append("Внутри диапазона: %d (%d %%)" % (report["inside"], round(report["share"] * 100)))
    lines += ["Промах: прогон %d — показано %d–%d с, на деле %d с" % (m["run"], m["shownLower"], m["shownUpper"], m["actual"])
              for m in report["misses"]]
    return "\n".join(lines)


def write_calibration(samples):
    known = {(r["kind"], r["class"], r["seconds"], r["runtime"], r["recordedAt"]) for r in calibration.load()}
    added = 0
    for sample in samples:
        if (sample["kind"], sample["class"], sample["seconds"], sample["runtime"], sample["recordedAt"]) in known:
            continue
        added += bool(calibration.record(sample))
    return added


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", nargs="+", required=True, metavar="DIR")
    parser.add_argument("--write-calibration", action="store_true")
    args = parser.parse_args(argv)
    samples = []
    report = analyse(args.runs, collect=samples if args.write_calibration else None)
    if not report["runsRead"]:
        print("Журналы прогонов не найдены")
        return 2
    print(render(report))
    if args.write_calibration:
        print("В файл калибровки добавлено образцов: %d" % write_calibration(samples))
    return 0


if __name__ == "__main__":
    sys.exit(main())
