"""Addressed preparation keeps source isolation, lifecycle and write guards."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
import aispa
from aispa_core import controls, packets, preparation, task_reports
from aispa_core.store import Invalid, Journal, file_hash
import test_preparation_workflow as prep_helpers
import test_adaptive_review as task_helpers
import test_cli_output as cli_helpers


class PreparationExchangeTests(unittest.TestCase):
    setUp = prep_helpers.PreparationTests.setUp
    tearDown = prep_helpers.PreparationTests.tearDown
    git = prep_helpers.PreparationTests.git
    commit = prep_helpers.PreparationTests.commit
    file = prep_helpers.PreparationTests.file
    sources = prep_helpers.PreparationTests.sources
    mapping = prep_helpers.PreparationTests.mapping

    def execute(self, *args):
        return controls.execute(self.store, self.state, self.flow, aispa.parser().parse_args(args))

    def restore(self, receipt):
        packet = json.loads(Path(receipt["absolutePath"]).read_text(encoding="utf-8"))
        result = {k: v for k, v in packet.items() if k not in ("sections", "readingRule")}
        for key, ref in packet.get("sections", {}).items():
            self.assertEqual(file_hash(Path(ref["absolutePath"])), ref["sha256"])
            result[key] = json.loads(self.store.text(ref))
        return result

    def test_five_preparation_drafts_preserve_empty_semantics_and_default_output(self):
        self.sources()
        self.mapping()
        before = copy.deepcopy(self.state)
        events = (self.store.run / "events.jsonl").read_bytes()
        for kind in task_reports.PREPARATION_KINDS:
            with self.subTest(kind=kind):
                actor = self.analyst if kind == "brief-extract" else self.reviewer
                original = preparation.template(self.store, self.state, kind, actor=actor)
                self.assertEqual(self.execute("template", kind, "--actor", actor), original)
                receipt = self.execute("template", kind, "--actor", actor, "--write")
                path = self.root / receipt["path"]
                self.assertEqual(Path(receipt["absolutePath"]), path.resolve())
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
                self.assertEqual(receipt["sha256"], file_hash(path))
                self.assertEqual(receipt["status"], "draft-not-evidence")
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("report", receipt)
                if kind in ("brief-extract", "brief-review"):
                    self.assertTrue(all(r["summary"] == r["reason"] == "" for r in original["units"]))
                    with self.assertRaises(Invalid):
                        preparation.record(self.store, self.state, kind, original)
                elif kind == "brief-map":
                    self.assertTrue(all(r["disposition"] == "TODO" and r["requirements"] == []
                                        and r["reason"] == "" for r in original["rows"]))
                elif kind == "scenarios":
                    self.assertEqual(original["depthReason"], "")
                    self.assertTrue(all(d["status"] == "not_tested" and d["scenario"] == d["verification"] == ""
                                        for r in original["features"] for d in r["dimensions"]))
                else:
                    self.assertEqual((original["interfaces"], original["noInterfaceReason"]), ([], ""))
        self.assertEqual(self.state, before)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), events)
        with self.assertRaises(Invalid):
            self.execute("template", "failure-checks", "--write")
        with self.assertRaises(Invalid):
            self.execute("template", "scenarios", "--task", "T01", "--write")

    def test_source_only_packet_remains_isolated_after_independent_reports_and_mapping(self):
        self.sources()
        original = self.execute("brief-source")
        before = self.execute("brief-source", "--write")
        self.mapping()
        after = self.execute("brief-source", "--write")
        self.assertEqual(before, after)
        self.assertEqual(self.restore(after), original)
        self.assertEqual(set(self.restore(after)), {"basis", "briefs"})
        self.assertNotIn("requirements", self.restore(after))
        self.assertNotIn("briefExtraction", self.restore(after))
        mapped = self.execute("brief-map-context", "--write")
        self.assertEqual(self.restore(mapped), self.execute("brief-map-context"))
        self.assertEqual(mapped, self.execute("brief-map-context", "--write"))
        self.assertIn("briefExtraction", self.restore(mapped))
        self.assertIn("briefReview", self.restore(mapped))
        self.assertIn("requirements", self.restore(mapped))

    def test_large_source_uses_stable_sections_without_losing_original_text(self):
        self.sources()
        self.flow.brief(self.file("Source words " * 2000 + ".", "large.md"))
        receipt = self.execute("brief-source", "--write")
        self.assertGreater(receipt["sectionCount"], 0)
        self.assertEqual(self.restore(receipt), self.execute("brief-source"))
        self.assertEqual(receipt, self.execute("brief-source", "--write"))
        self.assertLessEqual(receipt["envelopeBytes"], packets.MAX_PACKET_BYTES)

    def test_mapping_write_keeps_existing_prerequisite_and_integrity_guards(self):
        self.sources()
        with self.assertRaises(Invalid):
            self.execute("brief-map-context", "--write")
        self.assertFalse((self.store.run / "contexts").exists())
        self.mapping()
        path = self.store.run / self.state["briefReview"]["file"]
        path.write_text("tampered", encoding="utf-8")
        with self.assertRaises(Invalid):
            self.execute("brief-map-context", "--write")
        self.assertFalse((self.store.run / "contexts").exists())

    def test_preparation_draft_cannot_overwrite_or_use_an_unlisted_kind(self):
        report = preparation.template(self.store, self.state, "interfaces")
        with patch.object(task_reports.uuid, "uuid4") as uuid:
            uuid.return_value.hex = "fixed"
            first = task_reports.write_preparation(self.store, report, "interfaces")
            path = self.root / first["path"]
            raw = path.read_bytes()
            with self.assertRaisesRegex(Invalid, "перезапись"):
                task_reports.write_preparation(self.store, report, "interfaces")
            self.assertEqual(path.read_bytes(), raw)
        with self.assertRaises(Invalid):
            task_reports.write_preparation(self.store, report, "../outside")


class TaskBundleTests(unittest.TestCase):
    setUp = task_helpers.AdaptiveReviewTests.setUp
    tearDown = task_helpers.AdaptiveReviewTests.tearDown
    task = task_helpers.AdaptiveReviewTests.task
    attempt = task_helpers.AdaptiveReviewTests.attempt
    start = task_helpers.AdaptiveReviewTests.start
    verify = task_helpers.AdaptiveReviewTests.verify
    report = task_helpers.AdaptiveReviewTests.report
    first = task_helpers.AdaptiveReviewTests.first
    finding = task_helpers.AdaptiveReviewTests.finding
    repair = task_helpers.AdaptiveReviewTests.repair
    second_actor = task_helpers.AdaptiveReviewTests.second_actor

    def bundle(self, role="worker", **kwargs):
        return packets.write_task_bundle(self.store, self.state, self.team, "T01", role, **kwargs)

    def test_bundle_returns_only_references_and_unassessed_form_without_events(self):
        self.start()
        before = copy.deepcopy(self.state)
        events = (self.store.run / "events.jsonl").read_bytes()
        expected = packets.build(self.store, self.state, self.team, "task", "T01", "worker")
        first, repeated = self.bundle(), self.bundle()
        self.assertEqual(set(first), {"kind", "task", "role", "actor", "card", "context", "template"})
        self.assertEqual(first["context"], repeated["context"])
        self.assertNotEqual(first["template"]["path"], repeated["template"]["path"])
        self.assertEqual(json.loads(Path(first["context"]["absolutePath"]).read_text(encoding="utf-8")), expected)
        self.assertEqual(first["card"]["sha256"], file_hash(Path(first["card"]["absolutePath"])))
        self.assertEqual(set(first["card"]), {"file", "sha256", "absolutePath"})
        card = json.loads(Path(first["card"]["absolutePath"]).read_text(encoding="utf-8"))
        self.assertEqual(card["checks"], self.attempt()["roles"]["worker"]["checks"])
        self.assertEqual(card["specialization"], self.attempt()["roles"]["worker"]["specialization"])
        self.assertTrue(card["checks"])
        self.assertNotIn("expectedChecks", first)
        self.assertNotIn("checks", first["context"])
        self.assertNotIn("checks", first["template"])
        self.assertNotIn("specialization", json.dumps(first))
        self.assertNotIn(card["checks"][0], json.dumps(first))
        self.assertEqual(Path(first["template"]["absolutePath"]), (self.root / first["template"]["path"]).resolve())
        draft = json.loads(Path(first["template"]["absolutePath"]).read_text(encoding="utf-8"))
        self.assertEqual(draft, task_reports.template(self.team, "T01", "worker"))
        self.assertIs(draft["independent"], False)
        self.assertIsNone(draft["unmapped"])
        self.assertTrue(all(row["status"] == "not_tested" for row in draft["checks"]))
        self.assertEqual(self.state, before)
        self.assertEqual((self.store.run / "events.jsonl").read_bytes(), events)

    def test_missing_assignment_verification_and_out_of_zone_sources_fail_before_writes(self):
        self.host.task("T01")["status"] = "running"
        with self.assertRaisesRegex(Invalid, "actor"):
            self.bundle()
        self.start()
        check = self.attempt()["verification"]
        for change in (None, dict(check, exitCode=1), dict(check, truncated=True), dict(check, candidate="stale")):
            self.attempt()["verification"] = change
            with self.subTest(check=change), self.assertRaisesRegex(Invalid, "проверки"):
                self.bundle()
        self.attempt()["verification"] = check
        with self.assertRaises(Invalid):
            self.bundle(source_paths=["b.txt"])
        with self.assertRaises(Invalid):
            self.bundle("critic1")
        self.assertFalse((self.store.home / "inbox").exists())
        self.assertFalse((self.store.run / "contexts").exists())

    def test_first_pass_forbids_critic2_but_prepares_separate_acceptor(self):
        self.start()
        self.assertTrue(self.first())
        before = copy.deepcopy(self.state)
        with self.assertRaises(Invalid):
            self.bundle("critic2")
        self.assertEqual(self.state, before)
        result = self.bundle("acceptor")
        draft = json.loads(Path(result["template"]["absolutePath"]).read_text(encoding="utf-8"))
        self.assertIsNone(draft["verdict"])
        self.assertEqual(draft["previousReport"], self.attempt()["rounds"][0]["report"]["sha256"])
        self.assertNotIn("critic2", self.attempt()["actors"])
        self.assertEqual(len(self.attempt()["rounds"]), 1)

    def test_critic2_requires_real_repair_and_preserves_all_resolution_ids(self):
        self.start()
        self.assertFalse(self.first("WARN"))
        with self.assertRaises(Invalid):
            self.bundle("critic2")
        with self.assertRaises(Invalid):
            self.second_actor()
        self.repair()
        self.second_actor()
        result = self.bundle("critic2")
        draft = json.loads(Path(result["template"]["absolutePath"]).read_text(encoding="utf-8"))
        self.assertEqual(draft["repairReceipt"], self.attempt()["repair"]["report"]["sha256"])
        self.assertEqual(draft["resolutions"], [{"id": "F1", "status": "not_tested", "note": "", "artifact": ""}])
        self.assertIsNone(draft["verdict"])
        self.assertIsNone(draft["findings"])


class ExchangeCliTests(unittest.TestCase):
    setUp = cli_helpers.CliOutputTests.setUp
    git = cli_helpers.CliOutputTests.git
    call = cli_helpers.CliOutputTests.call
    init = cli_helpers.CliOutputTests.init

    WRITE_COMMANDS = (
        ("template", "interfaces", "--write"),
        ("brief-source", "--write"),
        ("brief-map-context", "--write"),
        ("context", "--kind", "task", "--task", "T01", "--role", "worker", "--write", "--with-template"),
    )

    def test_preparation_receipt_resolves_with_root_from_foreign_unicode_cwd(self):
        self.init()
        foreign = Path(self.temp.name) / "другой каталог 🙂"
        foreign.mkdir()
        result = subprocess.run([sys.executable, "-B", str(self.cli), "--root", str(self.root),
                                 "template", "interfaces", "--write"], cwd=foreign,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        path = Path(receipt["absolutePath"])
        self.assertTrue(path.is_absolute())
        self.assertEqual(path, self.root / receipt["path"])
        self.assertFalse((foreign / receipt["path"]).exists())
        self.assertEqual(file_hash(path), receipt["sha256"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["noInterfaceReason"], "")

    def test_runtime_pin_protects_every_new_write_but_defaults_stay_readable(self):
        self.init()
        before = self.call("template", "interfaces").stdout
        with (self.run / "runtime/SKILL.md").open("a", encoding="utf-8") as stream:
            stream.write("\nChanged runtime fixture\n")
        self.assertEqual(self.call("template", "interfaces").stdout, before)
        for command in self.WRITE_COMMANDS:
            with self.subTest(command=command):
                result = self.call(*command, expected=2)
                self.assertIn("Пакет изменён", result.stderr)
        self.assertFalse((self.root / ".aispa/inbox").exists())

    def test_pending_recovery_blocks_new_writes_without_journal_mutation(self):
        self.init()
        store = Journal(self.root)
        with store.lock():
            state = store.load()
            state["journalRecovery"] = {"pending": True}
            store.append(state, "synthetic-recovery-fixture")
        before = (self.run / "events.jsonl").read_bytes()
        self.call("template", "interfaces")
        for command in self.WRITE_COMMANDS:
            with self.subTest(command=command):
                result = self.call(*command, expected=2)
                self.assertIn("Восстановление ещё не сверено", result.stderr)
        self.assertEqual((self.run / "events.jsonl").read_bytes(), before)
        self.assertFalse((self.root / ".aispa/inbox").exists())

    def test_bundle_flags_require_task_write_and_role_before_preparation(self):
        self.init()
        for flags in (("--kind", "coverage", "--write", "--role", "worker", "--task", "T01"),
                      ("--kind", "task", "--role", "worker", "--task", "T01"),
                      ("--kind", "task", "--write", "--task", "T01"),
                      ("--kind", "task", "--write", "--role", "worker")):
            with self.subTest(flags=flags):
                result = self.call("context", *flags, "--with-template", expected=2)
                self.assertIn("--with-template требует", result.stderr)
        self.assertFalse((self.root / ".aispa/inbox").exists())


if __name__ == "__main__":
    unittest.main()
