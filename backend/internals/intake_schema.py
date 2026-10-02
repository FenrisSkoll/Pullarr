"""Additive completion-to-organizer correlations; no duplicate mutation journal."""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS acquisition_intakes(
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        download_id TEXT NOT NULL,
        completion TEXT NOT NULL,
        completion_digest TEXT NOT NULL,
        local_root TEXT,
        mapped_paths TEXT NOT NULL DEFAULT '[]',
        mapping_fingerprint TEXT,
        rename INTEGER NOT NULL CHECK(rename IN (0,1)),
        auto_apply INTEGER NOT NULL CHECK(auto_apply IN (0,1)),
        state TEXT NOT NULL DEFAULT 'pending',
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        next_observation REAL NOT NULL DEFAULT 0,
        retry_count INTEGER NOT NULL DEFAULT 0,
        preparation TEXT NOT NULL DEFAULT '{}',
        prepared_paths TEXT NOT NULL DEFAULT '[]',
        UNIQUE(kind,download_id)
    )''',
    '''CREATE INDEX IF NOT EXISTS acquisition_intakes_pending
       ON acquisition_intakes(state,next_observation,id)''',
    '''CREATE TABLE IF NOT EXISTS acquisition_artifacts(
        id TEXT PRIMARY KEY,
        intake_id TEXT NOT NULL REFERENCES acquisition_intakes(id),
        path TEXT NOT NULL,
        stamp TEXT NOT NULL,
        stable_since REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'observing',
        candidate_id TEXT,
        organization_job_id TEXT REFERENCES organization_jobs(id),
        final_file_id INTEGER,
        summary TEXT NOT NULL DEFAULT '{}',
        error TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE(intake_id,path)
    )''',
    '''CREATE INDEX IF NOT EXISTS acquisition_artifacts_intake
       ON acquisition_artifacts(intake_id,id)''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS acquisition_artifacts_job
       ON acquisition_artifacts(organization_job_id)
       WHERE organization_job_id IS NOT NULL''',
    '''CREATE TABLE IF NOT EXISTS acquisition_path_mappings(
        id TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        client_instance TEXT NOT NULL,
        remote_prefix TEXT NOT NULL,
        remote_style TEXT NOT NULL CHECK(remote_style IN ('posix','windows')),
        local_root TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        local_prefix TEXT
    )''',
)

SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
