"""One explicitly started foreground owner of preparation, decision and execution.

The AOS ledger remains the only lifecycle. The immutable prepared packet binds
inputs across restarts; it is not another progress journal or approval source.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

from . import project
from .approval_web import DecisionService
from .readiness import _runtime_identity
from .coordinator import prepare_decision, register_contract
from .lifecycle import Lifecycle, TERMINAL_EXECUTIONS
from .store import Rejected, Store, canonical, digest, identifier
from .worker import Worker

FORMAT = 'aos-supervisor-v1'
PACKET_FORMAT = 'aos-supervisor-prepared-v1'


def _emit(value: dict) -> None:
    print(json.dumps(value, separators=(',', ':')), flush=True)


def _operation(work_id: str, concept_digest: str, action: str) -> str:
    return 'supervisor-' + action + '-' + digest([work_id, concept_digest, action])[7:39]


def _package(concept: dict) -> dict:
    result = {'format': 'aos-concept-v1', **{key: concept[key] for key in (
        'work_id', 'package_id', 'revision', 'body', 'acceptance', 'team_envelope', 'authority')}}
    result.update({key: concept[key] for key in ('recommendation', 'alternative', 'consequence',
        'execution_envelope', 'decision_context') if concept.get(key) is not None})
    return result


def _read(store: Store, work_id: str) -> dict | None:
    try:
        return store.read(work_id)
    except Rejected as error:
        if error.code != 'work_not_found':
            raise
        return None


def _saved_contract(store: Store, work_id: str) -> dict | None:
    with store._connection() as db:
        row = db.execute('SELECT * FROM lifecycle WHERE work_id=?', (work_id,)).fetchone()
        return {**dict(row), 'contract': json.loads(row['contract'])} if row else None


def _retain(path: Path, packet: dict) -> None:
    value = {'format': PACKET_FORMAT, 'packet_digest': digest(packet), 'packet': packet}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, 'w') as stream:
        stream.write(canonical(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _retained(path: Path, bindings: dict, config: dict, request: dict) -> dict | None:
    if not path.exists() and not path.is_symlink():
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o400 or info.st_nlink != 1
                or info.st_size > 64 * 1024 * 1024):
            raise Rejected('supervisor_packet', 'Prepared packet must remain a private immutable regular file')
        value = json.load(stream)
    packet = value.get('packet') if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get('format') != PACKET_FORMAT or not isinstance(packet, dict)
            or value.get('packet_digest') != digest(packet)
            or packet.get('ready') is not True or packet.get('input_bindings') != bindings
            or packet.get('project') != config or packet.get('introduction') != request['introduction']
            or packet.get('approval_recorded') is not False or packet.get('reservation_performed') is not False):
        raise Rejected('supervisor_packet', 'Prepared packet or kickoff inputs changed; saved approvals are retained')
    concept = packet.get('concept', {})
    if ({k: v for k, v in concept.items() if k != 'execution_envelope'}
            != {k: v for k, v in request['concept'].items() if k != 'execution_envelope'}):
        raise Rejected('supervisor_packet', 'Prepared feature request changed')
    bounds = concept.get('execution_envelope', {})
    if bounds.get('runtime_recipe_digest') != digest(bounds.get('runtime_recipe')):
        raise Rejected('supervisor_packet', 'Prepared runtime recipe is inconsistent')
    if packet.get('readiness', {}).get('runtime_identity') != _runtime_identity():
        raise Rejected('supervisor_runtime_changed', 'Pinned runtime identity changed since readiness; retained work needs reviewed preparation')
    return packet


def _exact(record: dict, package: dict) -> bool:
    return (record['current_concept'] == package['revision']
            and any(c['revision'] == package['revision'] and c['package'] == package
                    and c['digest'] == digest(package) for c in record['concepts']))


def _bootstrap(store: Store, lifecycle: Lifecycle, packet: dict, fault) -> str:
    intro, concept = packet['introduction'], packet['concept']
    work_id, package = intro['work_id'], _package(concept)
    target = digest(package)
    record = _read(store, work_id)
    if record is None:
        store.introduce(**intro)
        fault('after_introduction')
        record = store.read(work_id)
    expected_intro = {'structure': intro['structure'], 'team': intro['team'], 'team_status': 'proposed'}
    if record['title'] != intro['title'] or record['introduction'] != expected_intro:
        raise Rejected('supervisor_identity', 'Work identity belongs to a different introduction')
    if record['current_concept'] is None:
        # The immutable request retains the exact original compare-and-set input.
        store.present(**concept)
        fault('after_presentation')
        record = store.read(work_id)
    if not _exact(record, package):
        raise Rejected('supervisor_superseded', 'The prepared concept is no longer current; no previous approval is reused')
    current = _saved_contract(store, work_id)
    expected = {**concept['execution_envelope'], 'concept_revision': concept['revision'], 'concept_digest': target}
    if current is None:
        record = store.read(work_id)
        register_contract(lifecycle, _operation(work_id, target, 'register'), work_id, record['revision'], target)
        fault('after_registration')
    elif current['contract'] != expected:
        raise Rejected('supervisor_contract', 'Saved execution contract differs from the prepared package')
    return target


def _runtime(lifecycle: Lifecycle, recipe: dict, work_id: str):
    from .codex_runtime import CodexRuntime
    return CodexRuntime(lifecycle, {work_id: recipe}, work_id=work_id)


def _decision(store: Store, record: dict, target: str) -> dict | None:
    decisions = [d for d in record['decisions'] if d['digest'] == target
                 and d['concept_revision'] == record['current_concept']
                 and d['outcome'] in {'approved', 'declined', 'cancelled'}]
    if not decisions:
        return None
    decision = decisions[-1]
    store.verify_decision(decision['decision_id'])
    return decision


def _result(work_id: str, state: str, *, detail: str = '', next_action: str = '', **extra) -> dict:
    result = {'format': FORMAT, 'work_id': work_id, 'state': state, **extra}
    if detail:
        result['detail'] = detail
    if next_action:
        result['next_action'] = next_action
    return result


def _busy(error: BaseException) -> bool:
    """Only explicit SQLite contention causes are safely retried, never bad evidence."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, sqlite3.OperationalError):
            code = getattr(error, 'sqlite_errorcode', None)
            return type(code) is int and code & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        error = error.__cause__
    return False


