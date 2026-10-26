"""Critical-flow guards over real engine entrypoints; observations here are synthetic.

The loopback fixture at the bottom is for a separate real browser observation.
Passing these deterministic tests does not claim that a browser was operated.
"""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/aispa/tools'))
from aispa_core import (blind, check_modes, cleanroom, design_contract as design,
                        mixed_evidence, packets, preparation, scale)
from aispa_core.contracts import basis, gate_valid
from aispa_core.display import project
from aispa_core.store import Invalid, digest, file_hash
import test_design_contract as design_fixture
import test_mixed_evidence as mixed_fixture
import test_workflow as legacy


def flow_row(rid, criterion, *, interface='web-ui'):
    return {'id': rid + '-main', 'criterion': criterion, 'role': 'owner',
            'interface': interface, 'controls': ['Export'] if interface != 'cli' else [],
            'action': 'Activate Export' if interface != 'cli' else 'Run app.py',
            'result': criterion, 'forbidden': ([{'role': 'guest', 'action': 'Export private CSV'}]
                                               if interface != 'cli' else []),
            'accessReason': 'Guest export must be denied.' if interface != 'cli' else
                            'A local command has no separate authenticated roles.',
            'ownerDecision': None}


def extension(flow):
    return {'applicability': 'applicable', 'reason': 'This is the requested primary user action.',
            'flows': [flow]}


