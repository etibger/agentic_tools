import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from agentic_tools.aos.backlog import prepare, preview, reconcile, selected
from agentic_tools.aos.store import Rejected, digest


class Reader:
    def __init__(self):
        self.value = {'issue': {'url': 'https://github.com/example/tools/issues/34', 'title': 'Sync backlog',
                               'body': 'Backlog ID: `B01`. Original history.', 'state': 'OPEN', 'updatedAt': '2026-11-01T10:00:00Z'},
                      'project': {'owner': 'example', 'number': 1, 'item_id': 'ITEM1', 'status': 'Backlog',
                                  'urgency': 'Urgent', 'importance': 'Important'}}
        self.unavailable = False

    def read(self, item):
        if self.unavailable:
            raise OSError('offline')
        return copy.deepcopy(self.value)


@pytest.fixture
def setup(tmp_path):
    markdown = tmp_path / 'futures.md'
    markdown.write_text('# Maintained backlog\n\n| ID | Outcome | Issue | Disposition |\n| --- | --- | --- | --- |\n'
                        '| B01 | Synchronize existing records | [#34](https://github.com/example/tools/issues/34) | Open; preserve the original history. |\n'
                        '\nUnrelated human notes stay unchanged.\n')
    return markdown, tmp_path / 'intent.json', Reader()


def test_preview_has_no_target_write_and_local_apply_is_replay_safe(setup):
    markdown, intent, reader = setup
    original = markdown.read_text()
    plan = preview(markdown, 'B01', 'github-to-markdown', reader)
    prepare(intent, plan)
    assert markdown.read_text() == original
    assert reconcile(intent, reader)['state'] == 'pending-local-write'
    assert reconcile(intent, reader, apply_local=True)['state'] == 'readback-verified'
    updated = markdown.read_text()
    assert 'issue: OPEN; status: Backlog; urgency: Urgent; importance: Important' in updated
    assert 'Open; preserve the original history.' in updated
    before = markdown.stat().st_mtime_ns
    assert reconcile(intent, reader, apply_local=True)['state'] == 'readback-verified'
    assert markdown.stat().st_mtime_ns == before
    assert preview(markdown, 'B01', 'github-to-markdown', reader)['changed'] is False


@pytest.mark.parametrize('side', ['markdown', 'body', 'project', 'identity'])
def test_conflicting_edits_preserved_without_last_writer_wins(setup, side):
    markdown, intent, reader = setup
    prepare(intent, preview(markdown, 'B01', 'github-to-markdown', reader))
    if side == 'markdown':
        markdown.write_text(markdown.read_text() + 'New human note\n')
    elif side == 'body':
        reader.value['issue']['body'] += ' New remote human note.'
    elif side == 'identity':
        markdown.write_text(markdown.read_text().replace('/issues/34', '/issues/99'))
    else:
        reader.value['project']['status'] = 'Done'
    retained = markdown.read_text()
    with pytest.raises(Rejected, match='changed'):
        reconcile(intent, reader, apply_local=True)
    assert markdown.read_text() == retained
    assert json.loads(intent.read_text())['events'][-1]['state'] == 'conflict'


def test_outbound_is_only_preview_and_fresh_readback(setup):
    markdown, intent, reader = setup
    plan = preview(markdown, 'B01', 'markdown-to-github', reader)
    prepare(intent, plan)
    assert reconcile(intent, reader, apply_local=True)['state'] == 'pending-external-write'
    assert reader.value['issue']['body'] == plan['before']
    # Simulate the existing, separately authorized issue-maintenance route.
    reader.value['issue']['body'] = plan['after']
    reader.value['issue']['updatedAt'] = '2026-11-01T10:01:00Z'
    result = reconcile(intent, reader)
    assert result['state'] == 'readback-verified' and result['external_write_performed'] is False
    assert preview(markdown, 'B01', 'markdown-to-github', reader)['changed'] is False


def test_unavailable_then_retry_retains_failed_observation(setup):
    markdown, intent, reader = setup
    prepare(intent, preview(markdown, 'B01', 'github-to-markdown', reader))
    reader.unavailable = True
    with pytest.raises(OSError):
        reconcile(intent, reader, apply_local=True)
    reader.unavailable = False
    reconcile(intent, reader, apply_local=True)
    assert [event['state'] for event in json.loads(intent.read_text())['events']] == ['prepared', 'unavailable', 'readback-verified']


def test_expired_preview_rejected_without_extending_original_clock(setup):
    markdown, intent, reader = setup
    plan = preview(markdown, 'B01', 'github-to-markdown', reader)
    plan['observed_at'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    prepare(intent, plan)
    with pytest.raises(Rejected, match='15 minutes'):
        reconcile(intent, reader, apply_local=True)


def test_concurrent_same_intent_does_not_duplicate_target_update(setup):
    markdown, intent, reader = setup
    prepare(intent, preview(markdown, 'B01', 'github-to-markdown', reader))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reconcile(intent, reader, apply_local=True), range(2)))
    assert all(r['state'] == 'readback-verified' for r in results)
    assert markdown.read_text().count('data-aos-tracker="B01"') == 1
    assert len(json.loads(intent.read_text())['events']) == 3


def test_private_summary_duplicate_identity_and_changed_intent_rejected(setup):
    markdown, intent, reader = setup
    text = markdown.read_text()
    markdown.write_text(text.replace('Open;', 'Open; /Users/private/person;'))
    with pytest.raises(Rejected, match='private'):
        preview(markdown, 'B01', 'markdown-to-github', reader)
    row = next(r for r in text.splitlines() if r.startswith('| B01'))
    with pytest.raises(Rejected, match='Exactly one'):
        selected(text + row, 'B01')
    markdown.write_text(text)
    plan = preview(markdown, 'B01', 'github-to-markdown', reader)
    prepare(intent, plan)
    plan['after'] += 'different'
    with pytest.raises(Rejected, match='different preview'):
        prepare(intent, plan)


def test_exact_table_line_and_crlf_are_preserved(setup):
    markdown, intent, reader = setup
    original = markdown.read_text()
    row = selected(original, 'B01')['row']
    original = ('Earlier inline example: ' + row + '\n' + original).replace('\n', '\r\n')
    markdown.write_bytes(original.encode())
    prepare(intent, preview(markdown, 'B01', 'github-to-markdown', reader))
    reconcile(intent, reader, apply_local=True)
    after = markdown.read_bytes().decode()
    assert after.startswith('Earlier inline example: ' + row + '\r\n')
    assert after.count('\r\n') == original.count('\r\n')
    assert after.count('data-aos-tracker="B01"') == 1


@pytest.mark.parametrize('damage', ['incomplete_span', 'extra_open', 'extra_close', 'truncated_open'])
def test_malformed_managed_boundaries_are_held(setup, damage):
    markdown, intent, reader = setup
    if damage == 'incomplete_span':
        markdown.write_text(markdown.read_text().replace('Open;', 'Open; <span data-aos-tracker="B01">missing end'))
    else:
        plan = preview(markdown, 'B01', 'markdown-to-github', reader)
        boundary = {'extra_open': '<!-- aos-maintained:B01 -->',
                    'extra_close': '<!-- /aos-maintained:B01 -->',
                    'truncated_open': '<!-- aos-maintained:B01'}[damage]
        reader.value['issue']['body'] = plan['after'] + boundary
    with pytest.raises(Rejected, match='marker|block'):
        preview(markdown, 'B01', 'markdown-to-github', reader)