def _drive(store, lifecycle, worker, packet, service, runtime_factory, interval, stop_event, emit, fault):
    work_id = packet['introduction']['work_id']
    package = _package(packet['concept'])
    target = digest(package)
    recipe = packet['concept']['execution_envelope']['runtime_recipe']
    last = None
    contention = 0
    while True:
        if stop_event.is_set():
            raise KeyboardInterrupt
        service.check()
        try:
            record = store.read(work_id)
            if not _exact(record, package):
                return _result(work_id, 'superseded', detail='A different concept is current; saved work was not resumed')
            run = _saved_contract(store, work_id)
            view = record['lifecycle']
            if view['execution'] in TERMINAL_EXECUTIONS:
                return _result(work_id, view['execution'], delivery=view.get('delivery'))
            if view['control'] != 'none':
                if worker.runtime is None:
                    worker.attach_runtime(runtime_factory(lifecycle, recipe, work_id))
                # The existing worker enumerates every saved cancellation attempt,
                # including conflicting terminal stops, and asks the adapter for
                # each exact observation. Never fabricate an empty cleanup list.
                worker.tick()
                observed = store.read(work_id)
                if observed['execution'] == 'cancelled':
                    return _result(work_id, 'cancelled')
                return _result(work_id, 'held', detail='Saved execution control requires exact owned reconciliation',
                               next_action='Inspect the retained owned-stop evidence; do not start a replacement job')
            # Re-read the same decision after a lost browser reply. No new answer is requested.
            decision = _decision(store, record, target)
            if decision and decision['outcome'] != 'approved':
                return _result(work_id, decision['outcome'], decision_id=decision['decision_id'])
            if decision:
                lifecycle.reconcile(work_id)
                record = store.read(work_id)
                view = record['lifecycle']
            blockers = view['technical_blockers']
            if blockers:
                first = blockers[0]
                return _result(work_id, 'held', detail=first['detail'], next_action=first['next_action'],
                               blocker=first['code'])
            if view['execution'] == 'paused':
                if not any(a['state'] != 'proposed' for a in worker.assignments()):
                    return _result(work_id, 'paused', next_action='Resume explicitly when ready')
                if worker.runtime is None:
                    worker.attach_runtime(runtime_factory(lifecycle, recipe, work_id))
                # Pause fences claims, not completion of the already-running job.
                # Existing tick preserves its original deadline and queues (but
                # cannot launch) its successor under the same saved approval.
                worker.tick()
                fault('after_tick')
                observed = store.read(work_id)
                current = observed['lifecycle']
                if (observed['execution'] != 'paused' or current['control'] != 'none'
                        or current['technical_blockers']):
                    continue
                if all(a['state'] == 'proposed' or worker._unlaunched_intent(a) for a in worker.assignments()):
                    return _result(work_id, 'paused', next_action='Resume explicitly when ready')
                progress = _result(work_id, 'draining',
                    detail='Pause is saved; the current assignment is finishing within its original limit')
            elif not decision:
                # Registration is complete and the listener is live before this mutation.
                pending = [r for r in store.pending_approvals() if r['work_id'] == work_id and r['digest'] == target]
                current = [r for r in pending if r['current']]
                if len(current) > 1 or pending and not current:
                    return _result(work_id, 'held', detail='The retained approval challenge is stale or ambiguous',
                                   next_action='Inspect the exact saved proposal; no duplicate approval was created')
                if current:
                    challenge = current[0]['challenge_id']
                else:
                    record = store.read(work_id)
                    answer = prepare_decision(store, _operation(work_id, target, 'decision'), work_id,
                                              record['revision'], target)
                    challenge = answer.get('challenge_id')
                    fault('after_challenge')
                    if not challenge:
                        continue  # A saved decision won the race; read it on the next iteration.
                progress = _result(work_id, 'pending', decision_url=service.origin + '/?challenge=' + challenge,
                                   detail='Approve this bounded feature once; this owner will continue automatically')
            else:
                if not run['admitted']:
                    record = store.read(work_id)
                    if packet.get('assessment_review_continuation'):
                        from .assessment_review_continuation import admit
                        admit(lifecycle, packet)
                    elif packet.get('assessment_continuation'):
                        from .assessment_continuation import admit
                        admit(lifecycle, packet)
                    else:
                        lifecycle.admit(_operation(work_id, target, 'admit'), work_id,
                                        expected_work_revision=record['revision'], expected_digest=target)
                    fault('after_admission')
                    record = store.read(work_id)
                    if record['lifecycle']['technical_blockers'] or record['execution'] == 'paused':
                        continue
                if worker.runtime is None:
                    worker.attach_runtime(runtime_factory(lifecycle, recipe, work_id))
                    fault('after_runtime_attachment')
                worker.tick()
                fault('after_tick')
                observed = store.read(work_id)
                if observed['execution'] in TERMINAL_EXECUTIONS or observed['execution'] in {'paused', 'held'}:
                    continue
                progress = _result(work_id, 'running', decision_id=decision['decision_id'],
                                   detail='Implementation and independent assessment are owned by this process')
            contention = 0
        except (sqlite3.OperationalError, Rejected) as error:
            if not _busy(error) or contention >= 2:
                raise
            contention += 1
            stop_event.wait(min(interval, .1 * contention))
            continue
        if progress != last:
            emit(progress)
            last = progress
        worker.heartbeat()
        stop_event.wait(interval)