def observation(store, state, rid, flow, *, attachments=None):
    """Synthetic final-scope observation; never a task/attempt masquerading as final."""
    decision = flow['ownerDecision']
    surface = decision['interface'] if decision else flow['interface']
    controls = decision['controls'] if decision else flow['controls']
    return {'schema': 2, 'scope': 'final-product-flow', 'run': state['run'],
            'candidate': basis(store, state, 'acceptance'),
            'contractSha256': digest({'run': state['run'], 'scenarios': state['featureScenarios']}),
            'requirement': rid, 'criterion': flow['criterion'], 'flow': flow['id'],
            'kind': 'browser' if surface == 'web-ui' else 'process', 'interface': surface,
            'procedure': 'Synthetic deterministic fixture executes the declared chain.',
            'observed': 'Synthetic result for structural validation only.',
            'source': ({'url': 'http://127.0.0.1:8000/', 'viewport': '900 x 700'} if surface == 'web-ui'
                       else {'reference': 'app.py', 'scope': surface}),
            'chain': {'role': flow['role'],
                      'controls': [{'name': name, 'visible': True, 'status': 'passed'} for name in controls],
                      'action': {'name': flow['action'], 'executed': True, 'status': 'passed'},
                      'result': {'expected': flow['result'], 'observed': 'Exact expected synthetic outcome.',
                                 'status': 'passed'},
                      'forbidden': [dict(item, attempted=True, denied=True, status='passed')
                                    for item in flow['forbidden']]},
            'attachments': attachments or []}


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.fx = design_fixture.DesignContractTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.fx.state['policy'] = {'criticalProductFlows': True}

    def report(self):
        report = self.fx.feature()
        report['features'][0]['productFlows'] = extension(flow_row('R01', 'Export works'))
        return report

    def test_new_policy_requires_explicit_primary_flow_applicability(self):
        with self.assertRaises(Invalid):
            design.record_scenarios(self.fx.store, self.fx.state, self.fx.feature())

    def test_valid_extension_preserves_all_eight_dimensions_and_history(self):
        report = self.report()
        ref = design.record_scenarios(self.fx.store, self.fx.state, report)
        self.assertEqual(len(self.fx.store.json(ref)['report']['features'][0]['dimensions']), 8)
        self.assertTrue(design.valid_scenarios(self.fx.store, self.fx.state))
        self.assertEqual(design.record_scenarios(self.fx.store, self.fx.state, report), ref)
        self.assertEqual(len(self.fx.state['featureScenarioHistory']), 1)

    def test_template_carries_unanswered_applicability_not_an_invented_pass(self):
        row = preparation.template(self.fx.store, self.fx.state, 'scenarios')['features'][0]
        self.assertIn('productFlows', row)
        self.assertIsNone(row['productFlows']['applicability'])
        self.assertEqual(row['productFlows']['flows'], [])

    def test_flow_schema_exact_identity_criterion_controls_and_owner_decision(self):
        valid = self.report()
        mutations = [lambda p: p['flows'][0].update(criterion='No disclosure'),
                     lambda p: p['flows'].append(copy.deepcopy(p['flows'][0])),
                     lambda p: p.update(applicability='not_applicable'),
                     lambda p: p.update(reason=''),
                     lambda p: p['flows'][0].update(controls=[]),
                     lambda p: p['flows'][0].update(forbidden=[], accessReason=''),
                     lambda p: p['flows'][0].update(critical=False),
                     lambda p: p['flows'][0].update(ownerDecision={'brief': 'B001', 'quote': 'Invented',
                                                                 'interface': 'cli', 'controls': []})]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad = copy.deepcopy(valid)
                mutate(bad['features'][0]['productFlows'])
                with self.assertRaises(Invalid):
                    design.record_scenarios(self.fx.store, self.fx.state, bad)

    def test_legacy_schema_stays_exact_and_does_not_silently_enable_policy(self):
        self.fx.state['policy'] = {}
        design.record_scenarios(self.fx.store, self.fx.state, self.fx.feature())
        with self.assertRaises(Invalid):
            design.record_scenarios(self.fx.store, self.fx.state, self.report())

    def test_separate_current_owner_decision_preserves_original_requested_button(self):
        self.fx.state['briefs'].append(dict(self.fx.store.artifact('briefs',
            b'Use a local CLI export instead of the Export button.', '.md'), id='B002'))
        report = self.report(); report['basis'] = design.scenario_basis(self.fx.state)
        report['features'][0]['productFlows']['flows'][0]['ownerDecision'] = {
            'brief': 'B002', 'quote': 'Use a local CLI export instead of the Export button.',
            'interface': 'cli', 'controls': []}
        ref = design.record_scenarios(self.fx.store, self.fx.state, report)
        flow = self.fx.store.json(ref)['report']['features'][0]['productFlows']['flows'][0]
        self.assertEqual((flow['interface'], flow['controls']), ('web-ui', ['Export']))
        self.assertEqual(flow['ownerDecision']['interface'], 'cli')

    def test_nonapplicable_requires_reason_and_no_silently_discarded_flows(self):
        report = self.report()
        report['features'][0]['productFlows'] = {'applicability': 'not_applicable',
            'reason': 'A documentation-only function with no executable interaction; reviewer must verify source.', 'flows': []}
        design.record_scenarios(self.fx.store, self.fx.state, report)
        report['features'][0]['productFlows']['reason'] = ' '
        with self.assertRaises(Invalid):
            design.record_scenarios(self.fx.store, self.fx.state, report)

    def test_duplicate_flow_identity_across_different_requirements_is_rejected(self):
        self.fx.state['requirements'].append({'id': 'A01', 'kind': 'addition', 'scope': 'active',
            'parent': 'R01', 'text': 'Preview', 'acceptance': ['Preview works']})
        report = self.report(); report['basis'] = design.scenario_basis(self.fx.state)
        addition = copy.deepcopy(report['features'][0])
        addition.update(requirement='A01', changeKind='addition')
        addition['productFlows']['flows'][0]['criterion'] = 'Preview works'
        report['features'].append(addition)
        with self.assertRaises(Invalid):
            design.record_scenarios(self.fx.store, self.fx.state, report)


