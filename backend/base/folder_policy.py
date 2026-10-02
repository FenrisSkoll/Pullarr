"""Immutable publication-folder intent, independent of acquisition and execution."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from backend.base.definitions import SpecialVersion
from backend.base.import_candidate import ProviderReference

POLICY_ID = 'kapowarr-folder-policy/v1'


class FolderMode(Enum):
    PRESERVE_EXISTING = 'preserve_existing'
    RECALCULATE = 'recalculate'


class MissingFolderValue(Enum):
    LEGACY = 'legacy_explicit_unknown_labels'
    BLOCK = 'block_missing_values'


class FolderStatus(Enum):
    RETAINED = 'retain_existing'
    CALCULATED = 'calculated'
    REVIEW = 'review_required'
    BLOCKED = 'blocked'


class FolderCode(Enum):
    ROOT = 'root_unavailable'
    ROOT_CHANGE = 'explicit_root_migration_required'
    TEMPLATE = 'invalid_template'
    MISSING = 'missing_token_value'
    FALLBACK = 'explicit_missing_value_fallback'
    PATH = 'unsafe_path_component'
    ESCAPE = 'path_escape'
    RESERVED = 'reserved_name'
    LENGTH = 'path_too_long'
    OWNERSHIP = 'folder_owned_by_another_volume'
    CUSTOM = 'custom_folder_change_requires_review'
    CASE = 'case_only_folder_transition'
    REORGANIZE = 'managed_folder_transition_requires_coordinated_plan'
    CANONICAL_UNAVAILABLE = 'canonical_folder_unavailable'


@dataclass(frozen=True)
class FolderPolicy:
    template: Optional[str] = None  # None uses the captured existing setting.
    mode: FolderMode = FolderMode.PRESERVE_EXISTING
    missing: MissingFolderValue = MissingFolderValue.LEGACY
    root_id: Optional[int] = None  # Explicit operator selection, never discovery.
    default_root_id: Optional[int] = None  # Only for an unowned publication.
    custom_relative: Optional[str] = None  # Explicit operator target, not metadata.
    policy_id: str = POLICY_ID


@dataclass(frozen=True)
class FolderPublication:
    title: Optional[str]
    year: Optional[int]
    volume_number: Optional[int]
    publisher: Optional[str]
    authority: Optional[ProviderReference]
    local_id: Optional[int] = None
    root_id: Optional[int] = None
    current_folder: Optional[str] = None
    custom_folder: bool = False
    comicvine_id: Optional[int] = None
    special_version: SpecialVersion = SpecialVersion.NORMAL


@dataclass(frozen=True)
class FolderDiagnostic:
    code: FolderCode
    field: Optional[str] = None
    blocking: bool = False


@dataclass(frozen=True)
class FolderDecision:
    mode: FolderMode
    status: FolderStatus
    root_id: Optional[int]
    root: Optional[str]
    current_folder: Optional[str]
    policy_folder: Optional[str]
    target_folder: Optional[str]
    relative_folder: Optional[str]
    raw_components: Tuple[str, ...]
    safe_components: Tuple[str, ...]
    authority: Optional[ProviderReference]
    reasons: Tuple[str, ...]
    diagnostics: Tuple[FolderDiagnostic, ...]
    fingerprint: str
    policy_id: str = POLICY_ID

    @property
    def retained(self) -> bool:
        return self.target_folder is not None and self.target_folder == self.current_folder
