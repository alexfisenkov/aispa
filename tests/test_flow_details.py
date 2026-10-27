"""Flow details (flowDetails): requested buttons, placement, persistence and accepted input.

Everything goes through the engine seams: scenario admission, the final
acceptance report, observation templates and the worker packet. Observations
are synthetic structural fixtures, not a claim that a browser was operated.
"""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/aispa/tools'))
from aispa_core import design_contract as design, final_repairs, packets, product_flows
from aispa_core.contracts import basis
from aispa_core.store import Invalid
import test_design_contract as design_fixture
import test_product_flows as base

DETAILS = {'placement': 'Top toolbar, left of the title', 'persistence': 'Stays visible on every screen',
           'acceptsInput': True,
           'positiveCase': {'input': 'report-2026.csv', 'expected': 'File is accepted and listed'}}


def detailed(flow):
    flow.update(copy.deepcopy(DETAILS))
    return flow


class ScenarioAdmission(unittest.TestCase):
    def setUp(self):
        self.fx = design_fixture.DesignContractTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.fx.state['policy'] = {'criticalProductFlows': True, 'flowDetails': True}

    def report(self, quote='Build export.', interface='web-ui', **extra):
        report = self.fx.registered_feature(quote)
        flow = base.flow_row('R01', 'Export works', interface=interface)
        flow.update(extra)
        report['features'][0]['productFlows'] = base.extension(flow)
        return report

    def rejects(self, report):
        with self.assertRaises(Invalid):
            design.record_scenarios(self.fx.store, self.fx.state, report)

    def test_requested_button_cannot_become_cli_without_owner_decision(self):
        for quote in ('Add an Export button to the page.', 'Нужны кнопки для экспорта данных.',
                      'Сделай меню с экспортом.', 'Нужна экранная клавиатура для ввода.'):
            with self.subTest(quote=quote):
                self.rejects(self.report(quote, interface='cli'))

    def test_negated_button_words_do_not_force_a_ui(self):
        for quote in ('Сделай экспорт без кнопок, только команды.', 'No buttons, commands only.',
                      'Do it without a button.'):
            with self.subTest(quote=quote):
                design.record_scenarios(self.fx.store, self.fx.state, self.report(quote, interface='cli'))

    def test_separate_owner_decision_allows_cli_for_a_button_quote(self):
        decision_text = 'Use a local CLI export instead of the Export button.'
        report = self.report('Add an Export button to the page.', interface='cli')
        self.fx.state['briefs'].append(dict(self.fx.store.artifact('briefs', decision_text.encode(), '.md'), id='B002'))
        report['basis'] = design.scenario_basis(self.fx.state)
        report['features'][0]['productFlows']['flows'][0]['ownerDecision'] = {
            'brief': 'B002', 'quote': decision_text, 'interface': 'cli', 'controls': []}
        design.record_scenarios(self.fx.store, self.fx.state, report)

    def test_button_quote_with_ui_flow_is_fine(self):
        design.record_scenarios(self.fx.store, self.fx.state, self.report('Add an Export button to the page.'))

    def test_without_the_flag_a_button_quote_keeps_the_old_scheme(self):
        self.fx.state['policy'] = {'criticalProductFlows': True}
        design.record_scenarios(self.fx.store, self.fx.state, self.report('Add an Export button to the page.', interface='cli'))
        self.fx.state['policy'] = {'criticalProductFlows': True}
        self.rejects(self.report(placement='Top toolbar'))

    def test_details_are_accepted_only_with_the_flag(self):
        design.record_scenarios(self.fx.store, self.fx.state, self.report(**copy.deepcopy(DETAILS)))
        self.fx.state['policy'] = {'criticalProductFlows': True}
        self.rejects(self.report(**copy.deepcopy(DETAILS)))

    def test_accepting_input_requires_a_positive_case(self):
        self.rejects(self.report(acceptsInput=True))
        self.rejects(self.report(acceptsInput=True, positiveCase={'input': 'x'}))
        self.rejects(self.report(acceptsInput=True, positiveCase={'input': ' ', 'expected': 'accepted'}))
        self.rejects(self.report(acceptsInput=False, positiveCase={'input': 'x', 'expected': 'accepted'}))
        self.rejects(self.report(acceptsInput='yes', positiveCase={'input': 'x', 'expected': 'accepted'}))

    def test_placement_and_persistence_must_be_real_text(self):
        self.rejects(self.report(placement=' '))
        self.rejects(self.report(persistence=5))


class DetailFixture(base.FlowFixture):
    """Acceptance of the R02 button flow that declares placement, persistence and input."""

    def prepare_details(self):
        original = base.flow_row

        def row(rid, criterion, *, interface='web-ui'):
            flow = original(rid, criterion, interface=interface)
            return detailed(flow) if rid == 'R02' else flow
        with patch.object(base, 'flow_row', row):
            self.prepare_flows()
        self.state['policy']['flowDetails'] = True

    def final_with(self, mutate=None, status='passed'):
        report = self.final()
        body, ref = self.observations['R02']
        body['chain']['placement'] = {'expected': DETAILS['placement'], 'observed': 'Toolbar, left of the title',
                                      'status': status}
        body['chain']['persistence'] = {'expected': DETAILS['persistence'], 'observed': 'Seen on all three screens',
                                        'status': 'passed'}
        body['chain']['positiveCase'] = dict(DETAILS['positiveCase'], accepted=True,
                                             observed='report-2026.csv listed', status='passed')
        if mutate:
            mutate(body['chain'])
        (self.store.run / ref['file']).write_text(json.dumps(body), encoding='utf-8')
        return report

    def failures(self):
        return self.state['gates']['acceptance']['failures']


