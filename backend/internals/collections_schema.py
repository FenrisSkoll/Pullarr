"""Schema67: additive local Collections, no library/file/domain row rewriting."""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS collections(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
        created_at REAL NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS collection_nodes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        parent_id INTEGER,
        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
        description TEXT NOT NULL DEFAULT '' CHECK(length(description)<=4000),
        kind TEXT NOT NULL DEFAULT 'unknown' CHECK(kind IN
            ('unknown','series','limited_series','trade_paperback','hardcover','deluxe',
             'omnibus','absolute','one_shot','anthology','compendium','other')),
        position INTEGER NOT NULL DEFAULT 0 CHECK(position>=0),
        monitoring TEXT NOT NULL DEFAULT 'inherit' CHECK(monitoring IN ('inherit','monitored','unmonitored')),
        UNIQUE(id,collection_id), CHECK(parent_id IS NULL OR parent_id!=id),
        FOREIGN KEY(parent_id,collection_id) REFERENCES collection_nodes(id,collection_id) ON DELETE CASCADE
    )''',
    'CREATE UNIQUE INDEX IF NOT EXISTS collection_one_root ON collection_nodes(collection_id) WHERE parent_id IS NULL',
    'CREATE INDEX IF NOT EXISTS collection_children ON collection_nodes(collection_id,parent_id,position,id)',
    '''CREATE TABLE IF NOT EXISTS collection_publications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        local_volume_id INTEGER UNIQUE REFERENCES volumes(id) ON DELETE SET NULL,
        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 500), year INTEGER,
        publisher TEXT NOT NULL DEFAULT '' CHECK(length(publisher)<=500),
        kind TEXT NOT NULL DEFAULT 'unknown' CHECK(kind IN
            ('unknown','series','limited_series','trade_paperback','hardcover','deluxe',
             'omnibus','absolute','one_shot','anthology','compendium','other')),
        kind_source TEXT NOT NULL CHECK(kind_source IN ('unknown','manual')),
        created_at REAL NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS collection_publication_refs(
        publication_id INTEGER NOT NULL REFERENCES collection_publications(id) ON DELETE CASCADE,
        provider TEXT NOT NULL, provider_id TEXT NOT NULL CHECK(length(provider_id) BETWEEN 1 AND 128),
        source TEXT NOT NULL CHECK(source IN ('provider_search','exact_local_identity')),
        PRIMARY KEY(provider,provider_id), UNIQUE(publication_id,provider),
        CHECK(provider IN ('comicvine','metron','gcd'))
    )''',
    'CREATE INDEX IF NOT EXISTS collection_refs_publication ON collection_publication_refs(publication_id)',
    '''CREATE TABLE IF NOT EXISTS collection_memberships(
        node_id INTEGER NOT NULL REFERENCES collection_nodes(id) ON DELETE CASCADE,
        publication_id INTEGER NOT NULL REFERENCES collection_publications(id) ON DELETE CASCADE,
        position INTEGER NOT NULL DEFAULT 0 CHECK(position>=0),
        note TEXT NOT NULL DEFAULT '' CHECK(length(note)<=1000),
        source TEXT NOT NULL CHECK(source IN ('manual','accepted_search_suggestion')),
        evidence TEXT NOT NULL CHECK(json_valid(evidence) AND length(evidence)<=8192),
        accepted_at REAL NOT NULL,
        PRIMARY KEY(node_id,publication_id)
    )''',
    'CREATE INDEX IF NOT EXISTS collection_member_publication ON collection_memberships(publication_id,node_id)',
    '''CREATE TABLE IF NOT EXISTS collection_suggestions(
        id TEXT PRIMARY KEY,
        node_id INTEGER NOT NULL REFERENCES collection_nodes(id) ON DELETE CASCADE,
        provider TEXT NOT NULL, provider_id TEXT NOT NULL,
        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 500), year INTEGER,
        publisher TEXT NOT NULL CHECK(length(publisher)<=500),
        evidence TEXT NOT NULL CHECK(json_valid(evidence) AND length(evidence)<=8192),
        version INTEGER NOT NULL CHECK(version=1),
        decision TEXT NOT NULL CHECK(decision IN ('pending','accepted','rejected','review_later')),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
        publication_id INTEGER REFERENCES collection_publications(id) ON DELETE SET NULL,
        observed_at REAL NOT NULL, decided_at REAL,
        UNIQUE(node_id,provider,provider_id,version),
        CHECK(provider IN ('comicvine','metron','gcd'))
    )''',
    'CREATE INDEX IF NOT EXISTS collection_suggestion_page ON collection_suggestions(node_id,decision,id)',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
