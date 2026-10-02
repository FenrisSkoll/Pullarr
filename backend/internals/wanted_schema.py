"""Automation history and claims; wantedness itself remains library-derived."""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS wanted_searches(
        id TEXT PRIMARY KEY, volume_id INTEGER NOT NULL, issue_ids TEXT NOT NULL,
        trigger TEXT NOT NULL, state TEXT NOT NULL, target_fingerprint TEXT,
        selection_policy TEXT NOT NULL, source_receipt TEXT NOT NULL DEFAULT '{}',
        counts TEXT NOT NULL DEFAULT '{}', outcome TEXT, error TEXT,
        started_at REAL NOT NULL, finished_at REAL
    )''',
    '''CREATE INDEX IF NOT EXISTS wanted_searches_target ON wanted_searches(volume_id,started_at,id)''',
    '''CREATE TABLE IF NOT EXISTS wanted_decisions(
        id TEXT PRIMARY KEY, search_id TEXT NOT NULL REFERENCES wanted_searches(id),
        authorization TEXT NOT NULL, candidate_id TEXT NOT NULL, source_kind TEXT NOT NULL,
        source_key TEXT NOT NULL, evaluation_id TEXT NOT NULL, scoring_fingerprint TEXT NOT NULL,
        selection_fingerprint TEXT NOT NULL, quality TEXT NOT NULL, issue_ids TEXT NOT NULL,
        title TEXT NOT NULL, state TEXT NOT NULL, mechanism TEXT NOT NULL,
        acquisition_id TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
    )''',
    '''CREATE INDEX IF NOT EXISTS wanted_decisions_state ON wanted_decisions(state,updated_at,id)''',
    '''CREATE TABLE IF NOT EXISTS wanted_acquisitions(
        decision_id TEXT NOT NULL REFERENCES wanted_decisions(id),
        kind TEXT NOT NULL, acquisition_id TEXT NOT NULL,
        PRIMARY KEY(kind,acquisition_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS wanted_reservations(
        decision_id TEXT NOT NULL REFERENCES wanted_decisions(id),
        issue_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        closed_reason TEXT, PRIMARY KEY(decision_id,issue_id)
    )''',
    '''CREATE INDEX IF NOT EXISTS wanted_acquisitions_decision ON wanted_acquisitions(decision_id)''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS wanted_reserved_issue ON wanted_reservations(issue_id) WHERE active=1''',
    '''CREATE TABLE IF NOT EXISTS wanted_schedule(
        issue_id INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
        next_search REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        last_search TEXT REFERENCES wanted_searches(id), requested INTEGER NOT NULL DEFAULT 0
    )''',
    '''CREATE INDEX IF NOT EXISTS wanted_schedule_due ON wanted_schedule(next_search,issue_id)''',
    '''CREATE TABLE IF NOT EXISTS wanted_blocks(
        source_kind TEXT NOT NULL, source_key TEXT NOT NULL, candidate_id TEXT NOT NULL,
        reason TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(source_kind,source_key,candidate_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS wanted_discovery(
        source_key TEXT PRIMARY KEY, cursor REAL NOT NULL, next_search REAL NOT NULL DEFAULT 0,
        error TEXT
    )''',
    '''CREATE INDEX IF NOT EXISTS wanted_blocks_candidate ON wanted_blocks(candidate_id)''',
    '''CREATE TABLE IF NOT EXISTS wanted_discovery_seen(
        source_key TEXT NOT NULL, candidate_id TEXT NOT NULL, observed_at REAL NOT NULL,
        PRIMARY KEY(source_key,candidate_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS wanted_source_retry(
        source_key TEXT PRIMARY KEY, next_request REAL NOT NULL, reason TEXT NOT NULL
    )''',
)

SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
