"""Optional pytest evidence plugin; no test behavior changes beyond node-ID sharding."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from time import monotonic as _monotonic

import pytest


def shard_for(nodeid: str, count: int) -> int:
    """Assign every parameterized test to exactly one stable shard."""
    return int(hashlib.sha256(nodeid.encode()).hexdigest(), 16) % count


class Evidence:
    def __init__(self, directory: Path, index: int, count: int):
        self.directory, self.index, self.count = directory, index, count
        self.events = (directory / 'events.jsonl').open('x', encoding='utf-8')
        self.all_nodeids = []

    def emit(self, **event):
        self.events.write(json.dumps({'monotonic': _monotonic(), **event}) + '\n')
        self.events.flush()

    def pytest_collection_modifyitems(self, session, config, items):
        self.all_nodeids = [item.nodeid for item in items]
        selected, deselected = [], []
        for item in items:
            (selected if shard_for(item.nodeid, self.count) == self.index else deselected).append(item)
        items[:] = selected
        if deselected:
            config.hook.pytest_deselected(items=deselected)

    def pytest_collection_finish(self, session):
        value = {'all': self.all_nodeids, 'selected': [item.nodeid for item in session.items],
                 'shard_index': self.index, 'shard_count': self.count,
                 'collection_errors': session.testsfailed}
        temporary = self.directory / 'inventory.json.tmp'
        temporary.write_text(json.dumps(value, indent=2) + '\n')
        temporary.replace(self.directory / 'inventory.json')

    def pytest_collectreport(self, report):
        if report.failed:
            self.emit(event='collection_error', nodeid=report.nodeid, detail=str(report.longrepr))

    def pytest_runtest_logstart(self, nodeid, location):
        self.emit(event='start', nodeid=nodeid)

    def pytest_runtest_logreport(self, report):
        value = {'event': 'report', 'nodeid': report.nodeid, 'phase': report.when,
                 'outcome': report.outcome, 'duration': report.duration}
        if hasattr(report, 'wasxfail'):
            value['wasxfail'] = report.wasxfail
        if report.failed or report.skipped:
            value['detail'] = str(report.longrepr)
        self.emit(**value)

    def pytest_sessionfinish(self, session, exitstatus):
        self.emit(event='session_finish', exitstatus=int(exitstatus))

    def pytest_unconfigure(self, config):
        self.events.close()


def pytest_configure(config):
    directory = os.environ.get('AOS_CI_EVIDENCE_DIR')
    if not directory:
        return
    try:
        count = int(os.environ.get('AOS_CI_SHARD_COUNT', '1'))
        index = int(os.environ.get('AOS_CI_SHARD_INDEX', '0'))
        if count < 1 or not 0 <= index < count:
            raise ValueError
    except ValueError as exc:
        raise pytest.UsageError('Invalid AOS CI shard index/count') from exc
    config.pluginmanager.register(Evidence(Path(directory), index, count), 'aos-ci-evidence')
