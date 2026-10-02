"""Schema 66: retained file identity, not generic soft deletion or purge.

The marker is written only by the versioned quarantine journal effect.
Historical content schema/migrations stay immutable. Current ownership uses
active_files, while historical coverage continues to reference files.id.
"""

from backend.internals.content_schema import STATEMENTS as CONTENT_STATEMENTS

TABLES = (
    '''CREATE TABLE quarantined_files(
        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE RESTRICT,
        job_id TEXT NOT NULL UNIQUE REFERENCES organization_jobs(id) ON DELETE RESTRICT,
        version INTEGER NOT NULL CHECK(version=1),
        original_filepath TEXT NOT NULL CHECK(length(original_filepath)>0),
        quarantine_filepath TEXT NOT NULL UNIQUE CHECK(length(quarantine_filepath)>0),
        quarantined_at TEXT NOT NULL,
        CHECK(original_filepath!=quarantine_filepath)
    )''',
    '''CREATE VIEW active_files AS SELECT f.id,f.filepath,f.size FROM files f
        WHERE NOT EXISTS(SELECT 1 FROM quarantined_files q WHERE q.file_id=f.id)''',
)

# Reuse the exact historical validity predicates; only artifact activity changes.
VIEWS = (
    'DROP VIEW canonical_issue_files',
    'DROP VIEW valid_file_content_coverage',
    *(sql.replace('JOIN files f', 'JOIN active_files f')
      for sql in CONTENT_STATEMENTS if sql.startswith('CREATE VIEW')),
)

GUARDS = (
    '''CREATE TRIGGER quarantine_file_delete BEFORE DELETE ON files
        WHEN EXISTS(SELECT 1 FROM quarantined_files WHERE file_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'quarantined_file_retained'); END''',
    '''CREATE TRIGGER quarantine_file_update BEFORE UPDATE ON files
        WHEN EXISTS(SELECT 1 FROM quarantined_files WHERE file_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'quarantined_file_retained'); END''',
    '''CREATE TRIGGER quarantine_marker_path BEFORE INSERT ON quarantined_files
        WHEN NOT EXISTS(SELECT 1 FROM files WHERE id=NEW.file_id
                        AND filepath=NEW.quarantine_filepath)
        BEGIN SELECT RAISE(ABORT,'quarantine_path_mismatch'); END''',
    '''CREATE TRIGGER quarantine_marker_immutable BEFORE UPDATE ON quarantined_files
        BEGIN SELECT RAISE(ABORT,'quarantine_marker_immutable'); END''',
    *(f'''CREATE TRIGGER quarantine_{table}_{operation.lower()}
        BEFORE {operation} ON {table}
        WHEN EXISTS(SELECT 1 FROM quarantined_files WHERE file_id=NEW.file_id)
        BEGIN SELECT RAISE(ABORT,'quarantined_file_inactive'); END'''
      for table in ('issues_files', 'volume_files') for operation in ('INSERT', 'UPDATE')),
)

STATEMENTS = TABLES + VIEWS + GUARDS
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
