"""Read-only same-device location admission; never creates a directory.

The quarantine executor repeats admission under its mutation guard.
This is not an OS permission guarantee and does not authorize a file move.
"""

import os
import re
import stat
from collections import defaultdict
from pathlib import Path

from backend.base.duplicate_review import DuplicateReviewError
from backend.base.organization_job import OrganizationError
from backend.implementations.organization_filesystem import safe_path
from backend.internals.organization_reservations import ReservationIndex

DIRECTORY = '.kapowarr-quarantine'
MAX_ROOTS = 1000
MAX_FILES = 20000


def location_context(cursor):
    """One bounded authoritative read for a batch, never a client DTO.

    Must be acquired inside the caller's snapshot/registration transaction.
    Shared indexes avoid rereading every library path for each selected file.
    """
    def rows(sql, bound):
        result = cursor.execute(sql + ' LIMIT ?', (bound + 1,)).fetchall()
        if len(result) > bound:
            raise DuplicateReviewError('quarantine_location_snapshot_limit')
        return result
    roots = rows('SELECT id,folder FROM root_folders ORDER BY id', MAX_ROOTS)
    files = rows('''SELECT f.id,f.filepath,f.size,q.file_id FROM files f
        LEFT JOIN quarantined_files q ON q.file_id=f.id ORDER BY f.id''', MAX_FILES)
    volumes = rows('SELECT id,root_folder,folder FROM volumes ORDER BY id', MAX_FILES)
    links = rows('''SELECT d.file_id,i.volume_id FROM issues_files d JOIN issues i ON i.id=d.issue_id
        UNION SELECT file_id,volume_id FROM volume_files ORDER BY 1,2''', MAX_FILES)
    owners = defaultdict(set)
    for fid, vid in links:
        owners[fid].add(vid)
    return dict(roots=roots, files={r[0]: (r[1], r[2]) for r in files if r[3] is None},
                volumes={r[0]: (r[1], r[2]) for r in volumes}, owners=owners,
                root_index=ReservationIndex((path, str(rid)) for rid, path in roots),
                active_index=ReservationIndex((r[1], str(r[0])) for r in files if r[3] is None),
                retained_index=ReservationIndex((r[1], str(r[0])) for r in files if r[3] is not None))


def reject_quarantine_root(cursor, folder: str) -> None:
    """Reserved namespace and retained paths cannot become active roots.

    Includes ancestor admission after original roots/volumes were removed.
    No filesystem setup, traversal, or deletion is performed.
    """
    if DIRECTORY in (part.casefold() for part in Path(folder).parts):
        raise DuplicateReviewError('quarantine_storage_not_library_root')
    # Bounded stream: do not retain one Python object per quarantined file.
    candidate = ReservationIndex(((folder, 'candidate'),))
    for count, row in enumerate(cursor.execute(
        'SELECT quarantine_filepath FROM quarantined_files LIMIT ?', (MAX_FILES + 1,)
    ), 1):
        if count > MAX_FILES:
            raise DuplicateReviewError('quarantine_root_snapshot_limit')
        if candidate.conflicts(row[0]):
            raise DuplicateReviewError('quarantine_storage_not_library_root')


