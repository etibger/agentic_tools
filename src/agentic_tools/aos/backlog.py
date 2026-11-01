"""Explicit, selected-item reconciliation; no watcher or workflow authority.

GitHub owns tracker status/priority; Markdown owns its maintained summary.
Inbound writes are local. Outbound writes are deliberately a public-safe preview
for the existing issue-maintenance route, followed by fresh remote readback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from html import escape
from datetime import datetime, timezone
from pathlib import Path

from .store import Rejected, canonical, digest, now

MARKER = re.compile(r' <span data-aos-tracker="[A-Za-z0-9-]+">.*?</span>')


@contextmanager
def _intent_lock(path: Path):
    import fcntl
    fd = os.open(path.with_name('.' + path.name + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _save(path: Path, value: str, *, mode: int = 0o600):
    """Atomic replacement keeps interrupted target/receipt writes readable."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.' + path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, mode)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def selected(markdown: str, item: str) -> dict:
    if not re.fullmatch(r'[A-Z][A-Z0-9-]*', item):
        raise Rejected('backlog_identity', 'Select one existing maintained table ID')
    rows = [line for line in markdown.splitlines() if re.match(r'\|\s*' + re.escape(item) + r'\s*\|', line)]
    if len(rows) != 1:
        raise Rejected('backlog_identity', 'Exactly one maintained row is required; no inferred or duplicate identity')
    markers = re.findall(r'data-aos-tracker="([^"]+)"', rows[0])
    if (len(markers) > 1 or any(value != item for value in markers)
            or len(markers) != len(MARKER.findall(rows[0]))
            or rows[0].count('data-aos-tracker=') != len(markers)):
        raise Rejected('backlog_conflict', 'Tracker marker does not belong uniquely to this row')
    fields = rows[0].strip().strip('|').split('|')
    if len(fields) != 4:
        raise Rejected('backlog_shape', 'Expected the existing four-column operational backlog table')
    urls = re.findall(r'https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)', fields[2])
    if len(urls) != 1:
        raise Rejected('backlog_identity', 'The selected row must link exactly one existing canonical issue')
    repository, number = urls[0]
    return {'id': item, 'row': rows[0], 'repository': repository, 'number': int(number),
            'url': f'https://github.com/{repository}/issues/{number}',
            'outcome': fields[1].strip(), 'disposition': MARKER.sub('', fields[3]).strip()}


class GitHubReader:
    def __init__(self, project_owner: str, project_number: int):
        if not re.fullmatch(r'[\w-]+', project_owner) or project_number < 1:
            raise Rejected('backlog_project', 'Explicit existing project owner and number required')
        self.owner, self.number = project_owner, project_number

    @staticmethod
    def _read(arguments):
        result = subprocess.run(['gh', *arguments], check=True, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)

    def read(self, item: dict) -> dict:
        issue = self._read(['issue', 'view', item['url'], '--json', 'url,title,body,state,updatedAt'])
        project = self._read(['project', 'item-list', str(self.number), '--owner', self.owner,
                              '--limit', '1000', '--format', 'json'])
        if project.get('totalCount', len(project['items'])) > len(project['items']):
            raise Rejected('backlog_project', 'Project inventory is incomplete; do not infer an absent item')
        matches = [p for p in project['items'] if p.get('content', {}).get('url') == item['url']]
        if len(matches) != 1 or issue['url'] != item['url']:
            raise Rejected('backlog_identity', 'Exactly one matching issue and project item is required')
        # Existing #33 reciprocal source identity is reused, never recreated.
        if not re.search(r'(?<![A-Za-z0-9-])' + re.escape(item['id']) + r'(?![A-Za-z0-9-])', issue['body']):
            raise Rejected('backlog_identity', 'Issue has no reciprocal maintained source ID')
        row = matches[0]
        return {'issue': issue, 'project': {'owner': self.owner, 'number': self.number, 'item_id': row['id'],
                'status': row.get('status'), 'urgency': row.get('urgency'), 'importance': row.get('importance')}}


