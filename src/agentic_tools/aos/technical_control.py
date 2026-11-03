"""Small, revision-bound control records for private native qualification.

The full qualification archive remains the historical evidence. Its mutation
invalidates executable controls, including writes by an older producer.
"""
from __future__ import annotations

import copy
import json

from .store import Rejected, canonical, digest

FORMAT = 'aos-technical-phase-control-v1'
MAX_BYTES = 2 * 1024 * 1024
IMMUTABLE = ('id', 'input_digest', 'recipe_digest', 'recipe', 'started_at', 'max_seconds',
             'deadline', 'started_monotonic', 'deadline_monotonic', 'resource_id', 'output')
MUTABLE = ('state', 'execution_started', 'observer_started', 'observer_owner', 'observer_joined',
           'execution_errors', 'resource_receipt', 'hold_receipt')
SCHEMA = {
    'technical_archive_revision': 'CREATE TABLE technical_archive_revision (id TEXT PRIMARY KEY, revision INTEGER NOT NULL)',
    'technical_phase_control': 'CREATE TABLE technical_phase_control (qualification_id TEXT NOT NULL, phase_id TEXT NOT NULL, active INTEGER NOT NULL, archive_revision INTEGER NOT NULL, record TEXT NOT NULL, PRIMARY KEY(qualification_id,phase_id))',
    'technical_active_phase_v1': 'CREATE UNIQUE INDEX technical_active_phase_v1 ON technical_phase_control(qualification_id) WHERE active=1',
}
TRIGGERS = {
    'technical_archive_insert_v1': "CREATE TRIGGER technical_archive_insert_v1 AFTER INSERT ON technical_qualification BEGIN INSERT INTO technical_archive_revision VALUES(NEW.id,1) ON CONFLICT(id) DO UPDATE SET revision=revision+1; END",
    'technical_archive_update_v1': "CREATE TRIGGER technical_archive_update_v1 AFTER UPDATE ON technical_qualification BEGIN INSERT INTO technical_archive_revision VALUES(NEW.id,1) ON CONFLICT(id) DO UPDATE SET revision=revision+1; END",
    'technical_archive_delete_v1': "CREATE TRIGGER technical_archive_delete_v1 AFTER DELETE ON technical_qualification BEGIN INSERT INTO technical_archive_revision VALUES(OLD.id,1) ON CONFLICT(id) DO UPDATE SET revision=revision+1; END",
}


def initialize(db):
    existing = db.execute("SELECT 1 FROM sqlite_master WHERE name GLOB 'technical_archive_*' "
                          "OR name IN ('technical_phase_control','technical_active_phase_v1') LIMIT 1").fetchone()
    if existing:
        verify(db)
        return
    for sql in SCHEMA.values():
        db.execute(sql)
    db.execute('INSERT INTO technical_archive_revision SELECT id,0 FROM technical_qualification')
    for sql in TRIGGERS.values():
        db.execute(sql)


def verify(db):
    expected = {**SCHEMA, **TRIGGERS}
    found = {name: sql for name, sql in db.execute(
        "SELECT name,sql FROM sqlite_master WHERE name GLOB 'technical_archive_*' "
        "OR name IN ('technical_phase_control','technical_active_phase_v1') "
        "OR (type='trigger' AND tbl_name IN ('technical_qualification','technical_phase_control','technical_archive_revision'))")}
    if found != expected:
        raise Rejected('technical_control', 'The exact private phase control schema is incomplete or changed')


def revision(db, qualification_id):
    row = db.execute('SELECT revision FROM technical_archive_revision WHERE id=?', (qualification_id,)).fetchone()
    if not row or type(row[0]) is not int or row[0] < 0:
        raise Rejected('technical_control', 'The qualification archive revision is missing')
    return row[0]


def binding(phase):
    return digest({key: phase[key] for key in IMMUTABLE})


