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
from aispa_core import briefmap, contracts, design_contract, discovery, ideas, mixed_evidence, packets, planning, test_intent
from aispa_core.store import Invalid, digest
from tests import test_mixed_evidence as mixed_fixture
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

    def setup_scope(self, count=300, source_text=None):
        text = source_text if source_text is not None else '\n'.join(
            f'Build operation {i:04d} with exact outcome {i:04d}.' for i in range(count))
        lines = text.splitlines()
        self.assertEqual(len(lines), count)
        self.flow.brief(self.manifest(text, 'brief-manifest.json', 'text'))
        rows = [{'id': f'R{i:04d}', 'text': f'Operation {i}', 'kind': 'requested',
                 'brief': 'B001', 'quote': lines[i], 'core': True,
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
        self.exercise_large_chain()

    def exercise_large_chain(self, source_text=None):
        """Exercise the complete scale path with optional recovered source text."""
        rows = self.setup_scope(source_text=source_text)
        if source_text is not None:
            self.assertEqual([row['quote'] for row in rows], source_text.splitlines())
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
        final_check = report['checks'].pop()
        with self.assertRaises(Invalid):
            contracts.evidence_rows(self.store, report, expected)
        report['checks'].append(final_check)
        self.assertFalse(contracts.evidence_rows(self.store, report, expected)[1])
        packet = packets.write(self.store, {'kind': 'scale-regression', 'basis': digest(rows), 'requirements': rows, 'report': report})
        self.assertLessEqual(packet['envelopeBytes'], packets.MAX_PACKET_BYTES)
        self.assertGreater(packet['sectionCount'], 0)
        return rows, tasks, report, packet

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

    def test_pack_chunk_override_refuses_before_writing_and_retry_is_possible(self):
        cli = Path(__file__).resolve().parents[1] / 'skills/aispa/tools/aispa.py'
        source = self.fx.inbox / 'pack-source.txt'
        source.write_bytes(b'x' * 65)
        output = self.fx.inbox / 'pack-output.json'
        before = set(self.fx.inbox.iterdir())
        command = [sys.executable, '-B', str(cli), 'pack', '--file', str(source),
                   '--output', str(output), '--kind', 'text', '--chunk-bytes', '128']
        env = dict(os.environ, AISPA_CHUNK_BYTES='64')
        refused = subprocess.run(command, capture_output=True, text=True, env=env)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn('RESOURCE_LIMIT', refused.stderr)
        self.assertIn('chunkBytes', refused.stderr)
        self.assertIn('AISPA_CHUNK_BYTES', refused.stderr)
        self.assertFalse(output.exists(), 'pack must refuse before publishing an unreadable manifest')
        self.assertEqual(set(self.fx.inbox.iterdir()), before, 'refusal must leave no chunks or directories')
        self.assertEqual(source.read_bytes(), b'x' * 65)
        env['AISPA_CHUNK_BYTES'] = '128'
        retried = subprocess.run(command, capture_output=True, text=True, env=env)
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertTrue(json.loads(retried.stdout)['verified'])
        with patch.dict(os.environ, {'AISPA_CHUNK_BYTES': '128'}):
            self.assertEqual(self.scale().read_file(output), source.read_bytes())

    def test_manifest_producer_rejects_chunk_override_above_reader_budget_without_files(self):
        output = self.fx.inbox / 'new-output-directory' / 'manifest.json'
        with patch.dict(os.environ, {'AISPA_CHUNK_BYTES': '64'}):
            with self.assertRaisesRegex(Invalid, 'RESOURCE_LIMIT.*chunkBytes.*AISPA_CHUNK_BYTES'):
                self.scale().write_manifest(output, b'x' * 65, kind='text',
                                            basis=digest({'producer': 'budget'}), chunk_bytes=128)
        self.assertFalse(output.parent.exists())

    def test_long_idea_compact_view_has_honest_summary_omission_and_full_reference(self):
        self.setup_scope(2)
        report = synthetic_report(self.store, self.state)
        report['summary'] = 'Точный вывод 🦊. ' * 720
        ref = ideas.record(self.store, self.state, report)
        before = copy.deepcopy(self.state)
        immutable = (self.store.run / ref['file']).read_bytes()
        view = ideas.view(self.store, self.state)
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False)), ideas.MAX_VIEW_CHARS)
        self.assertTrue(report['summary'].startswith(view['summary']))
        self.assertGreater(view['omitted']['summaryChars'], 0)
        self.assertEqual(view['omitted']['summaryChars'], len(report['summary']) - len(view['summary']))
        self.assertEqual(view['artifact'], ref)
        self.assertEqual(ideas.view(self.store, self.state, compact=False)['report'], report)
        self.assertEqual(ideas._saved_report(self.store, self.state), report)
        self.assertEqual((self.store.run / ref['file']).read_bytes(), immutable)
        self.assertEqual(self.state, before)

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


class ScaleMixedConsumerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = mixed_fixture.MixedEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fx = self.fixture.fx

    def intent(self, *, scalable=True, count=3, field=None, chars=1601):
        self.fx.state['policy']['scalableInputs'] = scalable
        self.fx.store.scale_enabled = scalable
        self.fx.state['tasks'][0]['criteria'] = [f'Exact criterion {i:04d}' for i in range(count)]
        self.fx.store.append(self.fx.state, 'scale-mixed-consumer-fixture')
        report = self.fixture.report(automatic=False)
        if field:
            report['routes'][0][field] = 'r' * chars
        return report

    def saved_consumer(self, intent):
        ref = self.fixture.save(intent)
        self.assertEqual(test_intent._saved(self.fx.store, ref)['report'], intent)
        self.assertTrue(self.fx.flow.verify([sys.executable, '-c', "print('isolated candidate sanity')"], 10, 'T01'))
        self.assertTrue(test_intent.valid(self.fx.store, self.fx.state, 'T01'))
        self.assertTrue(test_intent.history_valid(self.fx.store, self.fx.state, 'T01'))
        rows = mixed_evidence.report_template(self.fx.store, self.fx.state, 'T01')
        self.assertEqual([(r['criterion'], r['kind']) for r in rows],
                         [(r['criterion'], r['kind']) for r in intent['routes']])
        for row in rows:
            row.update(status='not_tested', note='Synthetic consumer check; no acceptance claimed', artifact='')
        report = {'basis': contracts.task_basis(self.fx.store, self.fx.state, 'T01'), 'routeChecks': rows}
        record = {'testIntent': ref, 'report': self.fx.store.artifact('reviews', json.dumps(report).encode(), '.json'),
                  'artifacts': [], 'passed': False}
        before = copy.deepcopy(self.fx.state)
        events = (self.fx.store.run / 'events.jsonl').read_bytes()
        immutable = (self.fx.store.run / ref['file']).read_bytes()
        artifacts, failures = mixed_evidence.check(self.fx.store, self.fx.state, 'T01', report)
        self.assertEqual(artifacts, [])
        self.assertEqual({(r['criterion'], r['kind']) for r in failures},
                         {(r['criterion'], r['kind']) for r in intent['routes']})
        self.assertTrue(mixed_evidence.record_valid(self.fx.store, record),
                        'sealed report must consume the same complete accepted intent')
        self.assertEqual(self.fx.state, before)
        self.assertEqual((self.fx.store.run / 'events.jsonl').read_bytes(), events)
        self.assertEqual((self.fx.store.run / ref['file']).read_bytes(), immutable)
        return ref, report, record

    def test_scalable_saved_mixed_prose_reaches_live_and_sealed_consumers(self):
        for field in ('reason', 'procedure', 'expected'):
            with self.subTest(field=field):
                if field != 'reason':
                    self.doCleanups()
                    self.setUp()
                self.saved_consumer(self.intent(field=field))

    def test_scalable_saved_mixed_route_count_reaches_live_and_sealed_consumers(self):
        self.saved_consumer(self.intent(count=4097))

    def test_legacy_mixed_admission_retains_route_count_and_prose_limits(self):
        reports = [self.intent(scalable=False, field=field) for field in ('reason', 'procedure', 'expected')]
        reports.append(self.intent(scalable=False, count=4097))
        for report in reports:
            # Each report is checked against its own complete task definition.
            self.fx.state['tasks'][0]['criteria'] = report['criteria']
            self.fx.store.append(self.fx.state, 'legacy-mixed-admission-fixture')
            report['basis'] = contracts.task_basis(self.fx.store, self.fx.state, 'T01')
            before = copy.deepcopy(self.fx.state)
            events = (self.fx.store.run / 'events.jsonl').read_bytes()
            with self.subTest(routes=len(report['routes']), reason=len(report['routes'][0]['reason'])):
                with self.assertRaises(Invalid):
                    self.fixture.save(report)
                self.assertEqual(self.fx.state, before)
                self.assertEqual((self.fx.store.run / 'events.jsonl').read_bytes(), events)

    def test_explicit_legacy_history_validation_retains_route_limits(self):
        for count, field in ((3, 'reason'), (4097, None)):
            with self.subTest(count=count, field=field):
                if count != 3:
                    self.doCleanups()
                    self.setUp()
                report = self.intent(count=count, field=field)
                ref = self.fixture.save(report)
                self.assertTrue(test_intent.history_valid(self.fx.store, self.fx.state, 'T01'))
                immutable = (self.fx.store.run / ref['file']).read_bytes()
                # Isolated policy-revalidation fixture; real run policy is never migrated.
                self.fx.state['policy']['scalableInputs'] = False
                self.fx.store.append(self.fx.state, 'explicit-legacy-validation-fixture')
                self.assertFalse(test_intent.valid(self.fx.store, self.fx.state, 'T01', require_green=False))
                self.assertFalse(test_intent.history_valid(self.fx.store, self.fx.state, 'T01'))
                self.assertEqual((self.fx.store.run / ref['file']).read_bytes(), immutable)

    def test_scalable_saved_mixed_consumer_preserves_schema_and_source_guards(self):
        ref, report, record = self.saved_consumer(self.intent(field='reason'))
        for mutate in (lambda r: r['routeChecks'].pop(),
                       lambda r: r['routeChecks'].append(copy.deepcopy(r['routeChecks'][0])),
                       lambda r: r['routeChecks'][0].update(criterion='foreign'),
                       lambda r: r['routeChecks'][0].update(status='unknown'),
                       lambda r: r['routeChecks'][0].update(note='')):
            wrong = copy.deepcopy(report)
            mutate(wrong)
            with self.assertRaises(Invalid):
                mixed_evidence.check(self.fx.store, self.fx.state, 'T01', wrong)
        for mutate in (lambda r: r['routes'].pop(),
                       lambda r: r['routes'].append(copy.deepcopy(r['routes'][0])),
                       lambda r: r['routes'][0].update(criterion='foreign'),
                       lambda r: r['routes'][0].update(kind='unknown'),
                       lambda r: r['routes'][0].update(reason=''),
                       lambda r: r['routes'][0].update(procedure=3),
                       lambda r: r['routes'][0].update(expected=None)):
            contract = test_intent._saved(self.fx.store, ref)
            mutate(contract['report'])
            with self.assertRaises(Invalid):
                mixed_evidence.check_contract(self.fx.store, contract, ref, report)
        (self.fx.store.run / ref['file']).write_bytes(b'{}')
        self.assertFalse(mixed_evidence.record_valid(self.fx.store, record))
        with self.assertRaises(Invalid):
            mixed_evidence.check(self.fx.store, self.fx.state, 'T01', report)


class ScaleDiscoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fx = WorkflowTests('test_empty_run_cannot_finish')
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

    def test_discovery_source_memory_preflight_refuses_before_read_and_retries(self):
        raw = b'Safe source paragraph.\n' * 1000
        name = 'docs/large.md'
        source = self.source(name, raw)
        before = self.snapshot()
        opened = []
        original_open = Path.open

        def track_source(path, *args, **kwargs):
            if path == source:
                opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.dict(os.environ, {'AISPA_MAX_MEMORY_BYTES': '4096'}):
            with self.assertRaisesRegex(Invalid, 'RESOURCE_LIMIT.*maxMemoryBytes'):
                self.fx.flow.read_input(source)
            with patch.object(Path, 'open', track_source):
                with self.assertRaisesRegex(Invalid, 'RESOURCE_LIMIT.*maxMemoryBytes.*continuation'):
                    discovery.collect(self.fx.store, self.fx.state, paths=[name])
        self.assertEqual(opened, [], 'known oversized materialization must refuse before opening the source')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(source.read_bytes(), raw)
        with patch.dict(os.environ, {'AISPA_MAX_MEMORY_BYTES': '1048576'}):
            collected = discovery.collect(self.fx.store, self.fx.state, paths=[name])
        row = next(r for r in collected['sources'] if r['path'] == name)
        self.assertEqual(row['excerpt'].encode(), raw)
        self.assertEqual(row['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(collected['includedPaths'], [name])
        self.assertEqual(row['omittedChars'], 0)


if __name__ == '__main__':
    unittest.main()
