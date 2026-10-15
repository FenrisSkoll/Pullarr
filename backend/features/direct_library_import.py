"""Explicit, per-file local library import. No organizer job is created here."""

import errno
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from uuid import uuid4

from backend.base.acquisition_intake import IntakeFailure
from backend.base.logging import LOGGER
from backend.base.organization_job import OrganizationError
from backend.features.managed_permissions import (apply_managed_permissions,
                                                  create_managed_folder)
from backend.implementations.acquisition_paths import contained
from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.implementations.volumes import Library, Volume
from backend.internals.db import get_db
from backend.internals.organization_reservations import load_reservations


class ImportFailure(ValueError):
    pass


def _regular(path):
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise ImportFailure('source_unavailable') from error
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.R_OK):
        raise ImportFailure('source_unreadable')
    return info


def _root(cursor, source):
    for root_id, root in cursor.execute('SELECT id,folder FROM root_folders ORDER BY length(folder) DESC'):
        try:
            if contained(source, root) == source:
                return root_id, root
        except (IntakeFailure, ValueError, OSError):
            pass
    raise ImportFailure('source_outside_library_roots')


def _selected_issues(cursor, volume_id, provider, issue_ids):
    if (not isinstance(issue_ids, list) or not issue_ids or len(issue_ids) > 100
            or any(not isinstance(value, str) or not value for value in issue_ids)
            or len(set(issue_ids)) != len(issue_ids)):
        raise ImportFailure('choose_issue')
    rows = cursor.execute('''SELECT i.id,e.provider_id,i.calculated_issue_number
        FROM issues i JOIN issue_external_ids e ON e.issue_id=i.id
        WHERE i.volume_id=? AND e.provider=?''', (volume_id, provider)).fetchall()
    mapped = {str(row[1]): (row[0], row[2]) for row in rows}
    if any(value not in mapped for value in issue_ids):
        raise ImportFailure('issue_not_in_publication')
    return [mapped[value] for value in issue_ids]


