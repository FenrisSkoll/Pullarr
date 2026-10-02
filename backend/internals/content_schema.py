"""Schema 62: no inferred claims, no changes to direct file associations."""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS bibliographic_content_claims(
        id TEXT PRIMARY KEY,
        target_provider TEXT NOT NULL, target_provider_id TEXT NOT NULL,
        source_provider TEXT NOT NULL, source_provider_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('partial_issue_content','complete_issue_containment')),
        authority TEXT NOT NULL CHECK(authority='operator_confirmed'),
        policy TEXT NOT NULL, created_at REAL NOT NULL, retired_at REAL,
        supersedes TEXT REFERENCES bibliographic_content_claims(id),
        CHECK(target_provider!=source_provider OR target_provider_id!=source_provider_id)
    )''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS content_claim_active_pair
        ON bibliographic_content_claims(target_provider,target_provider_id,source_provider,source_provider_id)
        WHERE retired_at IS NULL''',
    '''CREATE INDEX IF NOT EXISTS content_claim_target
        ON bibliographic_content_claims(target_provider,target_provider_id,created_at)''',
    '''CREATE TABLE IF NOT EXISTS bibliographic_content_claim_evidence(
        claim_id TEXT NOT NULL REFERENCES bibliographic_content_claims(id),
        provider TEXT NOT NULL, edge_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
        origin_issue TEXT NOT NULL, target_issue TEXT NOT NULL,
        origin_story TEXT, target_story TEXT, active INTEGER NOT NULL CHECK(active IN (0,1)),
        PRIMARY KEY(claim_id,provider,edge_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS file_content_coverage(
        id TEXT PRIMARY KEY, file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
        target_issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
        source_issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
        claim_id TEXT NOT NULL REFERENCES bibliographic_content_claims(id),
        original_file_id INTEGER NOT NULL, original_target_id INTEGER NOT NULL,
        original_source_id INTEGER NOT NULL, policy TEXT NOT NULL,
        created_at REAL NOT NULL, retired_at REAL
    )''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS coverage_active_file_source
        ON file_content_coverage(file_id,source_issue_id) WHERE retired_at IS NULL''',
    'CREATE INDEX IF NOT EXISTS coverage_source ON file_content_coverage(source_issue_id,file_id)',
    'CREATE INDEX IF NOT EXISTS coverage_target ON file_content_coverage(target_issue_id)',
    'CREATE INDEX IF NOT EXISTS coverage_claim ON file_content_coverage(claim_id)',
    '''CREATE VIEW IF NOT EXISTS valid_file_content_coverage AS
        SELECT c.id,c.file_id,c.target_issue_id,c.source_issue_id,c.claim_id
        FROM file_content_coverage c
        JOIN bibliographic_content_claims k ON k.id=c.claim_id
        JOIN files f ON f.id=c.file_id
        JOIN issues ti ON ti.id=c.target_issue_id
        JOIN volumes tv ON tv.id=ti.volume_id AND tv.metadata_provider=k.target_provider
        JOIN issue_external_ids tx ON tx.issue_id=ti.id AND tx.provider=k.target_provider
            AND tx.provider_id=k.target_provider_id
        JOIN issues si ON si.id=c.source_issue_id
        JOIN volumes sv ON sv.id=si.volume_id AND sv.metadata_provider=k.source_provider
        JOIN issue_external_ids sx ON sx.issue_id=si.id AND sx.provider=k.source_provider
            AND sx.provider_id=k.source_provider_id
        JOIN issues_files direct ON direct.file_id=c.file_id AND direct.issue_id=c.target_issue_id
        WHERE c.retired_at IS NULL AND k.retired_at IS NULL
            AND k.kind='complete_issue_containment' AND k.authority='operator_confirmed'
            AND k.policy='kapowarr-collected-content/v1'
            AND c.policy='kapowarr-collected-content/v1' ''',
    '''CREATE VIEW IF NOT EXISTS canonical_issue_files AS
        SELECT d.issue_id,d.file_id,'direct' AS role,NULL AS coverage_id,
            NULL AS target_issue_id,NULL AS claim_id
        FROM issues_files d JOIN files f ON f.id=d.file_id
        UNION ALL
        SELECT source_issue_id,file_id,'collected',id,target_issue_id,claim_id
        FROM valid_file_content_coverage''',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
