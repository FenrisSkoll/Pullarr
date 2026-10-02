"""Schema 56: durable selection/submission and observation, not comic import."""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS acquisition_downloads(
        id TEXT PRIMARY KEY,
        intent_digest TEXT NOT NULL,
        intent TEXT NOT NULL,
        client_id TEXT NOT NULL,
        client_instance TEXT NOT NULL,
        state TEXT NOT NULL,
        nzo_id TEXT,
        nzb_digest TEXT,
        nzb_size INTEGER,
        observation TEXT NOT NULL DEFAULT '{}',
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        submitted_at TEXT,
        completed_at TEXT,
        observed_at TEXT,
        UNIQUE(client_instance,nzo_id)
    )''',
    '''CREATE INDEX IF NOT EXISTS acquisition_downloads_poll
       ON acquisition_downloads(state,observed_at,id)''',
    '''CREATE TABLE IF NOT EXISTS acquisition_download_events(
        id INTEGER PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES acquisition_downloads(id),
        created_at TEXT NOT NULL,
        state TEXT NOT NULL,
        error TEXT
    )''',
    '''CREATE INDEX IF NOT EXISTS acquisition_download_events_job
       ON acquisition_download_events(job_id,id)''',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
