"""Administrator-only mapping configuration, bound to a saved SAB instance."""

from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from backend.base.acquisition_intake import (DownloaderPathMapping,
                                             IntakeErrorCode as
                                             E, IntakeFailure)
from backend.implementations.acquisition_paths import (contained,
                                                       map_download_path)
from backend.internals.managed_clients import load_clients


def save_mapping(store, data: object, key: str | None = None) -> dict:
    if (not isinstance(data, dict) or set(data) - {'client_id', 'remote_prefix', 'remote_style', 'local_root', 'local_prefix', 'enabled'}
            or any(not isinstance(data.get(k), str) or not data[k] for k in ('client_id', 'remote_prefix', 'remote_style', 'local_root'))
            or type(data.get('enabled', True)) is not bool):
        raise IntakeFailure(E.CONFIGURATION)
    clients = {c.key: c for c in load_clients(store.db)}
    client = clients.get(data['client_id'])
    if client is None:
        raise IntakeFailure(E.CONFIGURATION)
    existing = store.mappings()
    if key is not None and key not in {m.key for m in existing} or key is None and len(existing) >= 100:
        raise IntakeFailure(E.CONFIGURATION)
    prefix = data.get('local_prefix') or data['local_root']
    if not isinstance(prefix, str):
        raise IntakeFailure(E.CONFIGURATION)
    mapping = DownloaderPathMapping(key or uuid4().hex, client.key, client.instance,
        data['remote_prefix'], data['local_root'], data['remote_style'], True, prefix)
    contained(prefix, mapping.local_root)
    if not Path(mapping.local_root).is_dir():
        raise IntakeFailure(E.PATH_UNAVAILABLE)
    map_download_path(mapping.remote_prefix, client.key, client.instance, (mapping,))
    values = asdict(mapping)
    values['enabled'] = data.get('enabled', True)
    with store.transaction():
        store.db.execute('''INSERT INTO acquisition_path_mappings
            (id,client_id,client_instance,remote_prefix,local_root,remote_style,enabled,local_prefix)
            VALUES(:key,:client_id,:client_instance,:remote_prefix,:local_root,:remote_style,:enabled,:local_prefix)
            ON CONFLICT(id) DO UPDATE SET client_id=excluded.client_id,client_instance=excluded.client_instance,
            remote_prefix=excluded.remote_prefix,local_root=excluded.local_root,remote_style=excluded.remote_style,
            enabled=excluded.enabled,local_prefix=excluded.local_prefix''', values)
    return values
