"""collectedFindings: numbering, sources, status transitions and the read-only `findings` command."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "skills/aispa/tools/aispa.py"
sys.path.insert(0, str(REPO / "skills/aispa/tools"))

from aispa_core import findings  # noqa: E402
from aispa_core.store import Invalid, Journal  # noqa: E402

REPORT = {"file": "reviews/r1.json", "sha256": "a" * 64}


def source(round_number=1, report=REPORT):
    return {"kind": "task", "task": "T01", "attempt": "x" * 32, "round": round_number, "report": dict(report)}


def warning(fid, text, criterion="T01 works"):
    return {"id": fid, "severity": "warning", "text": text, "criterion": criterion}


class FindingsTests(unittest.TestCase):
    def setUp(self):
        self.state = {"policy": {"focusedReview": True}}

    def test_collect_numbers_warnings_once_and_ignores_other_severities(self):
        rows = [warning("F1", "Rename"), {"id": "F2", "severity": "blocking", "blockingClass": "security",
                                          "text": "Leak", "criterion": "T01 works"},
                {"id": "F3", "severity": "note", "text": "FYI", "criterion": "T01 works"}, warning("F4", "Docs")]
        self.assertEqual(findings.collect(self.state, source(), rows), ["W001", "W002"])
        self.assertEqual(findings.collect(self.state, source(), rows), [])
        other = {"file": "reviews/r2.json", "sha256": "b" * 64}
        self.assertEqual(findings.collect(self.state, source(2, other), [warning("F1", "Rename")]), ["W003"])
        first = self.state["collectedFindings"][0]
        self.assertEqual(first, {"id": "W001", "source": source(), "criterion": "T01 works", "note": "Rename",
                                 "status": "open", "resolution": None})
        self.assertEqual(findings.view(self.state), {"open": 3, "confirmed": 0, "fixed": 0, "remaining": 0})

    def test_runs_without_focused_review_collect_nothing(self):
        state = {"policy": {"leanControl": True}}
        self.assertEqual(findings.collect(state, source(), [warning("F1", "Rename")]), [])
        self.assertNotIn("collectedFindings", state)
        self.assertEqual(findings.listing(state)["findings"], [])

    def test_triage_follows_transitions_and_needs_a_reason(self):
        findings.collect(self.state, source(), [warning("F1", "Rename"), warning("F2", "Docs")])
        before = json.dumps(self.state, sort_keys=True)
        for decisions in ([{"id": "W001", "status": "fixed", "note": "Too early"}],
                          [{"id": "W001", "status": "confirmed", "note": ""}],
                          [{"id": "W009", "status": "confirmed", "note": "Unknown"}],
                          [{"id": "W001", "status": "confirmed", "note": "Real"},
                           {"id": "W001", "status": "dismissed", "note": "Twice"}]):
            with self.subTest(decisions=decisions), self.assertRaises(Invalid):
                findings.triage(self.state, decisions, "reviewer-1")
        self.assertEqual(json.dumps(self.state, sort_keys=True), before)
        self.assertEqual(findings.triage(self.state, [{"id": "W001", "status": "confirmed", "note": "Real"},
                                                      {"id": "W002", "status": "dismissed", "note": "Already said"}],
                                         "reviewer-1"), ["W001", "W002"])
        self.assertEqual(findings.open_items(self.state), [])
        findings.triage(self.state, [{"id": "W001", "status": "fixed", "note": "Repaired in F01"}], "checker")
        self.assertEqual(findings.view(self.state), {"open": 0, "confirmed": 0, "fixed": 1, "remaining": 0})
        self.assertEqual(self.state["collectedFindings"][1]["resolution"]["note"], "Already said")
        self.assertEqual([row["id"] for row in findings.listing(self.state, "dismissed")["findings"]], ["W002"])


class FindingsCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="aispa findings ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(CLI), "--root", str(self.root), *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def test_findings_command_lists_collected_warnings(self):
        result = self.run_cli("init", "--slug", "findings", "--title", "Findings", "--mode", "auto",
                              "--runtime", "claude-code")
        self.assertEqual(result.returncode, 0, result.stderr)
        store = Journal(self.root)
        state = store.load()
        findings.collect(state, source(), [warning("F1", "Rename"), warning("F2", "Docs")])
        findings.triage(state, [{"id": "W002", "status": "confirmed", "note": "Real"}], "reviewer")
        store.append(state, "fixture-findings")
        events = (store.run / "events.jsonl").read_bytes()
        listed = self.run_cli("findings")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        value = json.loads(listed.stdout)
        value = value.get("receipt", value)
        self.assertEqual([row["id"] for row in value["findings"]], ["W001", "W002"])
        opened = json.loads(self.run_cli("findings", "--status", "open").stdout)
        opened = opened.get("receipt", opened)
        self.assertEqual([row["id"] for row in opened["findings"]], ["W001"])
        self.assertEqual((store.run / "events.jsonl").read_bytes(), events)


if __name__ == "__main__":
    unittest.main()
