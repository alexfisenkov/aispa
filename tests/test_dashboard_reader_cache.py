"""Request-local code snapshots, and when the server may serve the same snapshot again.

A reader (`SnapshotJournal`) is request-local: every new calculation reloads and verifies
current bytes. Viewing is not a gate, though: `SnapshotCoordinator` serves the last ready
snapshot again while the journal, active pointer and journal cache are unchanged
(`REUSE_SECONDS` at most) and recalculates the moment one of them changes. Mutating
commands never use this and verify everything in full.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import VERSION, dashboard_reader
from aispa_core.dashboard_snapshot import REUSE_SECONDS, SnapshotCoordinator, source_key
from aispa_core.contracts import verified
from aispa_core.store import Invalid, Journal
import test_workflow as helpers


class DashboardReaderCacheTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    git = helpers.WorkflowTests.git
    commit = helpers.WorkflowTests.commit
    file = helpers.WorkflowTests.file

    def reader(self, cls=None):
        reader = (cls or dashboard_reader.SnapshotJournal)(self.root)
        state = reader.load()
        return reader, state

    def test_identical_code_queries_reuse_git_and_match_normal_journal(self):
        for cls in (dashboard_reader.SnapshotJournal, dashboard_reader.DisplayJournal):
            with self.subTest(reader=cls.__name__):
                normal = Journal(self.root)
                reader, _ = self.reader(cls)
                queries = ((["app.py", ".gitignore"], False),
                           ([".gitignore", "app.py", "app.py"], False),
                           (("app.py", ".gitignore"), False))
                with patch.object(normal, "git", wraps=normal.git) as baseline_git:
                    expected = [normal.code(zones, common) for zones, common in queries]
                with patch.object(reader, "git", wraps=reader.git) as cached_git:
                    actual = [reader.code(zones, common) for zones, common in queries]
                self.assertEqual(actual, expected)
                self.assertEqual(cached_git.call_count * len(queries), baseline_git.call_count)

    def test_none_empty_zones_and_common_inputs_have_distinct_cache_keys(self):
        reader, _ = self.reader()
        normal = Journal(self.root)
        queries = ((None, False), ([], False), (["app.py"], False), (["app.py"], True),
                   ([], True), (["."], False), (None, True))
        for zones, common in queries:
            with self.subTest(zones=zones, include_common=common):
                self.assertEqual(reader.code(zones, common), normal.code(zones, common))
        self.assertNotEqual(reader.code(None), reader.code([]))
        self.assertNotEqual(reader.code(["app.py"]), reader.code(["app.py"], include_common=True))
        with self.assertRaises(Invalid):
            reader.code(["../outside"])

    def test_new_request_after_code_change_invalidates_prior_verification(self):
        self.assertTrue(self.flow.verify([sys.executable, "-c", "print('verified')"], 10))
        first, state = self.reader()
        self.assertTrue(verified(first, state))
        before = first.code()
        (self.root / "app.py").write_text("print('changed')\n", encoding="utf-8")
        second, fresh_state = self.reader()
        self.assertEqual(fresh_state["updatedAt"], state["updatedAt"])
        self.assertNotEqual(second.code(), before)
        self.assertEqual(second.code(), Journal(self.root).code())
        self.assertFalse(verified(second, fresh_state))

    def test_reloading_same_reader_also_starts_a_fresh_code_snapshot(self):
        reader, _ = self.reader()
        before = reader.code()
        (self.root / "app.py").write_text("print('updated')\n", encoding="utf-8")
        reader.load()
        self.assertNotEqual(reader.code(), before)

    def test_evidence_changed_between_requests_cannot_keep_verification(self):
        self.assertTrue(self.flow.verify([sys.executable, "-c", "print('verified')"], 10))
        first, state = self.reader()
        self.assertTrue(verified(first, state))
        path = self.store.run / state["verification"]["output"]["file"]
        path.write_text("corrupted evidence\n", encoding="utf-8")
        second, fresh_state = self.reader()
        self.assertEqual(first.code(), second.code())
        self.assertFalse(verified(second, fresh_state))

    def test_corrupt_journal_between_requests_is_rejected(self):
        reader, _ = self.reader()
        reader.read_journal()
        with (self.store.run / "events.jsonl").open("ab") as stream:
            stream.write(b"corrupted\n")
        with self.assertRaises(Invalid):
            self.reader()

    def test_journal_reads_retain_existing_same_request_integrity_check(self):
        reader, _ = self.reader()
        reader.read_journal()
        with (self.store.run / "events.jsonl").open("ab") as stream:
            stream.write(b"corrupted\n")
        with self.assertRaises(Invalid):
            reader.read_journal()

    def test_source_corruption_between_requests_is_rejected(self):
        source = self.flow.brief(self.file("Original owner request.", "brief.md"))
        self.reader()
        (self.store.run / source["file"]).write_text("Altered source", encoding="utf-8")
        with self.assertRaises(Invalid):
            self.reader()

    def test_snapshot_reader_keeps_strict_version_guard(self):
        old_version = next(v for v in dashboard_reader.SUPPORTED_VERSIONS if v != VERSION)
        self.state["version"] = old_version
        self.store.append(self.state, "synthetic-old-runtime")
        with self.assertRaises(Invalid):
            self.reader()
        compatible, state = self.reader(dashboard_reader.DisplayJournal)
        self.assertEqual(state["version"], old_version)
        self.assertEqual(compatible.code(), Journal(self.root).code())


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class CoordinatorReuseTests(unittest.TestCase):
    """Reuse is decided by a cheap fingerprint; the calculation itself is never skipped when it changes."""

    def build(self, *, key=("same",), reuse=REUSE_SECONDS):
        self.clock, self.key, self.calls = Clock(), [key], []

        def calculation():
            self.calls.append(self.key[0])
            return {"run": "fixture", "observedAt": "2026-10-28T00:00:00+00:00", "calls": len(self.calls)}, {"journalSha256": "a" * 64}

        return SnapshotCoordinator(calculation, grace=2, fingerprint=lambda: self.key[0],
                                   reuse_seconds=reuse, clock=self.clock)

    def poll(self, coordinator):
        code, payload = coordinator.get()
        self.assertEqual(code, 200, payload)
        return payload

    def test_budget_of_polls_unchanged_journal_every_five_seconds_for_ten_minutes_calculates_at_most_three_times(self):
        coordinator = self.build()
        for _ in range(120):
            self.poll(coordinator)
            self.clock.value += 5
        self.assertLessEqual(len(self.calls), 3)
        self.assertGreaterEqual(len(self.calls), 2, "A snapshot older than the reuse limit was served again")

    def test_changed_fingerprint_calculates_at_once_and_unchanged_one_does_not(self):
        coordinator = self.build()
        first = self.poll(coordinator)
        self.clock.value += 1
        again = self.poll(coordinator)
        self.assertEqual(again["calculation"]["id"], first["calculation"]["id"])
        self.assertEqual(again["snapshot"]["capturedAt"], first["snapshot"]["capturedAt"])
        self.assertEqual(len(self.calls), 1)
        self.key[0] = ("journal", "grew")
        fresh = self.poll(coordinator)
        self.assertNotEqual(fresh["calculation"]["id"], first["calculation"]["id"])
        self.assertEqual(len(self.calls), 2)

    def test_reused_snapshot_is_recalculated_after_the_reuse_limit(self):
        coordinator = self.build()
        self.poll(coordinator)
        self.clock.value += REUSE_SECONDS - 1
        self.poll(coordinator)
        self.assertEqual(len(self.calls), 1)
        self.clock.value += 1
        self.poll(coordinator)
        self.assertEqual(len(self.calls), 2)

    def test_reused_snapshot_reports_its_age_so_clocks_can_catch_up(self):
        coordinator = self.build()
        self.assertEqual(self.poll(coordinator)["snapshot"]["ageSeconds"], 0)
        self.clock.value += 120
        self.assertEqual(self.poll(coordinator)["snapshot"]["ageSeconds"], 120)

    def test_without_a_fingerprint_or_when_it_cannot_tell_nothing_is_reused(self):
        coordinator = self.build(key=None)
        for _ in range(3):
            self.poll(coordinator)
        self.assertEqual(len(self.calls), 3)
        plain = SnapshotCoordinator(lambda: ({"run": "fixture"}, {}), grace=2, clock=Clock())
        self.assertNotEqual(plain.get()[1]["calculation"]["id"], plain.get()[1]["calculation"]["id"])
        zero = self.build(reuse=0)
        zero.get(), zero.get()
        self.assertEqual(len(self.calls), 2)

    def test_failed_calculation_is_not_reused_and_does_not_hide_the_last_good_snapshot(self):
        coordinator = self.build()
        good = self.poll(coordinator)
        self.key[0] = ("changed",)
        coordinator.calculation = lambda: (_ for _ in ()).throw(ValueError("private path /secret"))
        code, failed = coordinator.get()
        self.assertEqual(code, 409)
        self.assertEqual(failed["lastSnapshot"]["capturedAt"], good["snapshot"]["capturedAt"])
        self.assertNotIn("secret", json.dumps(failed))
        code, retry = coordinator.get()
        self.assertEqual(code, 409, "A failed generation must be retried, not served as ready")

    def test_a_fingerprint_failure_means_recalculate(self):
        coordinator = self.build()
        coordinator.fingerprint = lambda: (_ for _ in ()).throw(OSError("denied"))
        coordinator.get(), coordinator.get()
        self.assertEqual(len(self.calls), 2)


class SourceKeyTests(unittest.TestCase):
    setUp = helpers.WorkflowTests.setUp
    tearDown = helpers.WorkflowTests.tearDown
    git = helpers.WorkflowTests.git
    commit = helpers.WorkflowTests.commit
    file = helpers.WorkflowTests.file

    def test_key_changes_when_the_journal_grows_and_not_when_nothing_is_written(self):
        before = source_key(self.store.home)
        self.assertIsNotNone(before)
        self.assertEqual(source_key(self.store.home), before)
        self.state["notes"].append({"text": "one more decision", "at": self.state["updatedAt"]})
        self.store.append(self.state, "agent-decision-recorded")
        self.assertNotEqual(source_key(self.store.home), before)

    def test_key_follows_the_active_pointer_and_tells_nothing_for_a_broken_one(self):
        (self.store.home / "active.json").write_text("not json", encoding="utf-8")
        self.assertIsNone(source_key(self.store.home))
        (self.store.home / "active.json").write_text(json.dumps({"run": "../escape"}), encoding="utf-8")
        self.assertIsNone(source_key(self.store.home))
        (self.store.home / "active.json").write_text(json.dumps({"run": "absent-run"}), encoding="utf-8")
        self.assertIsNone(source_key(self.store.home))


if __name__ == "__main__":
    unittest.main()