def observe_location(cursor, file_id: int, identity: str, *, context=None) -> dict:
    """Resolve from active DB ownership plus an internal durable-job identity."""
    if type(file_id) is not int or file_id <= 0 or type(identity) is not str or not re.fullmatch('[a-f0-9]{32}', identity):
        raise DuplicateReviewError('invalid_quarantine_location_identity')
    file = (context['files'].get(file_id) if context is not None else
            cursor.execute('SELECT filepath,size FROM active_files WHERE id=?', (file_id,)).fetchone())
    if file is None:
        raise DuplicateReviewError('quarantine_active_file_required')
    owners = ([(v,) for v in sorted(context['owners'][file_id])] if context is not None else
              cursor.execute('''SELECT i.volume_id FROM issues_files d JOIN issues i ON i.id=d.issue_id
        WHERE d.file_id=? UNION SELECT volume_id FROM volume_files WHERE file_id=?''', (file_id, file_id)).fetchall())
    if len(owners) != 1:
        raise DuplicateReviewError('incoherent_managed_file_ownership')
    volume = (context['volumes'].get(owners[0][0]) if context is not None else
              cursor.execute('SELECT root_folder,folder FROM volumes WHERE id=?', (owners[0][0],)).fetchone())
    roots = (context['roots'] if context is not None else
             cursor.execute('SELECT id,folder FROM root_folders ORDER BY id LIMIT ?', (MAX_ROOTS + 1,)).fetchall())
    if len(roots) > MAX_ROOTS:
        raise DuplicateReviewError('quarantine_root_snapshot_limit')
    root_map = dict(roots)
    if volume is None or volume[0] not in root_map or not volume[1]:
        raise DuplicateReviewError('quarantine_root_unavailable')
    root, source = Path(root_map[volume[0]]), Path(file[0])
    folder = Path(volume[1])
    if root == root.parent or root not in folder.parents or folder not in source.parents:
        raise DuplicateReviewError('quarantine_source_outside_managed_tree')
    storage = root.parent / DIRECTORY / str(volume[0])
    target = storage / identity / source.name
    root_index = context['root_index'] if context is not None else ReservationIndex((path, str(rid)) for rid, path in roots)
    if root_index.conflicts(str(storage)):
        raise DuplicateReviewError('quarantine_storage_overlaps_library_root')
    if len(os.fsencode(target)) > 1024 or os.name == 'nt' and len(str(target)) > 240:
        raise DuplicateReviewError('quarantine_target_path_limit')
    try:
        for path in (root, source, target):
            safe_path(str(path))
        source_stat = os.lstat(source)
        root_stat = os.lstat(root)
        if not stat.S_ISREG(source_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise DuplicateReviewError('quarantine_source_unavailable')
        ancestor = target.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        parent_stat = os.lstat(ancestor)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise DuplicateReviewError('quarantine_parent_unsafe')
        if not source_stat.st_dev or not root_stat.st_dev or not parent_stat.st_dev:
            raise DuplicateReviewError('quarantine_device_unknown')
        if source_stat.st_dev != root_stat.st_dev or source_stat.st_dev != parent_stat.st_dev:
            raise DuplicateReviewError('cross_device_quarantine_not_supported')
        if not os.access(ancestor, os.W_OK | os.X_OK):
            raise DuplicateReviewError('quarantine_parent_not_writable')
        if os.path.lexists(target):
            raise DuplicateReviewError('quarantine_target_occupied')
    except (OSError, OrganizationError):
        raise DuplicateReviewError('quarantine_location_unsafe_or_unavailable') from None
    # Protect existing DB ownership anywhere under/above the managed storage.
    if context is not None:
        if context['active_index'].conflicts(str(storage)) or context['retained_index'].conflicts(str(target)):
            raise DuplicateReviewError('quarantine_storage_registered')
    else:
        _registered_storage(cursor, str(storage), str(target))
    return dict(file_id=file_id, volume_id=owners[0][0], root_id=volume[0], root=str(root),
                source=str(source), storage=str(storage), target=str(target),
                device=source_stat.st_dev, parent=str(ancestor),
                parent_identity=(parent_stat.st_dev, parent_stat.st_ino),
                source_identity=(source_stat.st_dev, source_stat.st_ino, source_stat.st_size,
                                 source_stat.st_mtime_ns, source_stat.st_ctime_ns))


def _registered_storage(cursor, storage, target):
    storage_index = ReservationIndex(((storage, 'storage'),))
    for count, row in enumerate(cursor.execute('''SELECT f.filepath,q.file_id FROM files f
        LEFT JOIN quarantined_files q ON q.file_id=f.id AND q.quarantine_filepath=f.filepath
        ORDER BY f.id LIMIT ?''', (MAX_FILES + 1,)), 1):
        if count > MAX_FILES:
            raise DuplicateReviewError('quarantine_file_snapshot_limit')
        if storage_index.conflicts(row[0]):
            # Existing properly retained artifacts are expected, but cannot own
            # this exact new target. Foreign ordinary rows are never ignored.
            if row[1] is None or row[0] == target:
                raise DuplicateReviewError('quarantine_storage_registered')
