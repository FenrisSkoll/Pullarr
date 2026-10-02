"""Additive local sequences and bounded accepted/pending source snapshots."""

STATEMENTS = (
    '''CREATE TABLE reading_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 500),
        description TEXT NOT NULL DEFAULT '' CHECK(length(description)<=8000),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
        created_at REAL NOT NULL
    )''',
    '''CREATE TABLE reading_order_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL REFERENCES reading_orders(id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK(position>=0),
        issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
        source TEXT NOT NULL CHECK(json_valid(source)),
        provenance TEXT NOT NULL CHECK(json_valid(provenance)),
        match_kind TEXT NOT NULL CHECK(match_kind IN ('manual','exact','unresolved','ambiguous')),
        UNIQUE(order_id,position)
    )''',
    '''CREATE INDEX reading_order_issue ON reading_order_entries(issue_id)''',
    '''CREATE TABLE reading_order_entry_refs (
        entry_id INTEGER NOT NULL REFERENCES reading_order_entries(id) ON DELETE CASCADE,
        provider TEXT NOT NULL CHECK(provider IN ('comicvine','metron','gcd')),
        issue_ref TEXT NOT NULL CHECK(length(issue_ref) BETWEEN 1 AND 19 AND issue_ref NOT GLOB '*[^0-9]*' AND substr(issue_ref,1,1)!='0'),
        volume_ref TEXT,
        PRIMARY KEY(entry_id,provider,issue_ref)
    )''',
    '''CREATE INDEX reading_order_ref_lookup ON reading_order_entry_refs(provider,issue_ref)''',
    '''CREATE TABLE reading_order_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL UNIQUE REFERENCES reading_orders(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('cbl_url','metron_list')),
        locator TEXT NOT NULL CHECK(length(locator)<=2048),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
        etag TEXT, last_modified TEXT, checked_at REAL, success_at REAL,
        digest TEXT, ignored_digest TEXT,
        accepted TEXT CHECK(accepted IS NULL OR json_valid(accepted)),
        pending TEXT CHECK(pending IS NULL OR json_valid(pending)),
        pending_digest TEXT,
        error TEXT
    )''',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
