import copy
import json
import os
import unittest
from unittest.mock import patch

import test_test_intent as fixture
from aispa_core import test_intent
from aispa_core.contracts import task_basis
from aispa_core.store import Invalid, file_hash


class MixedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.TestIntentTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.fx.state['policy']['mixedEvidence'] = True
        self.fx.state['tasks'][0]['criteria'] += ['Browser layout', 'Process handoff']
        self.fx.store.append(self.fx.state, 'mixed-fixture')

    def report(self, automatic=True):
        report = self.fx.behavior_report(self.fx.red()) if automatic else {
            'attempt': 'T01-A01', 'basis': task_basis(self.fx.store, self.fx.state, 'T01'),
            'newTestFiles': [], 'redExecution': None}
        report.update(mode='mixed', criteria=self.fx.state['tasks'][0]['criteria'], routes=[
            {'criterion': c, 'kind': ('automated' if i == 0 and automatic else 'browser' if i < 2 else 'process'),
             'reason': 'Observed at this boundary', 'procedure': 'Inspect expected outcome',
             'expected': 'Criterion satisfied'} for i, c in enumerate(self.fx.state['tasks'][0]['criteria'])])
        return report

    def save(self, report):
        ref = test_intent.record(self.fx.store, self.fx.state, 'T01', report)
        self.fx.store.append(self.fx.state, 'test-intent-recorded:T01')
        return ref

    def test_issue_automated_files_cover_only_automated_criteria(self):
        self.save(self.report())
        self.fx.green()
        self.assertTrue(test_intent.valid(self.fx.store, self.fx.state, 'T01'))

    def test_old_all_criteria_rule_reproduces_issue(self):
        report = self.fx.behavior_report(self.fx.red())
        report['criteria'] = self.fx.state['tasks'][0]['criteria']
        with self.assertRaisesRegex(Invalid, 'часть критериев'):
            self.save(report)

    def test_57_criteria_five_automated_46_browser_six_process(self):
        criteria = ['C%02d' % i for i in range(57)]
        self.fx.state['tasks'][0]['criteria'] = criteria
        self.fx.store.append(self.fx.state, '57-criteria-fixture')
        report = self.report()
        report['routes'] = [dict(criterion=c, kind='automated' if i < 5 else 'browser' if i < 51 else 'process',
            reason='Appropriate boundary', procedure='Execute declared check', expected='Satisfied')
            for i, c in enumerate(criteria)]
        report['newTestFiles'][0]['criteria'] = criteria[:5]
        self.save(report)
        self.fx.green()
        self.assertTrue(test_intent.valid(self.fx.store, self.fx.state, 'T01'))
        summary = test_intent.route_summary(self.fx.store, self.fx.state, 'T01')
        self.assertEqual(summary['counts'], dict(automated=5, browser=46, process=6))
        self.assertEqual(len(summary['routes']), 57)

    def test_no_automated_needs_no_red_but_fresh_task_check(self):
        report = self.report(automatic=False)
        self.save(report)
        self.assertFalse(test_intent.valid(self.fx.store, self.fx.state, 'T01'))
        self.assertTrue(self.fx.flow.verify([__import__('sys').executable, '-c', "print('manual candidate sanity')"], 10, 'T01'))
        self.assertTrue(test_intent.valid(self.fx.store, self.fx.state, 'T01'))
        self.assertIsNone(test_intent._saved(self.fx.store, self.fx.state['taskHistory']['T01'][-1]['testIntent'])['redExecution'])

    def test_missing_duplicate_and_foreign_routes_rejected(self):
        valid = self.report()
        for mutate in (lambda r: r['routes'].pop(), lambda r: r['routes'].append(copy.deepcopy(r['routes'][0])),
                       lambda r: r['routes'][0].update(criterion='foreign'),
                       lambda r: r['routes'][0].update(kind='guess'),
                       lambda r: r['routes'][0].update(reason=''),
                       lambda r: r['newTestFiles'][0].update(criteria=r['criteria'])):
            report = copy.deepcopy(valid)
            mutate(report)
            with self.assertRaises(Invalid):
                self.save(report)

    def test_mixed_requires_opt_in_and_exact_task_criteria(self):
        report = self.report()
        self.fx.state['policy'].pop('mixedEvidence')
        self.fx.store.append(self.fx.state, 'old-policy')
        with self.assertRaises(Invalid):
            self.save(report)
        self.fx.state['policy']['mixedEvidence'] = True
        self.fx.store.append(self.fx.state, 'new-policy')
        report['criteria'] = report['criteria'][:-1]
        with self.assertRaises(Invalid):
            self.save(report)

    def test_first_contract_preserves_started_attempt_cards_actors_history(self):
        attempt = self.fx.state['taskHistory']['T01'][-1]
        attempt['roles'] = {'worker': {'fixture': 'immutable card'}}
        attempt['actors'] = {'worker': 'original actor'}
        self.fx.store.append(self.fx.state, 'assigned-worker')
        before = copy.deepcopy(self.fx.state)
        self.save(self.report())
        after = copy.deepcopy(self.fx.state)
        for key in ('testIntent', 'testIntentHistory', 'verification'):
            after['taskHistory']['T01'][-1].pop(key, None)
            before['taskHistory']['T01'][-1].pop(key, None)
        # RED creates an execution record; it is additive and does not reset the attempt.
        for key in ('executions', 'updatedAt'):
            before.pop(key, None)
            after.pop(key, None)
        self.assertEqual(before, after)

    def test_routes_add_only_before_submit_never_change_remove_or_downgrade(self):
        report = self.report()
        first = self.save(report)
        for mutation in (lambda r: r['routes'].pop(), lambda r: r['routes'][0].update(kind='process'),
                         lambda r: r['routes'][0].update(procedure='weaker procedure'),
                         lambda r: r.update(mode='not_applicable')):
            changed = copy.deepcopy(report)
            mutation(changed)
            with self.assertRaises(Invalid):
                self.save(changed)
        addition = dict(report['routes'][0], kind='process')
        report['routes'].append(addition)
        second = self.save(report)
        self.assertNotEqual(first, second)
        self.fx.store.append(self.fx.state, 'task-submitted:T01')
        report['routes'].append(dict(report['routes'][1], kind='process'))
        with self.assertRaises(Invalid):
            self.save(report)
        report['routes'].pop()
        self.save(report)
        self.fx.state['taskHistory']['T01'][-1]['rounds'].append({'number': 1})
        self.fx.store.append(self.fx.state, 'critic-started:T01')
        with self.assertRaises(Invalid):
            self.save(report)

    def test_behavior_to_mixed_cannot_remove_automation(self):
        red = self.fx.red()
        report = self.fx.behavior_report(red)
        report['criteria'] = self.fx.state['tasks'][0]['criteria']
        report['newTestFiles'][0]['criteria'] = report['criteria']
        self.save(report)
        report['mode'] = 'mixed'
        report['routes'] = [dict(criterion=c, kind='automated', reason='Automated original',
            procedure='Run original test', expected='Satisfied') for c in report['criteria']]
        bad = copy.deepcopy(report)
        bad['routes'][-1]['kind'] = 'browser'
        bad['newTestFiles'][0]['criteria'] = report['criteria'][:-1]
        with self.assertRaises(Invalid):
            self.save(bad)
        self.save(report)
        self.fx.green()
        self.assertTrue(test_intent.valid(self.fx.store, self.fx.state, 'T01'))

    def test_first_contract_after_submit_rejected_and_no_red_without_auto(self):
        report = self.report(automatic=False)
        bad = copy.deepcopy(report)
        bad['redExecution'] = {'fake': True}
        with self.assertRaises(Invalid):
            self.save(bad)
        self.fx.store.append(self.fx.state, 'task-submitted:T01')
        with self.assertRaises(Invalid):
            self.save(report)

    def test_mixed_changed_test_needs_new_red_preserves_initial_provenance(self):
        first_report = self.report()
        first = self.save(first_report)
        self.fx.green()
        self.fx.test_file.write_text('assert VALUE == 2\n', encoding='utf-8')
        changed = copy.deepcopy(first_report)
        changed.update(basis=task_basis(self.fx.store, self.fx.state, 'T01'))
        changed['newTestFiles'][0]['sha256'] = file_hash(self.fx.test_file)
        with self.assertRaises(Invalid):
            self.save(changed)
        second_red = self.fx.red('tests/test_app.py: expected VALUE equals two')
        changed.update(basis=task_basis(self.fx.store, self.fx.state, 'T01'), redExecution=second_red)
        changed['newTestFiles'][0]['expectedFailure'] = 'tests/test_app.py: expected VALUE equals two'
        second = self.save(changed)
        old = test_intent._saved(self.fx.store, first)
        new = test_intent._saved(self.fx.store, second)
        self.assertEqual(old['initialSourceBasis'], new['initialSourceBasis'])
        self.assertNotEqual(old['redExecutionId'], new['redExecutionId'])
        (self.fx.root / 'app.py').write_text('VALUE = 2\n', encoding='utf-8')
        self.assertTrue(self.fx.flow.verify([__import__('sys').executable, '-c', "print('green')"], 10, 'T01'))
        self.assertTrue(test_intent.valid(self.fx.store, self.fx.state, 'T01'))

    def observations(self):
        from aispa_core import mixed_evidence
        self.save(self.report())
        self.fx.green()
        templates = mixed_evidence.observation_templates(self.fx.store, self.fx.state, 'T01')
        evidence = self.fx.store.artifact('evidence', b'fresh automatic output', '.txt')
        rows = mixed_evidence.report_template(self.fx.store, self.fx.state, 'T01')
        observations = {}
        for template in templates:
            kind = template['kind']
            template.update(procedure='Executed declared procedure', observed='Outcome visibly satisfied')
            template['source'] = {'url': 'http://localhost:8000/', 'viewport': '390 x 844'} if kind == 'browser' else {
                'reference': 'README.md#handoff', 'scope': 'Current candidate documented install procedure'}
            attachment = self.fx.store.artifact('evidence', b'observed screenshot or transcript', '.txt')
            template['attachments'] = [attachment]
            ref = self.fx.store.artifact('evidence', json.dumps(template).encode(), '.json')
            observations[kind] = (template, ref, attachment)
        for row in rows:
            row.update(status='passed', note='Independently checked observation', artifact=(evidence['file']
                if row['kind'] == 'automated' else observations[row['kind']][1]['file']))
        report = {'basis': task_basis(self.fx.store, self.fx.state, 'T01'), 'routeChecks': rows,
                  'checks': [{'requirement': 'T01', 'criterion': c, 'status': 'passed'}
                             for c in self.fx.state['tasks'][0]['criteria']]}
        return mixed_evidence, report, observations

    def test_receipts_and_attachments_bound_and_sealed(self):
        module, report, observations = self.observations()
        artifacts, failures = module.check(self.fx.store, self.fx.state, 'T01', report)
        self.assertFalse(failures)
        for _, ref, attachment in observations.values():
            self.assertIn(ref, artifacts)
            self.assertIn(attachment, artifacts)
        attempt = self.fx.state['taskHistory']['T01'][-1]
        record = {'testIntent': attempt['testIntent'], 'report': self.fx.store.artifact('reviews', json.dumps(report).encode(), '.json'),
                  'artifacts': artifacts, 'passed': True}
        self.assertTrue(module.record_valid(self.fx.store, record))
        attachment = observations['browser'][2]
        (self.fx.store.run / attachment['file']).write_text('tampered', encoding='utf-8')
        self.assertFalse(module.record_valid(self.fx.store, record))
        with self.assertRaises(Invalid):
            module.check(self.fx.store, self.fx.state, 'T01', report)

    def test_receipts_reject_foreign_binding_and_malformed_observations(self):
        module, report, observations = self.observations()
        original, ref, _ = observations['browser']
        path = self.fx.store.run / ref['file']
        for key, wrong in [('run', 'other'), ('task', 'T02'), ('attempt', 'T01-A00'), ('candidate', 'old'),
                           ('kind', 'process'), ('intentSha256', '0' * 64), ('criteria', ['foreign']),
                           ('criteria', ['Browser layout', 'Browser layout']), ('procedure', ''), ('observed', ''),
                           ('source', {'url': 'missing protocol', 'viewport': '390 x 844'}),
                           ('attachments', [{'file': 'evidence/missing', 'sha256': '0' * 64}])]:
            with self.subTest(key=key, wrong=wrong):
                changed = copy.deepcopy(original)
                changed[key] = wrong
                path.write_text(json.dumps(changed), encoding='utf-8')
                with self.assertRaises(Invalid):
                    module.check(self.fx.store, self.fx.state, 'T01', report)
        path.write_text('not JSON', encoding='utf-8')
        with self.assertRaises(Invalid):
            module.check(self.fx.store, self.fx.state, 'T01', report)
        path.unlink()
        with self.assertRaises(Invalid):
            module.check(self.fx.store, self.fx.state, 'T01', report)

    def test_route_reports_exact_and_no_common_pass_if_route_unchecked(self):
        module, report, _ = self.observations()
        for mutate in (lambda r: r.pop('routeChecks'), lambda r: r['routeChecks'].pop(),
                       lambda r: r['routeChecks'].append(copy.deepcopy(r['routeChecks'][0])),
                       lambda r: r['routeChecks'][1].update(status='not_tested')):
            wrong = copy.deepcopy(report)
            mutate(wrong)
            with self.assertRaises(Invalid):
                module.check(self.fx.store, self.fx.state, 'T01', wrong)
        report['routeChecks'][1].update(status='failed', note='Observed a failure', artifact=None)
        report['checks'][1]['status'] = 'failed'
        _, failures = module.check(self.fx.store, self.fx.state, 'T01', report)
        self.assertEqual(failures[0]['kind'], 'browser')

    def test_observation_templates_are_not_evidence_and_share_same_kind_only(self):
        from aispa_core import mixed_evidence
        report = self.report(automatic=False)
        self.save(report)
        self.assertTrue(self.fx.flow.verify([__import__('sys').executable, '-c', "print('sanity')"], 10, 'T01'))
        templates = mixed_evidence.observation_templates(self.fx.store, self.fx.state, 'T01')
        self.assertEqual(templates[0]['criteria'], ['VALUE equals one', 'Browser layout'])
        rows = mixed_evidence.report_template(self.fx.store, self.fx.state, 'T01')
        self.assertTrue(all(r['status'] == 'not_tested' and r['artifact'] == '' for r in rows))
        self.assertTrue(all(t['observed'] is None and t['procedure'] is None and not t['attachments'] for t in templates))
        for template in templates:
            template.update(procedure='Observed both criteria', observed='All declared criteria satisfied',
                source={'url': 'http://localhost/', 'viewport': '390 x 844'} if template['kind'] == 'browser' else {
                    'reference': 'README', 'scope': 'handoff'})
            ref = self.fx.store.artifact('evidence', json.dumps(template).encode(), '.json')
            for row in rows:
                if row['kind'] == template['kind']:
                    row.update(status='passed', note='Checked', artifact=ref['file'])
        _, failures = mixed_evidence.check(self.fx.store, self.fx.state, 'T01', {'basis': task_basis(self.fx.store, self.fx.state, 'T01'), 'routeChecks': rows})
        self.assertFalse(failures)

    def test_failure_boundaries_typed_manual_cases_are_not_na(self):
        from aispa_core import failure_checks
        self.fx.state['policy']['errorBoundaryChecks'] = True
        self.fx.store.append(self.fx.state, 'failure-policy')
        report = self.report()
        source = {'path': 'README.md', 'sha256': file_hash(self.fx.root / 'README.md')}
        report['failureChecks'] = {'cases': [dict(id='F'+str(i), boundary=boundary, scenario='Observe failure boundary',
            expected='Failure explained', criterion='Process handoff', kind='process', source=source)
            for i, boundary in enumerate(sorted(failure_checks.BOUNDARIES))], 'notApplicable': []}
        self.save(report)
        self.fx.green()
        self.assertTrue(test_intent.valid(self.fx.store, self.fx.state, 'T01'))
        wrong = copy.deepcopy(report)
        wrong['failureChecks']['cases'][0]['kind'] = 'browser'
        with self.assertRaises(Invalid):
            self.save(wrong)
        wrong = copy.deepcopy(report)
        wrong['failureChecks']['cases'][0].update(kind='automated', criterion='VALUE equals one')
        wrong['failureChecks']['cases'][0]['test'] = wrong['failureChecks']['cases'][0].pop('source')
        with self.assertRaises(Invalid):
            self.save(wrong)

    def test_real_submit_nonpass_repair_critic2_acceptor_and_whole_brief(self):
        import sys
        import test_workflow as workflow_fixture
        from aispa_core import executions, mixed_evidence
        from aispa_core.contracts import basis
        from aispa_core.team import direct_proof_current, historical_proof_valid
        fx = workflow_fixture.WorkflowTests()
        fx.setUp()
        self.addCleanup(fx.tearDown)
        fx.state['policy'] = {'testIntent': True, 'mixedEvidence': True}
        fx.prepare()
        fx.flow.plan(fx.file([{'id': 'T01', 'title': 'Command', 'requirements': ['R01'], 'dependsOn': [],
                              'zones': ['app.py', 'tests'], 'criteria': ['Выводит ready']}]))
        (fx.root / 'app.py').write_text("print('booting')\n", encoding='utf-8')
        (fx.root / 'tests').mkdir()
        test = fx.root / 'tests/test_ready.py'
        test.write_text("import subprocess,sys\nr=subprocess.check_output([sys.executable,'app.py'],text=True)\nassert r.strip()=='ready', 'tests/test_ready.py: expected ready'\n", encoding='utf-8')
        fx.flow.start('T01', basis(fx.store, fx.state, 'task', 'T01'))
        for role in ('worker', 'critic1', 'critic2', 'acceptor'):
            fx.flow.pipeline.assign('T01', role, 'fixture', 'mixed-'+role, 'session')
        self.assertFalse(fx.flow.verify([sys.executable, 'tests/test_ready.py'], 10, 'T01'))
        red = executions.record(fx.store, fx.state, kind='task', purpose='check', task_id='T01')
        fx.store.append(fx.state, 'execution-recorded')
        intent = {'mode': 'mixed', 'attempt': fx.attempt()['id'], 'basis': task_basis(fx.store, fx.state, 'T01'),
            'criteria': ['Выводит ready'], 'routes': [dict(criterion='Выводит ready', kind=k,
                reason='Required observation boundary', procedure='Inspect exact output', expected='ready')
                for k in ('automated', 'browser', 'process')], 'redExecution': red,
            'newTestFiles': [{'path': 'tests/test_ready.py', 'sha256': file_hash(test),
                             'expectedFailure': 'tests/test_ready.py: expected ready', 'criteria': ['Выводит ready']}]}
        test_intent.record(fx.store, fx.state, 'T01', intent)
        fx.store.append(fx.state, 'test-intent-recorded:T01')
        original = copy.deepcopy(fx.attempt())
        (fx.root / 'app.py').write_text("print('ready')\n", encoding='utf-8')
        self.assertTrue(fx.flow.verify([sys.executable, 'tests/test_ready.py'], 10, 'T01'))

        def route_rows():
            rows = mixed_evidence.report_template(fx.store, fx.state, 'T01')
            for row in rows:
                row.update(status='passed', note='Synthetic independent route observation')
                row['artifact'] = fx.attempt()['verification']['output']['file']
            for template in mixed_evidence.observation_templates(fx.store, fx.state, 'T01'):
                template.update(procedure='Checked current command surface', observed='ready observed')
                template['source'] = {'url': 'http://localhost:8000/', 'viewport': '390 x 844'} if template['kind']=='browser' else {
                    'reference': 'app.py', 'scope': 'Current command invocation and handoff'}
                ref = fx.store.artifact('evidence', json.dumps(template).encode(), '.json')
                for row in rows:
                    if row['kind'] == template['kind']:
                        row['artifact'] = ref['file']
            return rows

        def task_report(role, rows=None, failed=False, **extra):
            path = fx.task_report('T01', role, routeChecks=rows if rows is not None else route_rows(), **extra)
            if failed:
                value = json.loads(path.read_text(encoding='utf-8'))
                for row in value['checks']:
                    if row['requirement']=='T01' and row['criterion']=='Выводит ready':
                        row['status']='failed'
                        row['artifact']=None
                path.write_text(json.dumps(value), encoding='utf-8')
            return path

        rows = route_rows()
        with self.assertRaises(Invalid):
            fx.flow.pipeline.submit('T01', task_report('worker', rows=rows[:-1]))
        fx.flow.pipeline.submit('T01', task_report('worker', rows=rows))
        failed_rows = copy.deepcopy(rows)
        failed_rows[1].update(status='failed', note='Browser boundary fails', artifact=None)
        self.assertFalse(fx.flow.pipeline.review('T01', 1, task_report('critic1', rows=failed_rows, failed=True,
            findings=[{'id': 'M1', 'severity': 'blocking', 'text': 'Repair browser boundary', 'criterion': 'Выводит ready'}])))
        first = copy.deepcopy(fx.attempt()['rounds'][0])
        with self.assertRaises(Invalid):
            test_intent.record(fx.store, fx.state, 'T01', dict(intent, basis=task_basis(fx.store, fx.state, 'T01')))
        (fx.root / 'app.py').write_text("# repaired browser wrapper\nprint('ready')\n", encoding='utf-8')
        self.assertTrue(fx.flow.verify([sys.executable, 'tests/test_ready.py'], 10, 'T01'))
        with self.assertRaises(Invalid):
            fx.flow.pipeline.repair('T01', task_report('worker', rows=rows,
                previousReport=first['report']['sha256'], addresses=['M1']))
        fx.flow.pipeline.repair('T01', task_report('worker', previousReport=first['report']['sha256'], addresses=['M1']))
        self.assertTrue(fx.flow.pipeline.review('T01', 2, task_report('critic2', findings=[],
            previousReport=first['report']['sha256'], repairReceipt=fx.attempt()['repair']['report']['sha256'],
            resolutions=[{'id': 'M1', 'status': 'resolved', 'note': 'All routes rechecked',
                          'artifact': fx.attempt()['verification']['output']['file']}])))
        trace = [{'requirement': r['id'], 'brief': r['brief'], 'quote': r['quote'], 'result': 'aligned',
                  'note': 'Matched exact original brief', 'artifact': fx.attempt()['verification']['output']['file']}
                 for r in fx.flow.pipeline.trace_requirements('T01')]
        last = fx.attempt()['rounds'][-1]
        accept_extra = dict(trace=trace, inheritedProofs=fx.flow.pipeline.inherited_proofs('T01'), previousReport=last['report']['sha256'])
        with self.assertRaises(Invalid):
            fx.flow.complete_task('T01', task_report('acceptor', rows=route_rows()[:-1], **accept_extra))
        self.assertTrue(fx.flow.complete_task('T01', task_report('acceptor', **accept_extra)))
        self.assertTrue(direct_proof_current(fx.store, fx.state, 'T01'))
        self.assertEqual(original['id'], fx.attempt()['id'])
        self.assertEqual(original['actors'], fx.attempt()['actors'])
        self.assertEqual(original['roles'], fx.attempt()['roles'])
        self.assertEqual(first, fx.attempt()['rounds'][0])
        self.assertEqual(len(fx.attempt()['rounds']), 2)
        with self.assertRaises(Invalid):
            fx.flow.pipeline.reset_guard('T01')
        fx.verify()
        self.assertTrue(fx.flow.wave(1, fx.file(fx.assessment('wave'))))
        incomplete = fx.assessment()
        incomplete['checks'] = []
        with self.assertRaises(Invalid):
            fx.flow.acceptance(fx.file(incomplete))
        self.assertTrue(fx.flow.acceptance(fx.file(fx.assessment())))
        accepted_report = json.loads(fx.store.text(fx.attempt()['acceptance']['report']))
        receipt_path = fx.store.run / accepted_report['routeChecks'][1]['artifact']
        receipt_path.write_text('{}', encoding='utf-8')
        self.assertFalse(historical_proof_valid(fx.store, fx.state, 'T01'))
        self.assertFalse(direct_proof_current(fx.store, fx.state, 'T01'))


    def test_explicit_mixed_contract_binds_fast_without_changing_legacy_fast(self):
        attempt = self.fx.state['taskHistory']['T01'][-1]
        attempt['tier'] = 'fast'
        self.fx.store.append(self.fx.state, 'fast-fixture')
        self.assertFalse(test_intent.required(self.fx.state, attempt))
        self.save(self.report())
        self.assertTrue(test_intent.required(self.fx.state, attempt))
        self.assertFalse(test_intent.valid(self.fx.store, self.fx.state, 'T01'))
        self.fx.green()
        self.assertTrue(test_intent.valid(self.fx.store, self.fx.state, 'T01'))
        self.fx.test_file.write_text('assert VALUE == 99\n', encoding='utf-8')
        self.assertFalse(test_intent.valid(self.fx.store, self.fx.state, 'T01'))
        legacy = copy.deepcopy(self.fx.state)
        legacy['policy'].pop('mixedEvidence')
        self.assertFalse(test_intent.required(legacy, attempt))

    def test_safe_summary_rejects_foreign_contract(self):
        self.save(self.report())
        original = self.fx.state['taskHistory']['T01'][-1]['testIntent']
        body = test_intent._saved(self.fx.store, original)
        for field, wrong in [('run', 'foreign'), ('task', 'T02'), ('attempt', 'T01-A00'), ('taskContract', 'foreign')]:
            changed = copy.deepcopy(body)
            changed[field] = wrong
            self.fx.state['taskHistory']['T01'][-1]['testIntent'] = self.fx.store.artifact('test-intent', json.dumps(changed).encode(), '.json')
            self.assertEqual(test_intent.route_summary(self.fx.store, self.fx.state, 'T01')['routes'], [])
        self.fx.state['taskHistory']['T01'][-1]['testIntent'] = original

    def test_legacy_exception_between_contracts_cannot_erase_automation(self):
        red = self.fx.red()
        behavior = self.fx.behavior_report(red)
        behavior['criteria'] = self.fx.state['tasks'][0]['criteria']
        behavior['newTestFiles'][0]['criteria'] = behavior['criteria']
        self.save(behavior)
        na = {'mode': 'not_applicable', 'attempt': 'T01-A01', 'basis': task_basis(self.fx.store, self.fx.state, 'T01'),
              'criteria': behavior['criteria'], 'notApplicable': [dict(criterion=c, purpose='existing_behavior',
                reason='Existing behavior independently observed', source={'path': 'README.md',
                'sha256': file_hash(self.fx.root / 'README.md')}) for c in behavior['criteria']]}
        self.save(na)
        with self.assertRaises(Invalid):
            self.save(self.report(automatic=False))

    def test_current_intent_sha_invalidates_previous_receipt_even_same_candidate(self):
        module, report, observations = self.observations()
        attempt = self.fx.state['taskHistory']['T01'][-1]
        intent = copy.deepcopy(test_intent._saved(self.fx.store, attempt['testIntent'])['report'])
        intent['basis'] = task_basis(self.fx.store, self.fx.state, 'T01')
        self.save(intent)
        self.assertEqual(report['basis'], task_basis(self.fx.store, self.fx.state, 'T01'))
        with self.assertRaisesRegex(Invalid, 'контракт'):
            module.check(self.fx.store, self.fx.state, 'T01', report)

    def test_sealed_observation_cannot_be_replaced_with_rehashed_unstored_json(self):
        module, report, observations = self.observations()
        artifacts, _ = module.check(self.fx.store, self.fx.state, 'T01', report)
        attempt = self.fx.state['taskHistory']['T01'][-1]
        record = {'testIntent': attempt['testIntent'], 'report': self.fx.store.artifact('reviews', json.dumps(report).encode(), '.json'),
                  'artifacts': artifacts, 'passed': True}
        body, ref, _ = observations['browser']
        body['observed'] = 'Replaced observation claims another outcome'
        (self.fx.store.run / ref['file']).write_text(json.dumps(body), encoding='utf-8')
        self.assertFalse(module.record_valid(self.fx.store, record))


