"""Schema 71: bounded observations only; no network or acquisition on migration."""

STATEMENTS = (
    """CREATE TABLE discovery_sources(
        key TEXT PRIMARY KEY CHECK(key='getcomics'),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        automatic INTEGER NOT NULL DEFAULT 0 CHECK(automatic IN (0,1)),
        interval_minutes INTEGER NOT NULL DEFAULT 60 CHECK(interval_minutes BETWEEN 30 AND 1440),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
        etag TEXT, last_modified TEXT, last_checked REAL, last_success REAL,
        next_poll REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
        transport TEXT, error TEXT, gap INTEGER NOT NULL DEFAULT 0 CHECK(gap IN (0,1)),
        receipt TEXT NOT NULL DEFAULT '{}'
    )""",
    "INSERT INTO discovery_sources(key) VALUES('getcomics')",
    """CREATE TABLE discovery_posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL REFERENCES discovery_sources(key),
        guid TEXT, url TEXT NOT NULL, title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 1000),
        categories TEXT NOT NULL, published_at TEXT, published_precision TEXT NOT NULL
            CHECK(published_precision IN ('instant','day','unavailable')),
        source_updated_at TEXT, year_text TEXT NOT NULL, size_text TEXT NOT NULL, size_bytes INTEGER,
        summary TEXT NOT NULL CHECK(length(summary)<=2000),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('rss','atom','html')),
        release_kind TEXT NOT NULL CHECK(release_kind IN ('release','non_release','uncertain')),
        first_seen REAL NOT NULL, last_seen REAL NOT NULL, last_changed REAL NOT NULL,
        digest TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
        UNIQUE(source,guid), UNIQUE(source,url)
    )""",
    'CREATE INDEX discovery_recent ON discovery_posts(source,first_seen DESC,id DESC)',
    'CREATE INDEX discovery_year ON discovery_posts(year_text,id DESC)',
    'CREATE INDEX discovery_kind ON discovery_posts(release_kind,id DESC)',
    """CREATE TABLE discovery_categories(
        post_id INTEGER NOT NULL REFERENCES discovery_posts(id) ON DELETE CASCADE,
        category TEXT NOT NULL CHECK(length(category)<=100), PRIMARY KEY(post_id,category)
    )""",
    'CREATE INDEX discovery_category ON discovery_categories(category,post_id)',
    """CREATE TABLE discovery_details(
        post_id INTEGER PRIMARY KEY REFERENCES discovery_posts(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, fetched_at REAL NOT NULL, digest TEXT NOT NULL,
        facts TEXT NOT NULL CHECK(length(facts)<=65536)
    )""",
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
