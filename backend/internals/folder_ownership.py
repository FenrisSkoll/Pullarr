"""Authoritative bounded read acquisition for volume-tree organization.

All filesystem paths are resolved from library records. No filesystem work or
model constructors, and no commits. A SAVEPOINT keeps the batched read coherent
and also composes with the registrar's existing BEGIN IMMEDIATE transaction.
"""

import json
import os
from dataclasses import asdict, dataclass, fields
from typing import Tuple

from backend.base.folder_inventory import RegisteredTreeFile
from backend.base.library_health import canonical, fingerprint
from backend.base.naming_policy import NamingSettings
from backend.base.organization_job import ExecutionCode, OrganizationError
from backend.internals.organization_reservations import (ReservationIndex,
                                                         load_reservations,
                                                         path_key)
from backend.internals.provider_authority import AuthorityToken, capture

MAX_ROWS = 20000
MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class FolderOwnership:
    volume_id: int
    root_id: int
    root: str
    source: str
    custom: bool
    authority: AuthorityToken
    registrations: Tuple[RegisteredTreeFile, ...]
    state_json: str

    @property
    def digest(self) -> str:
        return fingerprint(json.loads(self.state_json))


@dataclass(frozen=True)
class FolderOwnershipBatch:
    volumes: Tuple[FolderOwnership, ...]
    roots: Tuple[Tuple[int, str], ...]
    owners: Tuple[Tuple[int, str], ...]
    all_files: Tuple[Tuple[int, str, int], ...]
    naming: NamingSettings
    reservations: ReservationIndex


