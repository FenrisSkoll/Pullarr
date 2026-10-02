"""Additional typed clients in the existing secret-at-rest config store."""
from dataclasses import asdict
from json import dumps, loads
from uuid import uuid4

from backend.base.download_job import DownloadErrorCode as E, DownloadFailure
from backend.base.managed_client import ManagedClientConfig, RetentionPolicy
from backend.internals.db import get_db
from backend.internals.sab_clients import load_sab_clients

PREFIX = 'managed_client_v1:'


def load_managed_clients(cursor=None):
    cursor = get_db() if cursor is None else cursor
    rows = cursor.execute('SELECT key,value FROM config WHERE key GLOB ? ORDER BY key', (PREFIX + '*',)).fetchall()
    if len(rows) > 32:
        raise DownloadFailure(E.CONFIGURATION)
    result = []
    try:
        for key, value in rows:
            data = loads(value)
            data['retention'] = RetentionPolicy(**data.get('retention', {}))
            result.append(ManagedClientConfig(key[len(PREFIX):], **data))
    except (ValueError, TypeError, KeyError):
        raise DownloadFailure(E.CONFIGURATION) from None
    return tuple(result)


def load_clients(cursor=None):
    return (*load_sab_clients(cursor), *load_managed_clients(cursor))


def save_client(data, key=None, cursor=None, *, expected_revision=None):
    cursor = get_db() if cursor is None else cursor
    cursor.execute("UPDATE config SET value=value WHERE key='database_version'")
    configs = {c.key: c for c in load_managed_clients(cursor)}
    if (not isinstance(data, dict) or set(data) - {'name', 'url', 'username', 'password', 'kind', 'enabled', 'category', 'priority', 'retention'}
            or key is not None and key not in configs or key is None and len(configs) >= 32):
        raise DownloadFailure(E.CONFIGURATION)
    previous = configs.get(key)
    if key is not None and expected_revision is not None and previous.revision != expected_revision:
        raise DownloadFailure(E.DRIFT)
    try:
        policy = RetentionPolicy(**data.get('retention', {}))
        config = ManagedClientConfig(key or uuid4().hex, data.get('name'), data.get('url'),
            data.get('username') or (previous.username if previous else ''),
            data.get('password') or (previous.password if previous else ''), data.get('kind'),
            data.get('enabled', False), data.get('category', 'pullarr'), data.get('priority', 0), policy)
    except (ValueError, TypeError):
        raise DownloadFailure(E.CONFIGURATION) from None
    # Explicitly one enabled additional client per protocol; existing selected
    # SAB remains the authoritative Usenet override for compatibility.
    if config.enabled and any(c.key != config.key and c.enabled and c.protocol == config.protocol for c in configs.values()):
        raise DownloadFailure(E.CONFIGURATION)
    private = asdict(config)
    private.pop('key')
    cursor.execute('INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (PREFIX + config.key, dumps(private, sort_keys=True)))
    return config


def delete_client(key, cursor=None, *, expected_revision=None):
    cursor = get_db() if cursor is None else cursor
    cursor.execute("UPDATE config SET value=value WHERE key='database_version'")
    configs = {c.key:c for c in load_managed_clients(cursor)}
    if key not in configs:
        raise DownloadFailure(E.CONFIGURATION)
    if expected_revision is not None and configs[key].revision != expected_revision:
        raise DownloadFailure(E.DRIFT)
    if cursor.execute("SELECT 1 FROM acquisition_downloads WHERE client_id=? AND state NOT IN ('completed','failed')", (key,)).fetchone():
        raise DownloadFailure(E.BUSY)
    if cursor.execute('''SELECT 1 FROM acquisition_torrents t JOIN acquisition_downloads d ON d.id=t.download_id
        WHERE d.client_id=? AND t.state NOT IN ('removed_keep','removed_data')''', (key,)).fetchone():
        raise DownloadFailure(E.BUSY)
    cursor.execute('DELETE FROM config WHERE key=?', (PREFIX + key,))
