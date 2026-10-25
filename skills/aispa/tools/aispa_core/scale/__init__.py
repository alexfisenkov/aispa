"""Hash-bound transport and operational budgets, independent of entity counts.

The complete decoded value still enters the existing domain validator. This is
bounded in-memory validation, not an unbounded-memory or transactional filesystem.
"""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from ..store import Invalid, confined, digest

_SHA = re.compile(r'[a-f0-9]{64}\Z')
_PRIVATE = re.compile(r'(?:^|[._-])(?:env|secrets?|credentials?|passwords?|tokens?|private|keys?)(?:$|[._-])', re.I)


def enabled(state):
    return state.get('policy', {}).get('scalableInputs') is True


def _positive(name, default, *, integer=True):
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw) if integer else float(raw)
    except (ValueError, TypeError, OverflowError) as error:
        raise Invalid('AISpa resource profile: invalid ' + name) from error
    if isinstance(value, bool) or value <= 0 or not math.isfinite(value):
        raise Invalid('AISpa resource profile: invalid ' + name)
    return value


class Budget:
    def __init__(self):
        self.max_bytes = _positive('AISPA_MAX_INPUT_BYTES', 64 * 1024 * 1024)
        self.manifest_bytes = _positive("AISPA_MAX_MANIFEST_BYTES", 8 * 1024 * 1024)
        self.chunk_bytes = _positive('AISPA_CHUNK_BYTES', 1024 * 1024)
        self.memory_bytes = _positive('AISPA_MAX_MEMORY_BYTES', 512 * 1024 * 1024)
        self.seconds = _positive('AISPA_OPERATION_SECONDS', 120, integer=False)
        self.deadline = time.monotonic() + self.seconds
        self.base_memory = 0

    def check_memory(self, size, *, path='', progress=0):
        size += self.base_memory
        if size > self.memory_bytes:
            raise Invalid(f'RESOURCE_LIMIT maxMemoryBytes: accountedBytes={size}, completedItems={progress}, path={path}; continuation: increase AISPA_MAX_MEMORY_BYTES and retry complete input')
        self.check(0, path=path, progress=progress)

    def check_metadata(self, size, *, path='', progress=0):
        self.check(size, path=path, progress=progress)
        if size > self.manifest_bytes:
            raise Invalid(f'RESOURCE_LIMIT manifestBytes: bytes={size}, completedBytes={progress}, path={path}; continuation: increase AISPA_MAX_MANIFEST_BYTES or use larger chunks and retry complete input')

    def check(self, size=0, *, path='', progress=0):
        field = ('maxInputBytes' if size > self.max_bytes else
                 'maxMemoryBytes' if size * 4 > self.memory_bytes else
                 'operationSeconds' if time.monotonic() > self.deadline else None)
        if field:
            raise Invalid(f'RESOURCE_LIMIT {field}: bytes={size}, completedBytes={progress}, path={path}; '
                          'continuation: increase the named operation budget and retry the complete input; scope unchanged')


def check_value(value, state):
    if enabled(state):
        budget = Budget()
        try:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError, RecursionError) as error:
            raise Invalid('AISpa scale: invalid JSON value') from error
        budget.check(len(raw))
        budget.base_memory = len(raw) * 4
        return budget
    return None