def _exclusive_move(source, target, source_info):
    """Keep source until registration; never replace a target or shared inode."""
    if source == target:
        return False
    if os.path.lexists(target):
        raise ImportFailure('target_exists: ' + target)
    try:
        os.link(source, target, follow_symlinks=False)
    except OSError as error:
        if error.errno == errno.EEXIST:
            raise ImportFailure('target_exists: ' + target) from error
        if error.errno not in (errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS,
                               errno.EPERM, errno.EMLINK):
            raise
        # Cross-device or unsupported hardlink: copy once into our exclusive
        # temporary file, verify the copy, then publish it without clobbering.
        temporary = target + '.pullarr-' + uuid4().hex + '.part'
        digest = hashlib.sha256()
        temporary_identity = None
        try:
            with open(source, 'rb') as incoming, open(temporary, 'xb') as outgoing:
                temporary_info = os.fstat(outgoing.fileno())
                temporary_identity = (temporary_info.st_dev, temporary_info.st_ino)
                while chunk := incoming.read(1024 * 1024):
                    digest.update(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if _regular(source).st_size != source_info.st_size or os.path.getsize(temporary) != source_info.st_size:
                raise ImportFailure('source_changed_during_copy')
            check = hashlib.sha256()
            with open(temporary, 'rb') as copied:
                while chunk := copied.read(1024 * 1024):
                    check.update(chunk)
            if check.digest() != digest.digest():
                raise ImportFailure('copy_verification_failed')
            try:
                os.link(temporary, target, follow_symlinks=False)
            except OSError as publish_error:
                if publish_error.errno == errno.EEXIST:
                    raise ImportFailure('target_exists: ' + target) from publish_error
                if publish_error.errno not in (errno.EOPNOTSUPP, errno.ENOSYS,
                                               errno.EPERM, errno.EMLINK):
                    raise
                # Some shares cannot hardlink even inside one directory.
                # An exclusive target is still safe if a copy is interrupted.
                published_here = False
                published_identity = None
                try:
                    with open(temporary, 'rb') as copied, open(target, 'xb') as published:
                        published_here = True
                        published_info = os.fstat(published.fileno())
                        published_identity = (published_info.st_dev, published_info.st_ino)
                        while chunk := copied.read(1024 * 1024):
                            published.write(chunk)
                        published.flush()
                        os.fsync(published.fileno())
                    if os.path.getsize(target) != source_info.st_size:
                        raise ImportFailure('copy_verification_failed')
                    published_digest = hashlib.sha256()
                    with open(target, 'rb') as published:
                        while chunk := published.read(1024 * 1024):
                            published_digest.update(chunk)
                    if published_digest.digest() != digest.digest():
                        raise ImportFailure('copy_verification_failed')
                except BaseException as error:
                    if published_here and published_identity is not None:
                        try:
                            current = os.stat(target, follow_symlinks=False)
                            if (current.st_dev, current.st_ino) == published_identity:
                                os.unlink(target)
                        except FileNotFoundError:
                            pass
                    if isinstance(error, FileExistsError):
                        raise ImportFailure('target_exists: ' + target) from error
                    raise
        finally:
            # Only the unique temporary path created by this attempt is ours.
            if temporary_identity is not None:
                try:
                    current = os.stat(temporary, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == temporary_identity:
                        os.unlink(temporary)
                except FileNotFoundError:
                    pass
    return True


def _destination(cursor, volume, selected, source, filename):
    from backend.implementations.naming import (generate_issue_name,
                                                generate_volume_folder_path)

    data = volume.get_data()
    root = cursor.execute('SELECT folder FROM root_folders WHERE id=?',
                          (data.root_folder,)).fetchone()[0]
    provider = cursor.execute('SELECT metadata_provider FROM volumes WHERE id=?',
                              (data.id,)).fetchone()[0]
    folder = data.folder if provider == 'local' and data.folder else generate_volume_folder_path(root, data)
    folder = contained(folder, root)
    values = [value for _, value in selected]
    number = values[0] if len(values) == 1 else (min(values), max(values))
    name = filename if filename is not None else generate_issue_name(data, number) + Path(source).suffix
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or Path(name).name != name or '/' in name or '\\' in name
            or Path(name).suffix.lower() != Path(source).suffix.lower()):
        raise ImportFailure('invalid_target_filename')
    return folder, contained(str(Path(folder) / name), root)


def _admit_claims(cursor, source, target, volume_id):
    index = load_reservations(cursor)
    owners = {owner for path in (source, target) for owner in index.conflicts(path)}
    for owner in owners:
        row = cursor.execute('SELECT state,intent FROM organization_jobs WHERE id=?',
                             (owner,)).fetchone()
        if row is None or row[0] not in ('recovery_required', 'failed'):
            raise ImportFailure('file_busy_or_recovery_ambiguous')
        try:
            intent = json.loads(row[1])
            prior_source, prior_target = intent['source'], intent['target']
        except (ValueError, KeyError, TypeError):
            raise ImportFailure('file_busy_or_recovery_ambiguous') from None
        if (not intent.get('library_import') or intent.get('volume_id') != volume_id
                or not os.path.isfile(prior_source) or os.path.lexists(prior_target)
                or cursor.execute('''SELECT 1 FROM organization_steps WHERE job_id=?
                    AND kind<>'ensure_target_directory' AND state='succeeded' LIMIT 1''',
                    (owner,)).fetchone()):
            raise ImportFailure('file_busy_or_recovery_ambiguous')
        cursor.execute("UPDATE organization_jobs SET state='failed',error='abandoned_after_direct_import' WHERE id=? AND state='recovery_required'", (owner,))
        cursor.execute('DELETE FROM organization_reservations WHERE job_id=?', (owner,))


def import_one(choice):
    if not isinstance(choice, dict):
        raise ImportFailure('invalid_selection')
    source = choice.get('source')
    provider = choice.get('provider')
    provider_id = choice.get('provider_id')
    mode = choice.get('mode')
    if (not isinstance(source, str) or not os.path.isabs(source) or '\x00' in source
            or '..' in Path(source).parts or provider not in ('comicvine', 'metron', 'gcd', 'local')
            or not isinstance(provider_id, str)
            or (not provider_id.isdecimal() if provider != 'local' else
                len(provider_id) != 32 or any(c not in '0123456789abcdef' for c in provider_id))
            or mode not in ('keep', 'move') or Path(source).suffix.lower() not in
            {'.cbr', '.cbz'}):
        raise ImportFailure('invalid_selection')
    source = os.path.abspath(source)
    cursor = get_db()
    root_id, root = _root(cursor, source)
    try:
        source_info = _regular(source)
    except ImportFailure:
        if mode != 'move':
            raise
        previous = cursor.execute('''SELECT v.id FROM volumes v JOIN volume_external_ids e
            ON e.volume_id=v.id AND e.provider=v.metadata_provider
            WHERE e.provider=? AND e.provider_id=?''', (provider, provider_id)).fetchone()
        if previous is None:
            raise
        previous_volume = Volume(previous[0])
        previous_issues = _selected_issues(cursor, previous[0], provider, choice.get('issue_ids'))
        _, previous_target = _destination(cursor, previous_volume, previous_issues,
                                          source, choice.get('filename'))
        record = cursor.execute('SELECT id FROM files WHERE filepath=?', (previous_target,)).fetchone()
        if record and _regular(previous_target):
            linked = {row[0] for row in cursor.execute('SELECT issue_id FROM issues_files WHERE file_id=?',
                                                       (record[0],))}
            if linked == {issue_id for issue_id, _ in previous_issues}:
                return dict(source=source, target=previous_target, volume_id=previous[0],
                            issue_ids=choice['issue_ids'], status='already_in_library')
        raise ImportFailure('source_unavailable')
    if cursor.execute('''SELECT 1 FROM acquisition_seed_artifacts WHERE source_path=? LIMIT 1''',
                      (source,)).fetchone() and mode == 'move':
        raise ImportFailure('seeding_source_use_keep_in_place')
    existing = cursor.execute('''SELECT v.id FROM volumes v JOIN volume_external_ids e
        ON e.volume_id=v.id AND e.provider=v.metadata_provider
        WHERE e.provider=? AND e.provider_id=?''', (provider, provider_id)).fetchone()
    if existing:
        volume_id = existing[0]
    else:
        if provider == 'local':
            raise ImportFailure('local_publication_not_found')
        # Existing metadata import creates the provider-qualified volume and
        # issue catalogue, without scanning or creating a destination folder.
        from backend.features.import_issue_catalog import \
            cached_metadata_result

        volume_id = Library.add_metadata(ProviderVolumeIdentity(provider, provider_id),
                                         root_id, True, organizer_registration=True,
                                         fetched_result=cached_metadata_result(
                                             cursor.connection.db_file, provider, provider_id))
    volume = Volume(volume_id)
    volume_data = volume.get_data()
    destination_root = cursor.execute('SELECT folder FROM root_folders WHERE id=?',
                                      (volume_data.root_folder,)).fetchone()[0]
    selected = _selected_issues(cursor, volume_id, provider, choice.get('issue_ids'))
    target = source
    if mode == 'move':
        folder, target = _destination(cursor, volume, selected, source, choice.get('filename'))
    # An exact path registration is idempotent; a changed explicit issue set
    # replaces only links owned by this publication.
    created = False
    target_info = None
    cursor.execute('BEGIN IMMEDIATE')
    try:
        _admit_claims(cursor, source, target, volume_id)
        ignored = cursor.execute('''SELECT size,mtime_ns,ctime_ns FROM ignored_import_files WHERE filepath=?''',
                                 (source,)).fetchone()
        if ignored and tuple(ignored) == (source_info.st_size, source_info.st_mtime_ns,
                                          source_info.st_ctime_ns):
            raise ImportFailure('source_is_ignored')
        contained(source, root)
        contained(target, destination_root)
        latest_source = _regular(source)
        if (latest_source.st_dev, latest_source.st_ino, latest_source.st_size,
                latest_source.st_mtime_ns) != (source_info.st_dev, source_info.st_ino,
                source_info.st_size, source_info.st_mtime_ns):
            raise ImportFailure('source_changed_before_import')
        current_file = cursor.execute('SELECT id FROM files WHERE filepath=?', (source,)).fetchone()
        current_owner = cursor.execute('''SELECT DISTINCT i.volume_id FROM files f
            JOIN issues_files b ON b.file_id=f.id JOIN issues i ON i.id=b.issue_id
            WHERE f.filepath=?''', (source,)).fetchall()
        if current_owner and (len(current_owner) != 1 or current_owner[0][0] != volume_id):
            raise ImportFailure('file_owned_by_other_publication')
        if current_file and cursor.execute('SELECT 1 FROM volume_files WHERE file_id=? LIMIT 1',
                                           (current_file[0],)).fetchone():
            raise ImportFailure('file_has_general_file_owner')
        if current_file and target == source:
            linked = {row[0] for row in cursor.execute('SELECT issue_id FROM issues_files WHERE file_id=?',
                                                       (current_file[0],))}
            if linked == {issue_id for issue_id, _ in selected}:
                cursor.connection.commit()
                return dict(source=source, target=target, volume_id=volume_id,
                            issue_ids=choice['issue_ids'], status='already_in_library')
        if target != source and os.path.lexists(target):
            raise ImportFailure('target_exists: ' + target)
        if target != source:
            create_managed_folder(folder, database=cursor.connection)
        created = _exclusive_move(source, target, source_info)
        if created:
            target_info = _regular(target)
            if (target_info.st_dev, target_info.st_ino) != (source_info.st_dev, source_info.st_ino):
                # Copy has its own inode; applying the configured file mode
                # cannot mutate the still-retained source.
                apply_managed_permissions(target, directory=False,
                                          database=cursor.connection)
        row = cursor.execute('SELECT id FROM files WHERE filepath=?', (target,)).fetchone()
        if row is None and current_file is not None and target != source:
            cursor.execute('UPDATE files SET filepath=?,size=? WHERE id=?',
                           (target, source_info.st_size, current_file[0]))
            file_id = current_file[0]
        elif row is None:
            file_id = cursor.execute('INSERT INTO files(filepath,size) VALUES(?,?)',
                                     (target, source_info.st_size)).lastrowid
        else:
            file_id = row[0]
        other = cursor.execute('''SELECT 1 FROM issues_files b JOIN issues i ON i.id=b.issue_id
            WHERE b.file_id=? AND i.volume_id<>? LIMIT 1''', (file_id, volume_id)).fetchone()
        if other:
            raise ImportFailure('file_owned_by_other_publication')
        if cursor.execute('SELECT 1 FROM volume_files WHERE file_id=? LIMIT 1',
                          (file_id,)).fetchone():
            raise ImportFailure('file_has_general_file_owner')
        cursor.execute('''DELETE FROM issues_files WHERE file_id=? AND issue_id IN
            (SELECT id FROM issues WHERE volume_id=?)''', (file_id, volume_id))
        cursor.executemany('INSERT INTO issues_files(file_id,issue_id,forced) VALUES(?,?,1)',
                           ((file_id, issue_id) for issue_id, _ in selected))
        cursor.execute('DELETE FROM ignored_import_files WHERE filepath=?', (source,))
        if not volume_data.folder:
            from backend.implementations.naming import \
                generate_volume_folder_path
            cursor.execute('UPDATE volumes SET folder=? WHERE id=?',
                           (contained(generate_volume_folder_path(destination_root, volume_data), destination_root), volume_id))
        cursor.connection.commit()
        warning = None
        if created:
            # The database now owns a verified destination. Source cleanup is
            # best effort; a retained source is safer than an unowned target.
            try:
                latest = _regular(source)
                if (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns) == (
                        source_info.st_dev, source_info.st_ino, source_info.st_size, source_info.st_mtime_ns):
                    os.unlink(source)
                    if os.stat(target, follow_symlinks=False).st_nlink == 1:
                        try:
                            apply_managed_permissions(target, directory=False,
                                                      database=cursor.connection)
                        except Exception:
                            warning = 'file_permission_update_failed'
            except (OSError, ImportFailure):
                warning = 'source_cleanup_failed'
        return dict(source=source, target=target, volume_id=volume_id,
                    issue_ids=choice['issue_ids'], status='imported', warning=warning)
    except BaseException:
        if cursor.connection.in_transaction:
            cursor.connection.rollback()
        if created and target_info is not None and cursor.execute('SELECT 1 FROM files WHERE filepath=?', (target,)).fetchone() is None:
            try:
                actual = os.stat(target, follow_symlinks=False)
                if (actual.st_dev, actual.st_ino) == (target_info.st_dev, target_info.st_ino):
                    os.unlink(target)
            except OSError:
                pass
        raise


def import_selected(choices):
    if not isinstance(choices, list) or not 1 <= len(choices) <= 100:
        raise ImportFailure('invalid_batch')
    results = []
    for choice in choices:
        source = choice.get('source') if isinstance(choice, dict) else None
        try:
            results.append(import_one(choice))
        except OrganizationError as error:
            detail = error.detail if error.detail in (
                'OperationalError', 'PermissionError', 'FileNotFoundError',
                'OSError', 'ValueError', 'KeyError') else 'unspecified'
            LOGGER.warning('Direct import permission/organization failure: %s (%s)',
                           error.code.value, detail)
            results.append(dict(source=source, status='failed', reason=error.code.value))
        except (ImportFailure, OSError, sqlite3.Error, ValueError) as error:
            code = str(error) if isinstance(error, ImportFailure) else type(error).__name__
            results.append(dict(source=source, status='failed', reason=code))
        except Exception as error:
            LOGGER.warning('Direct import unexpected failure: %s', type(error).__name__)
            results.append(dict(source=source, status='failed', reason='import_unavailable'))
    return dict(results=results)