class ObservationConfirmation(DetailFixture):
    def test_observation_confirming_all_details_is_accepted(self):
        self.prepare_details()
        self.assertTrue(self.flow.acceptance(self.file(self.final_with())))

    def test_observation_without_a_declared_detail_is_rejected(self):
        for key in ('placement', 'persistence', 'positiveCase'):
            with self.subTest(missing=key):
                self.tearDown(); self.setUp(); self.prepare_details()
                report = self.final_with(lambda chain, key=key: chain.pop(key))
                with self.assertRaises(Invalid):
                    self.flow.acceptance(self.file(report))

    def test_observation_cannot_confirm_a_different_detail(self):
        mutations = {'placement': lambda c: c['placement'].update(expected='Bottom right corner'),
                     'persistence': lambda c: c['persistence'].update(expected='Only on the first screen'),
                     'input': lambda c: c['positiveCase'].update(input='other.csv'),
                     'expected': lambda c: c['positiveCase'].update(expected='Rejected'),
                     'accepted': lambda c: c['positiveCase'].update(accepted=False)}
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                self.tearDown(); self.setUp(); self.prepare_details()
                with self.assertRaises(Invalid):
                    self.flow.acceptance(self.file(self.final_with(mutate)))

    def test_undeclared_detail_in_an_observation_is_rejected(self):
        self.prepare_details()
        report = self.final_with()
        body, ref = self.observations['R01']
        body['chain']['placement'] = {'expected': 'Toolbar', 'observed': 'Toolbar', 'status': 'passed'}
        (self.store.run / ref['file']).write_text(json.dumps(body), encoding='utf-8')
        with self.assertRaises(Invalid):
            self.flow.acceptance(self.file(report))

    def test_unconfirmed_placement_is_a_flow_failure_with_its_requirement(self):
        self.prepare_details()
        report = self.final_with(status='failed')
        self.assertFalse(self.flow.acceptance(self.file(report)))
        rows = [f for f in self.failures() if f.get('kind') == 'product-flow' and f.get('requirement') == 'R02']
        self.assertEqual([f['status'] for f in rows], ['failed'])
        self.assertEqual(rows[0]['flow'], 'R02-main')

    def test_refused_input_and_unconfirmed_persistence_are_flow_failures(self):
        self.prepare_details()

        def refuse(chain):
            chain['positiveCase'].update(accepted=False, status='failed')
            chain['persistence'].update(status='blocked')
        self.assertFalse(self.flow.acceptance(self.file(self.final_with(refuse))))
        rows = [f for f in self.failures() if f.get('requirement') == 'R02' and f.get('kind') == 'product-flow']
        self.assertEqual(sorted(f['status'] for f in rows), ['blocked', 'failed'])

    def test_new_failure_kind_has_a_requirement_that_a_blind_repair_can_take(self):
        self.prepare_details()
        self.assertFalse(self.flow.acceptance(self.file(self.final_with(status='failed'))))
        self.assertEqual(final_repairs.repairable_items(self.state['gates']['acceptance']), ['R02'])

    def test_run_without_the_flag_keeps_the_old_observation_scheme(self):
        self.prepare_flows()
        self.assertTrue(self.flow.acceptance(self.file(self.final())))
        self.assertTrue(self.state['gates']['acceptance'])
        self.assertFalse(self.state['gates']['acceptance'].get('failures'))


class TemplatesAndWorkerPacket(DetailFixture):
    def test_observation_template_carries_declared_details_unanswered(self):
        self.prepare_details()
        _value, _checks, observations = product_flows.templates(
            self.store, self.state, basis(self.store, self.state, 'acceptance'))
        by_flow = {o['flow']: o['chain'] for o in observations}
        self.assertNotIn('placement', by_flow['R01-main'])
        chain = by_flow['R02-main']
        self.assertEqual(chain['placement'], {'expected': DETAILS['placement'], 'observed': None, 'status': 'not_tested'})
        self.assertEqual(chain['persistence']['expected'], DETAILS['persistence'])
        self.assertEqual(chain['positiveCase'], dict(DETAILS['positiveCase'], accepted=None, observed=None,
                                                    status='not_tested'))

    def worker_flows(self, task):
        packet = packets.build(self.store, self.state, self.flow.pipeline, 'task', task, 'worker')
        return packet.get('productFlows')

    def test_worker_packet_has_only_the_flows_of_its_own_requirements(self):
        self.prepare_details()
        rows = self.worker_flows('T02')
        self.assertEqual([r['requirement'] for r in rows], ['R02'])
        self.assertEqual(rows[0]['flow']['id'], 'R02-main')
        self.assertEqual(rows[0]['flow']['placement'], DETAILS['placement'])
        self.assertEqual(rows[0]['flow']['positiveCase'], DETAILS['positiveCase'])
        self.assertEqual((rows[0]['interface'], rows[0]['controls']), ('web-ui', ['Export']))
        self.assertEqual([r['requirement'] for r in self.worker_flows('T01')], ['R01'])

    def test_worker_packet_has_no_flows_without_the_flag_or_for_a_critic(self):
        self.prepare_details()
        critic = packets.build(self.store, self.state, self.flow.pipeline, 'task', 'T02', 'critic1')
        self.assertNotIn('productFlows', critic)
        self.state['policy'].pop('flowDetails')
        self.assertIsNone(self.worker_flows('T02'))


if __name__ == '__main__':
    unittest.main()
