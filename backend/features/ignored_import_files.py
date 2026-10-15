"""Persistent, path-and-file-evidence exclusions for local import discovery."""

import os
import stat
import time
from pathlib import Path

from backend.base.acquisition_intake import IntakeFailure
from backend.implementations.acquisition_paths import contained
from backend.internals.db import get_db


class IgnoreFailure(ValueError):
    pass


def _paths(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 100
            or any(not isinstance(p, str) or len(p) > 4096 for p in value)
            or len(set(value)) != len(value)):
        raise IgnoreFailure('invalid_file_selection')
    return value


def _evidence(cursor, path):
    if (not os.path.isabs(path) or '\x00' in path or '..' in Path(path).parts
            or Path(path).suffix.casefold() not in ('.cbz', '.cbr')):
        raise IgnoreFailure('invalid_file_path')
    for root_id, root in cursor.execute('SELECT id,folder FROM root_folders ORDER BY length(folder) DESC'):
        try:
            if contained(path, root) != path:
                continue
        except (IntakeFailure, OSError):
            continue
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            raise IgnoreFailure('file_missing') from None
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.R_OK):
            raise IgnoreFailure('file_unreadable')
        return root_id, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    raise IgnoreFailure('file_outside_library_or_missing')


def ignore_files(paths):
    paths = _paths(paths)
    cursor = get_db()
    results = []
    cursor.execute('BEGIN IMMEDIATE')
    try:
        for path in paths:
            try:
                root_id, size, mtime_ns, ctime_ns = _evidence(cursor, path)
                if cursor.execute('SELECT 1 FROM files WHERE filepath=?', (path,)).fetchone():
                    raise IgnoreFailure('already_in_library')
                cursor.execute('''INSERT INTO ignored_import_files
                    (filepath,root_folder_id,size,mtime_ns,ctime_ns,ignored_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(filepath) DO UPDATE SET root_folder_id=excluded.root_folder_id,
                    size=excluded.size,mtime_ns=excluded.mtime_ns,
                    ctime_ns=excluded.ctime_ns,ignored_at=excluded.ignored_at''',
                    (path, root_id, size, mtime_ns, ctime_ns, int(time.time())))
                results.append(dict(path=path, status='ignored'))
            except IgnoreFailure as error:
                results.append(dict(path=path, status='failed', reason=str(error)))
        cursor.connection.commit()
    except BaseException:
        cursor.connection.rollback()
        raise
    return results


def restore_files(paths):
    paths = _paths(paths)
    cursor = get_db()
    cursor.execute('BEGIN IMMEDIATE')
    try:
        result = []
        for path in paths:
            deleted = cursor.execute('DELETE FROM ignored_import_files WHERE filepath=?', (path,)).rowcount
            result.append(dict(path=path, status='restored' if deleted else 'not_ignored'))
        cursor.connection.commit()
        return result
    except BaseException:
        cursor.connection.rollback()
        raise


def list_ignored(offset=0, limit=100):
    if type(offset) is not int or not 0 <= offset <= 1_000_000 or type(limit) is not int or not 1 <= limit <= 100:
        raise IgnoreFailure('invalid_pagination')
    cursor = get_db()
    total = cursor.execute('SELECT count(*) FROM ignored_import_files').fetchone()[0]
    rows = cursor.execute('''SELECT i.filepath,i.size,i.mtime_ns,i.ctime_ns,i.ignored_at,r.folder
        FROM ignored_import_files i LEFT JOIN root_folders r ON r.id=i.root_folder_id
        ORDER BY i.filepath COLLATE NOCASE,i.filepath LIMIT ? OFFSET ?''', (limit, offset))
    items = []
    for path, size, mtime_ns, ctime_ns, ignored_at, root in rows:
        state = 'root_removed' if root is None else 'outside_current_root'
        try:
            in_scope = root is not None and contained(path, root) == path
        except (IntakeFailure, OSError):
            in_scope = False
        if in_scope:
            state = 'missing'
            try:
                current = os.stat(path, follow_symlinks=False)
                if stat.S_ISREG(current.st_mode):
                    state = 'ignored' if (current.st_size, current.st_mtime_ns,
                                          current.st_ctime_ns) == (size, mtime_ns, ctime_ns) else 'changed'
            except OSError:
                pass
        items.append(dict(path=path, size=size, ignored_at=ignored_at, state=state))
    return dict(total=total, items=items, next_offset=offset + len(items) if offset + len(items) < total else None)
