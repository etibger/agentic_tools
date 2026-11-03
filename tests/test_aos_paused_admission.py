"""Admission cannot act as an implicit resume, including fresh-revision calls."""
import pytest

from agentic_tools.aos.store import Rejected
from test_aos_lifecycle import setup


@pytest.mark.parametrize('admitted', [False, True])
def test_current_revision_admission_cannot_clear_saved_pause(tmp_path, admitted):
    store, lifecycle, current = setup(tmp_path, admitted=admitted)
    lifecycle.control('pause', 'Q1', 'pause')
    before = lifecycle.snapshot('Q1')
    revision = store.read('Q1')['revision']
    with pytest.raises(Rejected) as error:
        lifecycle.admit('not-a-resume', 'Q1', expected_work_revision=revision,
                        expected_digest=current['digest'])
    assert error.value.code == 'execution_paused'
    after = lifecycle.snapshot('Q1')
    assert after['execution'] == 'paused'
    assert after['source_revision'] == revision
    assert after['resources'] == before['resources']
    assert after['assignments'] == before['assignments']
    assert after['technical_blockers'] == before['technical_blockers']


def test_paused_rejection_does_not_delete_existing_admission_blocker(tmp_path):
    store, lifecycle, current = setup(tmp_path, admitted=False,
                                      prerequisites={'q0': False, 'isolated_fixture': True})
    assert lifecycle.admit('not-ready', 'Q1')['execution'] == 'held'
    lifecycle.control('pause', 'Q1', 'pause')
    before = lifecycle.snapshot('Q1')
    assert any(item['code'] == 'admission' for item in before['technical_blockers'])
    with pytest.raises(Rejected, match='explicit resume'):
        lifecycle.admit('paused', 'Q1', expected_work_revision=store.read('Q1')['revision'],
                        expected_digest=current['digest'])
    after = lifecycle.snapshot('Q1')
    assert after['execution'] == 'paused' and after['technical_blockers'] == before['technical_blockers']


def test_late_pause_still_fences_stale_admission_by_revision(tmp_path):
    store, lifecycle, current = setup(tmp_path, admitted=False)
    old_revision = store.read('Q1')['revision']
    lifecycle.control('pause', 'Q1', 'pause')
    with pytest.raises(Rejected) as error:
        lifecycle.admit('stale-admit', 'Q1', expected_work_revision=old_revision,
                        expected_digest=current['digest'])
    assert error.value.code == 'stale_revision'
    assert lifecycle.snapshot('Q1')['execution'] == 'paused'
    assert lifecycle.assignments() == []


def test_explicit_resume_still_works_without_new_approval(tmp_path):
    store, lifecycle, current = setup(tmp_path)
    lifecycle.control('pause', 'Q1', 'pause')
    assignments = lifecycle.assignments()
    decisions = store.read('Q1')['decisions']
    result = lifecycle.control('resume', 'Q1', 'resume',
                               expected_work_revision=store.read('Q1')['revision'],
                               expected_digest=current['digest'])
    assert result['execution'] == 'eligible'
    assert lifecycle.assignments() == assignments and store.read('Q1')['decisions'] == decisions


def test_replayed_historical_admission_cannot_mutate_later_pause(tmp_path):
    _, lifecycle, _ = setup(tmp_path)
    lifecycle.control('pause', 'Q1', 'pause')
    before = lifecycle.snapshot('Q1')
    # Idempotent responses remain historical; callers read current state afterward.
    assert lifecycle.admit('admit-Q1', 'Q1')['execution'] == 'eligible'
    after = lifecycle.snapshot('Q1')
    assert after['execution'] == 'paused'
    assert after['source_revision'] == before['source_revision']
    assert after['assignments'] == before['assignments']