def _directories(project_path: Path, request_path: Path, bindings: dict, *, state_only: bool = False) -> None:
    """Use the project's exact static plan; this never invokes host readiness."""
    plan = project.static_plan(project_path, request_path)
    if plan.get('input_bindings') != bindings:
        raise Rejected('project_input_drift', 'Kickoff inputs changed before directory initialization')
    blocker = plan.get('blocker')
    if blocker and blocker.get('code') != 'project_directories':
        raise Rejected(blocker['code'], blocker['detail'])
    if plan.get('required_directories'):
        result = project.prepare_directories(project_path, request_path, plan=plan, state_only=state_only)
        if result.get('prepared') is not True:
            blocker = result.get('blocker') or {'code': 'project_directories', 'detail': 'Exact directory initialization failed'}
            raise Rejected(blocker['code'], blocker['detail'])


def start(project_path: Path, request_path: Path, *, continuation_path: Path | None = None, prepare=None, runtime_factory=None,
          service_factory=None, interval: float = .25, stop_event=None, emit=None, fault=None) -> dict:
    """Own one exact feature until delivery or an actionable stop.

    Optional Python callables are trusted test seams, never project JSON fields.
    The foreground command installs no persistent service and sends no chat messages.
    """
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not .01 <= interval <= 30:
        raise Rejected('supervisor_interval', 'The observation interval must be between .01 and 30 seconds')
    prepare = prepare or project.prepare
    runtime_factory = runtime_factory or _runtime
    service_factory = service_factory or DecisionService
    emit = emit or _emit
    fault = fault or (lambda _stage: None)
    stop_event = stop_event or threading.Event()
    config, project_binding = project._load(Path(project_path), project.ProjectConfiguration)
    request, request_binding = project._load(Path(request_path), project.WorkRequest)
    work_id = identifier(request['introduction']['work_id'])
    if request['concept']['work_id'] != work_id:
        raise Rejected('supervisor_identity', 'Introduction and concept must identify the same work')
    # The source checkout is an input. Only the project's validated directory
    # plan may initialize state/output paths; no recursive or guessed mkdir.
    project.native_paths._canonical(config['recipe'].get('source'), directory=True)
    state = project.native_paths._canonical(config['state_dir'])
    roots = tuple(project.native_paths._canonical(p) for p in config['worker_writable_roots'])
    if set(map(str, roots)) != {config['recipe'].get('source'), config['recipe'].get('outputs')}:
        raise Rejected('supervisor_scope', 'Only the exact isolated source and output roots may be worker writable')
    bindings = {'project': project_binding, 'request': request_binding}
    if not state.exists():
        try:
            # Establish only the exact private lock home from a full valid plan.
            # All remaining directory creation and host readiness happen owned.
            _directories(Path(project_path), Path(request_path), bindings, state_only=True)
        except (Rejected, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            return _result(work_id, 'held', blocker=getattr(error, 'code', 'project_directories'),
                           detail=str(error), next_action='Resolve the declared-directory issue and repeat the same start command')
    project.native_paths._canonical(str(state), directory=True)
    project._private(state)
    store = Store(state, roots)
    lifecycle = Lifecycle(store)
    retained_path = state / ('prepared-' + work_id + '.json')
    service = None
    packet = None
    target = None
    result = None
    cleanup = {'owned_stop': {'known_stopped': True, 'unresolved_attempts': []},
               'runtime_closed': True, 'decision_service': {'listener_stopped': True, 'requests_stopped': True}}
    with Worker(lifecycle, work_id=work_id, reconcile_on_enter=False) as worker:
        try:
            if continuation_path is not None:
                from .assessment_continuation import _file, load
                if _file(Path(continuation_path))[0].get('assessment_review_continuation'):
                    from .assessment_review_continuation import load
                packet = load(Path(continuation_path), bindings, config, request)
            else:
                packet = _retained(retained_path, bindings, config, request)
            if packet is None:
                if _read(store, work_id) is not None:
                    raise Rejected('supervisor_unbound', 'Existing work has no retained preparation from this start command')
                _directories(Path(project_path), Path(request_path), bindings)
                packet = prepare(Path(project_path), Path(request_path))
                if packet.get('ready') is not True:
                    blocker = packet.get('blocker') or {'code': 'readiness_failed', 'detail': 'Host readiness is incomplete'}
                    raise Rejected(blocker['code'], blocker['detail'])
                _retain(retained_path, packet)
                # Validate the retained bytes and inputs before any concept exists.
                packet = _retained(retained_path, bindings, config, request)
                fault('after_preparation')
            # A retained run must not recreate missing output/evidence roots.
            for root in roots:
                project.native_paths._canonical(str(root), directory=True)
            if packet.get('assessment_review_continuation'):
                from .assessment_review_continuation import publish
                target = publish(lifecycle, packet)
            elif packet.get('assessment_continuation'):
                from .assessment_continuation import publish
                target = publish(lifecycle, packet)
            else:
                target = _bootstrap(store, lifecycle, packet, fault)
            # Do not materialize a pre-challenge projection. begin_approval adds
            # the pending challenge without advancing the work revision, so that
            # earlier view would immediately be stale at the same revision.
            # _drive reconciles the saved decision before admission/dispatch;
            # Worker.tick also retains its compulsory reconciliation gate.
            record = store.read(work_id)
            if (record['execution'] in TERMINAL_EXECUTIONS or record['execution'] == 'paused'
                    and not any(a['state'] != 'proposed' for a in worker.assignments())):
                result = _result(work_id, record['execution'], delivery=record['lifecycle'].get('delivery'))
            else:
                service = service_factory(store, **config['decision_service'], work_id=work_id, digest=target,
                    status=lambda: {'stage': store.read(work_id)['execution']})
                service.__enter__()
                service.check()
                fault('after_listener')
                result = _drive(store, lifecycle, worker, packet, service, runtime_factory, interval,
                                stop_event, emit, fault)
        except KeyboardInterrupt:
            record = _read(store, work_id)
            if (target and record and _saved_contract(store, work_id)
                    and record['execution'] not in TERMINAL_EXECUTIONS and record['execution'] != 'paused'):
                lifecycle.pause_for_owner_interrupt(work_id)
            result = _result(work_id, 'interrupted', detail='Foreground owner stopped; saved work and decisions are retained',
                             next_action='Resume paused work explicitly before starting the same owner again')
        except (Rejected, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            result = _result(work_id, 'superseded' if getattr(error, 'code', '') == 'supervisor_superseded' else 'held',
                             detail=str(error), blocker=getattr(error, 'code', 'supervisor_unavailable'),
                             next_action='Inspect this retained issue; repeat the same start command only after it is resolved')
        finally:
            # Keep the single-owner lock through exact cancellation, runtime close,
            # and listener/request-thread joins. close() alone is never cleanup proof.
            try:
                active = [a for a in worker.assignments() if a['state'] != 'proposed']
                if active and worker.runtime is None and target is not None:
                    recipe = packet['concept']['execution_envelope']['runtime_recipe']
                    worker.attach_runtime(runtime_factory(lifecycle, recipe, work_id))
                if target is not None or worker.runtime is not None:
                    cleanup['owned_stop'] = worker.stop_owned(
                        preserve_absent_intents=bool(result and result['state'] == 'paused'))
                elif active:
                    cleanup['owned_stop'] = {'known_stopped': False,
                        'unresolved_attempts': [a['attempt_id'] for a in active],
                        'detail': 'Unbound retained work was not adopted or changed'}
            except Exception as error:
                cleanup['owned_stop'] = {'known_stopped': False, 'detail': str(error)}
            if worker.runtime is not None:
                try:
                    close = getattr(worker.runtime, 'close', None)
                    if close:
                        close()
                except Exception as error:
                    cleanup['runtime_closed'] = False
                    cleanup['runtime_error'] = str(error)
            if service is not None:
                try:
                    cleanup['decision_service'] = service.close()
                except Exception as error:
                    cleanup['decision_service'] = {**getattr(service, 'cleanup', {}), 'detail': str(error)}
            if result is not None:
                result['cleanup'] = cleanup
                if (not cleanup['owned_stop'].get('known_stopped') or not cleanup['runtime_closed']
                        or not cleanup['decision_service'].get('listener_stopped')
                        or not cleanup['decision_service'].get('requests_stopped')):
                    result.update(state='held', detail='Owned cleanup is incomplete; retained ownership prevents another launch',
                                  next_action='Reconcile the exact retained owner and effects; do not start a replacement job')
    return result
