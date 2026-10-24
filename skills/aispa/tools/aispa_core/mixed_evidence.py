"""Bounded, candidate-bound observations. Hashes prove binding, not truth."""
import copy
import json
from urllib.parse import urlsplit

from .contracts import artifact_valid, evidence_rows, require, task_basis, task_contract
from .portable import relative_name
from .store import Invalid, confined, file_hash
from . import test_intent

MAX_OBSERVATION_BYTES = 256 * 1024
MAX_ATTACHMENTS = 32
STATUSES = frozenset({'passed', 'failed', 'not_tested', 'blocked'})


def report_template(store, state, task_id):
    return [{'criterion': r['criterion'], 'kind': r['kind'], 'status': 'not_tested',
             'note': '', 'artifact': ''}
            for r in test_intent.route_summary(store, state, task_id)['routes']]


def observation_templates(store, state, task_id):
    summary = test_intent.route_summary(store, state, task_id)
    if summary['mode'] != 'mixed':
        return []
    _task, attempt = test_intent._attempt(state, task_id)
    result = []
    for kind in ('browser', 'process'):
        criteria = [r['criterion'] for r in summary['routes'] if r['kind'] == kind]
        if criteria:
            result.append({'schema': 1, 'run': state['run'], 'task': task_id,
                'attempt': attempt['id'], 'candidate': task_basis(store, state, task_id),
                'intentSha256': attempt['testIntent']['sha256'], 'kind': kind, 'criteria': criteria,
                'procedure': None, 'observed': None,
                'source': {'url': None, 'viewport': None} if kind == 'browser' else {'reference': None, 'scope': None},
                'attachments': []})
    return result


def _evidence_file(store, name):
    require(isinstance(name, str) and name.startswith('evidence/') and relative_name(name) == name,
            'Mixed evidence: нужен файл в evidence/')
    path = confined(store.run, name)
    require(path.is_file() and path.stat().st_size > 0, 'Mixed evidence: файл отсутствует или пуст')
    return path


def _text(value):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= 16000,
            'Mixed evidence: нужен ограниченный непустой текст наблюдения')


def _observation(store, name, contract, intent_ref, basis, kind, criterion):
    path = _evidence_file(store, name)
    require(path.stat().st_size <= MAX_OBSERVATION_BYTES, 'Mixed evidence: слишком большой JSON')
    try:
        body = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise Invalid('Mixed evidence: нужен JSON наблюдения') from exc
    require(isinstance(body, dict) and set(body) == {'schema', 'run', 'task', 'attempt', 'candidate',
            'intentSha256', 'kind', 'criteria', 'procedure', 'observed', 'source', 'attachments'}
            and type(body['schema']) is int and body['schema'] == 1,
            'Mixed evidence: неверная схема наблюдения')
    require(all(body[k] == contract[k] for k in ('run', 'task', 'attempt'))
            and body['candidate'] == basis and body['kind'] == kind
            and body['intentSha256'] == intent_ref['sha256'],
            'Mixed evidence: чужая попытка, кандидат, способ или контракт')
    criteria = body['criteria']
    declared = {r['criterion'] for r in contract['report']['routes'] if r['kind'] == kind}
    require(isinstance(criteria, list) and criteria and all(isinstance(c, str) for c in criteria)
            and len(criteria) == len(set(criteria)) and criterion in criteria and set(criteria) <= declared,
            'Mixed evidence: неизвестные или повторные критерии наблюдения')
    _text(body['procedure'])
    _text(body['observed'])
    source = body['source']
    fields = {'url', 'viewport'} if kind == 'browser' else {'reference', 'scope'}
    require(isinstance(source, dict) and set(source) == fields, 'Mixed evidence: неверный источник наблюдения')
    for value in source.values():
        _text(value)
    if kind == 'browser':
        url = urlsplit(source['url'])
        require(url.scheme in ('http', 'https') and bool(url.netloc), 'Mixed evidence: нужен URL браузера')
    attachments = body['attachments']
    require(isinstance(attachments, list) and len(attachments) <= MAX_ATTACHMENTS,
            'Mixed evidence: неверный список приложений')
    refs, seen = [{'file': name, 'sha256': file_hash(path)}], {name}
    for ref in attachments:
        require(isinstance(ref, dict) and set(ref) == {'file', 'sha256'}
                and isinstance(ref['sha256'], str) and test_intent.SHA.fullmatch(ref['sha256'])
                and isinstance(ref['file'], str) and ref['file'] not in seen,
                'Mixed evidence: неверное или повторное приложение')
        attachment = _evidence_file(store, ref['file'])
        require(file_hash(attachment) == ref['sha256'], 'Mixed evidence: приложение изменено')
        refs.append(copy.deepcopy(ref))
        seen.add(ref['file'])
    return refs


