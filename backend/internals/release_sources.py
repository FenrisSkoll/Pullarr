"""Acquisition settings in existing config storage; not metadata providers.

API keys use the existing local SQLite secret-at-rest convention. Protect DB
backups like the Metron token. No secret is returned through the public reader.
This module deliberately bypasses legacy indexer/config payload debug logging.
"""

from json import dumps, loads
from uuid import uuid4

from backend.base.release_search import (SearchError, SourceConfig,
                                         SourceFailure)
from backend.internals.db import get_db

PREFIX = 'release_source_v1:'


def load_sources():
    rows = get_db().execute('SELECT key, value FROM config WHERE key GLOB ? ORDER BY key',
                            (PREFIX + '*',)).fetchall()
    if len(rows) > 32:
        raise SourceFailure(SearchError.CONFIGURATION)
    result = []
    for key, value in rows:
        try:
            data = loads(value)
            data['categories'] = tuple(data['categories'])
            result.append(SourceConfig(key[len(PREFIX):], **data))
        except (ValueError, TypeError, KeyError):
            raise SourceFailure(SearchError.CONFIGURATION) from None
    return tuple(result)


def save_source(data, key=None):
    """Omitted/blank key on edit retains the stored secret. Validate before write."""
    sources = {c.key: c for c in load_sources()}
    if (not isinstance(data, dict) or set(data) - {'name', 'url', 'api_key', 'mode',
            'enabled', 'priority', 'categories'} or
            'api_key' in data and not isinstance(data['api_key'], str) or
            key is not None and key not in sources or key is None and len(sources) >= 32):
        raise SourceFailure(SearchError.CONFIGURATION)
    try:
        previous = sources.get(key)
        secret = data.get('api_key') or (previous.api_key if previous else '')
        config = SourceConfig(key or uuid4().hex, data.get('name'), data.get('url'), secret,
            data.get('mode', 'newznab'), data.get('enabled', True), data.get('priority', 0),
            tuple(data.get('categories', ())))
    except (ValueError, TypeError):
        raise SourceFailure(SearchError.CONFIGURATION) from None
    private = {k: v for k, v in config.preview().items() if k not in ('id', 'api_key_present')}
    private['api_key'] = config.api_key
    get_db().execute('INSERT INTO config(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (PREFIX + config.key, dumps(private, sort_keys=True)))
    return config


def delete_source(key):
    if key not in {c.key for c in load_sources()}:
        raise SourceFailure(SearchError.CONFIGURATION)
    get_db().execute('DELETE FROM config WHERE key=?', (PREFIX + key,))