def _read(path, budget, limit=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Invalid('AISpa scale: missing or symlink input: ' + str(path))
    size = path.stat().st_size
    if limit is None:
        budget.check(size, path=str(path))
        limit = budget.max_bytes
    elif size > limit:
        raise Invalid('RESOURCE_LIMIT chunkBytes: ' + str(path) + '; continuation: increase AISPA_CHUNK_BYTES and retry complete input')
    data = bytearray()
    with path.open('rb') as stream:
        while True:
            part = stream.read(min(65536, limit - len(data) + 1))
            if not part:
                break
            data.extend(part)
            budget.check(0, path=str(path), progress=len(data))
            if len(data) > limit:
                raise Invalid('RESOURCE_LIMIT maxInputBytes: ' + str(path) + '; continuation: increase operation budget and retry complete input')
    return bytes(data)


def _object(raw):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Invalid('AISpa scale: malformed manifest or chunk JSON') from error


def is_manifest(raw):
    # Only the reserved transport marker changes interpretation of ordinary data.
    if not raw.lstrip().startswith(b'{'):
        return False
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return False
    return isinstance(value, dict) and 'aispaManifest' in value


def read_file(path, *, allow_manifest=True, budget=None, expected_sha256=None, verify_plain=True):
    budget = budget or Budget()
    raw = _read(path, budget)
    if expected_sha256 is not None and (verify_plain or is_manifest(raw)) and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise Invalid('AISpa scale: immutable artifact SHA mismatch')
    if not is_manifest(raw):
        budget.check(len(raw), path=str(path), progress=len(raw))
        return raw
    if not allow_manifest:
        raise Invalid('AISpa scale: manifest requires a new scalableInputs init; historical policy unchanged')
    budget.check_metadata(len(raw), path=str(path))
    value = _object(raw)
    fields = {'aispaManifest', 'schema', 'kind', 'basis', 'digest', 'totalBytes', 'chunks'}
    if (set(value) != fields or type(value['aispaManifest']) is not int or value['aispaManifest'] != 1
            or type(value['schema']) is not int or value['schema'] != 1
            or value['kind'] not in ('json', 'text')
            or not isinstance(value['basis'], str) or not _SHA.fullmatch(value['basis'])
            or not isinstance(value['digest'], str) or not _SHA.fullmatch(value['digest'])
            or type(value['totalBytes']) is not int or value['totalBytes'] <= 0
            or not isinstance(value['chunks'], list) or not value['chunks']):
        raise Invalid('AISpa scale: invalid manifest schema or completeness')
    budget.check(value['totalBytes'], path=str(path))
    chunks, seen = [], set()
    size = 0
    for index, ref in enumerate(value['chunks']):
        if (not isinstance(ref, dict) or set(ref) != {'file', 'sha256', 'index', 'bytes'}
                or type(ref['index']) is not int or ref['index'] != index
                or type(ref['bytes']) is not int or ref['bytes'] <= 0
                or not isinstance(ref['sha256'], str) or not _SHA.fullmatch(ref['sha256'])
                or not isinstance(ref['file'], str) or ref['file'] in seen):
            raise Invalid('AISpa scale: duplicate, unordered or malformed chunk reference')
        if any(part.startswith('.') or _PRIVATE.search(part) for part in Path(ref['file']).parts):
            raise Invalid('AISpa scale: private or hidden chunk path')
        seen.add(ref['file'])
        source = confined(Path(path).parent, ref['file'])
        encoded = _read(source, budget, budget.chunk_bytes * 2 + 8192)
        if hashlib.sha256(encoded).hexdigest() != ref['sha256']:
            raise Invalid('AISpa scale: chunk SHA mismatch')
        chunk = _object(encoded)
        if (not isinstance(chunk, dict) or set(chunk) != {'schema', 'kind', 'basis', 'digest', 'index', 'total', 'content'}
                or type(chunk['schema']) is not int or chunk['schema'] != 1
                or chunk['kind'] != value['kind'] or chunk['basis'] != value['basis']
                or chunk['digest'] != value['digest'] or type(chunk['index']) is not int or chunk['index'] != index
                or type(chunk['total']) is not int or chunk['total'] != len(value['chunks'])
                or not isinstance(chunk['content'], str)):
            raise Invalid('AISpa scale: foreign or incomplete chunk')
        try:
            part = base64.b64decode(chunk['content'], validate=True)
        except (ValueError, UnicodeError) as error:
            raise Invalid('AISpa scale: invalid chunk encoding') from error
        if len(part) != ref['bytes'] or len(part) > budget.chunk_bytes:
            raise Invalid('RESOURCE_LIMIT chunkBytes or invalid chunk length; continuation: increase AISPA_CHUNK_BYTES and retry complete input')
        size += len(part)
        budget.check(size, path=str(path), progress=size)
        if size > value['totalBytes']:
            raise Invalid('AISpa scale: manifest byte overflow')
        chunks.append(part)
    decoded = b''.join(chunks)
    if size != value['totalBytes'] or hashlib.sha256(decoded).hexdigest() != value['digest']:
        raise Invalid('AISpa scale: missing payload or full digest mismatch')
    try:
        decoded.decode('utf-8')
    except UnicodeError as error:
        raise Invalid('AISpa scale: payload must be UTF-8') from error
    if value['kind'] == 'json':
        _object(decoded)
    budget.check(size, path=str(path), progress=size)
    return decoded


def write_manifest(path, raw, *, kind, basis, chunk_bytes=None):
    """Write chunks first, publish the immutable manifest last; clean our files on failure."""
    budget = Budget()
    budget.check(len(raw), path=str(path))
    chunk_bytes = budget.chunk_bytes if chunk_bytes is None else chunk_bytes
    if (type(chunk_bytes) is not int or chunk_bytes <= 0 or kind not in ('text', 'json')
            or not isinstance(basis, str) or not _SHA.fullmatch(basis) or not isinstance(raw, bytes) or not raw):
        raise Invalid('AISpa scale: invalid producer input')
    raw.decode('utf-8')
    if kind == 'json':
        _object(raw)
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise Invalid('AISpa scale: immutable manifest already exists')
    directory = path.parent / ('chunks-' + uuid.uuid4().hex)
    path.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir()
    created = []
    sha = hashlib.sha256(raw).hexdigest()
    total = (len(raw) + chunk_bytes - 1) // chunk_bytes
    refs = []
    metadata_bytes = 512
    try:
        for index, offset in enumerate(range(0, len(raw), chunk_bytes)):
            budget.check(len(raw), path=str(path), progress=offset)
            part = raw[offset:offset + chunk_bytes]
            value = {'schema': 1, 'kind': kind, 'basis': basis, 'digest': sha, 'index': index,
                     'total': total, 'content': base64.b64encode(part).decode('ascii')}
            encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
            target = directory / (str(index) + '.json')
            with target.open('xb') as stream:
                created.append(target)
                stream.write(encoded)
            ref = {'file': target.relative_to(path.parent).as_posix(), 'sha256': hashlib.sha256(encoded).hexdigest(),
                   'index': index, 'bytes': len(part)}
            metadata_bytes += len(json.dumps(ref).encode()) + 1
            budget.check_metadata(metadata_bytes, path=str(path), progress=offset + len(part))
            refs.append(ref)
        manifest = {'aispaManifest': 1, 'schema': 1, 'kind': kind, 'basis': basis, 'digest': sha,
                    'totalBytes': len(raw), 'chunks': refs}
        encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
        budget.check_metadata(len(encoded), path=str(path), progress=len(raw))
        with path.open('xb') as stream:
            created.append(path)
            stream.write(encoded)
        return manifest
    except BaseException:
        for target in reversed(created):
            target.unlink(missing_ok=True)
        directory.rmdir()
        raise


def page(values, *, cursor=None, limit=48):
    if not isinstance(values, list) or type(limit) is not int or limit <= 0:
        raise Invalid('AISpa scale: page requires a list and positive operation page size')
    sha = digest(values)
    offset = 0
    if cursor is not None:
        try:
            binding, position = cursor.split(':')
            offset = int(position)
        except (ValueError, TypeError, AttributeError) as error:
            raise Invalid('AISpa scale: malformed page cursor') from error
        if binding != sha or offset < 0 or offset >= len(values) or str(offset) != position:
            raise Invalid('AISpa scale: stale or foreign page cursor')
    end = min(len(values), offset + limit)
    return {'items': values[offset:end], 'total': len(values), 'omitted': len(values) - end,
            'cursor': sha + ':' + str(end) if end < len(values) else None, 'digest': sha}
