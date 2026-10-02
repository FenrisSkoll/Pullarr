"""Persistent application background budget, independent of Metron account limits."""

from time import time

from backend.implementations.metadata.metron_client import MetronError
from backend.internals.db import commit, get_db
from backend.internals.settings import Settings


def charge_background_request() -> None:
    """Called under the transport's process-wide lock, before any HTTP request.

    The config ledger survives restarts and is deliberately not a schema change.
    Only fetches use this function, outside domain metadata transactions. Partial
    detail progress is cached; no last_fetch or issue deletion follows deferral.
    """
    day = int(time()) // 86400
    limit = Settings().sv.metron_refresh_requests_per_day
    cursor = get_db()
    row = cursor.execute("SELECT value FROM config WHERE key='metron_background_usage'").fetchone()
    used = 0
    if row:
        try:
            saved_day, saved_used = str(row[0]).split(':')
            if int(saved_day) == day:
                used = int(saved_used)
        except (ValueError, TypeError):
            raise MetronError('deferred', (day + 1) * 86400) from None
    if used >= limit:
        raise MetronError('deferred', (day + 1) * 86400)
    cursor.execute('''INSERT INTO config(key,value) VALUES ('metron_background_usage',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (f'{day}:{used + 1}',))
    commit()
