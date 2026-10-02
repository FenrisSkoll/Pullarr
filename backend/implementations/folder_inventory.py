"""Bounded two-pass filesystem inventory. No DB, archive or provider access.

Paths/registrations are internal trusted inputs, not a transport contract. This
is evidence for a future folder review, not an execution precondition or a lock.
Incomplete scans never certify absence or complete ownership. No entry is opened
for its contents. Directory reads can change ordinary filesystem access times.
"""

import os
import stat
from pathlib import Path
from time import monotonic
from typing import Callable, Tuple

from backend.base.folder_inventory import (FolderInventory, InventoryLimits,
                                           InventoryState, RegisteredTreeFile,
                                           TreeEntry, TreeStamp)
from backend.base.organization_job import OrganizationError
from backend.implementations.organization_filesystem import safe_path


class _Incomplete(Exception):
    def __init__(self, state: InventoryState, reason: str):
        self.state, self.reason = state, reason


def _stamp(path: str) -> TreeStamp:
    safe_path(path)
    value = os.lstat(path)
    if (not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode))
            or value.st_ino == 0):
        raise _Incomplete(InventoryState.UNSAFE, 'unsupported_entry')
    return TreeStamp(value.st_dev, value.st_ino, value.st_mode, value.st_size,
                     value.st_mtime_ns, value.st_ctime_ns)


def _key(path: str) -> str:
    # Matches the existing organizer's conservative cross-platform reservations.
    return os.path.normpath(path).casefold()


