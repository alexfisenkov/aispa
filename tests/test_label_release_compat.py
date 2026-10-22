"""A wording-only upgrade must not require restarting a 0.3.2 project."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/aispa/tools'))
from aispa_core.store import Journal, Invalid
from aispa_core.dashboard_reader import DisplayJournal

class LabelReleaseCompatibilityTests(unittest.TestCase):
    def test_active_032_has_read_only_view_and_terminal_handoff_without_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            store=Journal(root)
            with patch('aispa_core.store.VERSION','0.3.2'):
                state=store.create('old','Synthetic previous stable')
            path=store.run/'events.jsonl'; before=path.read_bytes()
            with self.assertRaises(Invalid):store.load()
            self.assertEqual(DisplayJournal(root).load()['version'],'0.3.2')
            self.assertEqual(path.read_bytes(),before)
            state['status']='cancelled';store.append(state,'synthetic-terminal')
            before=path.read_bytes()
            self.assertEqual(store.load(handoff_only=True)['version'],'0.3.2')
            self.assertEqual(path.read_bytes(),before)

if __name__=='__main__':unittest.main()
