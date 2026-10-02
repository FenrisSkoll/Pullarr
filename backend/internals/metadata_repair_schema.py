"""Successful same-authority DB repair audit; no pending snapshots or undo."""

STATEMENTS = (
    '''CREATE TABLE metadata_repair_receipts(
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL UNIQUE,
        volume_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        authority_generation INTEGER NOT NULL CHECK(authority_generation>=0),
        revision INTEGER NOT NULL CHECK(revision>=0),
        review_digest TEXT NOT NULL CHECK(length(review_digest)=64),
        local_digest TEXT NOT NULL CHECK(length(local_digest)=64),
        target_digest TEXT NOT NULL CHECK(length(target_digest)=64),
        worklist_id TEXT NOT NULL,
        worklist_digest TEXT NOT NULL CHECK(length(worklist_digest)=64),
        policy TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        field_count INTEGER NOT NULL CHECK(field_count BETWEEN 1 AND 40000),
        classification_action TEXT NOT NULL,
        classification_summary TEXT NOT NULL CHECK(json_valid(classification_summary)),
        bibliography_action TEXT NOT NULL
    )''',
    '''CREATE INDEX metadata_repair_history ON metadata_repair_receipts(volume_id,applied_at,id)''',
    '''CREATE TABLE metadata_repair_fields(
        repair_id TEXT NOT NULL REFERENCES metadata_repair_receipts(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        scope TEXT NOT NULL CHECK(scope IN ('volume','issue')),
        local_id INTEGER NOT NULL,
        field TEXT NOT NULL,
        before_value TEXT NOT NULL CHECK(json_valid(before_value)),
        after_value TEXT NOT NULL CHECK(json_valid(after_value)),
        PRIMARY KEY(repair_id,ordinal),
        UNIQUE(repair_id,scope,local_id,field)
    )''',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
