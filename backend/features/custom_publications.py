"""Manually managed publications in the ordinary Library identity/issue tables.

``local`` is an internal authority, never a metadata provider. Its opaque IDs
are generated here and are not SQLite row IDs or remote provider claims.
"""

import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from backend.base.acquisition_intake import IntakeFailure
from backend.implementations.acquisition_paths import contained
from backend.internals.db import get_db

MAX_ISSUES = 500
MAX_SOURCES = 500
LABEL = re.compile(r'(?:[0-9]{1,4}(?:\.[0-9]{1,4})?[A-Za-z]?|\[[A-Za-z0-9]{1,12}\])\Z')


class CustomPublicationError(ValueError):
    pass


def _text(value, field, limit, *, required=False):
    if not isinstance(value, str):
        raise CustomPublicationError('invalid_' + field)
    result = value.strip()
    if len(result) > limit or any(ord(c) < 32 for c in result) or (required and not result):
        raise CustomPublicationError('invalid_' + field)
    return result


def _fields(data):
    if not isinstance(data, dict):
        raise CustomPublicationError('invalid_publication')
    title = _text(data.get('title'), 'title', 200, required=True)
    publisher = _text(data.get('publisher', ''), 'publisher', 200)
    description = _text(data.get('description', ''), 'description', 4000)
    year = data.get('year')
    if year in ('', None):
        year = None
    elif type(year) is not int or not 1000 <= year <= 2200:
        raise CustomPublicationError('invalid_year')
    issues = data.get('issues')
    if not isinstance(issues, list) or not 1 <= len(issues) <= MAX_ISSUES:
        raise CustomPublicationError('invalid_issue_count')
    cleaned, labels = [], set()
    for issue in issues:
        if not isinstance(issue, dict) or set(issue) - {'id', 'number', 'title'}:
            raise CustomPublicationError('invalid_issue')
        number = _text(issue.get('number'), 'issue_number', 20, required=True)
        if not LABEL.fullmatch(number):
            raise CustomPublicationError('invalid_issue_number')
        # 1 and 01, or 1A and 1a, cannot describe distinct issue slots.
        canonical = number.casefold().lstrip('0') or '0'
        if canonical in labels:
            raise CustomPublicationError('duplicate_issue_number')
        labels.add(canonical)
        cleaned.append(dict(id=issue.get('id'), number=number,
                            title=_text(issue.get('title', ''), 'issue_title', 200)))
    return title, year, publisher, description, cleaned


def _local(cursor, volume_id):
    row = cursor.execute('''SELECT v.id,e.provider_id,v.root_folder,v.folder
        FROM volumes v JOIN volume_external_ids e ON e.volume_id=v.id AND e.provider='local'
        WHERE v.id=? AND v.metadata_provider='local' ''', (volume_id,)).fetchone()
    if row is None:
        raise CustomPublicationError('local_publication_not_found')
    return row


def _source_root(cursor, paths):
    if (not isinstance(paths, list) or not 1 <= len(paths) <= MAX_SOURCES
            or any(not isinstance(p, str) or len(p) > 4096 for p in paths)
            or len(set(paths)) != len(paths)):
        raise CustomPublicationError('invalid_source_selection')
    roots = list(cursor.execute('SELECT id,folder FROM root_folders ORDER BY length(folder) DESC'))
    selected = None
    for path in paths:
        if (not os.path.isabs(path) or '\x00' in path or '..' in Path(path).parts
                or Path(path).suffix.casefold() not in ('.cbz', '.cbr')):
            raise CustomPublicationError('invalid_source_selection')
        root = next(((rid, folder) for rid, folder in roots if _contained(path, folder)), None)
        if root is None or selected is not None and root[0] != selected[0]:
            raise CustomPublicationError('sources_need_one_library_root')
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            raise CustomPublicationError('source_unavailable') from None
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.R_OK):
            raise CustomPublicationError('source_unreadable')
        if cursor.execute('SELECT 1 FROM files WHERE filepath=?', (path,)).fetchone():
            raise CustomPublicationError('source_already_registered')
        if cursor.execute('''SELECT 1 FROM ignored_import_files WHERE filepath=?
            AND size=? AND mtime_ns=? AND ctime_ns=?''',
            (path, info.st_size, info.st_mtime_ns, info.st_ctime_ns)).fetchone():
            raise CustomPublicationError('source_is_ignored')
        selected = root
    return selected


