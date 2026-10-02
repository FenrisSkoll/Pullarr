"""Additive authority generation and compact successful-transition receipts.

No pending review sessions, remote payloads or historical metadata snapshots.
The application layer must insert success receipts in its authority transaction.
"""

GENERATION_COLUMN = 'authority_generation INTEGER NOT NULL DEFAULT 0 CHECK(authority_generation>=0)'

STATEMENTS = (
    'ALTER TABLE volumes ADD COLUMN ' + GENERATION_COLUMN,
    '''CREATE TABLE provider_switch_receipts(
        id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 128),
        volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL UNIQUE CHECK(length(session_id) BETWEEN 1 AND 128),
        revision INTEGER NOT NULL CHECK(revision>0),
        source_provider TEXT NOT NULL, source_provider_id TEXT NOT NULL,
        source_generation INTEGER NOT NULL CHECK(source_generation>=0),
        target_provider TEXT NOT NULL, target_provider_id TEXT NOT NULL,
        target_generation INTEGER NOT NULL,
        policy TEXT NOT NULL, schema_version TEXT NOT NULL,
        mapping_digest TEXT NOT NULL CHECK(length(mapping_digest)=64),
        local_digest TEXT NOT NULL CHECK(length(local_digest)=64),
        target_digest TEXT NOT NULL CHECK(length(target_digest)=64),
        mapped_count INTEGER NOT NULL CHECK(mapped_count BETWEEN 0 AND 10000),
        added_count INTEGER NOT NULL CHECK(added_count BETWEEN 0 AND 10000),
        unresolved_count INTEGER NOT NULL DEFAULT 0 CHECK(unresolved_count=0),
        claim_count INTEGER NOT NULL CHECK(claim_count BETWEEN 0 AND 20000),
        coverage_count INTEGER NOT NULL CHECK(coverage_count BETWEEN 0 AND 20000),
        classification_action TEXT NOT NULL,
        classification_value TEXT, classification_policy TEXT,
        classification_source TEXT, classification_reason TEXT,
        applied_at TEXT NOT NULL,
        actor TEXT NOT NULL CHECK(actor='operator'),
        CHECK(source_provider!=target_provider),
        CHECK(target_generation=source_generation+1),
        CHECK(mapped_count+added_count<=10000),
        UNIQUE(volume_id,target_generation)
    )''',
    '''CREATE TABLE provider_switch_issue_receipts(
        switch_id TEXT NOT NULL REFERENCES provider_switch_receipts(id) ON DELETE CASCADE,
        local_issue_id INTEGER NOT NULL,
        source_provider TEXT, source_provider_id TEXT,
        target_provider TEXT NOT NULL, target_provider_id TEXT NOT NULL,
        correspondence TEXT NOT NULL,
        evidence TEXT NOT NULL CHECK(length(evidence)<=4096),
        is_new INTEGER NOT NULL CHECK(is_new IN (0,1)),
        PRIMARY KEY(switch_id,local_issue_id),
        UNIQUE(switch_id,target_provider,target_provider_id),
        CHECK((is_new=1 AND source_provider IS NULL AND source_provider_id IS NULL)
            OR (is_new=0 AND source_provider IS NOT NULL AND source_provider_id IS NOT NULL))
    )''',
    '''CREATE TABLE provider_switch_claim_receipts(
        switch_id TEXT NOT NULL REFERENCES provider_switch_receipts(id) ON DELETE CASCADE,
        old_claim_id TEXT NOT NULL, new_claim_id TEXT NOT NULL,
        PRIMARY KEY(switch_id,old_claim_id),
        UNIQUE(switch_id,new_claim_id)
    )''',
    '''CREATE TABLE provider_switch_coverage_receipts(
        switch_id TEXT NOT NULL REFERENCES provider_switch_receipts(id) ON DELETE CASCADE,
        old_coverage_id TEXT NOT NULL, new_coverage_id TEXT NOT NULL,
        PRIMARY KEY(switch_id,old_coverage_id),
        UNIQUE(switch_id,new_coverage_id)
    )''',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
