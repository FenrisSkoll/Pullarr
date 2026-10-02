"""Schema 55: bounded current evidence, not an infinite notification log."""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS monitor_roots(
        root_id INTEGER PRIMARY KEY REFERENCES root_folders(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        health TEXT NOT NULL DEFAULT 'unknown',
        generation INTEGER NOT NULL DEFAULT 0,
        requested INTEGER NOT NULL DEFAULT 1,
        completed_at REAL,
        attempted_at REAL,
        device INTEGER,
        inode INTEGER,
        error TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS monitor_paths(
        root_id INTEGER NOT NULL REFERENCES monitor_roots(root_id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        device INTEGER NOT NULL,
        inode INTEGER NOT NULL,
        directory INTEGER NOT NULL,
        generation INTEGER NOT NULL,
        stable_since REAL NOT NULL,
        samples INTEGER NOT NULL,
        change TEXT NOT NULL,
        status TEXT NOT NULL,
        checked_at REAL NOT NULL DEFAULT 0,
        reason TEXT,
        job_id TEXT REFERENCES organization_jobs(id),
        PRIMARY KEY(root_id,path)
    )''',
    '''CREATE INDEX IF NOT EXISTS monitor_paths_pending
       ON monitor_paths(root_id,status,stable_since)''',
    '''CREATE TABLE IF NOT EXISTS monitor_staging(
        root_id INTEGER NOT NULL REFERENCES monitor_roots(root_id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        device INTEGER NOT NULL,
        inode INTEGER NOT NULL,
        directory INTEGER NOT NULL,
        PRIMARY KEY(root_id,path)
    )''',
)

SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
