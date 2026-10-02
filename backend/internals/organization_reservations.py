"""Shared bounded path exclusion, including directory subtrees.

No schema change: a reserved path excludes itself, ancestors and descendants.
For a regular-file reservation, descendants cannot be valid artifacts anyway;
siblings remain independent. Directory intents reserve only their two tree roots.
Checks belong under the registering writer transaction, not only in preview.
"""

import os
from bisect import bisect_left
from pathlib import Path
from typing import Iterable, Optional

from backend.base.organization_job import ExecutionCode, OrganizationError

MAX_RESERVATIONS = 20000


def path_key(path: str) -> str:
    if not isinstance(path, str):
        raise OrganizationError(ExecutionCode.UNSAFE_PATH)
    if not os.path.isabs(path) or '..' in Path(path).parts or '\x00' in path:
        raise OrganizationError(ExecutionCode.UNSAFE_PATH)
    return os.path.normpath(path).casefold()


class ReservationIndex:
    def __init__(self, rows: Iterable[tuple[str, str]]):
        self.owners: dict[str, set[str]] = {}
        size = 0
        for count, (path, owner) in enumerate(rows, 1):
            size += len(path.encode('utf-8'))
            if count > MAX_RESERVATIONS or size > 4 * 1024 * 1024:
                raise OrganizationError(ExecutionCode.UNSUPPORTED, 'Reservation index bounded')
            self.owners.setdefault(path_key(path), set()).add(owner)
        self.keys = sorted(self.owners)

    def conflicts(self, path: str, *, exclude: Optional[str] = None) -> tuple[str, ...]:
        key = path_key(path)
        owners = set(self.owners.get(key, ()))
        for parent in Path(key).parents:
            owners.update(self.owners.get(str(parent), ()))
        prefix = key.rstrip(os.sep) + os.sep
        position = bisect_left(self.keys, prefix)
        while position < len(self.keys) and self.keys[position].startswith(prefix):
            owners.update(self.owners[self.keys[position]])
            position += 1
        owners.discard(exclude)
        return tuple(sorted(owners))


def load_reservations(cursor) -> ReservationIndex:
    return ReservationIndex(cursor.execute(
        'SELECT path_key,job_id FROM organization_reservations ORDER BY path_key LIMIT ?',
        (MAX_RESERVATIONS + 1,)))