class FlowFixture(unittest.TestCase):
    setUp = legacy.WorkflowTests.setUp
    tearDown = legacy.WorkflowTests.tearDown
    git = legacy.WorkflowTests.git
    commit = legacy.WorkflowTests.commit
    file = legacy.WorkflowTests.file
    header = legacy.WorkflowTests.header
    coverage = legacy.WorkflowTests.coverage
    verify = legacy.WorkflowTests.verify
    attempt = legacy.WorkflowTests.attempt
    task_report = legacy.WorkflowTests.task_report
    completed = legacy.WorkflowTests.completed
    assessment = legacy.WorkflowTests.assessment

    def prepare_flows(self, mode='balanced'):
        quote = 'Owner clicks Export to get CSV; guest export is denied.'
        self.flow.brief(self.file('Команда выводит ready.\n' + quote, 'brief.md'))
        self.flow.requirements(self.file([
            {'id': 'R01', 'text': 'Команда', 'brief': 'B001', 'quote': 'Команда выводит ready',
             'core': True, 'acceptance': ['Выводит ready']},
            {'id': 'R02', 'text': 'Export button', 'brief': 'B001', 'quote': quote,
             'core': False, 'acceptance': ['Создаёт CSV']}]))
        self.flow.seal()
        self.flow.spec(self.file('Команда выводит ready. Экспорт создаёт CSV.', 'spec.md'))
        self.coverage()
        self.flow.plan(self.file([
            {'id': 'T01', 'title': 'Command', 'requirements': ['R01'], 'dependsOn': [],
             'zones': ['app.py'], 'criteria': ['Выводит ready']},
            {'id': 'T02', 'title': 'Button', 'requirements': ['R02'], 'dependsOn': [],
             'zones': ['export.py'], 'criteria': ['Создаёт CSV']}]))
        (self.root / 'export.py').write_text("VALUE = 'synthetic-fixture'\n", encoding='utf-8')
        self.commit()
        self.state['policy'] = {'criticalProductFlows': True}
        check_modes.initialize(self.state, mode)
        features = []
        for req in self.state['requirements']:
            rid = req['id']
            features.append({'requirement': rid, 'changeKind': 'requested', 'sourceQuote': req['quote'],
                'specQuote': 'Команда выводит ready' if rid == 'R01' else 'Экспорт создаёт CSV',
                'dimensions': [{'name': name, 'status': 'covered', 'scenario': 'Exercise ' + name,
                                'verification': 'Synthetic exact guard assertion.',
                                'counterexample': 'Wrong outcome must fail.'} for name in design.DIMENSIONS],
                'productFlows': extension(flow_row(rid, req['acceptance'][0],
                                                  interface='cli' if rid == 'R01' else 'web-ui'))})
        report = {'basis': design.scenario_basis(self.state), 'depth': 'balanced',
                  'depthReason': 'Synthetic consumer fixture with the complete requested chain.', 'features': features}
        # Seed the immutable storage seam to isolate final consumers from the
        # separately tested scenario-admission validator on an unchanged engine.
        design._save(self.store, self.state, report, 'featureScenarios', 'featureScenarioHistory', 'feature-scenarios')
        self.flow.save('synthetic-flow-contract')
        self.completed('T01')
        self.completed('T02')
        self.flow.wave(1, self.file(self.assessment('wave')))
        return report

    def declared(self):
        return self.store.json(self.state['featureScenarios'])['report']['features']

    def final(self, status='passed'):
        report = self.assessment()
        report['productFlowReview'] = {'status': 'passed',
            'note': 'Synthetic source-first review confirms applicability, complete controls and owner decisions.'}
        self.observations = {}
        for check in report['checks']:
            feature = next(f for f in self.declared() if f['requirement'] == check['requirement'])
            flow = feature['productFlows']['flows'][0]
            body = observation(self.store, self.state, check['requirement'], flow)
            ref = self.store.artifact('evidence', json.dumps(body).encode(), '.json')
            self.observations[check['requirement']] = (body, ref)
            value = status if check['requirement'] == 'R02' else 'passed'
            check['flowChecks'] = [{'flow': flow['id'], 'status': value,
                                   'note': 'Synthetic flow outcome.', 'artifact': ref['file'] if value == 'passed' else ''}]
            if value != 'passed':
                check['status'] = value
        return report

    def record_blind(self, status='passed'):
        self.state['policy']['cleanClone'] = True
        self.state['cleanroom'] = cleanroom.prepare(self.store, self.state)
        self.state['cleanrun'] = cleanroom.execute(self.store, self.state, [sys.executable, 'app.py'], 10)
        observations = [{'brief': 'B001', 'quote': r['quote'], 'status': status if r['id'] == 'R02' else 'passed',
                         'note': 'Synthetic source-first original observation.',
                         'artifact': self.state['cleanrun']['output']['file']}
                        for r in self.state['requirements']]
        blind.record(self.store, self.state, {'basis': blind.basis(self.store, self.state),
            'reviewer': 'synthetic-reviewer', 'independent': True, 'observations': observations})

    def bind_blind(self, report):
        report.update(cleanrun=self.state['cleanrun']['artifact']['sha256'],
                      blindReport=self.state['blindReview']['report']['sha256'],
                      blindMapping=[{'observation': i, 'requirement': r['id']}
                                    for i, r in enumerate(self.state['requirements'])])
        return report


