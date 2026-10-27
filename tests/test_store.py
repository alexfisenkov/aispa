import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import VERSION
from aispa_core import scale
from aispa_core.store import Invalid, Journal, confined, file_hash, regular_file
from aispa_core.verified_reads import ResourceLimit


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-test-")
        self.root = Path(self.temp.name)
        self.store = Journal(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args],
                              check=True, capture_output=True).stdout

    def repository(self):
        self.git("init", "-q")
        (self.root / ".gitignore").write_text(".aispa/\ncache/\n", encoding="utf-8", newline="\n")
        (self.root / "app.py").write_text("print('example')\n", encoding="utf-8", newline="\n")
        self.git("add", ".gitignore", "app.py")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")

    def test_stream_hash_keeps_legal_regular_symlink_and_large_file(self):
        source = self.root / "large.bin"
        raw = b"source" * 1024
        source.write_bytes(raw)
        expected = hashlib.sha256(raw).hexdigest()
        with patch.dict(os.environ, {"AISPA_MAX_INPUT_BYTES": "1024"}):
            self.assertEqual(file_hash(source, budget=scale.Budget()), expected)
        linked = self.root / "regular-link.bin"
        try:
            linked.symlink_to(source)
        except OSError:
            return  # Symlink creation may require a Windows privilege.
        self.assertEqual(file_hash(linked), expected)
        with self.assertRaises(Invalid):
            file_hash(linked, nofollow=True)

        # Model a leaf replaced after confinement, just before the protected
        # artifact hash. Generic file_hash above must retain its link policy.
        self.store.run = self.root
        with patch("aispa_core.store.confined", return_value=linked):
            with self.assertRaises(Invalid):
                self.store.artifact_hash_valid({"file": linked.name, "sha256": expected})

    def test_stream_hash_adapts_chunk_to_small_memory_without_file_cap(self):
        source = self.root / "large.bin"
        raw = b"source" * 1293
        source.write_bytes(raw)
        expected = hashlib.sha256(raw).hexdigest()
        with patch.dict(os.environ, {"AISPA_MAX_MEMORY_BYTES": "1024",
                                          "AISPA_MAX_INPUT_BYTES": "1024"}):
            budget = scale.Budget()
            self.assertEqual(file_hash(source, budget=budget, nofollow=True), expected)
            budget.base_memory = budget.memory_bytes
            with self.assertRaisesRegex(ResourceLimit, "maxMemoryBytes"):
                file_hash(source, budget=budget, nofollow=True)

    def test_regular_fd_checks_deadline_after_final_stat_and_preserves_body_error(self):
        source = self.root / "source.bin"
        source.write_bytes(b"stable")
        budget = scale.Budget()
        original = Path.lstat
        calls = 0

        def final_stat(path, *args, **kwargs):
            nonlocal calls
            result = original(path, *args, **kwargs)
            if path == source:
                calls += 1
                if calls == 3:
                    budget.deadline = -1.0
            return result

        with patch.object(Path, "lstat", final_stat):
            with self.assertRaisesRegex(ResourceLimit, "operationSeconds"):
                file_hash(source, budget=budget, nofollow=True)
        self.assertGreaterEqual(calls, 3)

        fresh = scale.Budget()
        with self.assertRaisesRegex(RuntimeError, "original body"):
            with regular_file(source, budget=fresh, nofollow=True) as (stream, _, _opened):
                fresh.deadline = -1.0
                raise RuntimeError("original body")
        self.assertTrue(stream.closed)

    def test_direct_journal_materialization_obeys_one_deadline(self):
        state = self.store.create("materialize", "Materialized journal")
        state["taskHistory"] = {"T01": [{"id": "fixture", "testIntentHistory": [{"id": "one"}]}]}
        self.store.append(state, "task-history")
        from aispa_core import store as store_module
        original_copy = store_module.copy.deepcopy
        for name, method in (("journal", lambda: self.store.read_journal(include_events=True)),
                             ("pair", lambda: self.store.event_pair(1)),
                             ("timeline", lambda: self.store.task_timeline("T01"))):
            with self.subTest(method=name):
                budget = scale.Budget()

                def expire_copy(value):
                    result = original_copy(value)
                    budget.deadline = -1.0
                    return result

                with patch.object(scale, "Budget", return_value=budget), \
                        patch.object(store_module.copy, "deepcopy", side_effect=expire_copy):
                    with self.assertRaisesRegex(ResourceLimit, "operationSeconds"):
                        method()

    def test_direct_artifact_text_and_json_check_after_materialization(self):
        self.store.create("artifact-tail", "Artifact tail checks")
        ref = self.store.artifact("reports", b'{"value":"stable"}', ".bin")
        from aispa_core import store as store_module
        budget = scale.Budget()
        original_loads = store_module.json.loads
        loads = 0

        def expire_json(*args, **kwargs):
            nonlocal loads
            value = original_loads(*args, **kwargs)
            loads += 1
            if loads == 2:
                budget.deadline = -1.0
            return value

        with patch.object(scale, "Budget", return_value=budget), \
                patch.object(store_module.json, "loads", side_effect=expire_json):
            with self.assertRaisesRegex(ResourceLimit, "operationSeconds"):
                self.store.json(ref)
        self.assertGreaterEqual(loads, 2)

        fresh = scale.Budget()

        def expire_text(_ref, **kwargs):
            fresh.deadline = -1.0
            return b"stable text"

        with patch.object(scale, "Budget", return_value=fresh), \
                patch.object(self.store, "artifact_bytes", side_effect=expire_text):
            with self.assertRaisesRegex(ResourceLimit, "operationSeconds"):
                self.store.text(ref)

    def test_active_pointer_and_journal_scan_do_not_use_raw_path_reads(self):
        state = self.store.create("checked", "Checked journal")
        pointer = self.store.home / "active.json"
        history = self.store.run / "events.jsonl"
        expected_hash = hashlib.sha256(history.read_bytes()).hexdigest()
        original_open, original_read_text = Path.open, Path.read_text

        def guarded_open(path, *args, **kwargs):
            if path == history and (not args or "r" in args[0]):
                raise AssertionError("raw journal Path.open")
            return original_open(path, *args, **kwargs)

        def guarded_text(path, *args, **kwargs):
            if path == pointer:
                raise AssertionError("raw active pointer Path.read_text")
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open), patch.object(Path, "read_text", guarded_text):
            self.assertEqual(Journal(self.root).load()["run"], state["run"])
            self.assertEqual(Journal._hash_file(history), expected_hash)

    def test_journal_scan_has_a_finite_operation_deadline(self):
        self.store.create("bounded", "Bounded journal")
        expired = scale.Budget()
        expired.deadline = -1.0
        with patch.object(scale, "Budget", return_value=expired):
            with self.assertRaisesRegex(Invalid, "operationSeconds"):
                self.store._scan_journal()

    def test_code_fingerprint_hashes_regular_file_above_per_input_cap(self):
        self.repository()
        source = self.root / "app.py"
        source.write_bytes(b"source" * 1024)
        with patch.dict(os.environ, {"AISPA_MAX_INPUT_BYTES": "1024"}):
            records = self.store.code_records()
        self.assertIn(["app.py", {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                  "executable": False}], records)

    def test_concurrent_append_during_scan_keeps_retryable_generation_refusal(self):
        self.store.create("concurrent", "Concurrent journal")
        history = self.store.run / "events.jsonl"
        from aispa_core import store as store_module
        original = store_module.regular_file

        @contextmanager
        def append_after_open(path, **kwargs):
            with original(path, **kwargs) as generation:
                if Path(path) == history:
                    with history.open("ab") as stream:
                        stream.write(b"x")
                yield generation

        with patch.object(store_module, "regular_file", append_after_open):
            with self.assertRaisesRegex(Invalid, "Журнал изменился после проверки"):
                self.store._scan_journal()

    @unittest.skipIf(os.name == "nt", "POSIX executable bits are derived from the opened generation")
    def test_code_records_rejects_mode_change_after_hash(self):
        self.repository()
        source = self.root.resolve() / "app.py"
        original = file_hash

        def change_mode(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if Path(path) == source:
                os.chmod(source, 0o755)
            return result

        with patch("aispa_core.store.file_hash", side_effect=change_mode):
            with self.assertRaisesRegex(Invalid, "изменился"):
                self.store.code_records()

    def test_code_link_fingerprint_refuses_replacement_after_readlink(self):
        self.repository()
        source = self.root.resolve() / "app.py"
        source.unlink()
        first = self.root.resolve() / "first.py"
        second = self.root.resolve() / "second.py"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        try:
            source.symlink_to(first)
        except OSError:
            self.skipTest("Symlink creation requires a Windows privilege")
        expected_link = os.readlink(source)
        self.assertIn(["app.py", {"link": expected_link}], self.store.code_records())
        original = os.readlink

        def change_link(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if Path(path) == source:
                source.unlink()
                source.symlink_to(second)
            return value

        with patch("aispa_core.store.os.readlink", side_effect=change_link):
            with self.assertRaisesRegex(Invalid, "изменился"):
                self.store.code_records()

    def test_saved_source_hash_refuses_post_confinement_leaf_link(self):
        source = self.root / "source.md"
        source.write_bytes(b"source")
        linked = self.root / "linked.md"
        try:
            linked.symlink_to(source)
        except OSError:
            self.skipTest("Symlink creation requires a Windows privilege")
        self.store.run = self.root
        state = {"briefs": [{"file": "source.md", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
                 "spec": None}
        with patch("aispa_core.store.confined", return_value=linked):
            with self.assertRaises(Invalid):
                self.store._verify_sources(state)

    def test_protected_artifact_hashes_obey_operation_deadline(self):
        self.store.create("hash-budget", "Protected artifact hashes")
        from aispa_core import store as store_module
        from aispa_core.verified_reads import ResourceLimit
        original = store_module.file_hash

        def expire(path, *args, **kwargs):
            budget = kwargs.get("budget")
            self.assertIsNotNone(budget)
            budget.deadline = -1.0
            return original(path, *args, **kwargs)

        with patch.object(store_module, "file_hash", side_effect=expire):
            with self.assertRaises(ResourceLimit):
                self.store.artifact("reports", b"source", ".bin")
        ref = self.store.artifact("reports", b"source", ".bin")
        with patch.object(store_module, "file_hash", side_effect=expire):
            with self.assertRaises(ResourceLimit):
                self.store.artifact_hash_valid(ref)

    def test_history_roundtrip(self):
        with self.store.lock():
            state = self.store.create("example", "Пример")
            state["notes"].append("Первое решение")
            self.store.append(state, "note")
        self.assertEqual(self.store.load()["notes"], ["Первое решение"])

    def test_prior_version_can_only_be_read_for_terminal_handoff(self):
        state = self.store.create("prior", "Prior schema-one fixture")
        state["version"] = "0.1.0-alpha.3"
        self.store.append(state, "prior-version-fixture")
        with self.assertRaises(Invalid):
            self.store.load(handoff_only=True)
        state["status"] = "cancelled"
        self.store.append(state, "prior-stopped")
        with self.assertRaises(Invalid):
            self.store.load()
        self.assertEqual(self.store.load(handoff_only=True)["version"], "0.1.0-alpha.3")
        created = self.store.create("next", "New current run")
        self.assertNotEqual(created["run"], state["run"])

    def test_unknown_prior_version_is_not_silently_migrated(self):
        state = self.store.create("future", "Unsupported fixture")
        state.update(version="9.0.0", status="cancelled")
        self.store.append(state, "unsupported")
        with self.assertRaises(Invalid):
            self.store.load(handoff_only=True)

    def test_alpha10_terminal_handoff_is_read_only_after_stable_upgrade(self):
        for status in ("accepted", "delivered_with_remaining", "cancelled"):
            with self.subTest(status=status):
                store = Journal(self.root / status)
                with patch("aispa_core.store.VERSION", "0.1.0-alpha.10"):
                    state = store.create("prior", "Alpha.10 fixture")
                    state["status"] = status
                    store.append(state, "terminal-fixture")
                before = {p.relative_to(store.home): p.read_bytes()
                          for p in store.home.rglob("*") if p.is_file()}
                with self.assertRaisesRegex(Invalid, "закреплённым runtime"):
                    store.load()
                self.assertEqual(store.load(handoff_only=True), state)
                self.assertEqual({p.relative_to(store.home): p.read_bytes()
                                  for p in store.home.rglob("*") if p.is_file()}, before)
                next_state = store.create("next", "Stable run")
                self.assertEqual(next_state["version"], VERSION)
                self.assertEqual((store.home / "runs" / state["run"] / "events.jsonl").read_bytes(),
                                 before[Path("runs") / state["run"] / "events.jsonl"])

    def test_stable_010_terminal_handoff_is_read_only_after_020_upgrade(self):
        for status in ("accepted", "delivered_with_remaining", "cancelled"):
            with self.subTest(status=status):
                store = Journal(self.root / ("stable-" + status))
                with patch("aispa_core.store.VERSION", "0.1.0"):
                    state = store.create("prior", "Stable 0.1.0 fixture")
                    state["status"] = status
                    store.append(state, "terminal-fixture")
                with self.assertRaisesRegex(Invalid, "закреплённым runtime"):
                    store.load()
                self.assertEqual(store.load(handoff_only=True), state)
                self.assertEqual(store.create("next", "Run 0.2.0")["version"], VERSION)

    def test_active_stable_010_requires_its_pinned_runtime(self):
        with patch("aispa_core.store.VERSION", "0.1.0"):
            self.store.create("prior", "Active 0.1.0")
        for handoff_only in (False, True):
            with self.subTest(handoff_only=handoff_only), self.assertRaisesRegex(Invalid, "закреплённым runtime"):
                self.store.load(handoff_only=handoff_only)

    def test_active_alpha10_requires_its_pinned_runtime(self):
        with patch("aispa_core.store.VERSION", "0.1.0-alpha.10"):
            prior = self.store.create("prior", "Active alpha.10")
        before = {p.relative_to(self.store.home): p.read_bytes()
                  for p in self.store.home.rglob("*") if p.is_file()}
        for handoff_only in (False, True):
            with self.subTest(handoff_only=handoff_only), self.assertRaisesRegex(Invalid, "закреплённым runtime"):
                self.store.load(handoff_only=handoff_only)
        with self.assertRaisesRegex(Invalid, "закреплённым runtime"):
            self.store.create("next", "Must not replace active alpha.10")
        self.assertEqual({p.relative_to(self.store.home): p.read_bytes()
                          for p in self.store.home.rglob("*") if p.is_file()}, before)
        with patch("aispa_core.store.VERSION", "0.1.0-alpha.10"):
            self.assertEqual(self.store.load(), prior)

    def test_zone_aliases_hash_real_files_and_reject_reserved_paths(self):
        self.repository()
        before = self.store.code(["./app.py"])
        self.assertEqual(before, self.store.code(["app.py"]))
        (self.root / "app.py").write_text("print('changed')\n", encoding="utf-8", newline="\n")
        self.assertNotEqual(before, self.store.code(["./app.py"]))
        for zone in ("./.aispa/context", "./.git/config", "src/../app.py"):
            with self.subTest(zone=zone), self.assertRaises(Invalid):
                self.store.code([zone])

    def test_second_writer_cannot_enter(self):
        with self.store.lock():
            with self.assertRaises(Invalid):
                with Journal(self.root).lock():
                    self.fail("entered twice")
        self.assertFalse((self.store.home / ".lock").exists())

    def test_history_corruption_is_rejected(self):
        self.store.create("example", "Пример")
        path = self.store.run / "events.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace('"running"', '"accepted"'), encoding="utf-8", newline="\n")
        with self.assertRaises(Invalid):
            self.store.load()

    def test_partial_history_is_rejected(self):
        self.store.create("example", "Пример")
        with (self.store.run / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write('{"sequence":')
        with self.assertRaises(Invalid):
            self.store.load()

    def test_source_mutation_is_rejected(self):
        state = self.store.create("example", "Пример")
        artifact = self.store.artifact("briefs", "Исходное требование".encode(), ".md")
        state["briefs"].append(dict(artifact, id="B01"))
        self.store.append(state, "brief")
        (self.store.run / artifact["file"]).write_text("Другой текст", encoding="utf-8", newline="\n")
        with self.assertRaises(Invalid):
            self.store.load()

    def test_unfinished_run_cannot_be_overwritten(self):
        self.store.create("example", "Пример")
        with self.assertRaises(Invalid):
            self.store.create("second", "Другой")

    def test_invalid_slug_is_rejected(self):
        for slug in ("../outside", "x/y", "", "two words"):
            with self.subTest(slug=slug), self.assertRaises(Invalid):
                self.store.create(slug, "Пример")

    def test_traversal_and_symlink_escape_are_rejected(self):
        for name in ("../x", "/tmp/x", "x/../../z"):
            with self.subTest(name=name), self.assertRaises(Invalid):
                confined(self.root, name)
        (self.root / "link").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(Invalid):
            confined(self.root, "link/outside")

    def test_code_hash_tracks_edits_deletes_and_untracked_files(self):
        self.repository()
        baseline = self.store.code()
        (self.root / "new file.py").write_text("# new\n", encoding="utf-8", newline="\n")
        self.assertNotEqual(baseline, self.store.code())
        (self.root / "new file.py").unlink()
        self.assertEqual(baseline, self.store.code())
        (self.root / "app.py").unlink()
        self.assertNotEqual(baseline, self.store.code())

    def test_code_hash_ignores_run_records_and_gitignored_cache(self):
        self.repository()
        baseline = self.store.code()
        self.store.create("example", "Пример")
        (self.root / "cache").mkdir()
        (self.root / "cache/file").write_text("derived", encoding="utf-8", newline="\n")
        self.assertEqual(baseline, self.store.code())
        self.assertTrue(self.store.clean())

    def test_clean_detects_spaces_and_renames(self):
        self.repository()
        self.assertTrue(self.store.clean())
        self.git("mv", "app.py", "renamed app.py")
        self.assertFalse(self.store.clean())

    def test_root_must_be_git_root(self):
        self.repository()
        subdir = self.root / "subdir"
        subdir.mkdir()
        with self.assertRaises(Invalid):
            Journal(subdir).code()


if __name__ == "__main__":
    unittest.main()
