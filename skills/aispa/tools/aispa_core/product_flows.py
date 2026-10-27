"""Source-bound primary product flows, derived from featureScenarios.

All declared flows are mandatory regardless of task/core/check mode. Structural
checks establish provenance and recorded execution; independent source-first
review still decides applicability, completeness and truth of observations.
"""
import copy
import re

from .contracts import active, artifact_valid, require
from .store import Invalid, digest
from .verified_reads import ResourceLimit
from . import mixed_evidence, scale

INTERFACES = frozenset({'web-ui', 'native-ui', 'cli'})
STATUSES = mixed_evidence.STATUSES
FLOW_FIELDS = frozenset({'id', 'criterion', 'role', 'interface', 'controls',
                         'action', 'result', 'forbidden', 'accessReason', 'ownerDecision'})
DETAIL_FIELDS = frozenset({'placement', 'persistence', 'acceptsInput', 'positiveCase'})
# Lexical, not semantic: a word the owner wrote verbatim, unless negated just before it.
BUTTON_WORDS = re.compile(r'\b(?:кнопк\w*|button\w*|меню|menu\w*|клавиатур\w*)', re.IGNORECASE)
NEGATION_WORDS = frozenset({'без', 'не', 'нет', 'никаких', 'никакой', 'no', 'not', 'without', 'never', 'neither'})
NEGATION_WINDOW = 3


def enabled(state):
    return state.get('policy', {}).get('criticalProductFlows') is True


def details_enabled(state):
    return enabled(state) and state.get('policy', {}).get('flowDetails') is True


def requests_buttons(quote):
    """True when the owner's quote names buttons, a menu or a keyboard without a negation right before."""
    for match in BUTTON_WORDS.finditer(quote):
        before = re.findall(r'\w+', quote[:match.start()].casefold())[-NEGATION_WINDOW:]
        if not NEGATION_WORDS & set(before):
            return True
    return False


def declared_details(flow):
    """Detail fields a flow declares, which an observation must then confirm."""
    return [key for key in ('placement', 'persistence') if key in flow] + \
           (['positiveCase'] if flow.get('acceptsInput') is True else [])


def _text(value, state):
    mixed_evidence._text(value, scalable=scale.enabled(state))


def _controls(value, interface, state):
    require(isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)
            and len(value) == len(set(value)), 'Product flows: неверные или повторные элементы управления')
    require(interface == 'cli' or bool(value), 'Product flows: UI требует запрошенные видимые элементы')
    for item in value:
        _text(item, state)


def effective(flow):
    decision = flow['ownerDecision']
    return (decision['interface'], decision['controls']) if decision else (flow['interface'], flow['controls'])


def _details(flow, state):
    for key in ('placement', 'persistence'):
        if key in flow:
            _text(flow[key], state)
    accepts = flow.get('acceptsInput', False)
    require(type(accepts) is bool, 'Product flows: acceptsInput — true или false')
    case = flow.get('positiveCase')
    if accepts:
        require(isinstance(case, dict) and set(case) == {'input', 'expected'},
                'Product flows: потоку с вводом нужен positiveCase с input и expected')
        _text(case['input'], state); _text(case['expected'], state)
    else:
        require('positiveCase' not in flow, 'Product flows: positiveCase нужен только потоку с вводом (acceptsInput: true)')


