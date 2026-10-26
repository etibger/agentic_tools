"""Local fake destination only; no external publication authority is exercised."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentic_tools.aos.archive import ArchiveJournal, LocalFakeDestination, preview
from agentic_tools.aos.projection import collect
from agentic_tools.aos.store import Rejected, canonical, digest
from test_aos_lifecycle import setup


def prepared(tmp_path):
    store, _, _ = setup(tmp_path, admitted=False)
    snapshot = collect(store, 'Q1')
    journal = ArchiveJournal(tmp_path / 'archive' / 'intent.sqlite3')
    destination = LocalFakeDestination(tmp_path / 'fake-destination' / 'pages.sqlite3')
    intent = journal.prepare('milestone-1', snapshot, 'q3-local', destination)
    return store, snapshot, journal, destination, intent


def test_exact_preview_intent_duplicate_retry_and_fresh_readback(tmp_path):
    store, snapshot, journal, destination, intent = prepared(tmp_path)
    package = preview(snapshot, 'q3-local')
    assert package['payload']['source'] == snapshot['source']
    assert package['payload']['source_digest'] == digest(snapshot['source'])
    assert package['destination'] is package['audience'] is None
    assert package['external_publication_authorized'] is False
    assert journal.prepare('milestone-1', snapshot, 'q3-local', destination) == intent
    first = journal.publish_local('milestone-1', destination)
    reopened = ArchiveJournal(journal.path)
    second = reopened.publish_local('milestone-1', LocalFakeDestination(destination.path))
    assert first['saved_version'] == second['saved_version'] == 1
    assert first['payload_digest'] == second['payload_digest']
    assert first['source_revision'] == snapshot['source_revision']
    assert second['external_publication'] is False
    assert len(reopened.read('milestone-1')['attempts']) == 2
    with sqlite3.connect(destination.path) as db:
        assert db.execute('SELECT COUNT(*) FROM page').fetchone()[0] == 1
    assert collect(store, 'Q1')['source_digest'] == snapshot['source_digest']


def test_conflicting_edit_never_overwrites_and_failure_is_retained(tmp_path):
    _, _, journal, destination, _ = prepared(tmp_path)
    journal.publish_local('milestone-1', destination)
    with sqlite3.connect(destination.path) as db:
        db.execute("UPDATE page SET payload='conflicting local edit',version=2")
    with pytest.raises(Rejected, match='archive_conflict'):
        journal.publish_local('milestone-1', destination)
    assert destination.read('Q1:q3-local')['payload'] == 'conflicting local edit'
    attempts = journal.read('milestone-1')['attempts']
    assert [a['state'] for a in attempts] == ['verified-local', 'failed']
    assert attempts[0]['receipt']['saved_version'] == 1


@pytest.mark.parametrize('stage', ['before_destination_write', 'after_destination_write_before_commit'])
def test_failed_destination_write_rolls_back_then_retry_is_one_page(tmp_path, stage):
    _, _, journal, destination, _ = prepared(tmp_path)
    destination._fault = lambda s: (_ for _ in ()).throw(OSError('injected disk write failure')) if s == stage else None
    with pytest.raises(OSError):
        journal.publish_local('milestone-1', destination)
    assert destination.read('Q1:q3-local') is None
    destination._fault = lambda _: None
    journal.publish_local('milestone-1', destination)
    assert [a['state'] for a in journal.read('milestone-1')['attempts']] == ['failed', 'verified-local']


@pytest.mark.parametrize('stage', ['after_attempt_before_destination', 'after_destination_before_readback', 'after_readback_before_receipt', 'after_receipt_write_before_commit'])
def test_interruption_retains_unknown_attempt_and_retry_reads_same_saved_version(tmp_path, stage):
    _, _, journal, destination, _ = prepared(tmp_path)
    journal._fault = lambda s: (_ for _ in ()).throw(KeyboardInterrupt('simulated process interruption')) if s == stage else None
    with pytest.raises(KeyboardInterrupt):
        journal.publish_local('milestone-1', destination)
    reopened = ArchiveJournal(journal.path)
    assert reopened.read('milestone-1')['attempts'][0]['state'] == 'started'
    receipt = reopened.publish_local('milestone-1', destination)
    assert receipt['readback'] == 'verified' and receipt['saved_version'] == 1
    assert [a['state'] for a in reopened.read('milestone-1')['attempts']] == ['started', 'verified-local']


@pytest.mark.parametrize('stage', ['before_intent_write', 'after_intent_write_before_commit'])
def test_interrupted_intent_write_never_claims_prepared_or_published(tmp_path, stage):
    store, _, _ = setup(tmp_path, admitted=False)
    journal = ArchiveJournal(tmp_path / 'intents.sqlite3')
    destination = LocalFakeDestination(tmp_path / 'pages.sqlite3')
    journal._fault = lambda s: (_ for _ in ()).throw(KeyboardInterrupt()) if s == stage else None
    with pytest.raises(KeyboardInterrupt):
        journal.prepare('intent', collect(store, 'Q1'), 'q3', destination)
    with pytest.raises(Rejected, match='archive_intent_missing'):
        ArchiveJournal(journal.path).read('intent')
    assert destination.read('Q1:q3') is None


def test_readback_mismatch_retains_failed_attempt_and_saved_bytes(tmp_path, monkeypatch):
    _, _, journal, destination, intent = prepared(tmp_path)
    original = destination.read
    monkeypatch.setattr(destination, 'read', lambda key: {**original(key), 'version': 200})
    with pytest.raises(Rejected, match='archive_readback_failed'):
        journal.publish_local('milestone-1', destination)
    assert journal.read('milestone-1')['attempts'][0]['state'] == 'failed'
    assert original('Q1:q3-local')['payload'] == canonical(intent['payload'])


def test_changed_source_duplicate_intent_and_nonlocal_destination_rejected(tmp_path):
    store, snapshot, journal, destination, _ = prepared(tmp_path)
    with pytest.raises(Rejected, match='archive_intent_conflict'):
        journal.prepare('different-intent', snapshot, 'q3-local', destination)
    with store._transaction() as db:
        db.execute('UPDATE work SET revision=revision+1')
    with pytest.raises(Rejected, match='archive_intent_conflict'):
        journal.prepare('milestone-1', collect(store, 'Q1'), 'q3-local', destination)
    with pytest.raises(Rejected, match='local_destination_required'):
        journal.publish_local('milestone-1', object())
    snapshot['collector']['health'] = 'failed'
    with pytest.raises(Rejected, match='archive_source_unavailable'):
        preview(snapshot, 'q3')


def test_archive_paths_reject_authoritative_ledger_without_changing_it(tmp_path):
    store, _, _, _, _ = prepared(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(Rejected, match='archive_database_scope'):
        ArchiveJournal(store.path)
    with pytest.raises(Rejected, match='archive_database_scope'):
        LocalFakeDestination(store.path)
    assert store.path.read_bytes() == before


def test_local_archive_cli_round_trip_is_explicitly_nonexternal(tmp_path, capsys):
    from agentic_tools.aos.cli import main
    store, _, _ = setup(tmp_path, admitted=False)
    journal, destination = tmp_path / 'intent.sqlite3', tmp_path / 'destination.sqlite3'
    common = ['--state-dir', str(store.directory), '--worker-writable-root', str(tmp_path / 'fixture'), 'Q1', '--milestone', 'q3']
    assert main(['archive-preview', *common]) == 0
    assert json.loads(capsys.readouterr().out)['external_publication_authorized'] is False
    assert main(['archive-prepare-local', *common, '--journal', str(journal), '--intent-id', 'intent', '--fake-destination', str(destination)]) == 0
    assert 'local-intent-only' in json.loads(capsys.readouterr().out)['status']
    assert main(['archive-publish-local', '--journal', str(journal), '--intent-id', 'intent', '--fake-destination', str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)['external_publication'] is False
    assert main(['archive-read-local', '--journal', str(journal), '--intent-id', 'intent']) == 0
    assert json.loads(capsys.readouterr().out)['attempts'][0]['state'] == 'verified-local'


def test_concurrent_identical_retry_has_one_destination_version(tmp_path):
    _, _, journal, destination, _ = prepared(tmp_path)
    def publish(_):
        return ArchiveJournal(journal.path).publish_local('milestone-1', LocalFakeDestination(destination.path))
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(publish, range(2)))
    assert [r['saved_version'] for r in receipts] == [1, 1]
    assert [a['state'] for a in journal.read('milestone-1')['attempts']] == ['verified-local', 'verified-local']
