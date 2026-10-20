import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core.store import Invalid, Journal, timestamp, digest


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-recovery-")
        self.root = Path(self.temp.name)
        self.store = Journal(self.root)
        self.state = self.store.create("unicode", "Проверка 🌿")
        self.path = self.store.run / "events.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def torn(self, raw=b'{"sequence":'):
        with self.path.open("ab") as stream:
            stream.write(raw)
        return self.path.read_bytes()

    def partial_state(self):
        last = json.loads(self.path.read_bytes().splitlines()[-1])
        prefix = json.dumps({"sequence": last["sequence"] + 1, "previous": last["hash"],
                             "at": timestamp(), "event": "fixture"},
                            ensure_ascii=False, separators=(",", ":"))[:-1]
        state = json.dumps({k: last["state"][k] for k in ("schema", "version", "run")},
                           ensure_ascii=False, separators=(",", ":"))[:-1]
        return (prefix + ',"state":' + state + ',"title":"').encode()

    def test_review_backup_and_explicit_recovery_preserve_verified_prefix(self):
        self.state["notes"].append("Решение — оставить историю")
        self.state["verification"] = {"historic": "not a current proof"}
        self.state["gates"]["acceptance"] = {"passed": True}
        self.state["taskHistory"] = {"T01": [{"id": "A1", "criticBudget": 2}]}
        self.store.append(self.state, "note")
        prefix = self.path.read_bytes()
        original = self.torn(self.partial_state() + "Оборва".encode("utf-8"))
        candidate = self.store.inspect_recovery()
        self.assertEqual(candidate["journalSha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(candidate["validSequence"], 2)
        self.assertEqual(candidate["tailBytes"], len(original) - len(prefix))
        self.assertFalse((self.store.run / candidate["backupFile"]).exists())
        with self.assertRaises(Invalid):
            self.store.apply_recovery(candidate["journalSha256"])
        result = self.store.apply_recovery(candidate["journalSha256"], confirmed=True)
        self.assertEqual(result["recoverySequence"], 3)
        backup = self.store.run / candidate["backupFile"]
        self.assertEqual(backup.read_bytes(), original)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertTrue(self.path.read_bytes().startswith(prefix))
        recovered = self.store.load()
        self.assertEqual(recovered["status"], "paused")
        self.assertTrue(recovered["journalRecovery"]["pending"])
        self.assertIsNone(recovered["verification"])
        self.assertNotIn("acceptance", recovered["gates"])
        self.assertEqual(recovered["taskHistory"]["T01"][0]["criticBudget"], 2)
        self.assertEqual(self.store.verified_events()[-1]["event"], "journal-recovered")

    def test_corrupt_middle_remains_blocked_even_with_torn_tail(self):
        self.store.append(self.state, "second")
        raw = self.path.read_bytes().replace(b'"running"', b'"accepted"', 1)
        self.path.write_bytes(raw)
        self.torn()
        with self.assertRaises(Invalid):
            self.store.inspect_recovery()

    def test_complete_json_without_newline_is_ambiguous(self):
        self.torn(b'{"sequence":99}')
        with self.assertRaises(Invalid):
            self.store.inspect_recovery()

    def test_malformed_terminal_json_is_not_a_recovery_candidate(self):
        self.torn(b'{"sequence":1x')
        with self.assertRaises(Invalid):
            self.store.inspect_recovery()

    def test_incomplete_tail_with_wrong_sequence_is_not_a_candidate(self):
        self.torn(b'{"sequence":999,"state":')
        with self.assertRaises(Invalid):
            self.store.inspect_recovery()

    def test_torn_last_utf8_character_recovers_but_invalid_middle_does_not(self):
        self.torn(self.partial_state() + "Ж".encode("utf-8")[:1])
        candidate = self.store.inspect_recovery()
        self.store.apply_recovery(candidate["journalSha256"], confirmed=True)
        self.assertTrue(self.store.load()["journalRecovery"]["pending"])
        self.torn(self.partial_state() + b"\xffbroken")
        with self.assertRaises(Invalid):
            self.store.inspect_recovery()

    def test_empty_journal_does_not_infer_a_state(self):
        self.path.write_bytes(b"")
        with self.assertRaises(Invalid):
            self.store.inspect_recovery()

    def test_prior_version_torn_history_needs_its_pinned_runtime(self):
        self.state["version"] = "0.1.0-alpha.8"
        self.state["status"] = "cancelled"
        self.store.append(self.state, "terminal-prior-version")
        self.torn()
        with self.assertRaises(Invalid):
            self.store.inspect_recovery()

    def test_recovery_does_not_bypass_immutable_source_check(self):
        artifact = self.store.artifact("briefs", "Исходный текст".encode("utf-8"), ".md")
        self.state["briefs"].append({**artifact, "id": "B01"})
        self.store.append(self.state, "brief")
        self.torn(self.partial_state())
        (self.store.run / artifact["file"]).write_text("Подменённый текст", encoding="utf-8")
        with self.assertRaises(Invalid):
            self.store.inspect_recovery()

    def test_expected_hash_is_compare_and_swap(self):
        original = self.torn()
        candidate = self.store.inspect_recovery()
        self.torn(b" ")
        with self.assertRaises(Invalid):
            self.store.apply_recovery(candidate["journalSha256"], confirmed=True)
        self.assertEqual(self.path.read_bytes(), original + b" ")
        self.assertFalse((self.store.run / candidate["backupFile"]).exists())

    def test_race_before_replace_does_not_replace_changed_file(self):
        self.torn()
        candidate = self.store.inspect_recovery()
        original_hash = Journal._hash_file
        calls = 0

        def interleave(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                with self.path.open("ab") as stream:
                    stream.write(b" ")
            return original_hash(path)

        with patch.object(Journal, "_hash_file", staticmethod(interleave)):
            with self.assertRaises(Invalid):
                self.store.apply_recovery(candidate["journalSha256"], confirmed=True)
        self.assertTrue(self.path.read_bytes().endswith(b'"sequence": '))

    def test_linked_history_is_rejected(self):
        outside = self.root / "outside.jsonl"
        outside.write_bytes(self.path.read_bytes() + b'{"sequence":')
        self.path.unlink()
        try:
            self.path.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(Invalid):
            self.store.inspect_recovery()

    def test_linked_runs_directory_is_rejected(self):
        runs = self.store.home / "runs"
        moved = self.store.home / "saved-runs"
        runs.rename(moved)
        try:
            runs.symlink_to(moved, target_is_directory=True)
        except (OSError, NotImplementedError):
            moved.rename(runs)
            self.skipTest("symlinks unavailable")
        with self.assertRaises(Invalid):
            self.store.inspect_recovery()
        with self.assertRaises(Invalid):
            self.store.load()

    def test_append_rechecks_full_bytes_not_only_mtime(self):
        state = self.store.load()
        before = self.path.stat()
        raw = self.path.read_bytes().replace(b'"running"', b'"accepted"', 1)
        self.path.write_bytes(raw)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(Invalid):
            self.store.append(state, "must-not-write")

    def test_verified_events_rejects_corruption(self):
        self.store.append(self.state, "second")
        self.assertEqual(len(self.store.verified_events()), 2)
        raw = self.path.read_bytes().replace(b'"running"', b'"accepted"', 1)
        self.path.write_bytes(raw)
        with self.assertRaises(Invalid):
            self.store.verified_events()

    def test_verified_summary_reuses_cursor_without_reparsing_and_checks_bytes(self):
        self.store.load()
        with patch.object(Journal, "_scan_journal", side_effect=AssertionError("reparsed")):
            self.assertEqual(self.store.read_journal()["count"], 1)
            self.path.write_bytes(self.path.read_bytes() + b" ")
            with self.assertRaises(Invalid):
                self.store.read_journal()

    def test_recovery_rejects_impossible_envelope_order_and_wrong_parent_hash(self):
        prefix = self.path.read_bytes()
        wrong_state_order = self.partial_state().removesuffix(b'"title":"') + b'"bogus":'
        for tail in (b'{"sequence":2,"bogus":', b'{"sequence":2,"state":',
                     b'{"sequence":2,"previous":"' + b"0" * 64 + b'","at":', wrong_state_order):
            with self.subTest(tail=tail):
                self.path.write_bytes(prefix + tail)
                with self.assertRaises(Invalid):
                    self.store.inspect_recovery()

    def test_partial_hash_must_match_the_fully_known_row(self):
        prefix = self.path.read_bytes()
        last = json.loads(prefix.splitlines()[-1])
        row = {"sequence": 2, "previous": last["hash"], "at": timestamp(), "event": "fixture",
               "state": last["state"]}
        expected = digest(row)
        body = json.dumps(row, ensure_ascii=False, separators=(",", ":"))[:-1].encode() + b',"hash":"'
        self.path.write_bytes(prefix + body + expected[:8].encode())
        self.assertTrue(self.store.inspect_recovery()["recoverable"])
        wrong = ("0" if expected[0] != "0" else "1") + expected[1:8]
        self.path.write_bytes(prefix + body + wrong.encode())
        with self.assertRaises(Invalid):
            self.store.inspect_recovery()


if __name__ == "__main__":
    unittest.main()
