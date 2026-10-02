"""Schema 68: observations and bounded sync receipts, not another issue model."""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS release_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id INTEGER UNIQUE REFERENCES issues(id) ON DELETE CASCADE,
        publication_id INTEGER UNIQUE REFERENCES collection_publications(id) ON DELETE CASCADE,
        CHECK((issue_id IS NOT NULL) != (publication_id IS NOT NULL))
    )''',
    '''CREATE TABLE IF NOT EXISTS release_event_evidence(
        event_id INTEGER NOT NULL REFERENCES release_events(id) ON DELETE CASCADE,
        provider TEXT NOT NULL CHECK(provider IN ('comicvine','metron','gcd')),
        provider_id TEXT NOT NULL CHECK(length(provider_id) BETWEEN 1 AND 64),
        source_field TEXT NOT NULL CHECK(length(source_field) BETWEEN 1 AND 100),
        date TEXT CHECK(date IS NULL OR length(date) IN (4,7,10)),
        precision TEXT NOT NULL CHECK(precision IN ('day','month','year','unknown')),
        kind TEXT NOT NULL CHECK(kind IN ('on_sale','store','release','publication','cover','legacy_selected_unknown','unknown_provider_date')),
        provenance TEXT NOT NULL CHECK(length(provenance)<=200),
        fetched_at REAL NOT NULL,
        current INTEGER NOT NULL CHECK(current IN (0,1)),
        previous_date TEXT, previous_precision TEXT,
        PRIMARY KEY(event_id,provider,provider_id,source_field)
    )''',
    'CREATE INDEX IF NOT EXISTS release_evidence_date ON release_event_evidence(date,event_id)',
    '''CREATE TABLE IF NOT EXISTS calendar_sync_state(
        id TEXT PRIMARY KEY CHECK(length(id)=32),
        started_at REAL NOT NULL, completed_at REAL,
        state TEXT NOT NULL CHECK(state IN ('queued','running','complete','partial','bounded','cancelled','interrupted')),
        total INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0,
        providers TEXT NOT NULL DEFAULT '[]' CHECK(length(providers)<=65536)
    )''',
    'CREATE INDEX IF NOT EXISTS calendar_sync_order ON calendar_sync_state(started_at DESC,id)',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