def _tracker(snapshot: dict) -> str:
    fields = {'issue': snapshot['issue']['state'], **{k: snapshot['project'].get(k) for k in ('status', 'urgency', 'importance')}}
    # Never infer acceptance, installation or release from issue/project state.
    return '; '.join(k + ': ' + str(v if v is not None else 'untriaged') for k, v in fields.items())


def _public(text: str):
    if re.search(r'/Users/|/home/|private/tmp/|(?i:bearer\s+\S+|api[_-]?key\s*[:=]|BEGIN .*PRIVATE KEY)|gh[pousr]_[A-Za-z0-9]+', text):
        raise Rejected('backlog_private', 'Outbound summary contains a private path or credential-like material')


def _block(item: dict) -> str:
    text = f"<!-- aos-maintained:{item['id']} -->\nMaintained outcome: {item['outcome']}\n\nCurrent Markdown disposition: {item['disposition']}\n<!-- /aos-maintained:{item['id']} -->"
    _public(text)
    return text


def _body(old: str, item: dict) -> str:
    block = _block(item)
    pattern = re.compile(r'<!-- aos-maintained:' + re.escape(item['id']) + r' -->.*?<!-- /aos-maintained:' + re.escape(item['id']) + r' -->', re.S)
    matches = pattern.findall(old)
    boundaries = re.findall(r'<!-- /?aos-maintained:' + re.escape(item['id']) + r'(?=[\s>]|$)', old)
    if len(matches) > 1 or len(boundaries) != 2 * len(matches):
        raise Rejected('backlog_conflict', 'Malformed or duplicate maintained block; preserve it for reconciliation')
    return pattern.sub(lambda _: block, old) if matches else old.rstrip() + '\n\n' + block + '\n'


def preview(markdown: Path, item_id: str, direction: str, reader) -> dict:
    if direction not in ('github-to-markdown', 'markdown-to-github'):
        raise Rejected('backlog_direction', 'Choose the selected reconciliation direction explicitly')
    if markdown.is_symlink() or not markdown.is_file():
        raise Rejected('backlog_path', 'Use a real maintained Markdown file')
    text = markdown.read_bytes().decode('utf-8')
    item = selected(text, item_id)
    snapshot = reader.read(item)
    tracker = escape(_tracker(snapshot)).replace('|', '&#124;')
    marker = f' <span data-aos-tracker="{item_id}">Tracker: {tracker}. Tracker state is not delivery acceptance.</span>'
    row = MARKER.sub('', item['row'])
    wanted_row = row.rstrip()[:-1].rstrip() + marker + ' |'
    wanted_body = _body(snapshot['issue']['body'], item)
    before, after = ((item['row'], wanted_row) if direction == 'github-to-markdown'
                     else (snapshot['issue']['body'], wanted_body))
    return {'version': 1, 'item': item, 'direction': direction, 'markdown': str(markdown.resolve()),
            'markdown_sha256': hashlib.sha256(text.encode()).hexdigest(), 'snapshot': snapshot,
            'observed_at': now(), 'before': before, 'after': after, 'changed': before != after,
            'ownership': 'GitHub: tracker status/priority. Markdown: maintained summary. No AOS authority changes.',
            'external_write': 'preview only; use the existing explicitly authorized issue-maintenance route, then read back'}


def prepare(path: Path, plan: dict) -> dict:
    """One immutable intent per selected preview; failures/readbacks append history."""
    with _intent_lock(path):
        return _prepare(path, plan)


