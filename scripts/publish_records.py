#!/usr/bin/env python3
"""Serialize shared Markdown publications; preserve interrupted work for explicit retry.

No database or multi-file atomicity claim. A durable publication journal precedes
records, LOG, CURRENT; retries finish exactly that operation or report conflict.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid
import project_registry as registry

LIMIT = 16 * 1024 * 1024
ID = re.compile(r'(?:CON|HND|DEC|NOTE)-\d{8}-\d{2,6}\Z')


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def read(path):
    with registry._directory_fd(path.parent) as parent:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as f:
            info = os.fstat(f.fileno())
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 2:
                remnants = []
                for name in os.listdir(parent):
                    if re.fullmatch(r'\.publish-[0-9a-f]{32}', name):
                        candidate = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if stat.S_ISREG(candidate.st_mode) and (candidate.st_dev, candidate.st_ino) == (info.st_dev, info.st_ino):
                            remnants.append(name)
                if len(remnants) == 1:
                    os.unlink(remnants[0], dir_fd=parent); os.fsync(parent)
                    info = os.fstat(f.fileno())
            registry.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Independent regular file required')
            raw = f.read(LIMIT + 1)
            registry.require(len(raw) <= LIMIT, 'Publication file exceeds limit')
            registry._directory_still_bound(path.parent, parent)
            return raw


def write(path, raw, exclusive=False):
    registry.require(len(raw) <= LIMIT, 'Publication file exceeds limit')
    registry._secure_components(path, missing_leaf=True)
    with registry._directory_fd(path.parent) as parent:
        name = '.publish-' + uuid.uuid4().hex
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(raw); f.flush(); os.fsync(f.fileno())
            registry._directory_still_bound(path.parent, parent)
            registry._secure_components(path, missing_leaf=True)
            if exclusive:
                os.link(name, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.unlink(name, dir_fd=parent)
            else:
                os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try: os.unlink(name, dir_fd=parent)
            except FileNotFoundError: pass


def encode(obj):
    return registry.canonical_json(obj)


def validate_request(request, project):
    registry._keys(request, {'operation_id', 'expected_generation', 'expected_current_sha256', 'records', 'log_lines', 'current'}, 'Publication')
    registry._uuid(request['operation_id'])
    registry.require(registry._integer(request['expected_generation']), 'Invalid generation')
    h = request['expected_current_sha256']
    registry.require(h is None or isinstance(h, str) and registry.SHA256.fullmatch(h), 'Invalid CURRENT hash')
    registry.require(type(request['records']) is list and len(request['records']) <= 50, 'Invalid records')
    registry.require(type(request['log_lines']) is list and 0 < len(request['log_lines']) <= 50, 'Publication needs LOG lines')
    for line in request['log_lines']:
        registry.require(isinstance(line, str) and '\n' not in line and '\r' not in line and re.match(r'^(?:LOG|DEC)-\d{8}-\d{2,6}\s+\|', line), 'Invalid LOG line')
    log_ids = [line.split('|',1)[0].strip() for line in request['log_lines']]
    registry.require(len(log_ids) == len(set(log_ids)), 'Duplicate LOG identifier', 'CONFLICT')
    registry.require(isinstance(request['current'], str) and 0 < len(request['current'].splitlines()) <= 30, 'CURRENT must contain 1 to 30 lines')
    ids = set(); repos = {r['repo_uuid'] for r in project['repositories']}
    for item in request['records']:
        registry._keys(item, {'id', 'text', 'scope'}, 'Record')
        registry.require(isinstance(item['id'], str) and ID.fullmatch(item['id']) and item['id'] not in ids, 'Invalid or duplicate record ID')
        ids.add(item['id'])
        scope = item['scope']
        registry.require(scope == 'shared' or type(scope) is list and bool(scope) and all(isinstance(r,str) and r in repos for r in scope), 'Declare shared or registered repository UUID scope')
        registry.require(isinstance(item['text'], str) and bool(item['text']), 'Empty record')
    registry.require(len(encode(request)) <= 1024 * 1024, 'Publication exceeds 1 MiB')


def validate_journal(journal, project):
    keys = {'schema_version','state','request_sha256','request','project_uuid','registry_generation','previous_current','previous_log_sha256','log_after_sha256','log_suffix','provenance'}
    registry.require(type(journal) is dict and journal.get('state') in ('PREPARED','COMMITTED'), 'Invalid publication journal', 'RECOVERY_REQUIRED')
    registry._keys(journal, keys | ({'record_hashes'} if journal['state'] == 'COMMITTED' else set()), 'Publication journal')
    registry.require(journal['schema_version'] == 1 and journal['project_uuid'] == project['project_uuid'], 'Journal identity differs', 'RECOVERY_REQUIRED')
    validate_request(journal['request'], project)
    registry.require(journal['request_sha256'] == digest(encode(journal['request'])), 'Journal request changed', 'RECOVERY_REQUIRED')
    registry.require(type(journal['registry_generation']) is int and journal['registry_generation'] == journal['request']['expected_generation'], 'Journal generation differs', 'RECOVERY_REQUIRED')
    registry.require(journal['previous_current'] is None or isinstance(journal['previous_current'], str), 'Invalid previous CURRENT', 'RECOVERY_REQUIRED')
    old = journal['previous_current']
    registry.require(digest(old.encode() if old is not None else None) == journal['request']['expected_current_sha256'], 'Journal CURRENT binding changed', 'RECOVERY_REQUIRED')
    for key in ('previous_log_sha256','log_after_sha256'):
        value = journal[key]
        registry.require((value is None and key == 'previous_log_sha256') or isinstance(value,str) and registry.SHA256.fullmatch(value), 'Invalid journal LOG hash', 'RECOVERY_REQUIRED')
    registry.require(isinstance(journal['log_suffix'],str) and type(journal['provenance']) is dict, 'Invalid journal data', 'RECOVERY_REQUIRED')
    if journal['state'] == 'COMMITTED':
        registry.require(type(journal['record_hashes']) is list, 'Invalid committed receipt', 'RECOVERY_REQUIRED')


def publish(registry_path, cwd, host, request, fail_after=None):
    registry_path = Path(registry_path)
    initial = registry.resolve_project(registry_path, cwd, host)
    registry.require(initial['write_allowed'], 'Project is not active', 'INACTIVE')
    pid = initial['project_uuid']; deadline = time.monotonic() + 5
    with ExitStack() as stack:
        stack.enter_context(registry._lock(registry_path.parent / 'locks' / (pid + '.lock'), deadline))
        stack.enter_context(registry._lock(registry_path.parent / 'projects.lock', deadline))
        resolved = registry.resolve_project(registry_path, cwd, host)
        registry.require(resolved['project_uuid'] == pid and resolved['write_allowed'], 'Project changed', 'CONFLICT')
        data = registry.validate_registry(registry.read_json(registry_path))
        project = next(p for p in data['projects'] if p['project_uuid'] == pid)
        validate_request(request, project)
        home = Path(resolved['home']); record_dir = home / 'records'; publications = home / '.readback' / 'publications'
        for folder in [record_dir, home / '.readback', publications]:
            with registry._directory_fd(folder, create=True): pass
        op = request['operation_id']; journal_path = publications / (op + '.json'); request_hash = digest(encode(request))
        journal_raw = read(journal_path)
        if journal_raw is not None:
            journal = registry._decode_json(journal_raw)
            validate_journal(journal, project)
            registry.require(journal['request_sha256'] == request_hash, 'Operation UUID reused with different request', 'CONFLICT')
            if journal['state'] == 'COMMITTED':
                actual_hashes = [item['id'] + '@sha256:' + str(digest(read(record_dir/(item['id']+'.md')))) for item in request['records']]
                registry.require(actual_hashes == journal['record_hashes'], 'Committed records changed or missing', 'RECOVERY_REQUIRED')
                registry.require(('<!-- publication ' + op + ' records ').encode() in (read(home/'LOG.md') or b''), 'Committed LOG receipt is missing', 'RECOVERY_REQUIRED')
                return {'status': 'COMMITTED', 'operation_id': op, 'replayed': True, 'home': str(home)}
        else:
            registry.require(data['generation'] == request['expected_generation'], 'Registry generation changed', 'CONFLICT')
            for other in sorted(publications.glob('*.json')):
                previous = registry._decode_json(read(other))
                validate_journal(previous, project)
                registry.require(previous['state'] == 'COMMITTED', 'Unfinished publication: retry ' + other.stem, 'RECOVERY_REQUIRED')
            current = read(home / 'CURRENT.md'); log = read(home / 'LOG.md')
            registry.require(digest(current) == request['expected_current_sha256'], 'CURRENT changed; reread before publishing', 'CONFLICT')
            existing_ids = set(re.findall(r'^(?:LOG|DEC)-\d{8}-\d{2,6}(?=\s+\|)', (log or b'').decode('utf-8'), re.M))
            registry.require(not existing_ids.intersection(line.split('|',1)[0].strip() for line in request['log_lines']), 'LOG identifier already exists', 'CONFLICT')
            for item in request['records']:
                registry.require(read(record_dir / (item['id'] + '.md')) is None, 'Record ID already exists', 'CONFLICT')
            prepared_records = []
            for item in request['records']:
                content = ('<!-- readback project=' + pid + ' scope=' + json.dumps(item['scope'], separators=(',',':')) + ' -->\n' + item['text']).encode()
                prepared_records.append(item['id'] + '@sha256:' + digest(content))
            suffix = ('' if not log or log.endswith(b'\n') else '\n') + '\n'.join(request['log_lines']) + '\n<!-- publication ' + op + ' records ' + ','.join(prepared_records) + ' -->\n'
            prospective_log = (log or b'') + suffix.encode()
            registry.require(len(prospective_log) <= LIMIT, 'LOG would exceed publication limit', 'STORAGE_LIMIT')
            journal = {'schema_version': 1, 'state': 'PREPARED', 'request_sha256': request_hash, 'request': request,
                       'project_uuid': pid, 'registry_generation': data['generation'],
                       'previous_current': current.decode('utf-8') if current is not None else None,
                       'previous_log_sha256': digest(log), 'log_after_sha256': digest(prospective_log), 'log_suffix': suffix,
                       'provenance': {key: resolved.get(key) for key in ('repo_uuid','cwd','worktree_root','git_common_dir')}}
            final_preview = dict(journal, state='COMMITTED', record_hashes=prepared_records)
            registry.require(max(len(encode(journal)), len(encode(final_preview))) <= min(LIMIT, registry.MAX_JSON_BYTES), 'Journal would exceed publication limit', 'STORAGE_LIMIT')
            write(journal_path, encode(journal), exclusive=True)
        registry.require(data['generation'] == journal['registry_generation'], 'Generation changed during unfinished publication', 'CONFLICT')
        if fail_after == 'journal': raise RuntimeError('Injected interruption after journal')
        hashes = []
        for item in request['records']:
            raw = ('<!-- readback project=' + pid + ' scope=' + json.dumps(item['scope'], separators=(',',':')) + ' -->\n' + item['text']).encode()
            dest = record_dir / (item['id'] + '.md'); existing = read(dest)
            if existing is None: write(dest, raw, exclusive=True)
            else: registry.require(existing == raw, 'Prepared record changed', 'CONFLICT')
            hashes.append(item['id'] + '@sha256:' + digest(raw))
        if fail_after == 'records': raise RuntimeError('Injected interruption after records')
        log = read(home/'LOG.md')
        registry.require(digest(log) in (journal['previous_log_sha256'], journal['log_after_sha256']), 'LOG changed during publication', 'CONFLICT')
        if digest(log) != journal['log_after_sha256']:
            prospective_log = (log or b'') + journal['log_suffix'].encode()
            registry.require(digest(prospective_log) == journal['log_after_sha256'], 'Journal LOG binding changed', 'RECOVERY_REQUIRED')
            write(home/'LOG.md', prospective_log)
        if fail_after == 'LOG.md': raise RuntimeError('Injected interruption after LOG.md')
        for path, before, after in [(home/'CURRENT.md', journal['previous_current'], request['current'])]:
            before_bytes = before.encode() if before is not None else None; after_bytes = after.encode(); existing = read(path)
            registry.require(existing in (before_bytes, after_bytes), path.name + ' changed during publication', 'CONFLICT')
            if existing != after_bytes: write(path, after_bytes)
            if fail_after == path.name: raise RuntimeError('Injected interruption after ' + path.name)
        journal['state'] = 'COMMITTED'; journal['record_hashes'] = hashes
        write(journal_path, encode(journal))
        return {'status': 'COMMITTED', 'operation_id': op, 'replayed': False, 'home': str(home), 'records': hashes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True); parser.add_argument('--cwd', required=True)
    parser.add_argument('--host', choices=['codex','claude'], required=True); parser.add_argument('--request', required=True)
    args = parser.parse_args()
    try:
        result = publish(args.registry, args.cwd, args.host, registry.read_json(Path(args.request)))
        print(json.dumps(result)); return 0
    except (registry.RegistryError, OSError, ValueError, UnicodeError) as exc:
        print(json.dumps({'status': getattr(exc,'code','INVALID'), 'error':str(exc)}), file=sys.stderr); return 2

if __name__ == '__main__': sys.exit(main())
