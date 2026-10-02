"""Read-only folder-tree evidence, never folder-move authorization."""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional, Tuple

from backend.base.library_health import fingerprint

POLICY = 'kapowarr-folder-inventory/v1'


def _triple(value: object) -> bool:
    return type(value) is tuple and len(value) == 3


class InventoryState(str, Enum):
    COMPLETE = 'complete'
    INACCESSIBLE = 'inaccessible'
    BOUNDED = 'bounded'
    UNSAFE = 'unsafe'
    CHANGED = 'changed_during_scan'


@dataclass(frozen=True)
class InventoryLimits:
    entries: int = 10000
    depth: int = 32
    path_bytes: int = 1024 * 1024
    seconds: int = 30

    def __post_init__(self) -> None:
        for value, maximum in zip(asdict(self).values(), (20000, 64, 4 * 1024 * 1024, 120)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Invalid folder inventory limit')


@dataclass(frozen=True)
class TreeStamp:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class RegisteredTreeFile:
    """Supplied only by a trusted batched DB loader; coverage is not ownership.

    Include ALL registered paths in the tree, and all files owned by the volume
    even when outside it. A caller omitting either cannot establish ownership.
    This contract does not by itself certify that the supplied rows are complete.
    """

    file_id: int
    path: str
    direct: Tuple[Tuple[int, int, bool], ...] = ()  # volume, issue, forced
    general: Tuple[Tuple[int, bool, str], ...] = ()  # volume, forced, file type

    def __post_init__(self) -> None:
        if (type(self.file_id) is not int or self.file_id <= 0
                or type(self.path) is not str or not self.path or '\x00' in self.path
                or type(self.direct) is not tuple or type(self.general) is not tuple
                or len(self.direct) + len(self.general) > 20000):
            raise ValueError('Invalid folder registration')
        for link in self.direct:
            if (not _triple(link)
                    or any(type(v) is not int or v <= 0 for v in link[:2])
                    or type(link[2]) is not bool):
                raise ValueError('Invalid direct registration')
        for link in self.general:
            if (not _triple(link)
                    or type(link[0]) is not int or link[0] <= 0
                    or type(link[1]) is not bool or type(link[2]) is not str
                    or not 1 <= len(link[2]) <= 15):
                raise ValueError('Invalid general registration')
        object.__setattr__(self, 'direct', tuple(sorted(self.direct)))
        object.__setattr__(self, 'general', tuple(sorted(self.general)))


@dataclass(frozen=True)
class TreeEntry:
    relative: str
    kind: str
    stamp: TreeStamp
    registration: Optional[RegisteredTreeFile] = None

    @property
    def classification(self) -> str:
        if self.kind == 'directory':
            return 'directory'
        if self.registration is None:
            return 'unregistered_regular_file'
        if self.registration.direct and self.registration.general:
            return 'registered_direct_and_general'
        if self.registration.direct:
            return 'registered_direct'
        if self.registration.general:
            return 'registered_general'
        return 'registered_unowned'


@dataclass(frozen=True)
class FolderInventory:
    volume_id: int
    root: str
    source: str
    state: InventoryState
    reason: Optional[str]
    root_stamp: Optional[TreeStamp]
    source_stamp: Optional[TreeStamp]
    entries: Tuple[TreeEntry, ...]
    registrations: Tuple[RegisteredTreeFile, ...]
    inspected_entries: int
    policy: str = POLICY

    @property
    def complete(self) -> bool:
        return self.state == InventoryState.COMPLETE

    @property
    def digest(self) -> str:
        return fingerprint(asdict(self))

    def page(self, offset: int = 0, limit: int = 50) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid inventory page')
        return dict(state=self.state.value, reason=self.reason, complete=self.complete,
                    digest=self.digest, total=len(self.entries), offset=offset,
                    entries=[dict(asdict(e), classification=e.classification)
                             for e in self.entries[offset:offset + limit]],
                    apply_available=False)