def _contained(path, root):
    try:
        return contained(path, root) == path
    except (IntakeFailure, ValueError, OSError):
        return False


def create_publication(data):
    title, year, publisher, description, issues = _fields(data)
    if any(issue['id'] is not None for issue in issues):
        raise CustomPublicationError('invalid_issue')
    cursor = get_db()
    cursor.execute('BEGIN IMMEDIATE')
    try:
        root_id, root = _source_root(cursor, data.get('sources'))
        identity = uuid4().hex
        volume_id = cursor.execute('''INSERT INTO volumes(metadata_provider,comicvine_id,title,year,
            publisher,volume_number,description,site_url,monitored,monitor_new_issues,root_folder,
            special_version,special_version_locked,last_cv_fetch)
            VALUES('local',NULL,?,?,?,1,?,'',0,0,?,NULL,1,NULL)''',
            (title, year, publisher or None, description, root_id)).lastrowid
        cursor.execute('''INSERT INTO volume_external_ids(volume_id,provider,provider_id,provenance)
            VALUES(?,'local',?,'manual')''', (volume_id, identity))
        cursor.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(?,NULL)', (volume_id,))
        for position, issue in enumerate(issues, 1):
            local_issue = cursor.execute('''INSERT INTO issues(volume_id,comicvine_id,issue_number,
                calculated_issue_number,title,date,description,monitored)
                VALUES(?,NULL,?,?,?,NULL,'',0)''',
                (volume_id, issue['number'], position, issue['title'] or None)).lastrowid
            cursor.execute('''INSERT INTO issue_external_ids(issue_id,provider,provider_id,provenance)
                VALUES(?,'local',?,'manual')''', (local_issue, uuid4().hex))
        from backend.implementations.naming import generate_volume_folder_path
        from backend.implementations.volumes import Volume
        path = contained(generate_volume_folder_path(root, Volume(volume_id).get_data()), root)
        # Equal titles remain separate authorities and cannot share a move destination.
        if cursor.execute('SELECT 1 FROM volumes WHERE folder=? AND id<>?',
                          (path, volume_id)).fetchone():
            path = contained(path + ' [Local ' + identity[:8] + ']', root)
        cursor.execute('UPDATE volumes SET folder=? WHERE id=?', (path, volume_id))
        cursor.connection.commit()
        return publication(volume_id)
    except BaseException:
        cursor.connection.rollback()
        raise


def publication(volume_id):
    cursor = get_db()
    row = _local(cursor, volume_id)
    data = cursor.execute('''SELECT title,year,publisher,description FROM volumes WHERE id=?''',
                          (volume_id,)).fetchone()
    issues = [dict(id=issue_id, provider_id=provider_id, number=number, title=title or '',
                   owned=bool(owned)) for issue_id, provider_id, number, title, owned in
              cursor.execute('''SELECT i.id,e.provider_id,i.issue_number,i.title,
                    EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id)
                FROM issues i JOIN issue_external_ids e ON e.issue_id=i.id AND e.provider='local'
                WHERE i.volume_id=? ORDER BY i.calculated_issue_number,i.id''', (volume_id,))]
    return dict(volume_id=volume_id, provider='local', provider_id=row[1], root_folder_id=row[2],
                title=data[0], year=data[1], publisher=data[2] or '',
                description=data[3] or '', issues=issues, issue_count=len(issues))


def list_publications(query=''):
    if not isinstance(query, str) or len(query) > 160:
        raise CustomPublicationError('invalid_query')
    cursor = get_db()
    return [publication(row[0]) for row in cursor.execute('''SELECT id FROM volumes
        WHERE metadata_provider='local' AND instr(lower(title),lower(?))>0
        ORDER BY title COLLATE NOCASE,id LIMIT 100''', (query,))]