class ProductFlowTests(FlowFixture):
    def test_missing_noncore_flow_vetoes_acceptance_and_settled_finish_in_every_mode(self):
        for mode in ('fast', 'balanced', 'max'):
            with self.subTest(mode=mode):
                if self.state['tasks']:
                    self.tearDown(); self.setUp()
                self.prepare_flows(mode)
                report = self.final()
                report['checks'][1].pop('flowChecks')
                self.assertFalse(self.flow.acceptance(self.file(report)),
                                 'Mandatory noncore product flow missing from final report still permits readiness in all check modes')
                self.assertFalse(gate_valid(self.store, self.state, 'acceptance', settled=True))
                with self.assertRaises(Invalid):
                    self.flow.finish()

    def test_nonpassed_noncore_flow_statuses_are_saved_and_hard_block_readiness(self):
        for mode in ('fast', 'balanced', 'max'):
            if self.state['tasks']:
                self.tearDown(); self.setUp()
            self.prepare_flows(mode)
            for status in ('failed', 'blocked', 'not_tested'):
                with self.subTest(mode=mode, status=status):
                    report = self.final(status)
                    self.assertFalse(self.flow.acceptance(self.file(report)))
                    self.assertEqual(self.state['gates']['acceptance']['failures'][-1]['status'], status)
                    self.assertFalse(gate_valid(self.store, self.state, 'acceptance', settled=True))

    def test_good_typed_flow_passes_and_same_size_mtime_tamper_revokes_it(self):
        self.prepare_flows()
        report = self.final()
        self.assertTrue(self.flow.acceptance(self.file(report)))
        self.assertTrue(gate_valid(self.store, self.state, 'acceptance'))
        body, ref = self.observations['R02']
        path = self.store.run / ref['file']; before = path.stat()
        raw = path.read_bytes(); path.write_bytes(raw.replace(b'Exact expected', b'Wrong expected'))
        self.assertEqual(path.stat().st_size, before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        for mode in ('fast', 'balanced', 'max'):
            check_modes.initialize(self.state, mode)
            self.assertFalse(gate_valid(self.store, self.state, 'acceptance'))
            self.assertFalse(gate_valid(self.store, self.state, 'acceptance', settled=True))

    def test_typed_chain_and_final_binding_reject_wrong_scope_and_unexecuted_ui(self):
        self.prepare_flows()
        mutations = [lambda b: b.update(run='foreign'), lambda b: b.update(candidate='0' * 64),
                     lambda b: b.update(contractSha256='0' * 64), lambda b: b.update(scope='task'),
                     lambda b: b.update(schema=1, task='T02', attempt='fake'),
                     lambda b: b.update(kind='process', interface='cli', source={'reference': 'export.py', 'scope': 'cli'}),
                     lambda b: b['chain']['controls'][0].update(visible=False),
                     lambda b: b['chain']['action'].update(executed=False),
                     lambda b: b['chain'].update(role='guest'),
                     lambda b: b['chain'].update(forbidden=[]),
                     lambda b: b['chain']['forbidden'][0].update(denied=False)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                report = self.final(); body, ref = self.observations['R02']; mutate(body)
                (self.store.run / ref['file']).write_text(json.dumps(body), encoding='utf-8')
                with self.assertRaises(Invalid):
                    self.flow.acceptance(self.file(report))

    def test_flow_checks_reject_duplicate_foreign_or_cross_criterion_ids(self):
        self.prepare_flows()
        for mutation in (lambda rows: rows.append(copy.deepcopy(rows[0])),
                         lambda rows: rows[0].update(flow='unknown'),
                         lambda rows: rows[0].update(flow='R01-main')):
            report = self.final(); mutation(report['checks'][1]['flowChecks'])
            with self.assertRaises(Invalid):
                self.flow.acceptance(self.file(report))

    def test_saved_report_and_final_candidate_contract_change_cannot_keep_pass(self):
        self.prepare_flows('fast'); report = self.final()
        self.assertTrue(self.flow.acceptance(self.file(report)))
        gate = self.state['gates']['acceptance']; ref = gate['report']
        original = (self.store.run / ref['file']).read_bytes()
        (self.store.run / ref['file']).write_bytes(original + b' ')
        self.assertFalse(gate_valid(self.store, self.state, 'acceptance', settled=True))
        (self.store.run / ref['file']).write_bytes(original)
        self.state['requirements'][1]['acceptance'].append('New exact condition')
        self.assertFalse(gate_valid(self.store, self.state, 'acceptance', settled=True))

    def test_deferred_settled_flag_cannot_override_missing_critical_flow(self):
        self.prepare_flows('max'); report = self.final('not_tested')
        self.flow.acceptance(self.file(report))
        self.state['gates']['acceptance']['deferredSettled'] = True
        self.assertFalse(gate_valid(self.store, self.state, 'acceptance', settled=True))

    def test_original_noncore_blind_failure_survives_later_positive_flow(self):
        self.prepare_flows('fast'); self.record_blind('failed')
        report = self.bind_blind(self.final())
        self.assertFalse(self.flow.acceptance(self.file(report)))
        self.assertFalse(gate_valid(self.store, self.state, 'acceptance', settled=True))

    def test_packets_keep_source_first_boundary_then_supply_blank_final_templates(self):
        self.prepare_flows()
        before = packets.build(self.store, self.state, self.flow.pipeline, 'blind')
        self.assertNotIn('productFlows', before)
        self.assertNotIn('featureScenarios', before)
        with self.assertRaises(Invalid):
            packets.build(self.store, self.state, self.flow.pipeline, 'acceptance')
        self.record_blind()
        packet = packets.build(self.store, self.state, self.flow.pipeline, 'acceptance')
        self.assertIn('productFlows', packet)
        self.assertEqual(len(packet['productFlowObservationTemplates']), 2)
        self.assertTrue(all(t['schema'] == 2 and t['scope'] == 'final-product-flow'
                            and t['observed'] is None for t in packet['productFlowObservationTemplates']))
        self.assertTrue(all(c['status'] == 'not_tested' and not c['artifact']
                            for c in packet['productFlowCheckTemplates']))

    def test_projection_explains_flow_veto_even_with_all_tasks_completed(self):
        self.prepare_flows('fast'); report = self.final('not_tested')
        self.flow.acceptance(self.file(report))
        view = project(self.store, self.state)
        self.assertTrue(all(t['status'] == 'completed' for t in view['tasks']))
        self.assertFalse(view['gates']['acceptance'])
        self.assertIn('productFlows', view)
        self.assertFalse(view['productFlows']['ready'])
        self.assertTrue(view['productFlows']['failures'])

    def test_final_scalable_observation_manifest_and_attachment_chunks_replay(self):
        self.prepare_flows()
        self.state['policy']['scalableInputs'] = True
        self.store.append(self.state, 'scalable-final-flow-fixture')
        report = self.final(); body, ref = self.observations['R02']
        body['attachments'] = [self.store.artifact('evidence', ('screen-%d' % i).encode(), '.png') for i in range(40)]
        target = self.store.run / ref['file']; target.unlink()
        manifest = scale.write_manifest(target, json.dumps(body).encode(), kind='json',
                                        basis=report['basis'], chunk_bytes=256)
        self.assertTrue(self.flow.acceptance(self.file(report)))
        self.assertTrue(gate_valid(self.store, self.state, 'acceptance'))
        chunk = target.parent / manifest['chunks'][-1]['file']; chunk.unlink()
        self.assertFalse(gate_valid(self.store, self.state, 'acceptance'))

    def test_source_reconciliation_is_required_even_when_declared_flow_is_positive(self):
        self.prepare_flows('fast'); report = self.final()
        report.pop('productFlowReview')
        self.assertFalse(self.flow.acceptance(self.file(report)))
        self.assertFalse(gate_valid(self.store, self.state, 'acceptance', settled=True))


class ObservationTransportTests(unittest.TestCase):
    def setUp(self):
        self.fx = mixed_fixture.MixedEvidenceTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def test_scalable_task_observation_manifest_and_over_32_attachments_replay(self):
        module, report, observations = self.fx.observations()
        fx = self.fx.fx
        fx.state['policy']['scalableInputs'] = True
        fx.store.append(fx.state, 'scalable-observation-fixture')
        original, ref, _ = observations['browser']
        original['attachments'] = [fx.store.artifact('evidence', ('attachment-%d' % i).encode(), '.txt')
                                   for i in range(40)]
        target = fx.store.run / ref['file']; target.unlink()
        manifest = scale.write_manifest(target, json.dumps(original).encode(), kind='json',
                                        basis=report['basis'], chunk_bytes=256)
        artifacts, failures = module.check(fx.store, fx.state, 'T01', report)
        self.assertFalse(failures)
        attempt = fx.state['taskHistory']['T01'][-1]
        record = {'testIntent': attempt['testIntent'], 'report': fx.store.artifact('reviews', json.dumps(report).encode(), '.json'),
                  'artifacts': artifacts, 'passed': True}
        self.assertTrue(module.record_valid(fx.store, record))
        chunk = target.parent / manifest['chunks'][-1]['file']; chunk.write_bytes(chunk.read_bytes() + b' ')
        self.assertFalse(module.record_valid(fx.store, record))

    def test_legacy_task_observation_keeps_attachment_cap_and_rejects_manifest(self):
        module, report, observations = self.fx.observations()
        fx = self.fx.fx; body, ref, _ = observations['browser']
        body['attachments'] = [fx.store.artifact('evidence', ('old-%d' % i).encode(), '.txt') for i in range(33)]
        target = fx.store.run / ref['file']; target.write_text(json.dumps(body), encoding='utf-8')
        with self.assertRaises(Invalid):
            module.check(fx.store, fx.state, 'T01', report)
        body['attachments'] = []; target.unlink()
        scale.write_manifest(target, json.dumps(body).encode(), kind='json', basis=report['basis'], chunk_bytes=256)
        with self.assertRaises(Invalid):
            module.check(fx.store, fx.state, 'T01', report)

    def test_scalable_observation_resource_refusal_preserves_input_and_journal(self):
        module, report, observations = self.fx.observations(); fx = self.fx.fx
        fx.state['policy']['scalableInputs'] = True
        fx.store.append(fx.state, 'scalable-budget-fixture')
        before = (fx.store.run / 'events.jsonl').read_bytes()
        path = fx.store.run / observations['browser'][1]['file']; raw = path.read_bytes()
        with patch.dict(os.environ, AISPA_MAX_INPUT_BYTES='32'):
            with self.assertRaisesRegex(Invalid, 'RESOURCE_LIMIT'):
                module.check(fx.store, fx.state, 'T01', report)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual((fx.store.run / 'events.jsonl').read_bytes(), before)


class NewInitTests(unittest.TestCase):
    def test_only_new_cli_init_enables_critical_product_flows(self):
        import test_cli
        from aispa_core.store import Journal
        fx = test_cli.CliTests(); fx.setUp()
        self.addCleanup(fx.tearDown)
        fx.init()
        self.assertIs(Journal(fx.root).load()['policy'].get('criticalProductFlows'), True)


UI_FIXTURE_HTML = '''<!doctype html><html lang="en"><meta charset="utf-8">
<title>AISpa critical flow fixture</title><style>body{font:20px system-ui;max-width:760px;margin:60px auto;padding:20px}button,select{font:inherit;padding:12px;margin:10px}output{display:block;white-space:pre-wrap;margin:20px}</style>
<h1>Private CSV export</h1><p>Isolated role-dispatch fixture, not production authentication.</p>
<label>Role <select id="role"><option value="owner">Owner</option><option value="guest">Guest</option></select></label>
<button id="export">Export</button><output id="result" aria-live="polite">Ready for an observed action.</output>
<script>document.querySelector('#export').onclick=async()=>{const r=await fetch('/export?role='+document.querySelector('#role').value,{method:'POST'});document.querySelector('#result').textContent=await r.text();};</script></html>'''


def ui_fixture_server():
    """Actual localhost button and denied role, for independent browser evidence.

    This function records no observations and awards no engine PASS by itself.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body, content_type='text/plain; charset=utf-8'):
            raw = body.encode(); self.send_response(status)
            self.send_header('Content-Type', content_type); self.send_header('Content-Length', str(len(raw)))
            self.end_headers(); self.wfile.write(raw)

        def do_GET(self):
            self.reply(200, UI_FIXTURE_HTML, 'text/html; charset=utf-8') if self.path == '/' else self.reply(404, 'Not found')

        def do_POST(self):
            path = urlsplit(self.path)
            if path.path != '/export':
                self.reply(404, 'Not found'); return
            role = parse_qs(path.query).get('role', ['guest'])[0]
            self.reply(200, 'CSV exported: private.csv\nowner,value\nowner,42') if role == 'owner' else self.reply(403, 'Denied: guest cannot export private CSV')

        def log_message(self, *args):
            pass
    return ThreadingHTTPServer(('127.0.0.1', 0), Handler)


if __name__ == '__main__':
    if '--serve-fixture' in sys.argv:
        server = ui_fixture_server()
        print('http://127.0.0.1:%d/' % server.server_port, flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    else:
        unittest.main()
