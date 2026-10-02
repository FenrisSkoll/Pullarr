"""Bounded, read-only health snapshot. No model constructors or read repair."""

import sqlite3
from pathlib import Path

from backend.base.library_health import HealthScope, canonical, fingerprint


class HealthSnapshotLimit(Exception):
    pass


def read_snapshot(database: str, scope: HealthScope, row_limit: int) -> dict:
    # mode=ro refuses missing databases; query_only is defense in depth.
    db = sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        result = {}
        byte_count = 0
        queries = {
            'roots': 'SELECT id,folder FROM root_folders ORDER BY id',
            'volumes': '''SELECT id,title,year,volume_number,publisher,metadata_provider,authority_generation,
                root_folder,folder,custom_folder,special_version,special_version_locked,monitored,
                comicvine_id,last_cv_fetch FROM volumes ORDER BY id''',
            'issues': 'SELECT id,volume_id,issue_number,title,date,comicvine_id,monitored FROM issues ORDER BY id',
            'files': 'SELECT id,filepath,size FROM active_files ORDER BY id',
            'direct': 'SELECT * FROM issues_files ORDER BY file_id,issue_id',
            'general': 'SELECT * FROM volume_files ORDER BY file_id,volume_id',
            'volume_refs': 'SELECT * FROM volume_external_ids ORDER BY volume_id,provider',
            'issue_refs': 'SELECT * FROM issue_external_ids ORDER BY issue_id,provider',
            'numbers': 'SELECT * FROM issue_number_facts ORDER BY issue_id',
            'dates': 'SELECT * FROM issue_date_facts ORDER BY issue_id,source_field',
            'variants': 'SELECT * FROM issue_variant_of ORDER BY issue_id',
            'classification': 'SELECT volume_id,applied_value FROM classification_provenance ORDER BY volume_id',
            'coverage': 'SELECT id,file_id,source_issue_id,target_issue_id FROM valid_file_content_coverage ORDER BY id',
        }
        for name, sql in queries.items():
            rows = []
            for row in db.execute(sql):
                value = dict(row)
                byte_count += len(canonical(value))
                if len(rows) >= row_limit or byte_count > 16 * 1024 * 1024:
                    raise HealthSnapshotLimit()
                rows.append(value)
            result[name] = rows
        roots = {r['id']: r for r in result['roots']}
        volumes = {v['id']: v for v in result['volumes']}
        if scope.kind == 'root' and scope.ids[0] not in roots:
            raise ValueError('Unknown health root')
        if scope.kind == 'volumes' and any(i not in volumes for i in scope.ids):
            raise ValueError('Unknown health volume')
        selected = sorted(v for v in volumes if scope.kind == 'library'
            or scope.kind == 'volumes' and v in scope.ids
            or scope.kind == 'root' and volumes[v]['root_folder'] == scope.ids[0])
        if len(selected) > 1000:
            raise HealthSnapshotLimit()
        result['selected'] = selected
        # Policy acquisition is read-only, but its historic loader reads all
        # records. Admit it only behind the same global row/byte ceiling.
        size = db.execute('''SELECT (SELECT COALESCE(SUM(length(description)),0) FROM volumes)
            + (SELECT COALESCE(SUM(length(description)),0) FROM issues)''').fetchone()[0]
        if size > 8 * 1024 * 1024:
            raise HealthSnapshotLimit()
        from backend.implementations.metadata.registry import PROVIDERS
        from backend.internals.organization_plan import load_planning_records
        from backend.internals.provider_identity import MetadataIdentityError

        planning_cursor = db.cursor()
        try:
            result['planning'] = load_planning_records(tuple(PROVIDERS), planning_cursor)
            volumes, issues, root_rows, files, naming = result['planning']
            # sqlite3.Row repr contains an address, not its values. Health
            # evidence must survive a second read on another connection.
            result['planning'] = volumes, issues, tuple(tuple(r) for r in root_rows), files, naming
            result['planning_error'] = False
        except (MetadataIdentityError, ValueError, KeyError, TypeError):
            result['planning'] = None
            result['planning_error'] = True
        finally:
            planning_cursor.close()
        result['digest'] = fingerprint({k: v for k, v in result.items() if k != 'planning'})
        if result['planning'] is not None:
            # Dataclass repr captures the actual settings/facts consumed by the
            # pure policies. No secret settings were selected by their loader.
            result['digest'] = fingerprint((result['digest'], repr(result['planning'])))
        return result
    finally:
        db.rollback()
        db.close()
