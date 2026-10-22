"""Request-local code snapshots; fresh requests still verify current bytes."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import VERSION, dashboard_reader
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


if __name__ == "__main__":
    unittest.main()
