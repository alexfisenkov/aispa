"""Замер паритета AISpa 1.0 и Autopilot 2.2.0: инструмент, обёртка и замер по логам.

Модели не запускаются, сеть не нужна: вместо среды стоят подставные команды и подставные журналы.
"""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import measure

PINNED = "25b5fba"

FAKE = """\
import json, os, sys, time
from pathlib import Path
mode = sys.argv[1]
variant = sys.argv[2] if len(sys.argv) > 2 else ""
repo = os.getcwd()
open(os.path.join(repo, "marker-" + os.path.basename(repo) + ".txt"), "w").write("x")
events = os.environ["AISPA_MEASURE_EVENTS"]
usage = {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 1000}


def rollout(root, thread, parent, model, effort, totals, role="default", path=""):
    folder = Path(root) / "2026" / "10" / "28"
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"id": thread, "agent_role": role, "agent_path": path}
    if parent:
        meta["parent_thread_id"] = parent
    lines = [{"type": "session_meta", "payload": meta},
             {"type": "turn_context", "payload": {"model": model, "effort": effort}},
             {"type": "event_msg", "payload": {"type": "token_count", "info": None}}]
    for inp, out in totals:
        lines.append({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": inp, "cached_input_tokens": 0, "output_tokens": out, "reasoning_output_tokens": 0,
            "total_tokens": inp + out}}}})
    (folder / ("rollout-2026-10-28T00-00-00-%s.jsonl" % thread)).write_text(
        "\\n".join(json.dumps(line) for line in lines) + "\\n")


def claude_agent(root, session, name, agent_type, alias, actual):
    folder = Path(root) / "-project" / session / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ("agent-%s.meta.json" % name)).write_text(json.dumps(
        {"agentType": agent_type, "description": "Шаг 01", "model": alias}))
    (folder / ("agent-%s.jsonl" % name)).write_text(json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "model": actual}}) + "\\n")


if mode in ("events", "events-none"):
    if mode == "events":
        with open(events, "w") as stream:
            for name in ("question", "stop", "stop", "intervention", "intervention", "intervention", "tool"):
                stream.write(json.dumps({"event": name}) + "\\n")
    print(json.dumps({"type": "result", "result": "done", "usage": usage}))
elif mode == "claude":
    print(json.dumps({"type": "result", "result": "done", "num_turns": 3, "usage": usage}))
elif mode == "codex":
    print(json.dumps({"type": "thread.started"}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 200, "cached_input_tokens": 150, "output_tokens": 40}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 300, "cached_input_tokens": 0, "output_tokens": 60}}))
elif mode in ("claude-models", "claude-two"):
    answers = 2 if mode == "claude-two" else 1
    for index in range(answers):
        print(json.dumps({"type": "result", "result": "done", "session_id": "session-%d" % index,
                          "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.5 + index,
                          "modelUsage": {
                              "claude-opus-5-5": {"inputTokens": 100 + index, "outputTokens": 50,
                                                  "cacheReadInputTokens": 1000, "cacheCreationInputTokens": 10},
                              "claude-sonnet-5-5": {"inputTokens": 400, "outputTokens": 70,
                                                    "cacheReadInputTokens": 2000, "cacheCreationInputTokens": 30}}}))
elif mode == "claude-resumed":
    for opus_in, opus_out, cache_read, cache_creation in ((100, 50, 1000, 10), (160, 80, 3000, 30), (160, 80, 3000, 30)):
        print(json.dumps({"type": "result", "result": "done", "session_id": "one-session",
                          "usage": {"input_tokens": 1, "output_tokens": 1},
                          "modelUsage": {"claude-opus-5-5": {"inputTokens": opus_in, "outputTokens": opus_out,
                                                             "cacheReadInputTokens": cache_read,
                                                             "cacheCreationInputTokens": cache_creation}}}))
elif mode == "codex-long":
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 40}}))
    sys.stdout.write("x" * (5 * 1024 * 1024) + "\\n")
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 300, "output_tokens": 60}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}))
elif mode == "codex-subs":
    root = os.environ["AISPA_TEST_CODEX"]
    sub = {"ok": ("gpt-6.1-sol", "high", "default", "/root/worker"),
           "xhigh": ("gpt-6.1-sol", "xhigh", "default", "/root/worker"),
           "luna": ("gpt-6.1-luna", "low", "default", "/root/worker"),
           "luna-scripted": ("gpt-6.1-luna", "low", "default", "/root/aispa-mechanic-scripted")}[variant]
    print(json.dumps({"type": "thread.started", "thread_id": "thread-main"}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 500,
                                                           "output_tokens": 100, "reasoning_output_tokens": 20}}))
    if variant != "nologs":
        rollout(root, "thread-main", None, "gpt-6.1-sol", "high", [(1000, 100)])
        rollout(root, "thread-sub1", "thread-main", sub[0], sub[1], [(100, 10), (300, 30)], sub[2], sub[3])
        rollout(root, "thread-sub2", "thread-sub1", "gpt-6.1-sol", "medium", [(50, 5)])
        rollout(root, "thread-stranger", "thread-other", "fable", "max", [(9999, 999)])
elif mode == "claude-subs":
    root = os.environ["AISPA_TEST_CLAUDE"]
    print(json.dumps({"type": "result", "result": "done", "session_id": "sess-1", "usage": {"input_tokens": 1},
                      "modelUsage": {"claude-opus-5-5": {"inputTokens": 10, "outputTokens": 5}}}))
    if variant != "nologs":
        (Path(root) / "-project").mkdir(parents=True, exist_ok=True)
        (Path(root) / "-project" / "sess-1.jsonl").write_text("{}\\n")
        claude_agent(root, "sess-1", "a", "aispa-worker", "sonnet", "claude-sonnet-5-5")
        claude_agent(root, "sess-1", "b", "aispa-judge", "opus", "claude-opus-5-5")
        if variant == "scripted":
            claude_agent(root, "sess-1", "c", "aispa-mechanic-scripted", "haiku", "claude-haiku-5-5")
        elif variant == "haiku":
            claude_agent(root, "sess-1", "c", "general-purpose", "haiku", "claude-haiku-5-5")
        elif variant == "fable":
            claude_agent(root, "sess-1", "c", "aispa-worker", "sonnet", "claude-fable-5")
elif mode == "env":
    Path(repo, "seen-env.json").write_text(json.dumps({
        "request": Path(os.environ["AISPA_MEASURE_REQUEST"]).read_text(),
        "install": os.environ["AISPA_MEASURE_INSTALL"], "mode": os.environ["AISPA_MEASURE_MODE"],
        "autopilot": os.environ["AISPA_MEASURE_AUTOPILOT_SHA"]}))
    print(json.dumps({"type": "result", "result": "done", "usage": usage}))
elif mode == "deliver":
    # variant: aispa | autopilot | wip | nofinal | wrongdir
    work = {"aispa": ".aispa-work", "autopilot": ".autopilot", "wip": ".aispa-work", "nofinal": ".aispa-work",
            "wrongdir": ".autopilot"}[variant]
    name = "2026-10-28-demo--wip" if variant == "wip" else "2026-10-28-demo"
    Path(repo, work, name).mkdir(parents=True)
    if variant != "nofinal":
        Path(os.environ["AISPA_MEASURE_FINAL"]).write_text("Готово, всё сдано.")
    print(json.dumps({"type": "result", "result": "done", "usage": usage}))
elif mode == "limit":
    print(json.dumps({"type": "result", "is_error": True, "api_error_status": 429, "result": "Request failed",
                      "usage": usage}))
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
        self.codex = self.tmp / "codex-sessions"
        self.claude = self.tmp / "claude-projects"
        patch = mock.patch.dict(os.environ, {"AISPA_TEST_CODEX": str(self.codex), "AISPA_TEST_CLAUDE": str(self.claude)})
        patch.start()
        self.addCleanup(patch.stop)

    def command(self, *args):
        return shlex.join([sys.executable, str(self.fake), *args])

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            try:
                code = measure.main([str(a) for a in argv])
            except SystemExit as stop:  # argparse refuses unknown flags with exit code 2
                code = stop.code
        return code, out.getvalue()

    def pair(self, aispa, autopilot, extra=(), limit=20):
        out = self.tmp / "out"
        argv = ["pair", "--brief", self.brief, "--start-tree", self.start, "--out", out,
                "--aispa-cmd", aispa, "--autopilot-cmd", autopilot, "--runtime", "claude-code",
                "--limit-seconds", limit, "--codex-sessions", self.codex, "--claude-projects", self.claude, *extra]
        code, text = self.run_main(*argv)
        record = json.loads((out / "pair.json").read_text()) if (out / "pair.json").exists() else None
        return code, out, record


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

    def test_claude_tokens_count_every_model_of_model_usage_not_only_the_main_chat(self):
        code, out, record = self.pair(self.command("claude-models"), self.command("claude-two"))
        self.assertEqual(record["aispa"]["tokens"], {"input": 500, "output": 120, "cacheRead": 3000,
                                                      "cacheCreation": 40, "total": 3660})
        self.assertEqual(record["autopilot"]["tokens"], {"input": 1001, "output": 240, "cacheRead": 6000,
                                                          "cacheCreation": 80, "total": 7321})

    def test_claude_cost_is_the_last_total_cost_of_each_session_added_up(self):
        code, out, record = self.pair(self.command("claude-two"), self.command("claude"))
        self.assertEqual(record["aispa"]["costUsd"], 2.0)  # 0.5 + 1.5, two sessions
        self.assertEqual(record["autopilot"]["costUsd"], "unknown")

    def test_a_resumed_session_counts_its_last_running_total_once(self):
        code, out, record = self.pair(self.command("claude-resumed"), self.command("silent"))
        self.assertEqual(record["aispa"]["tokens"], {"input": 160, "output": 80, "cacheRead": 3000,
                                                      "cacheCreation": 30, "total": 3270})

    def test_tokens_are_counted_on_the_whole_stdout_and_only_the_saved_copy_is_cut(self):
        code, out, record = self.pair(self.command("codex-long"), self.command("silent"), limit=60)
        self.assertEqual(record["aispa"]["tokens"], {"input": 507, "output": 103, "total": 610})
        self.assertLessEqual((out / "aispa-stdout.txt").stat().st_size, measure.MAX_STDOUT_BYTES)

    def test_codex_tokens_add_every_subagent_rollout_of_the_main_thread_and_nested_ones(self):
        code, out, record = self.pair(self.command("codex-subs", "ok"), self.command("silent"))
        # Hand sums: main 1000/100; sub1's last total 300/30 (not the earlier 100/10); its child 50/5;
        # the rollout of a stranger thread (9999/999) is not counted.
        self.assertEqual(record["aispa"]["tokens"], {"input": 1350, "output": 135, "total": 1485})

    def test_codex_tokens_are_unknown_when_the_rollout_logs_cannot_be_read(self):
        code, out, record = self.pair(self.command("codex-subs", "nologs"), self.command("silent"))
        self.assertEqual(record["aispa"]["tokens"], "unknown")
        self.assertEqual(record["aispa"]["models"]["source"], "unknown")

    def test_questions_stops_and_interventions_come_from_the_events_file_and_are_unknown_without_it(self):
        code, out, record = self.pair(self.command("events"), self.command("events-none"))
        self.assertEqual((record["aispa"]["questions"], record["aispa"]["stops"], record["aispa"]["interventions"]),
                         (1, 2, 3))
        for key in ("questions", "stops", "interventions"):
            self.assertEqual(record["autopilot"][key], "unknown")

    def test_the_start_tree_is_copied_not_moved(self):
        self.pair(self.command("claude"), self.command("claude"))
        self.assertTrue((self.start / "src" / "main.py").is_file())
        self.assertFalse((self.start / ".git").exists())

    def test_a_missing_command_is_a_clear_failure_not_a_traceback(self):
        code, out, record = self.pair("definitely-not-a-program-xyz", self.command("claude"))
        self.assertEqual(record["aispa"]["exitCode"], "not-started")
        self.assertEqual(record["aispa"]["tokens"], "unknown")


class PairSameTreatmentTests(ToolCase):
    def seen(self, side, record):
        return json.loads((Path(record[side]["repo"]) / "seen-env.json").read_text())

    def test_both_sides_get_the_same_request_text_and_the_same_install_method(self):
        code, out, record = self.pair(self.command("env"), self.command("env"))
        self.assertEqual(code, 0)
        mine, theirs = self.seen("aispa", record), self.seen("autopilot", record)
        self.assertEqual(mine, theirs)
        self.assertEqual(mine["request"], self.brief.read_text())
        self.assertEqual((record["install"], mine["install"]), ("project", "project"))
        self.assertEqual(record["request"]["sha256"], hashlib.sha256(mine["request"].encode()).hexdigest())

    def test_the_install_method_is_named_in_pair_json_and_passed_to_both(self):
        code, out, record = self.pair(self.command("env"), self.command("env"), extra=["--install", "global"])
        self.assertEqual(record["install"], "global")
        self.assertEqual(self.seen("aispa", record)["install"], "global")
        self.assertEqual(self.seen("autopilot", record)["install"], "global")

    def test_an_own_request_file_replaces_the_brief_text_for_both_and_the_mode_is_recorded(self):
        request = self.tmp / "request.txt"
        request.write_text("продолжай\n")
        code, out, record = self.pair(self.command("env"), self.command("env"),
                                      extra=["--request", request, "--mode", "continue"])
        self.assertEqual(record["mode"], "continue")
        self.assertEqual(self.seen("aispa", record)["request"], "продолжай\n")
        self.assertEqual(self.seen("autopilot", record)["mode"], "continue")

    def test_autopilot_is_the_pinned_commit_and_any_other_one_is_refused_before_anything_runs(self):
        code, out, record = self.pair(self.command("claude"), self.command("claude"), extra=["--autopilot-sha", "abc1234"])
        self.assertEqual(code, 2)
        self.assertIsNone(record)
        code, out, record = self.pair(self.command("env"), self.command("env"))
        self.assertEqual(self.seen("autopilot", record)["autopilot"], PINNED)
        self.assertEqual(record["autopilotSha"], PINNED)
        full = PINNED + "0" * 33
        code, out, record = self.pair(self.command("claude"), self.command("claude"), extra=["--autopilot-sha", full])
        self.assertEqual(code, 0)
        self.assertEqual(record["autopilotSha"], full)

    def test_a_given_autopilot_checkout_at_another_commit_is_refused(self):
        source = self.tmp / "autopilot-src"
        source.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
        (source / "f").write_text("x")
        git(source, "add", "-A")
        subprocess.run(["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"],
                       check=True)
        code, out, record = self.pair(self.command("claude"), self.command("claude"),
                                      extra=["--autopilot-src", source])
        self.assertEqual(code, 2)
        self.assertIsNone(record)

    def test_the_order_of_the_sides_can_alternate(self):
        code, out, record = self.pair(self.command("claude"), self.command("claude"), extra=["--first", "autopilot"])
        self.assertEqual(record["order"], ["autopilot", "aispa"])
        self.assertLess(record["autopilot"]["startedAt"], record["aispa"]["startedAt"])


class DeliveredTests(ToolCase):
    def delivered(self, variant):
        code, out, record = self.pair(self.command("deliver", variant), self.command("deliver", variant))
        return record["aispa"]["delivered"], record["autopilot"]["delivered"]

    def test_delivered_is_a_run_folder_without_wip_in_the_sides_own_work_folder_and_a_final_message(self):
        self.assertEqual(self.delivered("aispa"), (True, False))
        self.assertEqual(self.delivered("autopilot"), (False, True))

    def test_an_unfinished_folder_a_missing_final_message_or_the_other_folder_is_not_delivered(self):
        self.assertEqual(self.delivered("wip"), (False, False))
        self.assertEqual(self.delivered("nofinal"), (False, False))
        self.assertEqual(self.delivered("wrongdir"), (False, True))

    def test_nothing_on_disk_is_not_delivered(self):
        code, out, record = self.pair(self.command("claude"), self.command("claude"))
        self.assertEqual((record["aispa"]["delivered"], record["autopilot"]["delivered"]), (False, False))


class LimitTests(ToolCase):
    def test_a_429_answer_marks_the_side_and_the_whole_pair_invalid(self):
        code, out, record = self.pair(self.command("claude"), self.command("limit"))
        self.assertEqual(code, 1)
        self.assertTrue(record["autopilot"]["rateLimited"])
        self.assertFalse(record["aispa"]["rateLimited"])
        self.assertFalse(record["valid"])
        self.assertIn("limit", " ".join(record["invalidReasons"]))

    def test_numbers_that_contain_429_do_not_count_as_a_limit(self):
        code, out, record = self.pair(self.command("claude-models"), self.command("claude"))
        self.assertTrue(record["valid"])
        self.assertFalse(measure.limit_hit('{"type":"result","usage":{"input_tokens":429}}'))

    def test_other_ways_a_limit_shows_in_the_two_environments(self):
        self.assertTrue(measure.limit_hit('{"type":"error","message":"You hit your usage limit"}'))
        self.assertTrue(measure.limit_hit('{"type":"turn.failed","error":{"message":"429 Too Many Requests"}}'))
        self.assertFalse(measure.limit_hit('{"type":"result","result":"I added a rate limit to the tracker"}'))


class ModelsTests(ToolCase):
    def models(self, left, right="silent"):
        code, out, record = self.pair(self.command(*left), self.command(right))
        return record["aispa"]["models"]

    def test_claude_subagent_logs_give_the_launches_and_no_violations_for_allowed_models(self):
        models = self.models(("claude-subs", "ok"))
        self.assertEqual((models["source"], models["launches"], models["violations"]), ("claude-logs", 2, []))

    def test_haiku_is_allowed_only_for_the_mechanic_with_a_script_check(self):
        self.assertEqual(self.models(("claude-subs", "scripted"))["violations"], [])
        found = self.models(("claude-subs", "haiku"))["violations"]
        self.assertEqual(len(found), 1)
        self.assertIn("haiku", found[0]["model"])

    def test_fable_is_a_violation(self):
        found = self.models(("claude-subs", "fable"))["violations"]
        self.assertEqual([v["model"] for v in found], ["claude-fable-5"])

    def test_claude_models_are_unknown_when_the_session_log_is_not_found(self):
        models = self.models(("claude-subs", "nologs"))
        self.assertEqual((models["source"], models["violations"]), ("unknown", "unknown"))

    def test_codex_rollouts_give_models_and_effort_of_the_subagents_only(self):
        models = self.models(("codex-subs", "ok"))
        self.assertEqual((models["source"], models["launches"], models["violations"]), ("codex-rollouts", 2, []))

    def test_a_banned_effort_and_a_cheap_model_outside_the_scripted_mechanic_are_violations(self):
        self.assertEqual([v["effort"] for v in self.models(("codex-subs", "xhigh"))["violations"]], ["xhigh"])
        self.assertEqual([v["model"] for v in self.models(("codex-subs", "luna"))["violations"]], ["gpt-6.1-luna"])
        self.assertEqual(self.models(("codex-subs", "luna-scripted"))["violations"], [])


def pair_record(runtime="claude-code", mode="run", brief="b1", valid=True, aispa=None, autopilot=None):
    side = {"seconds": 600.0, "timedOut": False, "questions": 0, "stops": 0, "interventions": 0,
            "tokens": {"total": 1000}, "costUsd": "unknown", "rateLimited": False, "delivered": True,
            "models": {"source": "claude-logs", "launches": 2, "violations": []}}
    return {"runtime": runtime, "mode": mode, "brief": {"sha256": brief}, "valid": valid,
            "invalidReasons": [] if valid else ["limit"], "aispa": {**side, **(aispa or {})},
            "autopilot": {**side, **(autopilot or {})}}


def verdict_record(aispa=None, autopilot=None):
    base = {"unmet": 1, "substitutions": 0, "additions": 1}
    return {"aispa": {**base, **(aispa or {})}, "autopilot": {**base, **(autopilot or {})}}


class CompareTests(ToolCase):
    def compare(self, pairs, verdicts=None):
        argv = ["compare"]
        for index, record in enumerate(pairs):
            path = self.tmp / ("pair-%d.json" % index)
            path.write_text(json.dumps(record))
            argv += ["--pair", path]
        for index, record in enumerate(verdicts or []):
            path = self.tmp / ("blind-%d.json" % index)
            path.write_text(json.dumps(record))
            argv += ["--blind", path]
        code, text = self.run_main(*argv)
        return code, json.loads(text) if text.lstrip().startswith("{") else text

    def axes(self, result, group=0):
        return {row["axis"]: row for row in result["groups"][group]["axes"]}

    def test_equal_within_a_fifth_on_time_and_tokens_is_on_par_and_the_ratio_is_shown(self):
        pairs = [pair_record(aispa={"seconds": 700.0, "tokens": {"total": 1100}}),
                 pair_record(aispa={"seconds": 690.0, "tokens": {"total": 1150}})]
        code, result = self.compare(pairs, [verdict_record(), verdict_record()])
        axes = self.axes(result)
        self.assertEqual((axes["time"]["verdict"], axes["tokens"]["verdict"]), ("наравне", "наравне"))
        self.assertAlmostEqual(axes["time"]["ratio"], 695.0 / 600.0, places=3)  # median of 700 and 690
        self.assertEqual(result["verdict"], "наравне")
        self.assertEqual(code, 0)

    def test_the_median_not_the_mean_decides_and_more_than_one_point_two_times_is_worse(self):
        pairs = [pair_record(aispa={"seconds": 700.0}), pair_record(aispa={"seconds": 900.0}),
                 pair_record(aispa={"seconds": 1000.0})]
        code, result = self.compare(pairs, [verdict_record()] * 3)
        row = self.axes(result)["time"]
        self.assertEqual((row["aispa"], row["autopilot"], row["verdict"]), (900.0, 600.0, "хуже"))
        self.assertEqual((result["verdict"], code), ("хуже", 1))

    def test_clearly_less_than_the_other_side_is_better_and_the_whole_verdict_is_better_if_nothing_is_worse(self):
        pairs = [pair_record(aispa={"seconds": 500.0, "tokens": {"total": 800}})]
        code, result = self.compare(pairs, [verdict_record(aispa={"unmet": 0})])
        axes = self.axes(result)
        self.assertEqual((axes["time"]["verdict"], axes["tokens"]["verdict"], axes["unmet"]["verdict"]),
                         ("лучше", "лучше", "лучше"))
        self.assertEqual((result["verdict"], code), ("лучше", 0))

    def test_without_the_judge_verdict_the_brief_axes_have_no_data(self):
        code, result = self.compare([pair_record()])
        axes = self.axes(result)
        for name in ("unmet", "substitutions", "additions"):
            self.assertEqual(axes[name]["verdict"], "нет данных")
        self.assertEqual((result["verdict"], code), ("нет данных", 1))

    def test_unknown_tokens_are_no_data_never_an_estimate(self):
        code, result = self.compare([pair_record(aispa={"tokens": "unknown"})], [verdict_record()])
        self.assertEqual(self.axes(result)["tokens"]["verdict"], "нет данных")
        self.assertEqual(result["verdict"], "нет данных")

    def test_unmet_items_and_substituted_ways_and_additions_are_each_compared(self):
        code, result = self.compare([pair_record()], [verdict_record(
            aispa={"unmet": 2, "substitutions": 1, "additions": 4}, autopilot={"unmet": 1, "additions": 1})])
        axes = self.axes(result)
        self.assertEqual({k: axes[k]["verdict"] for k in ("unmet", "substitutions", "additions")},
                         {"unmet": "хуже", "substitutions": "хуже", "additions": "хуже"})
        self.assertEqual((axes["unmet"]["aispa"], axes["unmet"]["autopilot"]), (2, 1))

    def test_human_interventions_and_questions_with_stops_are_separate_axes(self):
        code, result = self.compare([pair_record(aispa={"interventions": 1}, autopilot={"questions": 1})],
                                    [verdict_record()])
        axes = self.axes(result)
        self.assertEqual((axes["interventions"]["verdict"], axes["questions_stops"]["verdict"]), ("хуже", "лучше"))

    # Parity has absolute conditions: no substituted way, 0 interventions, a result on disk on every run.
    # One breach on the AISpa side in the group is «хуже», even when Autopilot has the same.
    def test_one_substituted_way_on_the_aispa_side_is_worse_even_against_the_same_count(self):
        code, result = self.compare([pair_record()], [verdict_record(aispa={"substitutions": 1},
                                                                     autopilot={"substitutions": 1})])
        self.assertEqual(self.axes(result)["substitutions"]["verdict"], "хуже")
        self.assertEqual((result["verdict"], code), ("хуже", 1))

    def test_one_intervention_on_the_aispa_side_is_worse_even_against_the_same_count(self):
        code, result = self.compare([pair_record(aispa={"interventions": 1}, autopilot={"interventions": 1})],
                                    [verdict_record()])
        self.assertEqual(self.axes(result)["interventions"]["verdict"], "хуже")
        self.assertEqual((result["verdict"], code), ("хуже", 1))

    def test_one_undelivered_aispa_run_in_the_group_is_worse_even_against_the_same_pattern(self):
        pairs = [pair_record(), pair_record(aispa={"delivered": False}, autopilot={"delivered": False})]
        code, result = self.compare(pairs, [verdict_record(), verdict_record()])
        self.assertEqual(self.axes(result)["delivered"]["verdict"], "хуже")
        self.assertEqual((result["verdict"], code), ("хуже", 1))

    def test_a_run_that_was_not_delivered_is_worse_than_one_that_was(self):
        code, result = self.compare([pair_record(aispa={"delivered": False})], [verdict_record()])
        self.assertEqual(self.axes(result)["delivered"]["verdict"], "хуже")

    def test_a_run_that_hit_the_time_limit_is_never_faster(self):
        code, result = self.compare([pair_record(aispa={"seconds": 100.0, "timedOut": True})], [verdict_record()])
        self.assertEqual(self.axes(result)["time"]["verdict"], "хуже")

    def test_banned_models_on_the_aispa_side_are_worse_and_the_other_sides_are_only_shown(self):
        bad = {"source": "codex-rollouts", "launches": 1, "violations": [{"model": "fable"}]}
        code, result = self.compare([pair_record(aispa={"models": bad})], [verdict_record()])
        self.assertEqual(self.axes(result)["models"]["verdict"], "хуже")
        code, result = self.compare([pair_record(autopilot={"models": bad})], [verdict_record()])
        row = self.axes(result)["models"]
        self.assertEqual((row["verdict"], row["autopilot"]), ("наравне", 1))
        unknown = {"source": "unknown", "launches": "unknown", "violations": "unknown"}
        code, result = self.compare([pair_record(aispa={"models": unknown})], [verdict_record()])
        self.assertEqual(self.axes(result)["models"]["verdict"], "нет данных")

    def test_cost_is_compared_where_both_sides_have_it_and_its_absence_does_not_block(self):
        code, result = self.compare([pair_record(aispa={"costUsd": 1.1}, autopilot={"costUsd": 1.0})], [verdict_record()])
        self.assertEqual(self.axes(result)["cost"]["verdict"], "наравне")
        code, result = self.compare([pair_record()], [verdict_record()])
        self.assertEqual(self.axes(result)["cost"]["verdict"], "нет данных")
        self.assertEqual(result["verdict"], "наравне")

    def test_an_invalid_pair_is_named_and_left_out_of_the_medians(self):
        pairs = [pair_record(aispa={"seconds": 600.0}),
                 pair_record(valid=False, aispa={"seconds": 9000.0}, autopilot={"rateLimited": True})]
        code, result = self.compare(pairs, [verdict_record(), verdict_record()])
        self.assertEqual(result["invalid"], [{"pair": 1, "reason": "limit"}])
        self.assertEqual(self.axes(result)["time"]["aispa"], 600.0)
        self.assertEqual(result["verdict"], "наравне")

    def test_only_invalid_pairs_leave_no_data(self):
        code, result = self.compare([pair_record(valid=False)], [verdict_record()])
        self.assertEqual((result["verdict"], code), ("нет данных", 1))

    def test_each_environment_and_mode_is_judged_on_its_own_and_one_bad_group_spoils_the_whole(self):
        pairs = [pair_record(runtime="claude-code"), pair_record(runtime="codex", aispa={"seconds": 2000.0}),
                 pair_record(runtime="codex", mode="continue")]
        code, result = self.compare(pairs, [verdict_record()] * 3)
        self.assertEqual(len(result["groups"]), 3)
        self.assertEqual([g["verdict"] for g in result["groups"]], ["наравне", "хуже", "наравне"])
        self.assertEqual(result["verdict"], "хуже")

    def test_the_number_of_judge_verdicts_must_match_the_pairs(self):
        code, text = self.compare([pair_record(), pair_record()], [verdict_record()])
        self.assertEqual(code, 2)


class ParseTokensTests(unittest.TestCase):
    @staticmethod
    def answer(session, opus_input, opus_output=0, **extra):
        item = {"type": "result", "modelUsage": {"claude-opus-5-5": {"inputTokens": opus_input,
                                                                       "outputTokens": opus_output}}}
        if session is not None:
            item["session_id"] = session
        return {**item, **extra}

    @staticmethod
    def text(*items):
        return "\n".join(json.dumps(item) for item in items)

    def test_answers_of_one_session_give_the_last_running_total(self):
        tokens = measure.parse_tokens(self.text(self.answer("a", 100, 10), self.answer("a", 160, 40),
                                                self.answer("a", 190, 90)))
        self.assertEqual(tokens, {"input": 190, "output": 90, "total": 280})

    def test_different_sessions_add_their_last_totals(self):
        tokens = measure.parse_tokens(self.text(self.answer("a", 100), self.answer("b", 1000),
                                                self.answer("a", 160), self.answer("b", 1500)))
        self.assertEqual(tokens, {"input": 1660, "output": 0, "total": 1660})

    def test_a_turn_stuck_on_the_limit_with_the_same_total_adds_nothing(self):
        stuck = self.answer("a", 160, 40)
        once = measure.parse_tokens(self.text(self.answer("a", 100, 10), stuck))
        repeated = measure.parse_tokens(self.text(self.answer("a", 100, 10), stuck, stuck, stuck))
        self.assertEqual(repeated, once)
        self.assertEqual(repeated, {"input": 160, "output": 40, "total": 200})

    def test_an_answer_without_session_id_is_its_own_session(self):
        tokens = measure.parse_tokens(self.text(self.answer(None, 7), self.answer(None, 5), self.answer("a", 100)))
        self.assertEqual(tokens, {"input": 112, "output": 0, "total": 112})

    def test_without_model_usage_every_result_usage_is_summed(self):
        def result(tokens):
            return {"type": "result", "session_id": "a", "usage": {"input_tokens": tokens, "output_tokens": 1}}
        tokens = measure.parse_tokens(self.text(result(10), result(20), result(0)))
        self.assertEqual(tokens, {"input": 30, "output": 3, "total": 33})

    def test_codex_turns_without_a_thread_are_summed(self):
        turn = {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}}
        self.assertEqual(measure.parse_tokens(self.text(turn, turn)), {"input": 10, "output": 2, "total": 12})

    @staticmethod
    def started(thread):
        return {"type": "thread.started", "thread_id": thread}

    @staticmethod
    def turn(tokens, output=1):
        return {"type": "turn.completed", "usage": {"input_tokens": tokens, "cached_input_tokens": tokens // 2,
                                                    "output_tokens": output, "reasoning_output_tokens": 1}}

    def test_codex_turns_of_one_session_give_the_last_running_total(self):
        tokens = measure.parse_tokens(self.text(self.started("t1"), self.turn(100, 10), self.turn(160, 40),
                                                self.turn(190, 90)))
        self.assertEqual(tokens, {"input": 190, "output": 90, "total": 280})

    def test_codex_resumed_turns_with_the_same_thread_id_count_once(self):
        tokens = measure.parse_tokens(self.text(self.started("t1"), self.turn(100, 10), self.started("t1"),
                                                self.turn(160, 40), self.started("t1"), self.turn(160, 40)))
        self.assertEqual(tokens, {"input": 160, "output": 40, "total": 200})

    def test_codex_sessions_add_their_last_totals(self):
        tokens = measure.parse_tokens(self.text(self.started("t1"), self.turn(100, 10), self.turn(160, 40),
                                                self.started("t2"), self.turn(1000, 100), self.turn(1500, 200)))
        self.assertEqual(tokens, {"input": 1660, "output": 240, "total": 1900})

    def test_no_numbers_stay_unknown(self):
        self.assertEqual(measure.parse_tokens("no json"), "unknown")


class BlindPackageTests(ToolCase):
    def make_source(self, tracked_extra=None):
        source = self.tmp / "source"
        source.mkdir()
        (source / "skills").mkdir()
        (source / "skills" / "tool.py").write_text("print(1)\n")
        (source / ".gitignore").write_text(".autopilot/\n.aispa-work/\n")
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
        self.assertFalse(any(part in (".autopilot", ".aispa-work") or Path(f).name in ("spec.md", "manifest.md", "interfaces.md")
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
        for leak in (".autopilot/leak.md", ".aispa-work/leak.md"):
            with self.subTest(leak=leak):
                source, first, head = self.make_source({leak: "x"})
                out = self.tmp / "judge"
                code, text = self.run_main("blind-package", "--repo", source, "--commit", head, "--brief", self.brief,
                                           "--out", out)
                self.assertEqual(code, 1)
                self.assertFalse(out.exists())
                shutil.rmtree(source)

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

    def test_a_missing_directory_is_a_failure(self):
        code, text = self.run_main("integrity", "snapshot", "--dir", self.tmp / "nowhere", "--out", self.snapshot)
        self.assertEqual(code, 2)


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WrapperTests(ToolCase):
    """The wrapper runs a stand-in `claude` or `codex` program; no model is started."""

    FAKE_CLAUDE = """\
