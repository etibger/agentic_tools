"""Exclusive, journaled ownership of exact temporary roots for native checks.

The trusted runtime supplies the durable save callback and calls remove only
after its existing census/no-launch verification. This module never adopts a
directory, infers ownership from a name, recursively deletes, or launches work.
"""
from __future__ import annotations

import copy
import os
import stat
from pathlib import Path

from . import native_sandbox
from .store import Rejected, digest, identifier, now


FORMAT = 'aos-native-owned-temporary-v1'


def _identity(info):
    return {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
            'mode': stat.S_IMODE(info.st_mode),
            'birth_ns': getattr(info, 'st_birthtime_ns', int(info.st_birthtime * 1e9)
                                if hasattr(info, 'st_birthtime') else None)}


def _contract(recipe, *, validate=True):
    value = (native_sandbox.config(recipe) if validate else recipe.get('native_sandbox')) or {}
    if not isinstance(value, dict):
        raise Rejected('native_temporary_journal', 'Retained temporary contract must be an object')
    return value.get('owned_temporary_roots', []), digest(value)


def _parent(path, expected=None):
    native_sandbox._path(str(path))
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        identity = _identity(os.fstat(fd))
        if _identity(path.parent.stat()) != identity or (expected is not None and expected != identity):
            raise Rejected('native_temporary_identity', 'Owned temporary parent identity changed')
        return fd, identity
    except BaseException:
        os.close(fd)
        raise


