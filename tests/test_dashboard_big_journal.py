"""A 300 MB journal in the old format: how long the first snapshot takes, and that polling does not repeat it.

Slow and disk-hungry, so it runs only on request: `AISPA_BIG_JOURNAL=1 python3 -B -m unittest
discover -s tests -p 'test_dashboard_big_journal.py' -v`. The measured time is printed; the
release notes (`docs/validation-0.5.0.md`) take it from here.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/aispa/tools"))
from aispa_core import dashboard_server as dashboard
from aispa_core.store import Journal, digest, timestamp

ROW_PADDING = 1_100_000          # bytes of state per event, about 1.1 MB
EVENTS = 300                     # 300 full-state rows: well over 300 MiB, like a long run of the old format
MINIMUM_BYTES = 300 * 1024 * 1024


def fetch(url):
    try:
        stream = urlopen(url, timeout=600)
    except HTTPError as error:
        stream = error
    with stream:
        return stream.code, json.loads(stream.read())


def build_old_format_journal(store, state):
    """Append full-state rows straight to the file, the way the old format wrote them (chain intact)."""
    path = store.run / "events.jsonl"
    last = store.read_journal()["last"]
    previous, sequence = last["hash"], last["sequence"]
    # Structured weight, not one long string: parsing cost should resemble a real state.
    padding = [{"n": index, "text": "padding row %08d" % index} for index in range(ROW_PADDING // 48)]
    with path.open("ab") as stream:
        while path.stat().st_size < MINIMUM_BYTES or sequence < EVENTS:
            sequence += 1
            state["updatedAt"] = timestamp()
            state["padding"] = padding  # not projected; only weight
            row = {"sequence": sequence, "previous": previous, "at": state["updatedAt"],
                   "event": "agent-decision-recorded", "state": state}
            row["hash"] = previous = digest(row)
            stream.write((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            stream.flush()
    return path.stat().st_size


@unittest.skipUnless(os.environ.get("AISPA_BIG_JOURNAL") == "1", "set AISPA_BIG_JOURNAL=1 to build the 300 MB journal")
class BigJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aispa-big-journal-")
        self.store = Journal(self.temp.name)
        self.state = self.store.create("big", "Big journal fixture")
        self.size = build_old_format_journal(self.store, self.state)

    def tearDown(self):
        try:
            dashboard.stop(self.store)
        finally:
            self.temp.cleanup()

    def test_first_snapshot_is_timed_and_polling_reuses_it(self):
        self.assertGreaterEqual(self.size, MINIMUM_BYTES)
        calls = []
        original = dashboard.project

        def counted(reader, state):
            calls.append(time.monotonic())
            return original(reader, state)

        with patch.dict(os.environ, {"AISPA_OPERATION_SECONDS": "900", "AISPA_MAX_MEMORY_BYTES": str(4 * 1024 ** 3)}), \
                patch.object(dashboard, "project", side_effect=counted):
            thread = threading.Thread(target=dashboard.serve, args=(self.store,),
                                      kwargs={"managed": True, "announce": False}, daemon=True)
            started = time.monotonic()
            thread.start()
            try:
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    metadata = dashboard._read_meta(self.store)
                    if metadata is not None and dashboard._verified(self.store, metadata):
                        break
                    time.sleep(.05)
                else:
                    self.fail("The dashboard service did not start")
                url = dashboard._url(metadata["port"], "status.json")
                begun = time.monotonic()
                code, first = fetch(url)
                while code == 202:
                    code, first = fetch(url + "?calculation=" + first["calculation"]["id"])
                    time.sleep(.2)
                calculated = time.monotonic() - begun
                self.assertEqual(code, 200, first)
                print("\nAISPA_BIG_JOURNAL: journal %.0f MiB, %d events; service start %.1f s; "
                      "first snapshot %.1f s" % (self.size / 1048576, EVENTS, begun - started, calculated))
                for _ in range(5):
                    polled = time.monotonic()
                    code, again = fetch(url)
                    self.assertEqual(code, 200, again)
                    self.assertEqual(again["calculation"]["id"], first["calculation"]["id"])
                    self.assertLess(time.monotonic() - polled, 2, "A repeated poll re-read the journal")
                self.assertEqual(len(calls), 1, "Polling an unchanged 300 MB journal calculated again")
            finally:
                dashboard.stop(self.store)
                thread.join(timeout=30)


if __name__ == "__main__":
    unittest.main()
