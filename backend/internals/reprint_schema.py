"""Schema 61. Catalog identities never reuse REST observation or managed IDs."""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS bibliographic_graph_snapshots(
        id TEXT PRIMARY KEY, provider TEXT NOT NULL, policy TEXT NOT NULL,
        source_policy TEXT NOT NULL, fingerprint TEXT NOT NULL, observed_at REAL NOT NULL,
        seed_count INTEGER NOT NULL, edge_count INTEGER NOT NULL, story_count INTEGER NOT NULL,
        creator_count INTEGER NOT NULL, external_count INTEGER NOT NULL, digest TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS bibliographic_issue_refs(
        provider TEXT NOT NULL, provider_id TEXT NOT NULL, series_id TEXT NOT NULL,
        series_name TEXT NOT NULL, number TEXT NOT NULL, title TEXT NOT NULL,
        deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
        snapshot_id TEXT NOT NULL REFERENCES bibliographic_graph_snapshots(id),
        PRIMARY KEY(provider,provider_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS bibliographic_story_entities(
        provider TEXT NOT NULL, provider_id TEXT NOT NULL, issue_id TEXT NOT NULL,
        title TEXT NOT NULL, sequence INTEGER NOT NULL, deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
        snapshot_id TEXT NOT NULL REFERENCES bibliographic_graph_snapshots(id),
        PRIMARY KEY(provider,provider_id),
        FOREIGN KEY(provider,issue_id) REFERENCES bibliographic_issue_refs(provider,provider_id)
    )''',
    'CREATE INDEX IF NOT EXISTS graph_story_issue ON bibliographic_story_entities(provider,issue_id)',
    '''CREATE TABLE IF NOT EXISTS bibliographic_creator_entities(
        provider TEXT NOT NULL, provider_id TEXT NOT NULL, name TEXT NOT NULL,
        deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
        snapshot_id TEXT NOT NULL REFERENCES bibliographic_graph_snapshots(id),
        PRIMARY KEY(provider,provider_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS bibliographic_creator_names(
        provider TEXT NOT NULL, provider_id TEXT NOT NULL, creator_id TEXT NOT NULL,
        name TEXT NOT NULL, official INTEGER NOT NULL CHECK(official IN (0,1)), name_type_id TEXT,
        deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
        snapshot_id TEXT NOT NULL REFERENCES bibliographic_graph_snapshots(id),
        PRIMARY KEY(provider,provider_id),
        FOREIGN KEY(provider,creator_id) REFERENCES bibliographic_creator_entities(provider,provider_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS bibliographic_story_credits(
        provider TEXT NOT NULL, provider_id TEXT NOT NULL, story_id TEXT NOT NULL,
        name_id TEXT NOT NULL, role_id TEXT NOT NULL, role TEXT NOT NULL,
        credited_as TEXT NOT NULL, signed_as TEXT NOT NULL, credited INTEGER NOT NULL,
        signed INTEGER NOT NULL, uncertain INTEGER NOT NULL, deleted INTEGER NOT NULL,
        snapshot_id TEXT NOT NULL REFERENCES bibliographic_graph_snapshots(id),
        PRIMARY KEY(provider,provider_id),
        FOREIGN KEY(provider,story_id) REFERENCES bibliographic_story_entities(provider,provider_id),
        FOREIGN KEY(provider,name_id) REFERENCES bibliographic_creator_names(provider,provider_id)
    )''',
    'CREATE INDEX IF NOT EXISTS graph_credit_story ON bibliographic_story_credits(provider,story_id)',
    '''CREATE TABLE IF NOT EXISTS bibliographic_reprint_edges(
        provider TEXT NOT NULL, provider_id TEXT NOT NULL,
        origin_issue TEXT NOT NULL, target_issue TEXT NOT NULL, origin_story TEXT, target_story TEXT,
        notes TEXT NOT NULL, modified TEXT NOT NULL,
        snapshot_id TEXT NOT NULL REFERENCES bibliographic_graph_snapshots(id),
        PRIMARY KEY(provider,provider_id), CHECK(origin_issue!=target_issue),
        FOREIGN KEY(provider,origin_issue) REFERENCES bibliographic_issue_refs(provider,provider_id),
        FOREIGN KEY(provider,target_issue) REFERENCES bibliographic_issue_refs(provider,provider_id),
        FOREIGN KEY(provider,origin_story) REFERENCES bibliographic_story_entities(provider,provider_id),
        FOREIGN KEY(provider,target_story) REFERENCES bibliographic_story_entities(provider,provider_id)
    )''',
    'CREATE INDEX IF NOT EXISTS graph_origin_issue ON bibliographic_reprint_edges(provider,origin_issue,provider_id)',
    'CREATE INDEX IF NOT EXISTS graph_target_issue ON bibliographic_reprint_edges(provider,target_issue,provider_id)',
    '''CREATE TABLE IF NOT EXISTS bibliographic_graph_scopes(
        provider TEXT NOT NULL, issue_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL REFERENCES bibliographic_graph_snapshots(id),
        PRIMARY KEY(provider,issue_id),
        FOREIGN KEY(provider,issue_id) REFERENCES bibliographic_issue_refs(provider,provider_id)
    )''',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
