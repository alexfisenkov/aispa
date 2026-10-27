import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import journal_delta
from aispa_core.dashboard_reader import DisplayJournal, SnapshotJournal
from aispa_core.store import Invalid, Journal, digest


def delta_policy(state):
    state["policy"] = {"deltaJournal": True}


def rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def rewrite(path, values):
    """Re-serialize rows exactly as Journal.append does, keeping a valid chain shape."""
    path.write_bytes(b"".join((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                              for row in values))


class DeltaOperationsTests(unittest.TestCase):
    def roundtrip(self, old, new):
        ops = journal_delta.diff(old, new)
        # Rows are stored as JSON; replay must work on the decoded form.
        ops = json.loads(json.dumps(ops, ensure_ascii=False))
        snapshot = copy.deepcopy(old)
        result = journal_delta.apply(old, ops)
        self.assertEqual(old, snapshot, "apply must not mutate its input state")
        self.assertEqual(digest(result), digest(new))
        return ops

    def test_operations_cover_set_delete_and_list_append(self):
        old = {"status": "running", "notes": ["a"], "gates": {"plan": {"passed": False}}, "gone": 1}
        new = {"status": "paused", "notes": ["a", "b", "c"], "gates": {"plan": {"passed": True}}}
        ops = self.roundtrip(old, new)
        self.assertIn(["set", ["status"], "paused"], ops)
        self.assertIn(["del", ["gone"]], ops)
        self.assertIn(["add", ["notes"], ["b", "c"]], ops)
        self.assertIn(["set", ["gates", "plan", "passed"], True], ops)

    def test_list_elements_are_addressed_by_index(self):
        old = {"tasks": [{"id": "T01", "status": "ready"}, {"id": "T02", "status": "ready"}]}
        new = {"tasks": [{"id": "T01", "status": "ready"}, {"id": "T02", "status": "running"}]}
        self.assertEqual(self.roundtrip(old, new), [["set", ["tasks", 1, "status"], "running"]])

    def test_shrunk_list_and_type_changes_are_replaced(self):
        old = {"a": [1, 2, 3], "b": 1, "c": {"x": 1}, "d": True}
        new = {"a": [1], "b": 1.0, "c": [1], "d": 1}
        self.roundtrip(old, new)

    def test_diff_is_deterministic_and_empty_for_equal_states(self):
        old = {"k": {"z": 1, "a": [1, {"b": 2}]}}
        self.assertEqual(journal_delta.diff(old, copy.deepcopy(old)), [])
        new = {"k": {"z": 2, "a": [1, {"b": 3}], "n": None}}
        self.assertEqual(journal_delta.diff(old, new), journal_delta.diff(copy.deepcopy(old), copy.deepcopy(new)))

    def test_malformed_operations_are_rejected(self):
        state = {"a": {"b": [1]}}
        for ops in ("set", [["move", ["a"], 1]], [["set", "a", 1]], [["del", ["missing"]]],
                    [["set", ["a", "b", 5], 1]], [["add", ["a"], [1]]], [["set", ["a", "b", True], 1]],
                    [["del", []]], [["add", ["a", "b"], "x"]]):
            with self.subTest(ops=ops):
                with self.assertRaises(ValueError):
                    journal_delta.apply(state, ops)
        self.assertEqual(state, {"a": {"b": [1]}})



class DeltaJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-delta-journal-")
        self.root = Path(self.temp.name).resolve()
        self.store = Journal(self.root)
        with self.store.lock():
            self.state = self.store.create("delta", "Журнал изменений", initializer=delta_policy)
        self.path = self.store.run / "events.jsonl"
        self.cache = self.store.run / "journal-cache.json"
        self.saved = [copy.deepcopy(self.state)]

    def tearDown(self):
        self.temp.cleanup()

    def write(self, event, change=None, *, store=None, locked=True):
        store = store or self.store
        if change is not None:
            change(self.state)
        else:
            self.state["notes"].append(event)
        if locked:
            with store.lock():
                store.append(self.state, event)
        else:
            store.append(self.state, event)
        self.saved.append(copy.deepcopy(self.state))

    def other(self):
        writer = Journal(self.root)
        writer.load()
        return writer

    def fresh_load(self, *, locked=True, cls=Journal):
        reader = cls(self.root)
        if locked:
            with reader.lock():
                return reader, reader.load()
        return reader, reader.load()

    def test_event_rows_carry_delta_and_checkpoints_carry_full_state(self):
        self.write("runtime-pinned")
        self.write("note")
        values = rows(self.path)
        self.assertIn("state", values[0])
        self.assertIn("state", values[1])
        self.assertNotIn("state", values[2])
        self.assertEqual(values[2]["stateDigest"], digest(self.saved[2]))
        self.assertTrue(any(op[0] == "add" and op[1] == ["notes"] for op in values[2]["delta"]))
        self.assertEqual(digest({k: v for k, v in values[2].items() if k != "hash"}), values[2]["hash"])
        self.assertEqual(values[2]["previous"], values[1]["hash"])
        for number in range(4, 2 + Journal.CHECKPOINT_EVERY):
            self.write("event-%d" % number, locked=False)
        values = rows(self.path)
        self.assertNotIn("state", values[2 + Journal.CHECKPOINT_EVERY - 2])
        self.write("periodic", locked=False)
        values = rows(self.path)
        self.assertEqual(len(values), 2 + Journal.CHECKPOINT_EVERY)
        self.assertIn("state", values[-1])
        self.write("after-periodic", locked=False)
        self.assertNotIn("state", rows(self.path)[-1])
        self.write("large", lambda state: state.update(blob="Ж" * (4 * len(json.dumps(state)))), locked=False)
        self.assertIn("state", rows(self.path)[-1])

    def test_finished_run_ends_in_full_state_rows_for_handoff_readers(self):
        self.write("note")
        self.write("accepted", lambda state: state.update(status="accepted"))
        self.write("after-acceptance")
        values = rows(self.path)
        self.assertNotIn("state", values[1])
        self.assertEqual(values[-2]["state"]["status"], "accepted")
        self.assertEqual(values[-1]["state"], self.saved[-1])

    def test_runs_without_the_flag_keep_full_state_rows(self):
        store = Journal(Path(tempfile.mkdtemp(dir=self.temp.name)))
        with store.lock():
            state = store.create("plain", "Old format")
            state["notes"].append("x")
            store.append(state, "note")
        self.assertTrue(all("state" in row and "delta" not in row for row in rows(store.run / "events.jsonl")))
        self.assertFalse((store.run / "journal-cache.json").exists())

    def test_five_hundred_events_with_one_megabyte_state_stay_under_ten_percent(self):
        self.state["taskHistory"] = {"T%03d" % index: [{"id": "A1", "log": "x" * 10000}] for index in range(100)}
        self.write("runtime-pinned", lambda state: None, locked=False)
        for number in range(3, 501):
            def change(state, number=number):
                state["notes"].append("event %d" % number)
                state["taskHistory"]["T%03d" % (number % 100)][0]["status"] = "step %d" % number
            self.write("event", change, locked=False)
        self.assertEqual(len(rows(self.path)), 500)
        full_format = sum(len((json.dumps(state, ensure_ascii=False, separators=(",", ":"))).encode())
                          for state in self.saved)
        self.assertGreater(full_format / 500, 1_000_000)
        self.assertLess(self.path.stat().st_size, full_format * 0.10)
        _, loaded = self.fresh_load(locked=False)
        self.assertEqual(loaded, self.saved[-1])

    def tamper(self, index, old, new):
        lines = self.path.read_bytes().splitlines(keepends=True)
        before = self.path.stat()
        self.assertIn(old, lines[index])
        lines[index] = lines[index].replace(old, new, 1)
        self.path.write_bytes(b"".join(lines))
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_one_byte_tamper_in_early_delta_or_checkpoint_row_is_rejected(self):
        self.write("runtime-pinned")
        self.write("note-a")
        self.write("note-b")
        original = self.path.read_bytes()
        for index, old, new in ((2, b"note-a", b"note-A"), (0, b"\"running\"", b"\"runninG\""),
                                (1, b"runtime-pinned", b"runtime-pinneD")):
            with self.subTest(row=index):
                self.path.write_bytes(original)
                self.fresh_load()  # a current cache exists for this exact prefix
                self.tamper(index, old, new)
                with self.assertRaises(Invalid):
                    self.fresh_load()
                with self.assertRaises(Invalid):
                    self.fresh_load(locked=False, cls=SnapshotJournal)

    def test_reconstructed_state_must_match_state_digest(self):
        self.write("runtime-pinned")
        self.write("note")
        values = rows(self.path)
        self.cache.unlink()
        for forge in ("digest", "delta"):
            with self.subTest(forge=forge):
                row = copy.deepcopy(values[2])
                if forge == "digest":
                    row["stateDigest"] = digest({"forged": True})
                else:
                    row["delta"] = [op if op[1] != ["notes"] else ["add", ["notes"], ["forged"]]
                                    for op in row["delta"]]
                row["hash"] = digest({k: v for k, v in row.items() if k != "hash"})
                rewrite(self.path, values[:2] + [row])
                with self.assertRaisesRegex(Invalid, "Повреждён журнал, запись 3"):
                    Journal(self.root).load()

    def test_unchanged_journal_load_parses_no_rows_and_new_row_parses_one(self):
        for number in range(5):
            self.write("note-%d" % number)
        reader, state = self.fresh_load()
        self.assertEqual(reader.parsed_rows, 0)
        self.assertEqual(state, self.saved[-1])
        self.write("unlocked", store=self.other(), locked=False)  # cache not refreshed
        reader, state = self.fresh_load()
        self.assertEqual(reader.parsed_rows, 1)
        self.assertEqual(state, self.saved[-1])
        reader, _ = self.fresh_load()
        self.assertEqual(reader.parsed_rows, 0)

    def test_cache_backed_cursor_still_rechecks_full_bytes(self):
        self.write("note")
        reader, state = self.fresh_load()
        self.assertEqual(reader.parsed_rows, 0)
        self.tamper(0, b"\"running\"", b"\"accepted\"")
        with self.assertRaises(Invalid):
            reader.read_journal()
        with self.assertRaises(Invalid):
            with reader.lock():
                reader.append(state, "must-not-write")

    def test_missing_corrupt_forged_or_stale_cache_is_rebuilt_with_the_same_result(self):
        for number in range(4):
            self.write("note-%d" % number)
        lagging = self.cache.read_bytes()
        self.write("last")
        self.cache.unlink()
        plain, expected_state = self.fresh_load(locked=False)
        self.assertFalse(self.cache.exists())
        expected = plain.read_journal()
        self.assertEqual(plain.parsed_rows, 6)
        current = None

        def forged_state(raw):
            value = json.loads(raw)
            value["state"]["status"] = "accepted"
            value["stateDigest"] = digest(value["state"])
            return json.dumps(value).encode()

        def too_long(raw):
            value = json.loads(raw)
            value["validBytes"] += 10
            return json.dumps(value).encode()

        cases = (("missing", None, 6), ("garbage", lambda raw: b"{not json", 6),
                 ("forged", forged_state, 6), ("ahead", too_long, 6), ("lagging", lambda raw: lagging, 1))
        for name, damage, parsed in cases:
            with self.subTest(cache=name):
                if current is not None:
                    self.cache.write_bytes(damage(current) if damage else current)
                    if damage is None:
                        self.cache.unlink()
                reader, state = self.fresh_load()
                self.assertEqual(reader.parsed_rows, parsed)
                self.assertEqual(state, expected_state)
                self.assertEqual(reader.read_journal(), expected)
                self.assertTrue(self.cache.exists())
                current = self.cache.read_bytes()
                again, _ = self.fresh_load()
                self.assertEqual(again.parsed_rows, 0)

    def test_readers_return_full_states_and_state_at_matches_each_saved_step(self):
        self.write("runtime-pinned")
        for number in range(Journal.CHECKPOINT_EVERY + 5):
            self.write("note-%d" % number, locked=number % 7 == 0)
        reader, _ = self.fresh_load()
        summary = reader.read_journal()
        self.assertEqual(summary["last"]["state"], self.saved[-1])
        self.assertEqual(summary["previous"]["state"], self.saved[-2])
        events = reader.read_journal(include_events=True)["events"]
        self.assertEqual([row["state"] for row in events], self.saved)
        self.assertEqual([row["sequence"] for row in events], list(range(1, len(self.saved) + 1)))
        fresh = Journal(self.root)
        fresh.load()
        for sequence in (1, 2, 3, 40, Journal.CHECKPOINT_EVERY + 1, Journal.CHECKPOINT_EVERY + 2,
                         len(self.saved) - 1, len(self.saved)):
            with self.subTest(sequence=sequence):
                value = fresh.state_at(sequence)
                self.assertEqual(value, self.saved[sequence - 1])
                value["notes"].append("detached")
                self.assertEqual(fresh.state_at(sequence), self.saved[sequence - 1])
        with self.assertRaises(Invalid):
            fresh.state_at(len(self.saved) + 1)
        self.tamper(0, b"\"running\"", b"\"accepted\"")
        with self.assertRaises(Invalid):
            fresh.state_at(3)

    def test_wrong_checkpoint_index_in_cache_falls_back_to_a_verified_scan(self):
        for number in range(6):
            self.write("note-%d" % number)
        value = json.loads(self.cache.read_bytes())
        value["checkpoints"] = [{"sequence": 1, "offset": 0}, {"sequence": 4, "offset": 7}]
        self.cache.write_text(json.dumps(value), encoding="utf-8")
        reader, _ = self.fresh_load(locked=False)
        self.assertEqual(reader.parsed_rows, 0)
        self.assertEqual(reader.state_at(5), self.saved[4])
        value["checkpoints"] = [{"sequence": 1, "offset": 0}, {"sequence": 3, "offset":
                                len(self.path.read_bytes().splitlines(keepends=True)[0])}]
        self.cache.write_text(json.dumps(value), encoding="utf-8")
        reader, _ = self.fresh_load(locked=False)
        self.assertEqual(reader.state_at(4), self.saved[3])

    def test_forged_cache_fields_never_change_what_readers_return(self):
        receipt = {"record": {"file": "evidence/T1-submitted.json", "sha256": "a" * 64}}
        self.write("note-0")
        self.write("submitted", lambda state: state.update(
            taskHistory={"T1": [{"id": "A1", "submitted": copy.deepcopy(receipt)}]}))
        for number in range(1, 4):
            self.write("note-%d" % number)
        honest = self.cache.read_bytes()
        self.cache.unlink()
        plain, expected_state = self.fresh_load(locked=False)  # no cache: every row verified
        expected = plain.read_journal()
        expected_receipt = plain.receipt_time(receipt)
        self.assertEqual(expected_receipt, rows(self.path)[2]["at"])
        self.assertFalse(self.cache.exists())

        def previous_row(value):
            value["previousRow"]["event"] = "verification-started"
            value["previousRow"]["state"]["activity"] = {"kind": "verification", "task": "T1"}

        def tail_event(value):
            value["eventTail"][-1]["event"] = "forged-tail"

        def early_tail_event(value):
            value["eventTail"][1]["at"] = "2000-01-01T00:00:00Z"

        def short_tail(value):
            value["eventTail"] = value["eventTail"][2:]

        def receipt_times(value):
            value["receiptTimes"] = [["evidence/T1-submitted.json", "a" * 64, "2000-01-01T00:00:00Z"]]

        for name, forge in (("previousRow", previous_row), ("eventTail", tail_event),
                            ("early eventTail", early_tail_event), ("short eventTail", short_tail),
                            ("receiptTimes", receipt_times)):
            with self.subTest(forged=name):
                value = json.loads(honest)
                forge(value)
                self.cache.write_text(json.dumps(value), encoding="utf-8")
                for locked in (False, True):
                    reader, state = self.fresh_load(locked=locked)
                    self.assertEqual(state, expected_state)
                    self.assertEqual(reader.read_journal(), expected)
                    self.assertEqual(reader.receipt_time(receipt), expected_receipt)
                snapshot = SnapshotJournal(self.root)
                snapshot.load()
                with snapshot._reads.scope():
                    self.assertEqual(snapshot.read_journal(), expected)
                    self.assertEqual(snapshot.receipt_time(receipt), expected_receipt)

    def test_snapshot_reader_uses_cache_and_keeps_generation_guard(self):
        self.write("runtime-pinned")
        self.write("note")
        reader = SnapshotJournal(self.root)
        state = reader.load()
        self.assertEqual(reader.parsed_rows, 0)
        self.assertEqual(state, self.saved[-1])
        with reader._reads.scope():
            self.assertEqual(reader.read_journal()["last"]["state"], self.saved[-1])
            self.assertEqual(reader.read_journal(include_events=True)["events"][-1]["state"], self.saved[-1])
            self.assertEqual(reader.finish_snapshot()["journalEvents"], 3)
        self.write("concurrent", store=self.other(), locked=False)
        with reader._reads.scope():
            # Inside one calculation the frozen generation is served; the
            # closing full-byte check refuses the snapshot.
            self.assertEqual(reader.read_journal()["count"], 3)
            with self.assertRaisesRegex(Invalid, "Журнал изменился после проверки"):
                reader.finish_snapshot()
        self.assertEqual(DisplayJournal(self.root).load(), self.saved[-1])


class OldFormatJournalTests(unittest.TestCase):
    def test_prior_version_journal_is_read_without_writing_anything(self):
        with tempfile.TemporaryDirectory(prefix="aispa-old-journal-") as temp:
            root = Path(temp).resolve()
            store = Journal(root)
            state = store.create("old", "Прогон 0.4.0")
            state["notes"].append("готово")
            state.update(version="0.4.0", status="accepted")
            store.append(state, "accepted")
            run = store.run
            before = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}
            for reader in (Journal(root), Journal(root)):
                with reader.lock():
                    self.assertEqual(reader.load(handoff_only=True)["version"], "0.4.0")
                    self.assertEqual(len(reader.read_journal(include_events=True)["events"]), 2)
                    self.assertEqual(reader.state_at(1)["status"], "running")
                    self.assertEqual(reader.state_at(2), state)
            self.assertEqual(DisplayJournal(root).load()["version"], "0.4.0")
            after = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            self.assertFalse((run / "journal-cache.json").exists())


if __name__ == "__main__":
    unittest.main()