def _at(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _owned(root):
    path = Path(root['path'])
    parent_fd, _ = _parent(path, root['parent'])
    try:
        info = _at(parent_fd, path.name)
        if (info is None or not stat.S_ISDIR(info.st_mode) or _identity(info) != root.get('witness')
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise Rejected('native_temporary_identity', 'Exact witnessed temporary directory is missing or replaced')
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def _save(save, receipt):
    if not callable(save):
        raise Rejected('native_temporary_journal', 'A durable runtime journal callback is required')
    # Callers must persist before assigning this copy into their live row.
    # A failed witness write must leave both live and durable state creating.
    save(copy.deepcopy(receipt))


def _receipt(recipe, check_id, receipt):
    identifier(check_id)
    paths, contract_digest = _contract(recipe, validate=False)
    if (not isinstance(receipt, dict) or receipt.get('format') != FORMAT
            or receipt.get('check_id') != check_id or receipt.get('contract_digest') != contract_digest
            or not isinstance(receipt.get('before'), dict) or receipt.get('before_digest') != digest(receipt['before'])
            or not isinstance(receipt.get('roots'), list)
            or [r.get('path') for r in receipt['roots']] != paths
            or any(r.get('state') not in ('planned', 'creating', 'owned', 'removing', 'removed')
                   or not isinstance(r.get('parent'), dict) for r in receipt['roots'])):
        raise Rejected('native_temporary_journal', 'Exact check and declared temporary-root journal required')
    return copy.deepcopy(receipt)


def prepare(recipe, check_id, before, save, *, previous=None):
    """Exclusively create absent roots; persist every uncertainty boundary.

    ``before`` is the original precreation temporary snapshot, also supplied to
    the renderer. An interrupted creating/removing state is never reconstructed.
    """
    identifier(check_id)
    paths, contract_digest = _contract(recipe)
    if not paths:
        if previous is not None:
            raise Rejected('native_temporary_journal', 'Owned roots were removed from the saved contract')
        return None
    if not isinstance(before, dict):
        raise Rejected('native_temporary_snapshot', 'Retained precreation temporary snapshot required')
    if previous is not None:
        receipt = _receipt(recipe, check_id, previous)
        if receipt['before'] != before:
            raise Rejected('native_temporary_snapshot', 'Replay must retain the original precreation snapshot')
        return verify(recipe, check_id, receipt)
    roots = []
    # Check every root before creating any; an existing directory is never ours.
    for value in paths:
        path = Path(value)
        fd, parent = _parent(path)
        try:
            if value in before or _at(fd, path.name) is not None:
                raise Rejected('native_temporary_preexisting', 'An owned temporary root must be absent; adoption is unsupported')
        finally:
            os.close(fd)
        roots.append({'path': value, 'parent': parent, 'state': 'planned'})
    receipt = {'format': FORMAT, 'check_id': check_id, 'contract_digest': contract_digest,
               'before': copy.deepcopy(before), 'before_digest': digest(before), 'roots': roots}
    _save(save, receipt)
    for root in roots:
        root['state'] = 'creating'
        _save(save, receipt)
        path = Path(root['path'])
        fd, _ = _parent(path, root['parent'])
        try:
            os.mkdir(path.name, 0o700, dir_fd=fd)
            info = _at(fd, path.name)
            if (info is None or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or _identity(path.parent.stat()) != root['parent']):
                raise Rejected('native_temporary_identity', 'Exclusive creation did not retain the required identity and mode')
            root.update(state='owned', witness=_identity(info), created_at=now())
            _save(save, receipt)
        finally:
            os.close(fd)
    return verify(recipe, check_id, receipt)


def verify(recipe, check_id, receipt):
    """Verify every retained root before native use; never create on replay."""
    retained = _receipt(recipe, check_id, receipt)
    _contract(recipe)
    if any(root['state'] != 'owned' for root in retained['roots']):
        raise Rejected('native_temporary_ambiguous', 'Incomplete or retired temporary ownership cannot authorize execution')
    for root in retained['roots']:
        os.close(_owned(root))
    return retained


def verify_removed(recipe, check_id, receipt):
    """Validate historical completion without touching a possibly reused path."""
    retained = _receipt(recipe, check_id, receipt)
    if any(root['state'] != 'removed' or not isinstance(root.get('witness'), dict)
           or not root.get('created_at') or not root.get('removed_at') for root in retained['roots']):
        raise Rejected('native_temporary_ambiguous', 'Every owned temporary root needs a retained completed removal')
    return retained


def remove(recipe, check_id, receipt, save, *, quiescent):
    """Remove only witnessed empty roots after trusted runtime stop verification.

    ``quiescent`` is an internal call-order guard, not an operator approval or a
    replacement for the runtime's exact native census/no-launch proof.
    """
    if quiescent is not True:
        raise Rejected('native_temporary_active', 'Verified native quiescence is required before removing owned roots')
    retained = _receipt(recipe, check_id, receipt)
    if any(root['state'] not in ('owned', 'removed') for root in retained['roots']):
        raise Rejected('native_temporary_ambiguous', 'Interrupted creation/removal remains held; absence is not a receipt')
    if all(root['state'] == 'removed' for root in retained['roots']):
        return verify_removed(recipe, check_id, retained)
    _contract(recipe)
    # A completed old receipt is historical. Never inspect or remove a path
    # that another later check may now own, even if an inode number was reused.
    for root in retained['roots']:
        if root['state'] == 'removed':
            continue
        path = Path(root['path'])
        parent_fd = _owned(root)
        root_fd = None
        try:
            root_fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            if _identity(os.fstat(root_fd)) != root['witness']:
                raise Rejected('native_temporary_identity', 'Owned temporary directory changed while opening')
            if os.listdir(root_fd):
                raise Rejected('native_temporary_nonempty', 'Owned temporary root is nonempty; recursive cleanup is forbidden')
            root['state'] = 'removing'
            _save(save, retained)
            info = _at(parent_fd, path.name)
            if (info is None or _identity(info) != root['witness']
                    or _identity(path.parent.stat()) != root['parent']):
                raise Rejected('native_temporary_identity', 'Owned temporary directory changed before removal')
            os.rmdir(path.name, dir_fd=parent_fd)
            root.update(state='removed', removed_at=now())
            _save(save, retained)
        finally:
            if root_fd is not None:
                os.close(root_fd)
            os.close(parent_fd)
    return verify_removed(recipe, check_id, retained)