def inspect_folder(root: str, source: str, volume_id: int,
                   registrations: Tuple[RegisteredTreeFile, ...] = (), *,
                   limits: InventoryLimits = InventoryLimits(),
                   clock: Callable[[], float] = monotonic) -> FolderInventory:
    """Inspect an existing, strict descendant of a configured managed root.

    The future service must resolve root/source from backend-owned volume state
    and bind/revalidate a complete DB snapshot. This helper cannot attest to DB
    ownership omitted by its caller. No C2 coverage belongs in registrations.
    """
    if (type(volume_id) is not int or volume_id <= 0 or type(registrations) is not tuple
            or type(root) is not str or type(source) is not str
            or '\x00' in root or '\x00' in source):
        raise ValueError('Invalid folder inventory scope')
    entries: list[TreeEntry] = []
    root_stamp = source_stamp = None
    inspected = 0
    admitted: Tuple[RegisteredTreeFile, ...] = ()
    started = clock()

    def budget() -> None:
        if clock() - started >= limits.seconds:
            raise _Incomplete(InventoryState.BOUNDED, 'deadline')

    def result(state: InventoryState, reason=None) -> FolderInventory:
        return FolderInventory(volume_id, root, source, state, reason, root_stamp,
                               source_stamp, tuple(sorted(entries, key=lambda e: e.relative)),
                               admitted, inspected)

    try:
        if (not Path(root).is_absolute() or not Path(source).is_absolute()
                or '..' in Path(root).parts or '..' in Path(source).parts
                or Path(root) not in Path(source).parents):
            raise _Incomplete(InventoryState.UNSAFE, 'outside_managed_root')
        if len(registrations) > limits.entries:
            raise _Incomplete(InventoryState.BOUNDED, 'registration_limit')
        root_stamp, source_stamp = _stamp(root), _stamp(source)
        if not stat.S_ISDIR(root_stamp.mode) or not stat.S_ISDIR(source_stamp.mode):
            raise _Incomplete(InventoryState.UNSAFE, 'directory_required')
        if root_stamp.device != source_stamp.device:
            raise _Incomplete(InventoryState.UNSAFE, 'source_device_boundary')
        by_path: dict[str, RegisteredTreeFile] = {}
        ids: set[int] = set()
        registered_bytes = 0
        for row in registrations:
            budget()
            if not isinstance(row, RegisteredTreeFile):
                raise ValueError('Invalid folder registration')
            if not Path(row.path).is_absolute() or '..' in Path(row.path).parts or Path(source) not in Path(row.path).parents:
                raise _Incomplete(InventoryState.UNSAFE, 'registered_path_outside_source')
            if row.file_id in ids or _key(row.path) in by_path:
                raise _Incomplete(InventoryState.UNSAFE, 'duplicate_registered_path_or_id')
            if any(link[0] != volume_id for link in (*row.direct, *row.general)):
                raise _Incomplete(InventoryState.UNSAFE, 'foreign_volume_file_owner')
            # Bound retained DB evidence as well as filesystem names. This also
            # bounds pathological multi-issue registration input.
            registered_bytes += len(repr(row).encode('utf-8'))
            if registered_bytes > limits.path_bytes:
                raise _Incomplete(InventoryState.BOUNDED, 'registration_bytes')
            ids.add(row.file_id)
            by_path[_key(row.path)] = row
        admitted = tuple(sorted(registrations, key=lambda r: r.file_id))

        device = source_stamp.device

        def walk() -> Tuple[TreeEntry, ...]:
            nonlocal inspected
            found: list[TreeEntry] = []
            pending = [(source, 0)]
            names: set[str] = set()
            name_bytes = 0
            while pending:
                budget()
                directory, depth = pending.pop()
                before = _stamp(directory)
                if not stat.S_ISDIR(before.mode) or before.device != device:
                    raise _Incomplete(InventoryState.UNSAFE, 'nested_device_or_type_changed')
                with os.scandir(directory) as stream:
                    for child in stream:
                        budget()
                        if len(found) >= limits.entries:
                            raise _Incomplete(InventoryState.BOUNDED, 'entry_limit')
                        path = os.path.join(directory, child.name)
                        relative = str(Path(path).relative_to(source))
                        name_bytes += len(relative.encode('utf-8'))
                        if name_bytes > limits.path_bytes:
                            raise _Incomplete(InventoryState.BOUNDED, 'path_bytes')
                        stamp = _stamp(path)
                        inspected += 1
                        if stamp.device != device:
                            raise _Incomplete(InventoryState.UNSAFE, 'nested_device_boundary')
                        if _key(relative) in names:
                            raise _Incomplete(InventoryState.UNSAFE, 'case_equivalent_entries')
                        names.add(_key(relative))
                        directory_entry = stat.S_ISDIR(stamp.mode)
                        registered = by_path.get(_key(path))
                        if registered is not None and (directory_entry or registered.path != path):
                            raise _Incomplete(InventoryState.UNSAFE, 'registered_path_type_or_case_conflict')
                        found.append(TreeEntry(relative, 'directory' if directory_entry else 'file', stamp, registered))
                        if directory_entry:
                            if depth + 1 > limits.depth:
                                raise _Incomplete(InventoryState.BOUNDED, 'depth_limit')
                            pending.append((path, depth + 1))
                if _stamp(directory) != before:
                    raise _Incomplete(InventoryState.CHANGED, 'directory_changed_during_scan')
            return tuple(sorted(found, key=lambda e: e.relative))

        entries.extend(walk())
        expected = {r.file_id for r in registrations}
        actual = {e.registration.file_id for e in entries if e.registration is not None}
        if expected != actual:
            raise _Incomplete(InventoryState.INACCESSIBLE, 'registered_file_missing')
        # Detect membership/content-stat changes across the walk, including a
        # changed early file while later directories were being enumerated.
        if (tuple(entries) != walk() or _stamp(source) != source_stamp
                or _stamp(root) != root_stamp):
            raise _Incomplete(InventoryState.CHANGED, 'tree_changed_during_scan')
        budget()
        return result(InventoryState.COMPLETE)
    except _Incomplete as error:
        return result(error.state, error.reason)
    except FileNotFoundError:
        return result(InventoryState.INACCESSIBLE if source_stamp is None else InventoryState.CHANGED,
                      'missing_root_or_source' if source_stamp is None else 'entry_disappeared')
    except OrganizationError:
        return result(InventoryState.UNSAFE, 'unsafe_path_or_reparse_point')
    except UnicodeError:
        return result(InventoryState.UNSAFE, 'unsupported_path_encoding')
    except OSError:
        return result(InventoryState.INACCESSIBLE, 'filesystem_inspection_failed')
