"""Schema 59 canonical evidence, separate from legacy compatibility columns."""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS issue_number_facts(
        issue_id INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
        raw_label TEXT,
        provenance TEXT NOT NULL,
        source_field TEXT NOT NULL,
        interpretation TEXT NOT NULL CHECK(interpretation IN
            ('numeric','suffixed','opaque','unnumbered','absent')),
        numeric_text TEXT,
        policy TEXT NOT NULL,
        selected_date_field TEXT,
        provider_ordinal INTEGER,
        ordinal_provenance TEXT,
        CHECK((interpretation='numeric') = (numeric_text IS NOT NULL)),
        CHECK((interpretation='absent') = (raw_label IS NULL)),
        CHECK((provider_ordinal IS NULL) = (ordinal_provenance IS NULL))
    )''',
    '''CREATE TABLE IF NOT EXISTS issue_date_facts(
        issue_id INTEGER NOT NULL REFERENCES issue_number_facts(issue_id) ON DELETE CASCADE,
        source_field TEXT NOT NULL,
        raw_value TEXT,
        kind TEXT NOT NULL,
        provenance TEXT NOT NULL,
        year INTEGER,
        month INTEGER,
        day INTEGER,
        precision TEXT NOT NULL CHECK(precision IN
            ('day','month','year','unknown','unsupported_text')),
        zero_placeholders INTEGER NOT NULL CHECK(zero_placeholders IN (0,1)),
        uncertainty TEXT,
        policy TEXT NOT NULL,
        PRIMARY KEY(issue_id,source_field),
        CHECK(year IS NULL OR year BETWEEN 1 AND 9999),
        CHECK(month IS NULL OR month BETWEEN 1 AND 12),
        CHECK(day IS NULL OR day BETWEEN 1 AND 31)
    )''',
    'CREATE INDEX IF NOT EXISTS issue_date_kind_index ON issue_date_facts(kind,issue_id)',
    '''CREATE TABLE IF NOT EXISTS issue_variant_of(
        issue_id INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
        base_provider TEXT NOT NULL,
        base_provider_id TEXT NOT NULL,
        provenance TEXT NOT NULL,
        CHECK(length(base_provider)>0 AND length(base_provider_id)>0 AND length(provenance)>0)
    )''',
    '''CREATE TRIGGER IF NOT EXISTS issues_facts_invalidate
        AFTER UPDATE OF issue_number,calculated_issue_number,date ON issues
        WHEN OLD.issue_number IS NOT NEW.issue_number
            OR OLD.calculated_issue_number IS NOT NEW.calculated_issue_number
            OR OLD.date IS NOT NEW.date
        BEGIN DELETE FROM issue_number_facts WHERE issue_id=NEW.id; END''',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'


def nullable_projection(sql: str) -> str:
    return sql.replace('calculated_issue_number FLOAT(20) NOT NULL',
                       'calculated_issue_number FLOAT(20)')