def _prepare(path: Path, plan: dict) -> dict:
    intent = {'plan': plan, 'plan_digest': digest(plan), 'events': [{'state': 'prepared' if plan['changed'] else 'unchanged', 'at': now()}]}
    if path.exists():
        prior = json.loads(path.read_text())
        if ({k:v for k,v in prior['plan'].items() if k != 'observed_at'} !=
                {k:v for k,v in plan.items() if k != 'observed_at'}):
            raise Rejected('backlog_intent', 'Intent already names a different preview; retain it and use a fresh path')
        return prior
    with path.open('x') as stream:
        os.chmod(path, 0o600)
        stream.write(canonical(intent) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return intent


def reconcile(path: Path, reader, *, apply_local: bool = False) -> dict:
    with _intent_lock(path):
        return _reconcile(path, reader, apply_local=apply_local)


def _reconcile(path: Path, reader, *, apply_local: bool = False) -> dict:
    intent = json.loads(path.read_text())
    plan = intent['plan']
    if digest(plan) != intent['plan_digest']:
        raise Rejected('backlog_intent', 'Retained preview changed')
    markdown = Path(plan['markdown'])
    try:
        text = markdown.read_bytes().decode('utf-8')
        item = selected(text, plan['item']['id'])
        current = reader.read(item)
        if item['url'] != plan['item']['url']:
            raise Rejected('backlog_conflict', 'Maintained identity changed')
        if plan['direction'] == 'github-to-markdown':
            if current != plan['snapshot']:
                raise Rejected('backlog_conflict', 'Issue or project changed since preview; refresh before writing')
            if item['row'] == plan['after']:
                state = 'readback-verified'
            elif (item['row'] != plan['before'] or hashlib.sha256(text.encode()).hexdigest() != plan['markdown_sha256']):
                raise Rejected('backlog_conflict', 'Markdown changed since preview; no overwrite')
            elif not apply_local:
                state = 'pending-local-write'
            else:
                if (datetime.now(timezone.utc) - datetime.fromisoformat(plan['observed_at'])).total_seconds() > 900:
                    raise Rejected('backlog_stale', 'Preview is older than 15 minutes; refresh it')
                if markdown.is_symlink():
                    raise Rejected('backlog_path', 'Maintained file became an alias')
                # One selected row only; preserve every other byte and all prose.
                lines = text.splitlines(keepends=True)
                index = next(i for i, line in enumerate(lines) if line.rstrip('\r\n') == plan['before'])
                ending = lines[index][len(plan['before']):]
                lines[index] = plan['after'] + ending
                updated = ''.join(lines)
                with markdown.open('r+', newline='') as stream:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX)
                    if stream.read() != text:
                        raise Rejected('backlog_conflict', 'Markdown changed during local admission')
                    before_stat = os.fstat(stream.fileno())
                    current_stat = markdown.stat(follow_symlinks=False)
                    if (before_stat.st_dev, before_stat.st_ino) != (current_stat.st_dev, current_stat.st_ino):
                        raise Rejected('backlog_conflict', 'Maintained file was replaced during local admission')
                    _save(markdown, updated, mode=before_stat.st_mode & 0o777)
                if markdown.read_bytes().decode('utf-8') != updated:
                    raise Rejected('backlog_readback', 'Local readback differs; reconcile before retrying')
                state = 'readback-verified'
        else:
            if item['row'] != plan['item']['row']:
                raise Rejected('backlog_conflict', 'Maintained summary changed since preview')
            if (current['project'] != plan['snapshot']['project'] or
                    any(current['issue'][key] != plan['snapshot']['issue'][key] for key in ('state', 'title', 'url'))):
                raise Rejected('backlog_conflict', 'Tracker state or priority changed')
            if current['issue']['body'] == plan['after']:
                state = 'readback-verified'
            elif current != plan['snapshot']:
                raise Rejected('backlog_conflict', 'Remote issue changed; preserve human edits')
            else:
                state = 'pending-external-write'
        event = {'state': state, 'at': now(), 'observed_digest': digest(current)}
    except Exception as error:
        event = {'state': 'conflict' if isinstance(error, Rejected) else 'unavailable', 'at': now(), 'detail': str(error)}
        intent['events'].append(event)
        _save(path, canonical(intent) + '\n')
        raise
    intent['events'].append(event)
    _save(path, canonical(intent) + '\n')
    return {'item': item['id'], 'state': state, 'last_observed_at': event['at'],
            'last_successful_reconciliation': next((e['at'] for e in reversed(intent['events']) if e['state'] == 'readback-verified'), None),
            'external_write_performed': False, 'intent': str(path)}
