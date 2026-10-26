"""Focused discovery resource boundaries; synthetic fixtures are not product acceptance."""
import copy
import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/aispa/tools'))
from aispa_core import discovery
from aispa_core.store import Invalid
from tests import test_workflow as workflow_fixture


class DiscoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fx = workflow_fixture.WorkflowTests('test_empty_run_cannot_finish')
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.fx.state['policy'] = {'scalableInputs': True}
        self.fx.store.scale_enabled = True

    def source(self, name, raw):
        path = self.fx.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path.resolve()

    def snapshot(self):
        return (copy.deepcopy(self.fx.state), {
            p.relative_to(self.fx.store.run).as_posix(): p.read_bytes() if p.is_file() else None
            for p in self.fx.store.run.rglob('*')})

    def test_discovery_retained_page_memory_is_not_only_a_per_source_limit(self):
        names = [f'docs/part{i}.md' for i in range(3)]
        originals = {name: (f'Complete safe part {i}.\n' * 75).encode() for i, name in enumerate(names)}
        for name, raw in originals.items():
            self.source(name, raw)
            self.assertLessEqual(len(raw) * 4, 8192, 'each source fits the isolated input estimate')
        before = self.snapshot()
        with patch.dict(os.environ, {'AISPA_MAX_MEMORY_BYTES': '8192'}):
            with self.assertRaisesRegex(Invalid, 'RESOURCE_LIMIT.*maxMemoryBytes.*continuation'):
                discovery.collect(self.fx.store, self.fx.state, paths=names)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({name: (self.fx.root / name).read_bytes() for name in names}, originals)
        with patch.dict(os.environ, {'AISPA_MAX_MEMORY_BYTES': '131072'}):
            collected = discovery.collect(self.fx.store, self.fx.state, paths=names)
        rows = {r['path']: r for r in collected['sources']}
        self.assertEqual(collected['includedPaths'], names)
        self.assertTrue(set(names) <= set(rows), 'resource handling must not silently omit requested sources')
        for name, raw in originals.items():
            self.assertEqual(rows[name]['excerpt'].encode(), raw)
            self.assertEqual(rows[name]['omittedChars'], 0)

    def test_discovery_post_stat_growth_is_bounded_before_materialization(self):
        name = 'docs/growing.md'
        raw = b'Safe original.\n'
        source = self.source(name, raw)
        before = self.snapshot()
        requests, received = [], []
        original_fdopen = os.fdopen
        source_stat = source.stat()
        identity = source_stat.st_dev, source_stat.st_ino
        grown = []

        class GrowingSource:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def fileno(self):
                return self.stream.fileno()

            def read(self, size=-1):
                if not grown:
                    # Grow the real disposable file only after physical_read's
                    # stat/fstat checks, immediately before its first read.
                    with source.open('ab') as writer:
                        writer.write(b'Safe grown paragraph.\n' * 1000)
                    grown.append(identity)
                requests.append(size)
                part = self.stream.read(size)
                received.append(len(part))
                return part

        def open_grown_source(fd, *args, **kwargs):
            opened = os.fstat(fd)
            stream = original_fdopen(fd, *args, **kwargs)
            if (opened.st_dev, opened.st_ino) == identity:
                return GrowingSource(stream)
            return stream

        try:
            with patch.dict(os.environ, {'AISPA_MAX_MEMORY_BYTES': '4096'}):
                with patch('aispa_core.verified_reads.os.fdopen', open_grown_source):
                    with self.assertRaisesRegex(Invalid, 'RESOURCE_LIMIT.*maxMemoryBytes.*continuation'):
                        discovery.collect(self.fx.store, self.fx.state, paths=[name])
        finally:
            source.write_bytes(raw)
        self.assertEqual(grown, [identity])
        self.assertTrue(requests)
        self.assertTrue(all(0 < size <= 1025 for size in requests),
                        'reads must fit the remaining materialization budget plus one-byte overflow detection')
        self.assertLessEqual(sum(received), 1025, 'growth must stop before an oversized source allocation')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(source.read_bytes(), raw)

    def test_legacy_discovery_retains_its_original_limits(self):
        self.fx.state['policy'] = {}
        self.fx.store.scale_enabled = False
        raw = b'Safe legacy source.\n' * 1000
        name = 'docs/legacy.md'
        source = self.source(name, raw)
        with patch.dict(os.environ, {'AISPA_MAX_MEMORY_BYTES': '1'}):
            collected = discovery.collect(self.fx.store, self.fx.state, paths=[name])
        row = next(r for r in collected['sources'] if r['path'] == name)
        self.assertEqual(row['excerpt'].encode(), raw[:discovery.MAX_EXCERPT_CHARS])
        self.assertEqual(row['omittedChars'], len(raw) - discovery.MAX_EXCERPT_CHARS)
        self.assertEqual(row['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertNotIn('pagination', collected)
        self.assertEqual(source.read_bytes(), raw)

    def test_discovery_memory_counts_utf8_bytes_not_only_characters(self):
        name = 'docs/utf8.md'
        ascii_text = 'a' * 2200 + '\n'
        wide_text = '🦊' * 2200 + '\n'
        self.assertEqual(len(ascii_text), len(wide_text))
        self.assertLessEqual(len(ascii_text.encode()) * 4, 32768)
        self.assertGreater(len(wide_text.encode()) * 4, 32768)
        source = self.source(name, ascii_text.encode())
        with patch.dict(os.environ, {'AISPA_MAX_MEMORY_BYTES': '32768'}):
            baseline = discovery.collect(self.fx.store, self.fx.state, paths=[name])
        self.assertEqual(next(r for r in baseline['sources'] if r['path'] == name)['excerpt'], ascii_text)
        source.write_bytes(wide_text.encode())
        before = self.snapshot()
        with patch.dict(os.environ, {'AISPA_MAX_MEMORY_BYTES': '32768'}):
            with self.assertRaisesRegex(Invalid, 'RESOURCE_LIMIT.*maxMemoryBytes.*continuation'):
                discovery.collect(self.fx.store, self.fx.state, paths=[name])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(source.read_bytes(), wide_text.encode())
        with patch.dict(os.environ, {'AISPA_MAX_MEMORY_BYTES': '262144'}):
            collected = discovery.collect(self.fx.store, self.fx.state, paths=[name])
        row = next(r for r in collected['sources'] if r['path'] == name)
        self.assertEqual(row['excerpt'], wide_text)
        self.assertEqual(row['sha256'], hashlib.sha256(wide_text.encode()).hexdigest())
        self.assertEqual(row['omittedChars'], 0)
        self.assertEqual(collected['includedPaths'], [name])


if __name__ == '__main__':
    unittest.main()
