"""Additive, versioned Library Import review receipts (schema 73).

Rows are separate so an operator can save or inspect a large draft without
rewriting a whole-library JSON document. Journal jobs remain execution authority.
"""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS library_import_drafts(
        id TEXT PRIMARY KEY,
        version INTEGER NOT NULL CHECK(version=3),
        state TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0),
        rename INTEGER NOT NULL CHECK(rename IN (0,1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        scope TEXT NOT NULL,
        delegate TEXT,
        result TEXT
    )''',
    '''CREATE INDEX IF NOT EXISTS library_import_drafts_updated
       ON library_import_drafts(updated_at,id)''',
    '''CREATE TABLE IF NOT EXISTS library_import_draft_rows(
        draft_id TEXT NOT NULL REFERENCES library_import_drafts(id),
        ordinal INTEGER NOT NULL,
        row_id TEXT NOT NULL,
        source TEXT NOT NULL,
        selection TEXT NOT NULL,
        plan TEXT NOT NULL,
        stamp TEXT NOT NULL,
        receipt TEXT,
        PRIMARY KEY(draft_id,ordinal),
        UNIQUE(draft_id,row_id)
    )''',
    '''CREATE INDEX IF NOT EXISTS library_import_draft_rows_source
       ON library_import_draft_rows(draft_id,source)''',
)

SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
