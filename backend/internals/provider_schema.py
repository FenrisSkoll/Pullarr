"""Schema 53. Historical schema-52 definitions remain immutable."""

from backend.internals.identity_schema import IDENTITY_TABLES


def provider_check(column: str) -> str:
    return (f"CHECK(length({column}) > 0 AND {column} GLOB '[a-z]*' "
            f"AND {column} NOT GLOB '*[^a-z0-9_]*')")


def relax_definition(sql: str) -> str:
    return sql.replace('comicvine_id INTEGER NOT NULL', 'comicvine_id INTEGER').replace(
        "CHECK (metadata_provider = 'comicvine')", provider_check('metadata_provider')
    ).replace("CHECK(provider IN ('comicvine','metron','gcd'))", provider_check('provider'))


TABLES = tuple(relax_definition(sql) for sql in IDENTITY_TABLES)


def cv_triggers(table: str, key: str, volume: bool):
    external = 'volume_external_ids' if volume else 'issue_external_ids'
    columns = ',last_fetch' if volume else ''
    values = ',NEW.last_cv_fetch' if volume else ''
    parity = 'AND last_fetch IS OLD.last_cv_fetch' if volume else ''
    updated = ',last_fetch=excluded.last_fetch' if volume else ''
    watched = 'comicvine_id,last_cv_fetch' if volume else 'comicvine_id'
    entity = 'volume' if volume else 'issue'
    return (
        f'''CREATE TRIGGER {table}_identity_insert AFTER INSERT ON {table}
        WHEN NEW.comicvine_id IS NOT NULL BEGIN
            INSERT INTO {external}({key},provider,provider_id,provenance{columns})
            VALUES(NEW.id,'comicvine',CAST(NEW.comicvine_id AS TEXT),'legacy'{values});
        END''',
        f'''CREATE TRIGGER {table}_identity_update
        AFTER UPDATE OF {watched} ON {table} BEGIN
            SELECT CASE WHEN
                (OLD.comicvine_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM {external} WHERE {key}=OLD.id AND provider='comicvine'
                    AND provider_id=CAST(OLD.comicvine_id AS TEXT) {parity}))
                OR (OLD.comicvine_id IS NULL AND EXISTS (
                    SELECT 1 FROM {external} WHERE {key}=OLD.id AND provider='comicvine'))
                THEN RAISE(ABORT,'ComicVine {entity} identity shadow conflict') END;
            DELETE FROM {external} WHERE {key}=NEW.id AND provider='comicvine'
                AND NEW.comicvine_id IS NULL;
            INSERT INTO {external}({key},provider,provider_id,provenance{columns})
            SELECT NEW.id,'comicvine',CAST(NEW.comicvine_id AS TEXT),'legacy'{values}
            WHERE NEW.comicvine_id IS NOT NULL
            ON CONFLICT({key},provider) DO UPDATE SET
                provider_id=excluded.provider_id,
                provenance=CASE WHEN OLD.comicvine_id IS NEW.comicvine_id
                    THEN provenance ELSE 'legacy' END {updated};
        END'''
    )


TRIGGERS = cv_triggers('volumes', 'volume_id', True) + cv_triggers('issues', 'issue_id', False)
SCHEMA = ';\n'.join(TABLES + TRIGGERS) + ';\n'
