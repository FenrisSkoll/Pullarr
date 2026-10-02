"""Schema-52 additive identity storage and statement-atomic legacy shadows.

Keep these statements stable after release: migration 51 uses the same DDL.
No executescript inside the migration transaction (it implicitly commits).
"""

METADATA_PROVIDER_COLUMN = (
    "metadata_provider TEXT NOT NULL DEFAULT 'comicvine' "
    "CHECK (metadata_provider = 'comicvine')"
)

IDENTITY_TABLES = (
    """CREATE TABLE volume_external_ids(
        volume_id INTEGER NOT NULL,
        provider TEXT NOT NULL CHECK(provider IN ('comicvine','metron','gcd')),
        provider_id TEXT NOT NULL CHECK(length(provider_id) > 0),
        provenance TEXT NOT NULL CHECK(length(provenance) > 0),
        last_fetch NUMERIC,
        PRIMARY KEY(volume_id, provider),
        FOREIGN KEY(volume_id) REFERENCES volumes(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX volume_external_ids_lookup
        ON volume_external_ids(provider, provider_id)""",
    """CREATE INDEX volume_external_ids_fetch
        ON volume_external_ids(provider, last_fetch, volume_id)""",
    """CREATE INDEX volumes_metadata_provider_index
        ON volumes(metadata_provider)""",
    """CREATE TABLE issue_external_ids(
        issue_id INTEGER NOT NULL,
        provider TEXT NOT NULL CHECK(provider IN ('comicvine','metron','gcd')),
        provider_id TEXT NOT NULL CHECK(length(provider_id) > 0),
        provenance TEXT NOT NULL CHECK(length(provenance) > 0),
        PRIMARY KEY(issue_id, provider),
        UNIQUE(provider, provider_id),
        FOREIGN KEY(issue_id) REFERENCES issues(id) ON DELETE CASCADE
    )""",
)

# AFTER triggers preserve the domain INSERT's lastrowid. ABORT rolls back the
# offending statement only, preserving refresh's existing partial transactions.
IDENTITY_TRIGGERS = (
    """CREATE TRIGGER volumes_identity_insert AFTER INSERT ON volumes BEGIN
        INSERT INTO volume_external_ids
            (volume_id,provider,provider_id,provenance,last_fetch)
        VALUES (NEW.id,'comicvine',CAST(NEW.comicvine_id AS TEXT),
                'legacy',NEW.last_cv_fetch);
    END""",
    """CREATE TRIGGER issues_identity_insert AFTER INSERT ON issues BEGIN
        INSERT INTO issue_external_ids(issue_id,provider,provider_id,provenance)
        VALUES (NEW.id,'comicvine',CAST(NEW.comicvine_id AS TEXT),'legacy');
    END""",
    """CREATE TRIGGER volumes_identity_update
    AFTER UPDATE OF comicvine_id,last_cv_fetch ON volumes BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM volume_external_ids
            WHERE volume_id=OLD.id AND provider='comicvine'
                AND provider_id=CAST(OLD.comicvine_id AS TEXT)
                AND last_fetch IS OLD.last_cv_fetch
        ) THEN RAISE(ABORT,'ComicVine volume identity shadow conflict') END;
        UPDATE volume_external_ids SET
            provider_id=CAST(NEW.comicvine_id AS TEXT),
            last_fetch=NEW.last_cv_fetch,
            provenance=CASE WHEN OLD.comicvine_id IS NEW.comicvine_id
                THEN provenance ELSE 'legacy' END
        WHERE volume_id=NEW.id AND provider='comicvine';
    END""",
    """CREATE TRIGGER issues_identity_update
    AFTER UPDATE OF comicvine_id ON issues BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM issue_external_ids
            WHERE issue_id=OLD.id AND provider='comicvine'
                AND provider_id=CAST(OLD.comicvine_id AS TEXT)
        ) THEN RAISE(ABORT,'ComicVine issue identity shadow conflict') END;
        UPDATE issue_external_ids SET
            provider_id=CAST(NEW.comicvine_id AS TEXT),
            provenance=CASE WHEN OLD.comicvine_id IS NEW.comicvine_id
                THEN provenance ELSE 'legacy' END
        WHERE issue_id=NEW.id AND provider='comicvine';
    END""",
)

IDENTITY_SCHEMA = ';\n'.join(IDENTITY_TABLES + IDENTITY_TRIGGERS) + ';\n'
