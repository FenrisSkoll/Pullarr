"""Typed SAB settings in existing config storage; never return stored keys."""

from json import dumps, loads
from uuid import uuid4

from backend.base.download_job import (DownloadErrorCode as E,
                                       DownloadFailure, SABConfig)
from backend.internals.db import get_db

PREFIX = 'sab_client_v1:'


def load_sab_clients(cursor=None):
    cursor = get_db() if cursor is None else cursor
    rows = cursor.execute('SELECT key,value FROM config WHERE key GLOB ? ORDER BY key', (PREFIX + '*',)).fetchall()
    if len(rows) > 32:
        raise DownloadFailure(E.CONFIGURATION)
    try:
        return tuple(SABConfig(key[len(PREFIX):], **loads(value)) for key, value in rows)
    except (ValueError, TypeError, KeyError):
        raise DownloadFailure(E.CONFIGURATION) from None


def save_sab_client(data, key=None):
    configs = {c.key: c for c in load_sab_clients()}
    if (not isinstance(data, dict) or set(data) - {'name', 'url', 'api_key', 'enabled', 'category', 'priority'}
            or 'api_key' in data and not isinstance(data['api_key'], str)
            or key is not None and key not in configs or key is None and len(configs) >= 32):
        raise DownloadFailure(E.CONFIGURATION)
    previous = configs.get(key)
    config = SABConfig(key or uuid4().hex, data.get('name'), data.get('url'),
        data.get('api_key') or (previous.api_key if previous else ''), data.get('enabled', True),
        data.get('category', '*'), data.get('priority', -100))
    private = {k: v for k, v in config.preview().items() if k not in ('id', 'api_key_present')}
    private['api_key'] = config.api_key
    get_db().execute('INSERT INTO config(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (PREFIX + config.key, dumps(private, sort_keys=True)))
    return config


def delete_sab_client(key):
    if key not in {c.key for c in load_sab_clients()}:
        raise DownloadFailure(E.CONFIGURATION)
    get_db().execute('DELETE FROM config WHERE key=?', (PREFIX + key,))
