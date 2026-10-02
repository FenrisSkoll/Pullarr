"""Additive organizer journal; never cascade history with library deletion."""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS organization_jobs(
        id TEXT PRIMARY KEY,
        plan_digest TEXT NOT NULL UNIQUE,
        executor_version TEXT NOT NULL,
        intent TEXT NOT NULL,
        intent_digest TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        claim TEXT,
        inverse_of TEXT REFERENCES organization_jobs(id),
        batch_id TEXT,
        error TEXT
    )''',
    '''CREATE INDEX IF NOT EXISTS organization_jobs_state_created
       ON organization_jobs(state,created_at)''',
    '''CREATE INDEX IF NOT EXISTS organization_jobs_batch
       ON organization_jobs(batch_id)''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS organization_jobs_inverse
       ON organization_jobs(inverse_of) WHERE inverse_of IS NOT NULL''',
    '''CREATE TABLE IF NOT EXISTS organization_steps(
        job_id TEXT NOT NULL REFERENCES organization_jobs(id),
        ordinal INTEGER NOT NULL,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT '{}',
        evidence_digest TEXT NOT NULL DEFAULT '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
        PRIMARY KEY(job_id,ordinal)
    )''',
    '''CREATE TABLE IF NOT EXISTS organization_events(
        id INTEGER PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES organization_jobs(id),
        ordinal INTEGER,
        created_at TEXT NOT NULL,
        event TEXT NOT NULL,
        detail TEXT NOT NULL
    )''',
    '''CREATE INDEX IF NOT EXISTS organization_events_job
       ON organization_events(job_id,id)''',
    '''CREATE TABLE IF NOT EXISTS organization_reservations(
        path_key TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES organization_jobs(id)
    )''',
)

SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