def validate_features(store, state, rows, requirements, sources):
    """Admission extension; no semantic guessing from a regex of owner prose."""
    seen = set()
    for feature in rows:
        declaration = feature.get('productFlows')
        require(isinstance(declaration, dict) and set(declaration) == {'applicability', 'reason', 'flows'},
                'Product flows: нужна явная применимость основных пользовательских действий')
        require(declaration['applicability'] in ('applicable', 'not_applicable'),
                'Product flows: применимость не проверена')
        _text(declaration['reason'], state)
        flows = declaration['flows']
        require(isinstance(flows, list) and bool(flows) == (declaration['applicability'] == 'applicable'),
                'Product flows: применимой функции нужен основной поток; неприменимой — только причина')
        req = requirements[feature['requirement']]
        for flow in flows:
            details = details_enabled(state)
            require(isinstance(flow, dict) and FLOW_FIELDS <= set(flow)
                    and set(flow) <= (FLOW_FIELDS | DETAIL_FIELDS if details else FLOW_FIELDS),
                    'Product flows: нужны роль, запрошенный UI, действие, результат и запреты доступа')
            fid = flow['id']
            require(isinstance(fid, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', fid)
                    and fid not in seen, 'Product flows: неизвестный формат или повторный ID потока')
            seen.add(fid)
            require(isinstance(flow['criterion'], str) and flow['criterion'] in req['acceptance'],
                    'Product flows: критерий принадлежит другому требованию')
            require(isinstance(flow['interface'], str) and flow['interface'] in INTERFACES,
                    'Product flows: интерфейс web-ui, native-ui или cli')
            _controls(flow['controls'], flow['interface'], state)
            for key in ('role', 'action', 'result', 'accessReason'):
                _text(flow[key], state)
            if details:
                _details(flow, state)
                require(flow['interface'] != 'cli' or flow['ownerDecision'] is not None
                        or not requests_buttons(feature['sourceQuote']),
                        'Product flows: владелец просил кнопки, меню или клавиатуру; поток нельзя заменить '
                        'командной строкой без отдельного решения владельца')
            forbidden = flow['forbidden']
            require(isinstance(forbidden, list), 'Product flows: нужны запрещённые действия ролей')
            pairs = set()
            for item in forbidden:
                require(isinstance(item, dict) and set(item) == {'role', 'action'},
                        'Product flows: запрету нужны роль и действие')
                _text(item['role'], state); _text(item['action'], state)
                pair = item['role'], item['action']
                require(pair not in pairs, 'Product flows: повторный запрет доступа')
                pairs.add(pair)
            decision = flow['ownerDecision']
            if decision is not None:
                require(isinstance(decision, dict) and set(decision) == {'brief', 'quote', 'interface', 'controls'},
                        'Product flows: замена UI требует отдельного решения владельца')
                require(isinstance(decision['brief'], str) and decision['brief'] in sources
                        and isinstance(decision['quote'], str) and bool(decision['quote'].strip())
                        and decision['quote'] in sources[decision['brief']]
                        and decision['quote'] != feature['sourceQuote'],
                        'Product flows: решение владельца не связано с отдельной исходной цитатой')
                require(isinstance(decision['interface'], str) and decision['interface'] in INTERFACES,
                        'Product flows: неверный интерфейс решения владельца')
                _controls(decision['controls'], decision['interface'], state)


def contract(store, state):
    from . import design_contract
    report = design_contract._saved(store, state, 'featureScenarios', 'featureScenarioHistory', 'feature-scenarios')
    saved = state['featureScenarios']
    design_contract._features(store, state, report, saved.get('checkMode'))
    return {'sha256': digest({'run': state['run'], 'scenarios': saved}), 'scenarios': copy.deepcopy(saved),
            'features': [{k: copy.deepcopy(f[k]) for k in ('requirement', 'sourceQuote', 'productFlows')}
                         for f in report['features']]}


def _flows(value):
    return [(feature['requirement'], flow) for feature in value['features']
            for flow in feature['productFlows']['flows']]


def templates(store, state, candidate):
    value = contract(store, state)
    checks, observations = [], []
    details = details_enabled(state)
    for rid, flow in _flows(value):
        interface, controls = effective(flow)
        kind = 'browser' if interface == 'web-ui' else 'process'
        checks.append({'requirement': rid, 'criterion': flow['criterion'], 'flow': flow['id'],
                       'status': 'not_tested', 'note': '', 'artifact': ''})
        observations.append({'schema': 2, 'scope': 'final-product-flow', 'run': state['run'],
            'candidate': candidate, 'contractSha256': value['sha256'], 'requirement': rid,
            'criterion': flow['criterion'], 'flow': flow['id'], 'kind': kind, 'interface': interface,
            'procedure': None, 'observed': None,
            'source': {'url': None, 'viewport': None} if kind == 'browser' else {'reference': None, 'scope': None},
            'chain': {'role': flow['role'], 'controls': [{'name': c, 'visible': None, 'status': 'not_tested'} for c in controls],
                      'action': {'name': flow['action'], 'executed': None, 'status': 'not_tested'},
                      'result': {'expected': flow['result'], 'observed': None, 'status': 'not_tested'},
                      'forbidden': [dict(item, attempted=None, denied=None, status='not_tested')
                                    for item in flow['forbidden']]}, 'attachments': []})
        if details:
            observations[-1]['chain'].update(_detail_templates(flow))
    return value, checks, observations


def _detail_templates(flow):
    rows = {key: {'expected': flow[key], 'observed': None, 'status': 'not_tested'}
            for key in ('placement', 'persistence') if key in flow}
    if flow.get('acceptsInput') is True:
        rows['positiveCase'] = {'input': flow['positiveCase']['input'], 'expected': flow['positiveCase']['expected'],
                                'accepted': None, 'observed': None, 'status': 'not_tested'}
    return rows


def worker_flows(store, state, requirement_ids):
    """Contract of the buttons and flows for the worker's own requirements, nothing else."""
    if not details_enabled(state) or not state.get('featureScenarios'):
        return None
    from . import design_contract
    report = design_contract._saved(store, state, 'featureScenarios', 'featureScenarioHistory', 'feature-scenarios')
    wanted = set(requirement_ids)
    rows = []
    for feature in report['features']:
        if feature['requirement'] not in wanted:
            continue
        for flow in feature.get('productFlows', {}).get('flows', []):
            interface, controls = effective(flow)
            rows.append({'requirement': feature['requirement'], 'interface': interface,
                         'controls': copy.deepcopy(controls), 'flow': copy.deepcopy(flow)})
    return rows


def _observation(store, state, name, value, rid, flow, candidate):
    scalable = scale.enabled(state)
    body, observation_ref = mixed_evidence.read_observation(store, name, scalable=scalable)
    fields = {'schema', 'scope', 'run', 'candidate', 'contractSha256', 'requirement', 'criterion', 'flow',
              'kind', 'interface', 'procedure', 'observed', 'source', 'chain', 'attachments'}
    require(isinstance(body, dict) and set(body) == fields and type(body['schema']) is int
            and body['schema'] == 2 and body['scope'] == 'final-product-flow',
            'Product flows: нужно отдельное финальное наблюдение schema 2, не свидетельство задачи')
    interface, controls = effective(flow)
    kind = 'browser' if interface == 'web-ui' else 'process'
    require(body['run'] == state['run'] and body['candidate'] == candidate
            and body['contractSha256'] == value['sha256'] and body['requirement'] == rid
            and body['criterion'] == flow['criterion'] and body['flow'] == flow['id']
            and body['kind'] == kind and body['interface'] == interface,
            'Product flows: чужой кандидат/контракт/поток или команда вместо запрошенного UI')
    _text(body['procedure'], state); _text(body['observed'], state)
    mixed_evidence.source_valid(body['source'], kind, scalable=scalable)
    chain = body['chain']
    extras = declared_details(flow) if details_enabled(state) else []
    require(isinstance(chain, dict) and set(chain) == {'role', 'controls', 'action', 'result', 'forbidden', *extras}
            and chain['role'] == flow['role'], 'Product flows: исполненная роль не совпадает с запросом')
    recorded = chain['controls']
    require(isinstance(recorded, list) and len(recorded) == len(controls),
            'Product flows: неполная проверка запрошенных элементов')
    seen = set()
    for row in recorded:
        require(isinstance(row, dict) and set(row) == {'name', 'visible', 'status'}
                and isinstance(row['name'], str) and row['name'] in controls and row['name'] not in seen
                and row['visible'] is True and row['status'] == 'passed',
                'Product flows: элемент не виден или не проверен')
        seen.add(row['name'])
    action, result = chain['action'], chain['result']
    require(isinstance(action, dict) and set(action) == {'name', 'executed', 'status'}
            and action['name'] == flow['action'] and action['executed'] is True and action['status'] == 'passed',
            'Product flows: запрошенное действие не исполнено')
    require(isinstance(result, dict) and set(result) == {'expected', 'observed', 'status'}
            and result['expected'] == flow['result'] and result['status'] == 'passed',
            'Product flows: результат не подтверждён')
    _text(result['observed'], state)
    forbidden = chain['forbidden']
    expected = {(x['role'], x['action']) for x in flow['forbidden']}
    require(isinstance(forbidden, list) and len(forbidden) == len(expected),
            'Product flows: неполная проверка запрещённых ролей')
    seen = set()
    for row in forbidden:
        require(isinstance(row, dict) and set(row) == {'role', 'action', 'attempted', 'denied', 'status'}
                and isinstance(row['role'], str) and isinstance(row['action'], str),
                'Product flows: неверное наблюдение запрета')
        pair = row['role'], row['action']
        require(pair in expected and pair not in seen and row['attempted'] is True
                and row['denied'] is True and row['status'] == 'passed',
                'Product flows: запрещённое действие не проверено или не запрещено')
        seen.add(pair)
    failures = _detail_failures(state, flow, chain, rid)
    return mixed_evidence.attachment_refs(store, body['attachments'], observation_ref, scalable=scalable), failures


def _detail_failures(state, flow, chain, rid):
    """Confirm declared placement, persistence and accepted input; a non-passed claim is a flow failure."""
    failures = []
    for key in declared_details(flow) if details_enabled(state) else ():
        row = chain[key]
        if key == 'positiveCase':
            case = flow['positiveCase']
            require(isinstance(row, dict) and set(row) == {'input', 'expected', 'accepted', 'observed', 'status'}
                    and row['input'] == case['input'] and row['expected'] == case['expected'],
                    'Product flows: наблюдение проверяет не тот корректный ввод, что заявлен в потоке')
            accepted = row['accepted']
            require(type(accepted) is bool and isinstance(row['status'], str) and row['status'] in STATUSES
                    and (row['status'] != 'passed' or accepted is True),
                    'Product flows: корректный ввод не подтверждён принятым')
            note = 'Корректный ввод не принят или не проверен: ' + case['input']
        else:
            require(isinstance(row, dict) and set(row) == {'expected', 'observed', 'status'}
                    and row['expected'] == flow[key]
                    and isinstance(row['status'], str) and row['status'] in STATUSES,
                    'Product flows: наблюдение не подтверждает заявленное ' + key)
            note = {'placement': 'Расположение элемента не подтверждено: ', 'persistence': 'Постоянство элемента не подтверждено: '}[key] + flow[key]
        if row['status'] == 'passed':
            _text(row['observed'], state)
        else:
            failures.append({'requirement': rid, 'criterion': flow['criterion'], 'flow': flow['id'],
                             'kind': 'product-flow', 'status': row['status'], 'detail': key, 'note': note})
    return failures


def check(store, state, report, source_failures=()):
    if not enabled(state):
        return [], []
    value = contract(store, state)
    pairs = {}
    for rid, flow in _flows(value):
        pairs.setdefault((rid, flow['criterion']), {})[flow['id']] = flow
    artifacts, failures = [], []
    review = report.get('productFlowReview')
    if review is None:
        failures.append({'requirement': None, 'criterion': 'product-flow-review', 'kind': 'product-flow',
                         'status': 'blocked', 'note': 'Не сверена применимость потоков с исходными словами владельца.'})
    else:
        require(isinstance(review, dict) and set(review) == {'status', 'note'}
                and isinstance(review['status'], str) and review['status'] in STATUSES,
                'Product flows: неверная итоговая сверка исходного запроса')
        _text(review['note'], state)
        if review['status'] != 'passed':
            failures.append({'requirement': None, 'criterion': 'product-flow-review', 'kind': 'product-flow', **review})
    checked_pairs = set()
    for row in report.get('checks', []):
        pair = row.get('requirement'), row.get('criterion')
        expected = pairs.get(pair, {})
        rows = row.get('flowChecks', [])
        require(isinstance(rows, list), 'Product flows: нужен список flowChecks в строке критерия')
        seen = set()
        for item in rows:
            require(isinstance(item, dict) and set(item) == {'flow', 'status', 'note', 'artifact'}
                    and isinstance(item['flow'], str) and item['flow'] in expected and item['flow'] not in seen
                    and isinstance(item['status'], str) and item['status'] in STATUSES,
                    'Product flows: чужой, повторный поток или неверный статус')
            _text(item['note'], state); seen.add(item['flow'])
            if item['status'] == 'passed':
                refs, detail_failures = _observation(store, state, item['artifact'], value, pair[0],
                                                     expected[item['flow']], report['basis'])
                artifacts.extend(refs)
                failures.extend(detail_failures)
            else:
                failures.append({'requirement': pair[0], 'criterion': pair[1], 'flow': item['flow'],
                                 'kind': 'product-flow', 'status': item['status'], 'note': item['note']})
        for fid in sorted(set(expected) - seen):
            failures.append({'requirement': pair[0], 'criterion': pair[1], 'flow': fid, 'kind': 'product-flow',
                             'status': 'not_tested', 'note': 'Отсутствует проверка обязательного пользовательского потока.'})
        checked_pairs.add(pair)
    require(set(pairs) <= checked_pairs, 'Product flows: отсутствует строка критерия обязательного потока')
    flow_requirements = {rid for rid, _flow in _flows(value)}
    for failure in source_failures:
        if (failure.get('requirement') in flow_requirements and str(failure.get('criterion', '')).startswith('blind:')):
            failures.append({**failure, 'kind': 'product-flow', 'note': 'Исходное blind-наблюдение: ' + failure['note']})
    if report.get('unmapped'):
        failures.append({'requirement': None, 'criterion': 'product-flow-source', 'kind': 'product-flow',
                         'status': 'blocked', 'note': 'Несопоставленный исходный запрос не позволяет подтвердить полноту потоков.'})
    return artifacts, failures


def saved_check(store, state):
    """Recompute from immutable report and current contract, never a stored PASS bit."""
    gate = state.get('gates', {}).get('acceptance')
    require(gate and artifact_valid(store, gate['report']), 'Product flows: нет неизменного итогового отчёта')
    report = store.json(gate['report'])
    from .contracts import basis
    require(report.get('basis') == basis(store, state, 'acceptance'), 'Product flows: итоговый кандидат устарел')
    source_failures = []
    if state.get('policy', {}).get('cleanClone'):
        from . import blind
        source_failures = blind.mapping(store, state, report)
    artifacts, failures = check(store, state, report, source_failures)
    require(all(ref in gate.get('artifacts', []) for ref in artifacts), 'Product flows: не сохранены ссылки наблюдений')
    return failures


def valid(store, state):
    if not enabled(state):
        return True
    try:
        return not saved_check(store, state)
    except ResourceLimit:
        raise
    except (Invalid, OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return False


def view(store, state):
    if not enabled(state):
        return None
    try:
        failures = saved_check(store, state)
        return {'ready': not failures, 'failures': failures}
    except ResourceLimit:
        raise
    except (Invalid, OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return {'ready': False, 'failures': [{'kind': 'product-flow', 'status': 'blocked',
                'note': 'Обязательные потоки отсутствуют, устарели или их доказательства повреждены.'}]}
