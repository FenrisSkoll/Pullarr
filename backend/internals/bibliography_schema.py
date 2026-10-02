"""Schema 60: source-owned bibliography, no global story/edition identity."""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS volume_bibliography(
        volume_id INTEGER PRIMARY KEY REFERENCES volumes(id) ON DELETE CASCADE,
        provider TEXT NOT NULL, policy TEXT NOT NULL,
        binding TEXT, publishing_format TEXT, color TEXT, dimensions TEXT, paper_stock TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS publication_bibliography_diagnostics(
        volume_id INTEGER NOT NULL REFERENCES volume_bibliography(volume_id) ON DELETE CASCADE,
        code TEXT NOT NULL, PRIMARY KEY(volume_id,code)
    )''',
    '''CREATE TABLE IF NOT EXISTS issue_bibliography(
        issue_id INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
        provider TEXT NOT NULL, policy TEXT NOT NULL,
        isbn TEXT, isbn_normalized TEXT, isbn_validity TEXT NOT NULL DEFAULT 'unknown',
        barcode TEXT, page_count TEXT, page_count_numeric TEXT, variant_name TEXT,
        indicia_publisher TEXT, indicia_printer TEXT, brand TEXT, rating TEXT,
        indicia_frequency TEXT, cover_reference TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS bibliography_diagnostics(
        issue_id INTEGER NOT NULL REFERENCES issue_bibliography(issue_id) ON DELETE CASCADE,
        code TEXT NOT NULL, PRIMARY KEY(issue_id,code)
    )''',
    '''CREATE TABLE IF NOT EXISTS story_observation_sets(
        id INTEGER PRIMARY KEY,
        issue_id INTEGER NOT NULL REFERENCES issue_bibliography(issue_id) ON DELETE CASCADE,
        provider TEXT NOT NULL, policy TEXT NOT NULL, scope TEXT NOT NULL,
        digest TEXT NOT NULL, observed_at REAL NOT NULL, observation_count INTEGER NOT NULL
    )''',
    'CREATE INDEX IF NOT EXISTS story_set_issue_index ON story_observation_sets(issue_id,id)',
    '''CREATE TABLE IF NOT EXISTS story_observations(
        id INTEGER PRIMARY KEY, set_id INTEGER NOT NULL REFERENCES story_observation_sets(id) ON DELETE CASCADE,
        position INTEGER NOT NULL, source_position INTEGER NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('identified_story','issue_scoped_story_observation')),
        provider_story_id TEXT, sequence TEXT, story_type TEXT, title TEXT, feature TEXT,
        page_count TEXT, page_count_numeric TEXT, characters TEXT, genre TEXT,
        UNIQUE(set_id,position), UNIQUE(set_id,source_position),
        CHECK((mode='identified_story')=(provider_story_id IS NOT NULL))
    )''',
    '''CREATE TABLE IF NOT EXISTS story_credit_observations(
        observation_id INTEGER NOT NULL REFERENCES story_observations(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('script','pencils','inks','colors','letters','editing')),
        text TEXT, PRIMARY KEY(observation_id,role)
    )''',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
