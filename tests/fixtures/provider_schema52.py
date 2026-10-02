"""Historical schema-52 fixtures, independent of the latest bootstrap."""

from unittest.mock import patch

from fixtures.provider_identity import build_legacy

from backend.internals.db_migration import DatabaseMigrationHandler


def build_schema52(db, volumes=3, issues_per_volume=3, edges=True):
    build_legacy(db, volumes, issues_per_volume, edges)
    with patch('backend.internals.db_migration.get_db', side_effect=db.cursor):
        DatabaseMigrationHandler.handlers[51]()
    if volumes:
        db.execute("INSERT INTO volume_external_ids VALUES (10,'metron','M:10','verified',NULL)")
        db.execute("INSERT INTO issue_external_ids VALUES (10,'gcd','G:10','verified')")
    db.commit()
