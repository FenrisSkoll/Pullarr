"""Explicit automation enablement. Upgrade never starts a new backlog grab."""

import json

from backend.base.download_job import DownloadFailure, endpoint
from backend.internals.wanted import WantedConflict

KEY = 'wanted_automation_v1'
DEFAULT = {'mode': 'off', 'sab_client_id': None, 'redirect_origins': []}


def load_automation(db):
    row = db.execute('SELECT value FROM config WHERE key=?', (KEY,)).fetchone()
    return validate_configuration(json.loads(row[0])) if row else dict(DEFAULT, redirect_origins=[])


def validate_configuration(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULT):
        raise WantedConflict('invalid_configuration')
    value = {**DEFAULT, **data}
    if value['mode'] not in ('off', 'search_only', 'grab'):
        raise WantedConflict('invalid_configuration')
    client = value['sab_client_id']
    if client is not None and (not isinstance(client, str) or not 1 <= len(client) <= 64
                              or not all(c.isascii() and (c.isalnum() or c in '-_') for c in client)):
        raise WantedConflict('invalid_configuration')
    origins = value['redirect_origins']
    if not isinstance(origins, list) or len(origins) > 16:
        raise WantedConflict('invalid_configuration')
    from urllib.parse import urlsplit

    try:
        if any(urlsplit(endpoint(v)).path for v in origins):
            raise ValueError
    except (TypeError, ValueError, DownloadFailure):
        raise WantedConflict('invalid_configuration') from None
    return dict(value, redirect_origins=sorted(set(origins)))


def save_automation(db, data):
    value = validate_configuration(data)
    db.execute('INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (KEY, json.dumps(value, sort_keys=True)))
    return value