def load_ownership(cursor, volume_ids: tuple[int, ...], *, row_limit: int = MAX_ROWS) -> FolderOwnershipBatch:
    if (type(volume_ids) is not tuple or not 1 <= len(volume_ids) <= 50
            or any(type(i) is not int or i <= 0 for i in volume_ids)
            or len(set(volume_ids)) != len(volume_ids)
            or type(row_limit) is not int or not 1 <= row_limit <= MAX_ROWS):
        raise ValueError('Invalid folder ownership scope')
    byte_count = 0

    def rows(sql, parameters=()):
        nonlocal byte_count
        result = []
        cursor.execute(sql, parameters)
        names = [c[0] for c in cursor.description]
        for row in cursor:
            value = dict(zip(names, row))
            byte_count += len(canonical(value))
            if len(result) >= row_limit or byte_count > MAX_BYTES:
                raise OrganizationError(ExecutionCode.UNSUPPORTED, 'Folder ownership snapshot bounded')
            result.append(value)
        return result

    cursor.execute('SAVEPOINT folder_ownership_read')
    try:
        roots = rows('SELECT id,folder FROM root_folders ORDER BY id')
        volumes = rows('''SELECT id,title,year,publisher,volume_number,root_folder,folder,custom_folder,
            metadata_provider,authority_generation,comicvine_id,special_version,special_version_locked,
            monitored,monitor_new_issues FROM volumes ORDER BY id''')
        files = rows('SELECT id,filepath,size FROM active_files ORDER BY id')
        # LEFT JOIN deliberately retains broken issue links rather than hiding
        # them through an inner join and calling the resulting snapshot complete.
        direct = rows('''SELECT b.file_id,b.issue_id,b.forced,i.volume_id FROM issues_files b
            LEFT JOIN issues i ON i.id=b.issue_id ORDER BY b.file_id,b.issue_id''')
        general = rows('SELECT file_id,volume_id,forced,file_type FROM volume_files ORDER BY file_id,volume_id')
        refs = rows('SELECT * FROM volume_external_ids ORDER BY volume_id,provider')
        marker = ','.join('?' for _ in volume_ids)
        issues = rows(f'SELECT id,volume_id,issue_number,calculated_issue_number,title,date FROM issues WHERE volume_id IN ({marker}) ORDER BY id', volume_ids)
        facts = {}
        for table in ('issue_number_facts', 'issue_date_facts', 'issue_variant_of'):
            facts[table] = rows(f'''SELECT f.* FROM {table} f JOIN issues i ON i.id=f.issue_id
                WHERE i.volume_id IN ({marker}) ORDER BY f.issue_id''', volume_ids)
        classification = rows(f'SELECT * FROM classification_provenance WHERE volume_id IN ({marker}) ORDER BY volume_id', volume_ids)
        controls = rows(f'SELECT * FROM classification_control_state WHERE volume_id IN ({marker}) ORDER BY volume_id', volume_ids)
        keys = tuple(f.name for f in fields(NamingSettings))
        settings = {r['key']: r['value'] for r in rows(
            'SELECT key,value FROM config WHERE key IN (' + ','.join('?' for _ in keys) + ') ORDER BY key', keys)}
        for key in ('replace_illegal_characters', 'long_special_version'):
            settings[key] = bool(int(settings[key]))
        for key in ('volume_padding', 'issue_padding'):
            settings[key] = int(settings[key])
        naming = NamingSettings(**settings)
        authorities = capture(cursor, volume_ids)
        if set(authorities) != set(volume_ids):
            raise OrganizationError(ExecutionCode.STALE, 'Exact selected authority missing')
        reservations = load_reservations(cursor)
    finally:
        cursor.execute('RELEASE SAVEPOINT folder_ownership_read')

    volumes_by_id = {v['id']: v for v in volumes}
    roots_by_id = {r['id']: r['folder'] for r in roots}
    files_by_id = {f['id']: f for f in files}
    direct_by_file: dict[int, list] = {}
    general_by_file: dict[int, list] = {}
    owned: dict[int, set[int]] = {}
    for row in direct:
        if row['file_id'] not in files_by_id or row['volume_id'] not in volumes_by_id:
            raise OrganizationError(ExecutionCode.CONFLICT, 'Broken direct file ownership')
        direct_by_file.setdefault(row['file_id'], []).append((row['volume_id'], row['issue_id'], bool(row['forced'])))
        owned.setdefault(row['volume_id'], set()).add(row['file_id'])
    for row in general:
        if row['file_id'] not in files_by_id or row['volume_id'] not in volumes_by_id:
            raise OrganizationError(ExecutionCode.CONFLICT, 'Broken general file ownership')
        general_by_file.setdefault(row['file_id'], []).append((row['volume_id'], bool(row['forced']), row['file_type']))
        owned.setdefault(row['volume_id'], set()).add(row['file_id'])

    selected = []
    for vid in sorted(volume_ids):
        volume = volumes_by_id.get(vid)
        if volume is None or volume['root_folder'] not in roots_by_id or not volume['folder']:
            raise OrganizationError(ExecutionCode.STALE, 'Volume/root ownership missing')
        source = volume['folder']
        prefix = path_key(source).rstrip(os.sep) + os.sep
        # Bounded by 50 volumes, not one SELECT per file. Include all physical
        # DB paths even if unowned, and all volume-owned paths even if outside.
        member_ids = owned.get(vid, set()) | {f['id'] for f in files
            if path_key(f['filepath']).startswith(prefix) or path_key(f['filepath']) == path_key(source)}
        members = tuple(RegisteredTreeFile(fid, files_by_id[fid]['filepath'],
            tuple(direct_by_file.get(fid, ())), tuple(general_by_file.get(fid, ()))) for fid in sorted(member_ids))
        issue_ids = {i['id'] for i in issues if i['volume_id'] == vid}
        state = dict(volume=volume, root=[volume['root_folder'], roots_by_id[volume['root_folder']]],
            authority=asdict(authorities[vid]), files=[files_by_id[fid] for fid in sorted(member_ids)],
            registrations=[asdict(r) for r in members],
            refs=[r for r in refs if r['volume_id'] == vid],
            issues=[i for i in issues if i['id'] in issue_ids],
            facts={name: [r for r in values if r['issue_id'] in issue_ids] for name, values in facts.items()},
            classification=[r for r in classification if r['volume_id'] == vid],
            controls=[r for r in controls if r['volume_id'] == vid], naming=asdict(naming))
        selected.append(FolderOwnership(vid, volume['root_folder'], roots_by_id[volume['root_folder']],
            source, bool(volume['custom_folder']), authorities[vid], members, canonical(state)))
    return FolderOwnershipBatch(tuple(selected), tuple((r['id'], r['folder']) for r in roots),
        tuple((v['id'], v['folder']) for v in volumes if v['folder']),
        tuple((f['id'], f['filepath'], f['size']) for f in files), naming, reservations)
