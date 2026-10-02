"""Completed acquisition facts, not local identity or organization authority."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class AcquisitionKind(Enum):
    SABNZBD = 'sabnzbd'
    NZBGET = 'nzbget'
    QBITTORRENT = 'qbittorrent'
    DIRECT_DOWNLOAD = 'direct_download'


class IntakeErrorCode(Enum):
    CONFIGURATION = 'configuration'
    PATH_MAPPING = 'path_mapping'
    PATH_UNAVAILABLE = 'path_unavailable'
    PERMISSION = 'permission_denied'
    UNSAFE_PATH = 'unsafe_path'
    ENUMERATION = 'enumeration'
    PREPARATION = 'preparation'
    UNSUPPORTED_ARTIFACT = 'unsupported_artifact'
    UNSTABLE = 'unstable'
    IDENTIFICATION = 'identification'
    PLANNING = 'planning'
    ORGANIZATION = 'organization'
    RECOVERY = 'organization_recovery_required'


class IntakeFailure(Exception):
    def __init__(self, code: IntakeErrorCode):
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class AcquisitionCompletion:
    """Trusted internal receipt; never constructed from an arbitrary path DTO."""

    kind: AcquisitionKind
    download_id: str
    candidate_id: str
    volume_id: int
    issue_ids: Tuple[int, ...]
    reported_paths: Tuple[str, ...]
    completed_at: str
    title: str = ''
    source_key: str = ''
    client_id: Optional[str] = None
    client_instance: Optional[str] = None
    remote_job_id: Optional[str] = None
    evaluation_id: str = ''
    offering_id: str = ''
    forced: bool = False
    original_state: str = ''
    queue_id: Optional[int] = None
    mechanism: str = ''

    def __post_init__(self) -> None:
        if (not self.download_id or not self.candidate_id or self.volume_id <= 0
                or any(type(i) is not int or i <= 0 for i in self.issue_ids)
                or len(self.issue_ids) > 1000 or len(set(self.issue_ids)) != len(self.issue_ids)
                or len(self.reported_paths) > 1000
                or any(not p or len(p) > 4096 or any(ord(c) < 32 for c in p) for p in self.reported_paths)
                or not self.completed_at or len(self.title) > 4096):
            raise IntakeFailure(IntakeErrorCode.CONFIGURATION)
        if self.kind in (AcquisitionKind.SABNZBD, AcquisitionKind.NZBGET, AcquisitionKind.QBITTORRENT) and not all((
                self.client_id, self.client_instance, self.remote_job_id)):
            raise IntakeFailure(IntakeErrorCode.CONFIGURATION)


@dataclass(frozen=True)
class DownloaderPathMapping:
    """Administrator-owned mapping, explicitly bound to a downloader instance."""

    key: str
    client_id: str
    client_instance: str
    remote_prefix: str
    local_root: str
    remote_style: str = 'posix'
    enabled: bool = True
    local_prefix: Optional[str] = None


@dataclass(frozen=True)
class ArtifactObservation:
    path: str
    size: int
    mtime_ns: int
    device: int
    inode: int

    @property
    def stamp(self) -> Tuple[int, int, int, int]:
        return self.size, self.mtime_ns, self.device, self.inode