#!{python}
import json, os, sys
from pathlib import Path
repo = Path(os.getcwd())
count = repo / ".fake-count"
turn = int(count.read_text()) + 1 if count.exists() else 1
count.write_text(str(turn))
with open(repo / ".fake-calls.jsonl", "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
deliver_on = int(os.environ.get("FAKE_DELIVER_ON", "2"))
if turn == deliver_on:
    (repo / ".aispa-work" / "2026-10-28-demo").mkdir(parents=True, exist_ok=True)
    (repo / ".autopilot" / "2026-10-28-demo").mkdir(parents=True, exist_ok=True)
text = "Сдано, всё готово." if turn == deliver_on else ("Что выбрать, А или Б?" if turn % 2 else "Пока остановился.")
print(json.dumps({{"type": "result", "session_id": "s1", "result": text,
                   "modelUsage": {{"claude-opus-5-5": {{"inputTokens": 10 * turn, "outputTokens": turn}}}}}}))
"""
    FAKE_CODEX = """\
#!{python}
import json, os, sys
from pathlib import Path
repo = Path(os.getcwd())
out = sys.argv[sys.argv.index("-o") + 1]
(repo / ".autopilot" / "2026-10-28-demo").mkdir(parents=True, exist_ok=True)
Path(out).write_text("Итог: сдано.")
print(json.dumps({"type": "thread.started", "thread_id": "t-9"}))
print("not json")
print(json.dumps({"type": "item.completed", "item": {"text": "x"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}}))
"""

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("x")
        git(self.repo, "add", "-A")
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "-m", "x"], check=True)
        self.events = self.tmp / "events.jsonl"
        self.final = self.tmp / "final.txt"
        self.request = self.tmp / "request.txt"
        self.request.write_text("Сделай трекер.\n")
        self.skill = self.tmp / "skill-src"
        (self.skill / "agents" / "claude-code").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("skill")
        (self.skill / "agents" / "claude-code" / "aispa-worker.md").write_text("agent")

    def program(self, name, template, doubled=False):
        # A `.py` stand-in without the execute bit: the wrapper starts it with the current Python,
        # as Windows (no shebang, no execute bit) needs on every OS of the CI matrix.
        path = self.tmp / (name + ".py")
        text = template.replace("{python}", sys.executable)
        path.write_text(text.replace("{{", "{").replace("}}", "}") if doubled else text)
        return path

    def wrapper(self, *args, claude=None, codex=None, extra_env=None):
        env = {**os.environ, "AISPA_MEASURE_BRIEF": str(self.brief), "AISPA_MEASURE_REPO": str(self.repo),
               "AISPA_MEASURE_EVENTS": str(self.events), "AISPA_MEASURE_REQUEST": str(self.request),
               "AISPA_MEASURE_FINAL": str(self.final), "AISPA_MEASURE_INSTALL": "project",
               "AISPA_MEASURE_MODE": "run", "AISPA_MEASURE_AUTOPILOT_SHA": PINNED, **(extra_env or {})}
        if claude:
            env["AISPA_MEASURE_CLAUDE"] = str(claude)
        if codex:
            env["AISPA_MEASURE_CODEX"] = str(codex)
        return subprocess.run([sys.executable, str(ROOT / "tools" / "measure_wrapper.py"), *map(str, args)],
                              cwd=self.repo, env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        return [json.loads(line) for line in (self.repo / ".fake-calls.jsonl").read_text().splitlines()]

    def written_events(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_both_skills_get_the_same_request_and_differ_only_in_the_call_name(self):
        wrapper = load_script("measure_wrapper", "measure_wrapper.py")
        request = "Сделай трекер.\n"
        self.assertEqual(wrapper.first_prompt("claude", "aispa", request, "run"), "/aispa full\n\n" + request)
        self.assertEqual(wrapper.first_prompt("claude", "autopilot", request, "run"), "/autopilot full\n\n" + request)
        self.assertEqual(wrapper.first_prompt("codex", "aispa", request, "run"), "$aispa full\n\n" + request)
        self.assertEqual(wrapper.first_prompt("codex", "autopilot", "", "continue"), "$autopilot")
        self.assertEqual(wrapper.first_prompt("claude", "aispa", "продолжай", "continue"), "/aispa продолжай")

    def test_a_turn_that_ends_with_a_question_is_an_event_and_every_continue_is_an_intervention(self):
        claude = self.program("claude-fake", self.FAKE_CLAUDE, doubled=True)
        done = self.wrapper("--runtime", "claude", "--skill", "aispa", claude=claude)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([e["event"] for e in self.written_events()], ["question", "intervention"])
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][calls[0].index("-p") + 1], "/aispa full\n\nСделай трекер.\n")
        self.assertEqual(calls[1][calls[1].index("-p") + 1], "Продолжай")
        self.assertEqual(calls[1][calls[1].index("--resume") + 1], "s1")
        self.assertEqual(self.final.read_text(), "Сдано, всё готово.")
        self.assertEqual(len(done.stdout.strip().splitlines()), 2)  # the environment's own answers, nothing else

    def test_the_events_file_exists_from_the_first_turn_and_an_answer_without_a_question_is_a_stop(self):
        claude = self.program("claude-fake", self.FAKE_CLAUDE, doubled=True)
        done = self.wrapper("--runtime", "claude", "--skill", "autopilot", claude=claude,
                            extra_env={"FAKE_DELIVER_ON": "3"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([e["event"] for e in self.written_events()], ["question", "intervention", "stop", "intervention"])

    def test_out_of_turns_returns_one_and_the_last_unfinished_turn_is_an_event_without_a_continue(self):
        claude = self.program("claude-fake", self.FAKE_CLAUDE, doubled=True)
        done = self.wrapper("--runtime", "claude", "--skill", "aispa", "--max-turns", "1", claude=claude,
                            extra_env={"FAKE_DELIVER_ON": "9"})
        self.assertEqual(done.returncode, 1)
        self.assertEqual([e["event"] for e in self.written_events()], ["question"])
        self.assertFalse(self.final.exists())

    def test_the_end_is_a_run_folder_without_wip_in_the_skills_own_work_folder(self):
        wrapper = load_script("measure_wrapper", "measure_wrapper.py")
        (self.repo / ".aispa-work" / "2026-10-28-demo--wip").mkdir(parents=True)
        (self.repo / ".autopilot" / "2026-10-28-demo").mkdir(parents=True)
        self.assertFalse(wrapper.finished(self.repo, "aispa"))
        self.assertTrue(wrapper.finished(self.repo, "autopilot"))
        (self.repo / ".aispa-work" / "2026-10-28-demo--wip").rename(self.repo / ".aispa-work" / "2026-10-28-demo")
        self.assertTrue(wrapper.finished(self.repo, "aispa"))

    def test_the_skill_is_installed_from_the_clean_copy_into_the_project_and_committed(self):
        claude = self.program("claude-fake", self.FAKE_CLAUDE, doubled=True)
        self.wrapper("--runtime", "claude", "--skill", "aispa", "--skill-src", self.skill, claude=claude)
        self.assertEqual((self.repo / ".claude/skills/aispa/SKILL.md").read_text(), "skill")
        self.assertEqual((self.repo / ".claude/agents/aispa-worker.md").read_text(), "agent")
        self.assertIn("замера", git(self.repo, "log", "-1", "--format=%s"))

    def test_global_install_copies_nothing_into_the_project(self):
        claude = self.program("claude-fake", self.FAKE_CLAUDE, doubled=True)
        self.wrapper("--runtime", "claude", "--skill", "aispa", "--skill-src", self.skill, claude=claude,
                     extra_env={"AISPA_MEASURE_INSTALL": "global"})
        self.assertFalse((self.repo / ".claude").exists())

    def test_an_autopilot_copy_at_another_commit_is_refused_before_any_turn(self):
        source = self.tmp / "autopilot-src"
        source.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
        (source / "SKILL.md").write_text("x")
        git(source, "add", "-A")
        subprocess.run(["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"],
                       check=True)
        claude = self.program("claude-fake", self.FAKE_CLAUDE, doubled=True)
        done = self.wrapper("--runtime", "claude", "--skill", "autopilot", "--skill-src", source, claude=claude)
        self.assertEqual(done.returncode, 2)
        self.assertFalse((self.repo / ".fake-calls.jsonl").exists())

    def test_codex_keeps_only_the_numbers_the_environment_returned(self):
        codex = self.program("codex-fake", self.FAKE_CODEX)
        done = self.wrapper("--runtime", "codex", "--skill", "autopilot", codex=codex)
        self.assertEqual(done.returncode, 0, done.stderr)
        kept = [json.loads(line)["type"] for line in done.stdout.strip().splitlines()]
        self.assertEqual(kept, ["thread.started", "turn.completed"])
        self.assertEqual(self.final.read_text(), "Итог: сдано.")
        self.assertEqual(self.events.read_text(), "")

    def test_a_codex_error_line_is_kept_so_that_a_limit_can_be_seen(self):
        wrapper = load_script("measure_wrapper", "measure_wrapper.py")
        self.assertTrue(wrapper.keep_line({"type": "error", "message": "429"}))
        self.assertTrue(wrapper.keep_line({"type": "turn.failed"}))
        self.assertFalse(wrapper.keep_line({"type": "item.completed"}))


class SourcesAreClean(unittest.TestCase):
    def test_no_machine_paths_in_the_tools(self):
        for name in ("measure.py", "measure-run.py", "measure_wrapper.py"):
            text = (ROOT / "tools" / name).read_text(encoding="utf-8")
            for bad in ("/Users/", "/private/", "/var/folders"):
                self.assertNotIn(bad, text, name)

    def test_the_wrapper_has_no_old_status_names(self):
        text = (ROOT / "tools" / "measure_wrapper.py").read_text(encoding="utf-8")
        for bad in ("delivered_with_remaining", "aispa.py", "accepted"):
            self.assertNotIn(bad, text)


class MeasureRunTests(unittest.TestCase):
    """tools/measure-run.py is the author's file; only the roles and the work folder are ours."""

    @classmethod
    def setUpClass(cls):
        cls.script = load_script("measure_run", "measure-run.py")

    def test_the_author_and_the_licence_are_named_on_top(self):
        head = (ROOT / "tools" / "measure-run.py").read_text(encoding="utf-8")[:900]
        for part in ("github.com/nick-vels/skills", "Nick Vels", "Alexander Fisenkov", "MIT"):
            self.assertIn(part, head)

    def test_aispa_agents_are_told_apart_by_their_type(self):
        role = self.script.role_of
        self.assertEqual(role("Шаг 03", "aispa-worker"), "исполнитель")
        self.assertEqual(role("Шаг 03", "aispa-sensitive-worker"), "исполнитель")
        self.assertEqual(role("Шаг 04", "aispa-mechanic-scripted"), "механика")
        self.assertEqual(role("Шаг 04", "aispa-mechanic"), "механика")
        self.assertEqual(role("Обзор проекта", "aispa-researcher"), "разведка")
        self.assertEqual(role("Проверка ветки", "aispa-judge"), "ревью")
        self.assertEqual(role("Слепая приёмка по брифу", "aispa-judge"), "приёмка")

    def test_without_an_agent_type_the_description_still_decides(self):
        self.assertEqual(self.script.role_of("Оркестратор"), "оркестратор")
        self.assertEqual(self.script.role_of("Ремонт по находкам ревью"), "ремонт")

    def test_the_work_folder_of_a_project_is_aispa_work(self):
        self.assertEqual(self.script.WORK_DIR, ".aispa-work")
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / ".aispa-work"
            (work / "2026-10-28-a").mkdir(parents=True)
            (work / "2026-10-28-b--wip").mkdir()
            (work / "ap.py").write_text("x")
            self.assertEqual(self.script.run_folders(tmp), (1, 1))
            self.assertEqual(self.script.run_folders(str(Path(tmp) / "missing")), (0, 0))

    def test_without_arguments_it_prints_the_help_and_stops(self):
        done = subprocess.run([sys.executable, str(ROOT / "tools" / "measure-run.py")], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Использование", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