def load(db, qualification_id, phase_id=None, *, active_only=False, historical=False):
    verify(db)
    where, params = 'qualification_id=?', [qualification_id]
    if phase_id is not None:
        where += ' AND phase_id=?'
        params.append(phase_id)
    if active_only or phase_id is None:
        where += ' AND active=1'
    rows = db.execute('SELECT phase_id,active,archive_revision,record FROM technical_phase_control WHERE ' + where, params).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise Rejected('technical_control', 'Ambiguous active private phase control')
    key, active, epoch, raw = rows[0]
    if len(raw.encode()) > MAX_BYTES:
        raise Rejected('technical_control', 'Private phase control exceeds its bound')
    control = json.loads(raw)
    phase = control.get('phase', {})
    current_revision = revision(db, qualification_id)
    if (control.get('format') != FORMAT or control.get('qualification_id') != qualification_id
            or phase.get('id') != key or active not in (0, 1)
            or control.get('active') is not bool(active)
            or type(epoch) is not int or epoch < 0 or epoch > current_revision
            or type(control.get('archive_revision')) is not int
            or control.get('archive_revision') != epoch
            or (epoch != current_revision and not (historical and active == 0))
            or control.get('archive_binding') != binding(phase)
            or phase.get('recipe_digest') != digest(phase.get('recipe'))
            or phase.get('state') not in ('prepared', 'running', 'closing', 'stopped', 'cleanup_unknown')
            or bool(active) != (phase.get('state') != 'stopped')):
        raise Rejected('technical_control', 'Private phase control differs from its exact archive binding')
    return control


def save(db, qualification_id, phase, *, expected=None):
    """Call only inside the admission/transition writer transaction."""
    verify(db)
    if not db.in_transaction:
        raise Rejected('technical_control', 'A phase control transition needs its owning transaction')
    if expected is not None:
        current = load(db, qualification_id, phase['id'])
        if current != expected or binding(phase) != expected['archive_binding']:
            raise Rejected('technical_control', 'Private phase control changed before transition')
    selected = {key: copy.deepcopy(phase[key]) for key in (*IMMUTABLE, *MUTABLE) if key in phase}
    control = {'format': FORMAT, 'qualification_id': qualification_id,
               'archive_revision': revision(db, qualification_id), 'archive_binding': binding(phase),
               'active': phase['state'] != 'stopped', 'phase': selected}
    raw = canonical(control)
    if len(raw.encode()) > MAX_BYTES:
        raise Rejected('technical_control', 'Private phase control exceeds its bound')
    db.execute('INSERT INTO technical_phase_control VALUES(?,?,?,?,?) ON CONFLICT(qualification_id,phase_id) DO UPDATE SET active=excluded.active,archive_revision=excluded.archive_revision,record=excluded.record',
               (qualification_id, phase['id'], int(control['active']), control['archive_revision'], raw))
    return control


def check_archive(control, phase):
    if control['archive_binding'] != binding(phase):
        raise Rejected('technical_control', 'The full phase archive differs from the retained control binding')


def check_completed_archive(db, qualification_id, record):
    """A legacy overwrite cannot erase an independently retained stopped phase."""
    if record is None:
        if db.execute('SELECT 1 FROM technical_phase_control WHERE qualification_id=? LIMIT 1',
                      (qualification_id,)).fetchone():
            raise Rejected('technical_control', 'The archive is missing while retained phase controls remain')
        return
    phases = record.get('phases', [])
    if not isinstance(phases, list) or len({phase.get('id') for phase in phases}) != len(phases):
        raise Rejected('technical_control', 'The archive has duplicate or missing phase identities')
    archived = {phase.get('id'): phase for phase in phases}
    rows = db.execute('SELECT phase_id FROM technical_phase_control WHERE qualification_id=? AND active=0',
                      (qualification_id,)).fetchall()
    for (phase_id,) in rows:
        control = load(db, qualification_id, phase_id, historical=True)
        phase = archived.get(phase_id)
        if (control is None or phase is None or phase.get('state') != 'stopped'
                or phase.get('receipt', {}).get('stopped') is not True):
            raise Rejected('technical_control', 'The archive omitted or changed a retained stopped phase')
        check_archive(control, phase)
