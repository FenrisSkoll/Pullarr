"""Explicit catalog enrichment, independent of REST and acquisition workers."""

from threading import Lock

from backend.base.reprint_graph import GraphConflict
from backend.implementations.metadata.gcd_catalog import Catalog, CatalogError
from backend.internals.db import get_db
from backend.internals.reprint_graph import persist, seeds
from backend.internals.settings import Settings

_sync_lock = Lock()


def status():
    cursor = get_db()
    receipt = cursor.execute('SELECT * FROM bibliographic_graph_snapshots ORDER BY rowid DESC LIMIT 1').fetchone()
    error = cursor.execute("SELECT value FROM config WHERE key='gcd_catalog_error'").fetchone()
    return dict(enabled=Settings().sv.gcd_catalog_enabled,
                configured=bool(Settings().sv.gcd_catalog_path),
                last_sync=dict(receipt) if receipt else None,
                last_safe_error=error[0] if error else None,
                availability='Use Test to validate the saved catalog; status does not open it.')


def operate(*, sync):
    settings = Settings().sv
    if not settings.gcd_catalog_enabled or not settings.gcd_catalog_path:
        raise CatalogError('not_configured')
    if not _sync_lock.acquire(blocking=False):
        raise CatalogError('busy')
    try:
        saved_path = settings.gcd_catalog_path
        catalog = Catalog(saved_path)
        if not sync:
            return catalog.test()  # No app mutation during connection test.
        cursor = get_db()
        if cursor.connection.in_transaction:
            raise CatalogError('transaction_active')
        local = seeds(cursor)
        revision = cursor.execute('SELECT MAX(rowid) FROM bibliographic_graph_snapshots').fetchone()[0]
        snapshot = catalog.acquire({iid: value[0] for iid, value in local.items()})
        # No external reads after entering the app's write transaction.
        if Settings().sv.gcd_catalog_path != saved_path or not Settings().sv.gcd_catalog_enabled:
            raise CatalogError('configuration_changed')
        try:
            result = persist(cursor, snapshot, local, revision=revision)
        except GraphConflict:
            raise CatalogError('local_state_changed') from None
        cursor.execute("DELETE FROM config WHERE key='gcd_catalog_error'")
        return result
    except CatalogError as exc:
        if sync:
            get_db().execute('''INSERT INTO config(key,value) VALUES('gcd_catalog_error',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (exc.reason,))
        raise
    finally:
        _sync_lock.release()
