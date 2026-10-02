"""Current application provenance, not a metadata/event history."""

STATEMENTS = (
    '''CREATE TABLE classification_state(
        volume_id INTEGER PRIMARY KEY REFERENCES volumes(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL DEFAULT 0,
        invalidated INTEGER NOT NULL DEFAULT 0
    )''',
    '''CREATE TABLE classification_provenance(
        volume_id INTEGER PRIMARY KEY REFERENCES volumes(id) ON DELETE CASCADE,
        schema_version TEXT NOT NULL,
        applied_value TEXT,
        application_kind TEXT NOT NULL,
        source TEXT, reason TEXT, policy_id TEXT,
        evaluated_at TEXT, recorded_at TEXT NOT NULL,
        input_scope TEXT NOT NULL, replay_status TEXT NOT NULL,
        lock_input INTEGER NOT NULL,
        issue_count INTEGER, volume_numbered_count INTEGER,
        issue_date TEXT, age_seconds REAL
    )''',
    '''CREATE TABLE classification_evidence_receipts(
        volume_id INTEGER NOT NULL REFERENCES classification_provenance(volume_id) ON DELETE CASCADE,
        axis TEXT NOT NULL CHECK(axis IN ('physical','publication')),
        availability TEXT NOT NULL, disposition TEXT,
        provider TEXT, provider_id TEXT, source_field TEXT,
        raw_value TEXT CHECK(length(raw_value)<=512), raw_truncated INTEGER NOT NULL,
        normalized_value TEXT,
        PRIMARY KEY(volume_id,axis)
    )''',
    '''CREATE TABLE classification_control_state(
        volume_id INTEGER PRIMARY KEY REFERENCES volumes(id) ON DELETE CASCADE,
        action TEXT NOT NULL, occurred_at TEXT NOT NULL, context TEXT NOT NULL
    )''',
    '''CREATE TRIGGER classification_value_write AFTER UPDATE OF special_version ON volumes BEGIN
        INSERT INTO classification_state(volume_id,revision,invalidated) VALUES(NEW.id,1,1)
        ON CONFLICT(volume_id) DO UPDATE SET revision=revision+1,invalidated=1;
        DELETE FROM classification_provenance WHERE volume_id=NEW.id;
    END''',
    '''CREATE TRIGGER classification_lock_write AFTER UPDATE OF special_version_locked ON volumes BEGIN
        INSERT INTO classification_state(volume_id,revision) VALUES(NEW.id,1)
        ON CONFLICT(volume_id) DO UPDATE SET revision=revision+1;
        DELETE FROM classification_control_state WHERE volume_id=NEW.id;
    END''',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