def _issue_in_use(cursor, issue_id, volume_id):
    # Removing an issue must not cascade or null out user/library references.
    checks = (
        ('issues_files', 'issue_id'), ('reading_order_entries', 'issue_id'),
        ('file_content_coverage', 'target_issue_id'),
        ('file_content_coverage', 'source_issue_id'),
        ('acquisition_provenance', 'issue_id'), ('download_history', 'issue_id'),
        ('blocklist', 'issue_id'), ('quality_rejections', 'issue_id'),
        ('release_events', 'issue_id'), ('wanted_schedule', 'issue_id'),
        ('wanted_reservations', 'issue_id'), ('issue_bibliography', 'issue_id'),
    )
    if cursor.execute('SELECT 1 FROM collection_publications WHERE local_volume_id=?',
                      (volume_id,)).fetchone():
        return True
    if cursor.execute("SELECT 1 FROM issue_external_ids WHERE issue_id=? AND provider!='local'",
                      (issue_id,)).fetchone():
        return True
    for table, key in checks:
        if cursor.execute(f'SELECT 1 FROM {table} WHERE {key}=? LIMIT 1',
                          (issue_id,)).fetchone():
            return True
    for table in ('wanted_searches', 'wanted_decisions'):
        if cursor.execute(f'''SELECT 1 FROM {table},
            json_each(CASE WHEN json_valid({table}.issue_ids) THEN {table}.issue_ids ELSE '[]' END)
            WHERE json_each.value=? LIMIT 1''',
            (issue_id,)).fetchone():
            return True
    return False


def edit_publication(volume_id, data):
    title, year, publisher, description, issues = _fields(data)
    if type(data.get('allow_remove', False)) is not bool:
        raise CustomPublicationError('invalid_publication')
    cursor = get_db()
    cursor.execute('BEGIN IMMEDIATE')
    try:
        _local(cursor, volume_id)
        existing = {row[0]: row[1] for row in cursor.execute(
            'SELECT id,issue_number FROM issues WHERE volume_id=?', (volume_id,))}
        presented = [issue['id'] for issue in issues if issue['id'] is not None]
        if (len(set(presented)) != len(presented) or any(type(i) is not int or i not in existing
                for i in presented)):
            raise CustomPublicationError('issue_not_in_publication')
        removed = set(existing) - set(presented)
        if removed and not data.get('allow_remove', False):
            raise CustomPublicationError('issue_removal_needs_confirmation')
        for issue_id in removed:
            if _issue_in_use(cursor, issue_id, volume_id):
                raise CustomPublicationError('issue_in_use')
        cursor.execute('''UPDATE volumes SET title=?,year=?,publisher=?,description=? WHERE id=?''',
                       (title, year, publisher or None, description, volume_id))
        for position, issue in enumerate(issues, 1):
            if issue['id'] is None:
                issue_id = cursor.execute('''INSERT INTO issues(volume_id,comicvine_id,issue_number,
                    calculated_issue_number,title,date,description,monitored)
                    VALUES(?,NULL,?,?,?,NULL,'',0)''',
                    (volume_id, issue['number'], position, issue['title'] or None)).lastrowid
                cursor.execute('''INSERT INTO issue_external_ids(issue_id,provider,provider_id,provenance)
                    VALUES(?,'local',?,'manual')''', (issue_id, uuid4().hex))
            else:
                cursor.execute('''UPDATE issues SET issue_number=?,calculated_issue_number=?,title=?
                    WHERE id=? AND volume_id=?''', (issue['number'], position,
                                                   issue['title'] or None, issue['id'], volume_id))
        for issue_id in removed:
            cursor.execute('DELETE FROM issues WHERE id=? AND volume_id=?', (issue_id, volume_id))
        cursor.connection.commit()
        return publication(volume_id)
    except BaseException:
        cursor.connection.rollback()
        raise