def check_contract(store, contract, intent_ref, report):
    """Validate live or sealed reports using their exact immutable intent and basis."""
    if contract.get('mode') != 'mixed':
        return [], []
    test_intent._routes(contract['report'], set(contract['report']['criteria']))
    require(artifact_valid(store, intent_ref), 'Mixed evidence: повреждён контракт')
    routes = {(r['criterion'], r['kind']) for r in contract['report']['routes']}
    rows = report.get('routeChecks')
    require(isinstance(rows, list), 'Mixed evidence: нужен routeChecks')
    seen, artifacts, failures = set(), [], []
    for row in rows:
        require(isinstance(row, dict) and set(row) == {'criterion', 'kind', 'status', 'note', 'artifact'},
                'Mixed evidence: неверная схема routeChecks')
        require(isinstance(row['criterion'], str) and isinstance(row['kind'], str), 'Mixed evidence: неверная пара')
        pair = row['criterion'], row['kind']
        require(pair in routes and pair not in seen and isinstance(row['status'], str) and row['status'] in STATUSES,
                'Mixed evidence: неизвестная, повторная пара или статус')
        _text(row['note'])
        seen.add(pair)
        if row['status'] == 'passed':
            if row['kind'] == 'automated':
                found, _ = evidence_rows(store, {'checks': [{'requirement': contract['task'],
                    'criterion': row['criterion'], 'status': 'passed', 'note': row['note'],
                    'artifact': row['artifact']}]}, {(contract['task'], row['criterion'])})
            else:
                found = _observation(store, row['artifact'], contract, intent_ref, report['basis'], row['kind'], row['criterion'])
            artifacts.extend(found)
        else:
            failures.append({'requirement': contract['task'], 'criterion': row['criterion'],
                             'kind': row['kind'], 'status': row['status'], 'note': row['note']})
    require(seen == routes, 'Mixed evidence: проверены не все способы критериев')
    failed = {r['criterion'] for r in failures}
    require(not any(r.get('requirement') == contract['task'] and r.get('criterion') in failed
                    and r.get('status') == 'passed' for r in report.get('checks', [])),
            'Mixed evidence: общий PASS невозможен при непроверенном способе')
    return artifacts, failures


def check(store, state, task_id, report):
    _task, attempt = test_intent._attempt(state, task_id)
    ref = attempt.get('testIntent')
    if not ref:
        return [], []
    contract = test_intent._saved(store, ref)
    if contract['mode'] == 'mixed':
        require(state.get('policy', {}).get('mixedEvidence') is True, 'Mixed evidence: политика выключена')
        require(contract.get('run') == state.get('run') and contract.get('task') == task_id
                and contract.get('attempt') == attempt['id']
                and contract.get('taskContract') == task_contract(state, task_id),
                'Mixed evidence: чужой или устаревший контракт задачи')
        require(report.get('basis') == task_basis(store, state, task_id), 'Mixed evidence: устаревший кандидат отчёта')
    return check_contract(store, contract, ref, report)


def record_valid(store, record):
    """Re-read sealed observations and attachments; unstored mutable files cannot bypass proof."""
    ref = record.get('testIntent')
    if not ref:
        return True
    try:
        contract = test_intent._saved(store, ref)
        report = json.loads(store.text(record['report']))
        artifacts, failures = check_contract(store, contract, ref, report)
        return (all(a in record.get('artifacts', []) for a in artifacts)
                and (not record.get('passed') or not failures))
    except (Invalid, OSError, ValueError, TypeError, KeyError, UnicodeError):
        return False