class AttachmentReplayRegressions(unittest.TestCase):
    """Supplemental T03 repair cases; fixtures are synthetic engine checks."""

    def mixed_fixture(self):
        fx = MixedEvidenceTests(); fx.setUp()
        self.addCleanup(fx.doCleanups)
        module, report, observations = fx.observations()
        return fx.fx, module, report, observations

    def flow_fixture(self):
        # Lazy import: test_product_flows imports this module for mixed fixtures.
        from test_product_flows import FlowFixture
        fx = FlowFixture(); fx.setUp()
        self.addCleanup(fx.tearDown)
        fx.prepare_flows()
        fx.state['policy']['scalableInputs'] = True
        fx.store.append(fx.state, 'supplemental-scalable-flow-fixture')
        return fx

    def test_legacy_literal_attachment_preserves_sealed_replay(self):
        from aispa_core.dashboard_reader import SnapshotJournal
        fx, module, report, observations = self.mixed_fixture()
        body, ref, _ = observations['browser']
        for suffix in ('.txt', '.md', '.json'):
            with self.subTest(suffix=suffix):
                literal = fx.store.artifact('evidence', b'{"aispaManifest":1,"ordinaryLegacyEvidence":true}', suffix)
                body['attachments'] = [literal]
                (fx.store.run / ref['file']).write_text(json.dumps(body), encoding='utf-8')
                # Seal the literal/hash-only legacy interpretation without
                # importing historical code or rewriting a canonical report.
                refs = [{'file': p.relative_to(fx.store.run).as_posix(), 'sha256': file_hash(p)}
                        for p in (fx.store.run / 'evidence').rglob('*') if p.is_file()]
                record = {'testIntent': fx.state['taskHistory']['T01'][-1]['testIntent'],
                          'report': fx.store.artifact('reviews', json.dumps(report).encode(), '.json'),
                          'artifacts': refs, 'passed': True}
                self.assertTrue(module.record_valid(fx.store, record),
                                'Legacy opaque attachment marker must not invalidate an admitted sealed proof')
                self.assertFalse(module.check(fx.store, fx.state, 'T01', report)[1])
                snapshot = SnapshotJournal(fx.root); snapshot.load()
                with snapshot._reads.scope():
                    self.assertTrue(module.record_valid(snapshot, record))
                    snapshot.finish_snapshot()

    def test_scalable_log_chunk_loss_revokes_fresh_scoped_gate_and_finish(self):
        from aispa_core import product_flows, scale
        from aispa_core.contracts import gate_valid
        from aispa_core.dashboard_reader import SnapshotJournal
        from aispa_core.display import project
        for suffix in ('.log', '.txt'):
            with self.subTest(suffix=suffix):
                fx = self.flow_fixture(); report = fx.final()
                body, ref = fx.observations['R02']
                target = fx.store.run / ('evidence/actual-process-evidence' + suffix)
                manifest = scale.write_manifest(target, b'Actual disposable process output for this flow\n',
                    kind='text', basis=report['basis'], chunk_bytes=16)
                body['attachments'] = [{'file': target.relative_to(fx.store.run).as_posix(),
                                        'sha256': file_hash(target)}]
                (fx.store.run / ref['file']).write_text(json.dumps(body), encoding='utf-8')
                self.assertTrue(fx.flow.acceptance(fx.file(report)))
                self.assertTrue(gate_valid(fx.store, fx.state, 'acceptance'))
                self.assertTrue(project(fx.store, fx.state)['productFlows']['ready'])
                chunk = target.parent / manifest['chunks'][0]['file']; original = chunk.read_bytes()
                journal = file_hash(fx.store.run / 'events.jsonl')
                snapshot = SnapshotJournal(fx.root); state = snapshot.load()
                with snapshot._reads.scope():
                    self.assertTrue(gate_valid(snapshot, state, 'acceptance'))
                    chunk.unlink()
                    with self.assertRaises(Invalid, msg='Consumed attachment chunks belong to the snapshot integrity set'):
                        snapshot.finish_snapshot()
                self.assertFalse(gate_valid(fx.store, fx.state, 'acceptance'),
                                 'Missing scalable .log payload must revoke the final gate')
                self.assertFalse(gate_valid(fx.store, fx.state, 'acceptance', settled=True))
                self.assertFalse(product_flows.valid(fx.store, fx.state))
                self.assertFalse(project(fx.store, fx.state)['productFlows']['ready'])
                fresh = SnapshotJournal(fx.root); fresh_state = fresh.load()
                with fresh._reads.scope():
                    self.assertFalse(gate_valid(fresh, fresh_state, 'acceptance'))
                    self.assertFalse(project(fresh, fresh_state)['productFlows']['ready'])
                with self.assertRaises(Invalid):
                    fx.flow.finish()
                self.assertEqual(file_hash(fx.store.run / 'events.jsonl'), journal)
                self.assertEqual(fx.state['status'], 'running')
                chunk.write_bytes(original)
                self.assertTrue(gate_valid(fx.store, fx.state, 'acceptance'))
                restored = SnapshotJournal(fx.root); restored_state = restored.load()
                with restored._reads.scope():
                    self.assertTrue(gate_valid(restored, restored_state, 'acceptance'))
                    restored.finish_snapshot()

    def test_observation_specific_budget_preserves_resource_refusal_through_readiness(self):
        from aispa_core import product_flows
        from aispa_core.contracts import gate_current, gate_valid
        from aispa_core.dashboard_reader import SnapshotJournal
        from aispa_core.display import project
        from aispa_core.verified_reads import ResourceLimit
        fx, module, task_report, observations = self.mixed_fixture()
        fx.state['policy']['scalableInputs'] = True
        fx.store.append(fx.state, 'supplemental-observation-budget-fixture')
        body, ref, _ = observations['browser']
        body['observed'] = 'Actual intact observation, beyond the operation budget. ' * 2200
        path = fx.store.run / ref['file']; path.write_text(json.dumps(body), encoding='utf-8')
        intent = fx.state['taskHistory']['T01'][-1]['testIntent']
        self.assertLess((fx.store.run / intent['file']).stat().st_size, 50000)
        self.assertLess(len(json.dumps(task_report).encode()), 50000)
        self.assertGreater(path.stat().st_size, 50000)
        artifacts, failures = module.check(fx.store, fx.state, 'T01', task_report)
        self.assertFalse(failures)
        record = {'testIntent': intent, 'report': fx.store.artifact('reviews', json.dumps(task_report).encode(), '.json'),
                  'artifacts': artifacts, 'passed': True}
        with patch.dict(os.environ, AISPA_MAX_INPUT_BYTES='50000'):
            for operation in (lambda: module.read_observation(fx.store, ref['file'], scalable=True),
                              lambda: module.check(fx.store, fx.state, 'T01', task_report),
                              lambda: module.record_valid(fx.store, record)):
                with self.subTest(scope='task', operation=operation):
                    with self.assertRaises(ResourceLimit) as caught:
                        operation()
                    for text in ('RESOURCE_LIMIT', 'maxInputBytes', str(path), 'continuation:'):
                        self.assertIn(text, str(caught.exception))
        final = self.flow_fixture(); report = final.final()
        body, ref = final.observations['R02']
        body['observed'] = 'Actual intact final observation, beyond the operation budget. ' * 2200
        path = final.store.run / ref['file']; path.write_text(json.dumps(body), encoding='utf-8')
        self.assertLess(len(json.dumps(report).encode()), 50000)
        self.assertLess((final.store.run / final.state['featureScenarios']['file']).stat().st_size, 50000)
        self.assertTrue(final.flow.acceptance(final.file(report)))
        original = path.read_bytes(); journal = file_hash(final.store.run / 'events.jsonl')
        with patch.dict(os.environ, AISPA_MAX_INPUT_BYTES='50000'):
            for operation in (lambda: product_flows.saved_check(final.store, final.state),
                              lambda: product_flows.valid(final.store, final.state),
                              lambda: gate_valid(final.store, final.state, 'acceptance'),
                              lambda: gate_current(final.store, final.state, 'acceptance'),
                              lambda: product_flows.view(final.store, final.state),
                              lambda: project(final.store, final.state),
                              final.flow.finish):
                with self.subTest(scope='final', operation=operation):
                    with self.assertRaises(ResourceLimit) as caught:
                        operation()
                    for text in ('RESOURCE_LIMIT', 'maxInputBytes', str(path), 'continuation:'):
                        self.assertIn(text, str(caught.exception))
            snapshot = SnapshotJournal(final.root); state = snapshot.load()
            with snapshot._reads.scope():
                with self.assertRaises(ResourceLimit):
                    project(snapshot, state)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(file_hash(final.store.run / 'events.jsonl'), journal)
        self.assertEqual(final.state['status'], 'running')
        self.assertTrue(gate_valid(final.store, final.state, 'acceptance'))
