"""Scale regressions: real domain validation and immutable, bounded transport.

Synthetic fixtures establish code contracts, not model or production acceptance.
"""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/aispa/tools'))
from aispa_core import briefmap, contracts, design_contract, discovery, ideas, packets, planning
from aispa_core.store import Invalid, digest
from tests.test_ideas import synthetic_report
from tests.test_workflow import WorkflowTests


class ScaleTests(unittest.TestCase):
    def setUp(self):
        self.fx = WorkflowTests('test_empty_run_cannot_finish')
        self.fx.setUp()
        self.store, self.state, self.flow = self.fx.store, self.fx.state, self.fx.flow
        self.state['policy'] = {'scalableInputs': True}

    def tearDown(self):
        self.fx.tearDown()

    def scale(self):
        return importlib.import_module('aispa_core.scale')

    def manifest(self, value, name='manifest.json', kind='json', chunk_bytes=4096):
        raw = (json.dumps(value, ensure_ascii=False).encode() if kind == 'json' else value.encode())
        path = self.fx.inbox / name
        self.scale().write_manifest(path, raw, kind=kind, basis=digest({'fixture': name}), chunk_bytes=chunk_bytes)
        return path

    def setup_scope(self, count=300):
        text = '\n'.join(f'Build operation {i:04d} with exact outcome {i:04d}.' for i in range(count))
        self.flow.brief(self.manifest(text, 'brief-manifest.json', 'text'))
        rows = [{'id': f'R{i:04d}', 'text': f'Operation {i}', 'kind': 'requested',
                 'brief': 'B001', 'quote': text.splitlines()[i], 'core': True,
                 'acceptance': [f'Exact output {i}', f'Preserve failure {i}']} for i in range(count)]
        self.flow.requirements(self.manifest(rows, 'requirements-manifest.json'))
        return rows

    def test_existing_large_idea_and_planning_regression_without_new_transport(self):
        # Exercise existing APIs directly so RED is a domain failure, not just
        # the intentionally absent manifest module.
        text = 'Build every exact operation.'
        self.flow.brief(self.fx.file(text, 'brief.md'))
        rows = [{'id': f'R{i:04d}', 'text': f'Operation {i}', 'kind': 'requested',
                 'brief': 'B001', 'quote': text, 'core': True,
                 'acceptance': [f'Exact result {i}']} for i in range(300)]
        self.flow.requirements(self.fx.file(rows))
        with self.subTest(contract='idea over 256 requirements'):
            ideas.record(self.store, self.state, synthetic_report(self.store, self.state))
        tasks = [{'id': f'T{i:04d}', 'title': f'Operation group {i}',
                  'requirements': [r['id'] for r in rows[i::110]], 'dependsOn': [],
                  'zones': [f'src/group{i}.py'], 'criteria': [f'Exact group {i}']} for i in range(110)]
        with self.subTest(contract='planning over 96 tasks'):
            self.assertEqual(planning.analyze(self.store, self.state, tasks)['taskCount'], 110)

    def test_large_preparation_plan_and_reports_preserve_every_id_and_criterion(self):
        rows = self.setup_scope()
        idea = synthetic_report(self.store, self.state)
        ideas.record(self.store, self.state, idea)
        self.assertEqual(ideas._saved_report(self.store, self.state), idea)
        self.flow.seal()
        self.flow.spec(self.manifest('All operations preserve their exact outcomes.', 'spec-manifest.json', 'text'))
        scenarios = {'basis': design_contract.scenario_basis(self.state), 'depth': 'balanced',
                     'depthReason': 'Complete source-bound synthetic operations.', 'features': [
            {'requirement': row['id'], 'changeKind': 'requested', 'sourceQuote': row['quote'],
             'specQuote': 'All operations preserve their exact outcomes.', 'dimensions': [
                 {'name': name, 'status': 'covered', 'scenario': 'Exercise ' + name,
                  'verification': 'Assert exact result and failure preservation.', 'counterexample': 'Invalid operation input.'}
                 for name in design_contract.DIMENSIONS]} for row in rows]}
        design_contract.record_scenarios(self.store, self.state, scenarios)
        self.assertTrue(design_contract.valid_scenarios(self.store, self.state))
        interfaces = {'basis': design_contract.interface_basis(self.state), 'interfaces': [
            {'name': 'operation.' + row['id'], 'signature': 'operate(value) -> exact result',
             'ownerZone': f'src/op{i}.py', 'consumers': [], 'requirements': [row['id']],
             'testSeam': 'Call the operation with valid and invalid input.', 'references': [],
             'referenceReason': 'Self-contained synthetic domain contract.'} for i, row in enumerate(rows)]}
        design_contract.record_interfaces(self.store, self.state, interfaces)
        self.assertTrue(design_contract.valid_interfaces(self.store, self.state))
        coverage = self.fx.header('coverage')
        coverage['checks'] = [{'requirement': r['id'], 'status': 'covered',
                               'specQuote': 'All operations preserve their exact outcomes.'} for r in rows]
        self.assertTrue(self.flow.coverage(self.manifest(coverage, 'coverage-manifest.json')))
        tasks = [{'id': f'T{i:04d}', 'title': f'Operations group {i}',
                  'requirements': [r['id'] for r in rows[i::110]], 'dependsOn': [],
                  'zones': [f'src/op{j}.py' for j in range(i, len(rows), 110)],
                  'criteria': [c for r in rows[i::110] for c in r['acceptance']]} for i in range(110)]
        advice = planning.analyze(self.store, self.state, tasks)
        review = {'basis': advice['basis'], 'draft': tasks, 'groups': [
            {'task': t['id'], 'from': [t['id']], 'reason': 'Independent operation group.'} for t in tasks],
                  'separate': []}
        self.flow.plan(self.manifest(tasks, 'plan-manifest.json'), self.manifest(review, 'review-manifest.json'))
        self.assertEqual(len(self.state['tasks']), 110)
        self.assertEqual({r['id'] for r in self.state['requirements']}, {r['id'] for r in rows})
        self.assertEqual({c for t in self.state['tasks'] for c in t['criteria']}, {c for r in rows for c in r['acceptance']})
        evidence = self.store.artifact('evidence', b'Synthetic exact checks.\n', '.txt')
        expected = {(r['id'], c) for r in rows for c in r['acceptance']}
        report = {'checks': [{'requirement': rid, 'criterion': c, 'status': 'passed',
                              'note': 'Synthetic fixture observation.', 'artifact': evidence['file']} for rid, c in sorted(expected)]}
        self.assertFalse(contracts.evidence_rows(self.store, report, expected)[1])
        report['checks'].pop()
        with self.assertRaises(Invalid):
            contracts.evidence_rows(self.store, report, expected)
        packet = packets.write(self.store, {'kind': 'scale-regression', 'basis': digest(rows), 'requirements': rows, 'report': report})
        self.assertLessEqual(packet['envelopeBytes'], packets.MAX_PACKET_BYTES)
        self.assertGreater(packet['sectionCount'], 0)

    def test_unicode_byte_splits_preserve_inline_manifest_semantics(self):
        text = 'Точный 🦊 e\u0301 исходник.\n' * 12
        path = self.manifest(text, 'unicode-text.json', 'text', chunk_bytes=1)
        self.assertEqual(self.flow.read_input(path), text)
        value = {'quotes': [text, text[::-1]], 'criteria': ['Последний критерий 🦊']}
        path = self.manifest(value, 'unicode-json.json', chunk_bytes=7)
        self.assertEqual(json.loads(self.flow.read_input(path)), value)
        self.assertEqual(json.loads(self.flow.read_input(self.fx.file(value, 'inline.json'))), value)

    def test_manifest_tamper_missing_duplicate_foreign_and_overflow_never_commit(self):
        self.flow.brief(self.fx.file('Build complete operations.', 'brief.md'))
        value = [{'id': 'R01', 'text': 'Operation', 'brief': 'B001', 'quote': 'Build complete operations.',
                  'core': True, 'acceptance': ['Complete result']}]
        for mutation in ('tamper', 'missing', 'duplicate', 'foreign', 'overflow', 'escape'):
            with self.subTest(mutation=mutation):
                path = self.manifest(value, mutation + '.json', chunk_bytes=40)
                manifest = json.loads(path.read_text())
                chunk = path.parent / manifest['chunks'][0]['file']
                if mutation == 'tamper': chunk.write_bytes(chunk.read_bytes() + b' ')
                elif mutation == 'missing': chunk.unlink()
                elif mutation == 'duplicate': manifest['chunks'].append(copy.deepcopy(manifest['chunks'][0]))
                elif mutation == 'escape': manifest['chunks'][0]['file'] = '../escape.json'
                elif mutation == 'overflow': manifest['totalBytes'] -= 1
                else:
                    body = json.loads(chunk.read_text()); body['basis'] = 'f' * 64
                    chunk.write_text(json.dumps(body)); manifest['chunks'][0]['sha256'] = hashlib.sha256(chunk.read_bytes()).hexdigest()
                path.write_text(json.dumps(manifest))
                before, events = copy.deepcopy(self.state), (self.store.run / 'events.jsonl').read_bytes()
                with self.assertRaises(Invalid): self.flow.requirements(path)
                self.assertEqual(self.state, before)
                self.assertEqual((self.store.run / 'events.jsonl').read_bytes(), events)

    def test_manifest_global_duplicate_and_unknown_dependency_are_rejected(self):
        self.setup_scope(2)
        before = copy.deepcopy(self.state)
        duplicate = [dict(self.state['requirements'][0], id='R999'), dict(self.state['requirements'][1], id='R999')]
        with self.assertRaises(Invalid): self.flow.requirements(self.manifest(duplicate, chunk_bytes=80))
        self.assertEqual(self.state, before)
        tasks = [{'id': 'T01', 'title': 'All', 'requirements': ['R0000', 'R0001'],
                  'dependsOn': ['T99'], 'zones': ['app.py'], 'criteria': ['Complete']}]
        with self.assertRaises(Invalid): contracts.validate_plan(self.state, tasks)
        tasks[0]['dependsOn'] = ['T01']
        with self.assertRaises(Invalid): contracts.validate_plan(self.state, tasks)

    def test_bounded_reader_guards_before_whole_read_and_reports_resource_refusal(self):
        path = self.fx.inbox / 'oversize.md'
        path.write_bytes(b'x' * 4096)
        before = copy.deepcopy(self.state)
        with patch.dict(os.environ, {'AISPA_MAX_INPUT_BYTES': '64'}):
            with patch.object(Path, 'read_text', side_effect=AssertionError('whole-file read is forbidden')):
                with self.assertRaisesRegex(Invalid, 'RESOURCE_LIMIT.*maxInputBytes.*continu'):
                    self.flow.brief(path)
        self.assertEqual(self.state, before)
        with patch.dict(os.environ, {'AISPA_MAX_INPUT_BYTES': '8192'}):
            self.flow.brief(path)
        self.assertEqual(self.store.text(self.state['briefs'][0]), 'x' * 4096)

    def test_saved_large_reports_have_verified_chunks_and_tamper_invalidates_artifact(self):
        self.store.scale_enabled = True
        with patch.dict(os.environ, {'AISPA_CHUNK_BYTES': '1024'}):
            value = {'checks': [{'id': f'R{i}', 'criterion': 'All exact content ' * 30} for i in range(300)]}
            raw = json.dumps(value).encode()
            ref = self.store.artifact('reviews', raw, '.json')
        saved = json.loads((self.store.run / ref['file']).read_text())
        self.assertEqual(saved.get('aispaManifest'), 1)
        self.assertEqual(json.loads(self.store.text(ref)), value)
        self.assertTrue(contracts.artifact_valid(self.store, ref))
        chunk = self.store.run / Path(ref['file']).parent / saved['chunks'][-1]['file']
        chunk.write_bytes(b'{}')
        self.assertFalse(contracts.artifact_valid(self.store, ref))
        with self.assertRaises(Invalid): self.store.text(ref)

    def test_iterative_dependency_walk_exceeds_python_recursion_depth(self):
        tasks = [{'id': f'T{i:05d}', 'dependsOn': [f'T{i-1:05d}'] if i else []} for i in range(1500)]
        closure = contracts.task_closure({'tasks': tasks}, tasks[-1]['id'])
        self.assertEqual([t['id'] for t in closure], [t['id'] for t in tasks])
        tasks[0]['dependsOn'] = [tasks[-1]['id']]
        with self.assertRaises(Invalid): contracts.task_closure({'tasks': tasks}, tasks[-1]['id'])

    def test_pages_bind_complete_order_and_reject_stale_cursor(self):
        values = [{'id': f'R{i}', 'criteria': [f'Exact {i}']} for i in range(300)]
        seen, cursor = [], None
        while True:
            page = self.scale().page(values, cursor=cursor, limit=17)
            self.assertEqual(page['total'], len(values))
            self.assertEqual(page['digest'], digest(values))
            seen.extend(page['items']); cursor = page['cursor']
            self.assertEqual(page['omitted'], len(values) - len(seen))
            if cursor is None: break
        self.assertEqual(seen, values)
        cursor = self.scale().page(values, limit=17)['cursor']
        with self.assertRaises(Invalid): self.scale().page(values[:-1], cursor=cursor, limit=17)

    def test_explicit_discovery_selection_survives_excerpt_budget_with_addressed_pages(self):
        names = [f'docs/chapter{i:03d}.md' for i in range(60)]
        for i, name in enumerate(names):
            path = self.fx.root / name; path.parent.mkdir(exist_ok=True)
            path.write_text(('Source paragraph ' + str(i) + '.\n') * 500)
        seen, cursor = set(), None
        for _ in range(100):
            collection = discovery.collect(self.store, self.state, paths=names, cursor=cursor)
            seen.update(row['path'] for row in collection['sources'] if row['path'] in names)
            self.assertIn('pagination', collection)
            cursor = collection['pagination']['cursor']
            if cursor is None: break
        else: self.fail('Discovery pagination failed to make progress')
        self.assertEqual(seen, set(names))

    def test_legacy_policy_keeps_original_caps_and_new_init_enables_scaling(self):
        self.setup_scope()
        self.state['policy'] = {}
        with self.assertRaises(Invalid): ideas.record(self.store, self.state, synthetic_report(self.store, self.state))
        cli = Path(__file__).resolve().parents[1] / 'skills/aispa/tools/aispa.py'
        fresh = self.fx.root / 'fresh-project'; fresh.mkdir()
        subprocess.run(['git', 'init', '-q', str(fresh)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(fresh), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.invalid', 'commit', '-qm', 'fixture', '--allow-empty'],
                       check=True, capture_output=True)
        result = subprocess.run([sys.executable, '-B', str(cli), '--root', str(fresh), '--compact',
                                 'init', '--slug', 'scale', '--title', 'Scale policy fixture',
                                 '--mode', 'auto', '--runtime', 'codex', '--check', 'max'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        from aispa_core.store import Journal
        self.assertIs(Journal(fresh).load()['policy'].get('scalableInputs'), True)


if __name__ == '__main__':
    unittest.main()
