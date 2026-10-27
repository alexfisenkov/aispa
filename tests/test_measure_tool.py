"""tools/measure.py on substitute commands: no model, no network, no environment CLI is ever started."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import measure

FAKE = """\
import json, os, sys, time
mode = sys.argv[1]
repo = os.getcwd()
open(os.path.join(repo, "marker-" + os.path.basename(repo) + ".txt"), "w").write("x")
events = os.environ["AISPA_MEASURE_EVENTS"]
if "events" in sys.argv:
    with open(events, "w") as stream:
        for name in ("question", "stop", "stop", "tool"):
            stream.write(json.dumps({"event": name}) + "\\n")
if mode == "claude":
    print(json.dumps({"type": "result", "result": "done", "num_turns": 3,
                      "usage": {"input_tokens": 100, "output_tokens": 50,
                                "cache_creation_input_tokens": 10, "cache_read_input_tokens": 1000}}))
elif mode == "codex":
    print(json.dumps({"type": "thread.started"}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 200, "cached_input_tokens": 150, "output_tokens": 40}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 300, "cached_input_tokens": 0, "output_tokens": 60}}))
elif mode == "silent":
    print("no json here")
elif mode == "sleep":
    time.sleep(30)
elif mode == "fail":
    sys.exit(3)
"""


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class ToolCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        self.fake = self.tmp / "fake.py"
        self.fake.write_text(FAKE)
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("# Brief\nA tiny CLI utility.\n")
        self.start = self.tmp / "start"
        (self.start / "src").mkdir(parents=True)
        (self.start / "src" / "main.py").write_text("print('hi')\n")
        (self.start / "README.md").write_text("start tree\n")

    def command(self, *args):
        return shlex.join([sys.executable, str(self.fake), *args])

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = measure.main([str(a) for a in argv])
        return code, out.getvalue()

    def pair(self, aispa, autopilot, **extra):
        out = self.tmp / "out"
        argv = ["pair", "--brief", self.brief, "--start-tree", self.start, "--out", out,
                "--aispa-cmd", aispa, "--autopilot-cmd", autopilot, "--runtime", "claude-code",
                "--limit-seconds", extra.pop("limit", 20)]
        code, text = self.run_main(*argv)
        return code, out, json.loads((out / "pair.json").read_text()) if (out / "pair.json").exists() else None


class PairTests(ToolCase):
    def test_two_separate_repositories_start_from_the_same_tree_and_each_command_runs_inside_its_own(self):
        code, out, record = self.pair(self.command("claude"), self.command("claude"))
        self.assertEqual(code, 0)
        a, b = Path(record["aispa"]["repo"]), Path(record["autopilot"]["repo"])
        self.assertNotEqual(a, b)
        self.assertEqual(record["aispa"]["startTree"], record["autopilot"]["startTree"])
        for repo in (a, b):
            self.assertEqual(git(repo, "rev-parse", "HEAD^{tree}"), record["aispa"]["startTree"])
            self.assertTrue((repo / ("marker-" + repo.name + ".txt")).is_file())
        self.assertFalse((a / ("marker-" + b.name + ".txt")).exists())
        self.assertEqual(record["brief"]["sha256"], sha(self.brief))

    def test_time_is_measured_and_exit_codes_are_recorded(self):
        code, out, record = self.pair(self.command("claude"), self.command("fail"))
        self.assertGreater(record["aispa"]["seconds"], 0)
        self.assertEqual((record["aispa"]["exitCode"], record["autopilot"]["exitCode"]), (0, 3))
        self.assertEqual((record["aispa"]["timedOut"], record["autopilot"]["timedOut"]), (False, False))

    def test_the_time_limit_stops_a_slow_command(self):
        began = time.monotonic()
        code, out, record = self.pair(self.command("claude"), self.command("sleep"), limit=1)
        self.assertLess(time.monotonic() - began, 15)
        self.assertTrue(record["autopilot"]["timedOut"])
        self.assertLessEqual(record["autopilot"]["seconds"], 3)
        self.assertFalse(record["aispa"]["timedOut"])

    def test_tokens_come_from_the_environment_answer_and_are_unknown_otherwise(self):
        code, out, record = self.pair(self.command("claude"), self.command("silent"))
        self.assertEqual(record["aispa"]["tokens"], {"input": 100, "output": 50, "cacheCreation": 10,
                                                      "cacheRead": 1000, "total": 1160})
        self.assertEqual(record["autopilot"]["tokens"], "unknown")
        code, out2, record = self.pair(self.command("codex"), self.command("silent"))
        self.assertEqual(record["aispa"]["tokens"], {"input": 500, "output": 100, "total": 600})

    def test_questions_and_stops_come_from_the_events_file_and_are_unknown_without_it(self):
        code, out, record = self.pair(self.command("claude", "events"), self.command("claude"))
        self.assertEqual((record["aispa"]["questions"], record["aispa"]["stops"]), (1, 2))
        self.assertEqual((record["autopilot"]["questions"], record["autopilot"]["stops"]), ("unknown", "unknown"))

    def test_the_start_tree_is_copied_not_moved(self):
        self.pair(self.command("claude"), self.command("claude"))
        self.assertTrue((self.start / "src" / "main.py").is_file())
        self.assertFalse((self.start / ".git").exists())

    def test_a_missing_command_is_a_clear_failure_not_a_traceback(self):
        code, out, record = self.pair("definitely-not-a-program-xyz", self.command("claude"))
        self.assertEqual(record["aispa"]["exitCode"], "not-started")
        self.assertEqual(record["aispa"]["tokens"], "unknown")


class CompareTests(ToolCase):
    def record(self, **sides):
        base = {"seconds": 600.0, "questions": 1, "stops": 1, "tokens": {"total": 1000}, "timedOut": False}
        return {"aispa": {**base, **sides.get("aispa", {})}, "autopilot": {**base, **sides.get("autopilot", {})}}

    def compare(self, record, blind=None):
        pair = self.tmp / "pair.json"
        pair.write_text(json.dumps(record))
        argv = ["compare", "--pair", pair]
        if blind is not None:
            verdict = self.tmp / "blind.json"
            verdict.write_text(json.dumps(blind))
            argv += ["--blind", verdict]
        code, text = self.run_main(*argv)
        return code, json.loads(text)

    def axes(self, result):
        return {row["axis"]: row for row in result["axes"]}

    GOOD = {"aispa": {"unmet": 0, "total": 7}, "autopilot": {"unmet": 1, "total": 7}}

    def test_no_worse_on_every_axis_meets_r02(self):
        code, result = self.compare(self.record(), self.GOOD)
        self.assertEqual(code, 0)
        self.assertEqual({k: v["verdict"] for k, v in self.axes(result).items()},
                         {"brief": "не хуже", "interruptions": "не хуже", "time": "не хуже", "tokens": "не хуже"})
        self.assertEqual(result["r02"], "выполнено")

    def test_a_worse_axis_carries_both_numbers_and_fails_r02(self):
        code, result = self.compare(self.record(aispa={"seconds": 900.0}), self.GOOD)
        row = self.axes(result)["time"]
        self.assertEqual((row["verdict"], row["aispa"], row["autopilot"]), ("хуже", 900.0, 600.0))
        self.assertEqual(result["r02"], "не выполнено")
        self.assertEqual([w["axis"] for w in result["worse"]], ["time"])

    def test_more_unmet_brief_items_or_more_interruptions_are_worse(self):
        code, result = self.compare(self.record(aispa={"questions": 3}),
                                    {"aispa": {"unmet": 2, "total": 7}, "autopilot": {"unmet": 0, "total": 7}})
        self.assertEqual(self.axes(result)["brief"]["verdict"], "хуже")
        self.assertEqual(self.axes(result)["interruptions"]["verdict"], "хуже")
        self.assertEqual((self.axes(result)["interruptions"]["aispa"], self.axes(result)["interruptions"]["autopilot"]), (4, 2))

    def test_an_axis_without_data_is_not_reported_as_not_worse(self):
        code, result = self.compare(self.record(aispa={"tokens": "unknown"}), self.GOOD)
        self.assertEqual(self.axes(result)["tokens"]["verdict"], "нет данных")
        self.assertEqual(result["r02"], "выполнено")  # tokens only where the environment returned them
        code, result = self.compare(self.record(), None)
        self.assertEqual(self.axes(result)["brief"]["verdict"], "нет данных")
        self.assertEqual(result["r02"], "нет данных")
        code, result = self.compare(self.record(autopilot={"questions": "unknown", "stops": "unknown"}), self.GOOD)
        self.assertEqual(self.axes(result)["interruptions"]["verdict"], "нет данных")
        self.assertEqual(result["r02"], "нет данных")

    def test_a_timed_out_run_never_counts_as_faster(self):
        code, result = self.compare(self.record(aispa={"seconds": 100.0, "timedOut": True}), self.GOOD)
        self.assertEqual(self.axes(result)["time"]["verdict"], "хуже")


class BlindPackageTests(ToolCase):
    def make_source(self, tracked_extra=None):
        source = self.tmp / "source"
        source.mkdir()
        (source / "skills").mkdir()
        (source / "skills" / "tool.py").write_text("print(1)\n")
        (source / ".gitignore").write_text(".autopilot/\n")
        for name, text in (tracked_extra or {}).items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text(text)
        (source / ".autopilot" / "plan").mkdir(parents=True)
        for name in ("spec.md", "manifest.md", "interfaces.md"):
            (source / ".autopilot" / "plan" / name).write_text("SECRET PLAN")
        subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
        git(source, "add", "-A")
        for name in tracked_extra or {}:
            git(source, "add", "-f", name)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one"], check=True)
        (source / "later.txt").write_text("later")
        git(source, "add", "-A")
        subprocess.run(["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "two"], check=True)
        return source, git(source, "rev-parse", "HEAD~1"), git(source, "rev-parse", "HEAD")

    def files(self, folder):
        return sorted(str(p.relative_to(folder)) for p in Path(folder).rglob("*") if p.is_file() and ".git/" not in str(p))

    def test_the_judge_folder_has_a_clean_clone_of_the_commit_and_only_the_brief(self):
        source, first, head = self.make_source()
        out = self.tmp / "judge"
        extra = self.tmp / "brief-addendum.md"
        extra.write_text("Addendum\n")
        code, text = self.run_main("blind-package", "--repo", source, "--commit", first, "--brief", self.brief,
                                   "--brief", extra, "--out", out)
        self.assertEqual(code, 0, text)
        self.assertEqual(git(out / "repo", "rev-parse", "HEAD"), first)
        self.assertEqual(git(out / "repo", "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(git(out / "repo", "status", "--porcelain"), "")
        self.assertFalse((out / "repo" / "later.txt").exists())
        listing = self.files(out)
        self.assertEqual([f for f in listing if not f.startswith("repo/")], ["brief/brief-addendum.md", "brief/brief.md"])
        self.assertFalse(any(part in (".autopilot",) or Path(f).name in ("spec.md", "manifest.md", "interfaces.md")
                             for f in listing for part in Path(f).parts))
        self.assertNotIn("SECRET PLAN", "".join((out / f).read_text() for f in listing if f.endswith(".md")))
        self.assertEqual(git(out / "repo", "remote"), "")

    def test_plan_files_among_the_brief_files_are_refused(self):
        source, first, head = self.make_source()
        plan = self.tmp / "spec.md"
        plan.write_text("plan")
        out = self.tmp / "judge"
        code, text = self.run_main("blind-package", "--repo", source, "--commit", head, "--brief", plan, "--out", out)
        self.assertEqual(code, 1)
        self.assertFalse((out / "brief").exists())

    def test_a_commit_that_tracks_plan_material_is_refused_and_leaves_nothing(self):
        source, first, head = self.make_source({".autopilot/leak.md": "x"})
        out = self.tmp / "judge"
        code, text = self.run_main("blind-package", "--repo", source, "--commit", head, "--brief", self.brief, "--out", out)
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())

    def test_an_unknown_commit_is_refused(self):
        source, first, head = self.make_source()
        code, text = self.run_main("blind-package", "--repo", source, "--commit", "0" * 40, "--brief", self.brief,
                                   "--out", self.tmp / "judge")
        self.assertEqual(code, 1)


class IntegrityTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.tmp / "old-run"
        (self.run_dir / "sub").mkdir(parents=True)
        (self.run_dir / "events.jsonl").write_text('{"a":1}\n{"a":2}\n')
        (self.run_dir / "sub" / "x.txt").write_text("x")
        self.snapshot = self.tmp / "snap.json"

    def listing(self):
        return sorted((str(p.relative_to(self.run_dir)), p.stat().st_size, p.stat().st_mtime_ns)
                      for p in self.run_dir.rglob("*"))

    def take(self):
        return self.run_main("integrity", "snapshot", "--dir", self.run_dir, "--out", self.snapshot)

    def verify(self):
        code, text = self.run_main("integrity", "verify", "--dir", self.run_dir, "--snapshot", self.snapshot)
        return code, json.loads(text)

    def test_snapshot_records_sha_size_and_file_list_without_touching_the_directory(self):
        before = self.listing()
        code, _ = self.take()
        self.assertEqual(code, 0)
        self.assertEqual(self.listing(), before)
        data = json.loads(self.snapshot.read_text())
        self.assertEqual(sorted(data["files"]), ["events.jsonl", "sub/x.txt"])
        self.assertEqual(data["files"]["events.jsonl"], {"size": 16, "sha256": sha(self.run_dir / "events.jsonl")})
        self.assertEqual(data["events"], {"size": 16, "sha256": sha(self.run_dir / "events.jsonl")})

    def test_an_unchanged_directory_verifies_and_verify_changes_nothing(self):
        self.take()
        before = self.listing()
        code, result = self.verify()
        self.assertEqual((code, result["same"]), (0, True))
        self.assertEqual(self.listing(), before)

    def test_changes_additions_and_removals_are_named(self):
        self.take()
        (self.run_dir / "events.jsonl").write_text('{"a":1}\n{"a":3}\n')
        (self.run_dir / "new.txt").write_text("n")
        (self.run_dir / "sub" / "x.txt").unlink()
        code, result = self.verify()
        self.assertEqual(code, 1)
        self.assertEqual((result["changed"], result["added"], result["removed"]),
                         (["events.jsonl"], ["new.txt"], ["sub/x.txt"]))
        self.assertFalse(result["same"])

    def test_a_missing_directory_is_a_failure(self):
        code, text = self.run_main("integrity", "snapshot", "--dir", self.tmp / "nowhere", "--out", self.snapshot)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
