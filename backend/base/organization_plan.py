"""Immutable organization intent. There is deliberately no executor here."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from backend.base.folder_policy import (POLICY_ID as FOLDER_POLICY,
                                        FolderDecision, FolderPolicy)
from backend.base.identification import (IdentificationResult,
                                         LocalMatchIssue, LocalMatchVolume)
from backend.base.naming_policy import NamingSettings
from backend.base.rename_policy import (POLICY_ID as NAMING_POLICY,
                                        RenameDecision, RenamePolicy)

PLAN_POLICY = 'kapowarr-organization-plan/v1'
MERGE_POLICY = 'kapowarr-comicinfo-merge/v1'


class PlanStatus(Enum):
    READY = 'ready'
    NO_CHANGES = 'no_changes'
    REVIEW = 'review_required'
    BLOCKED = 'blocked'
    UNRESOLVED = 'unresolved'


class Severity(Enum):
    INFO = 'info'
    WARNING = 'warning'
    REVIEW = 'review'
    BLOCKING = 'blocking'


class PlanCode(Enum):
    FOLDER = 'folder_policy_diagnostic'
    IDENTIFICATION = 'identification_not_actionable'
    NEW_VOLUME = 'explicit_local_volume_creation_required'
    INVALID_IDENTITY = 'local_identity_or_parent_changed'
    ROOT = 'unsafe_or_unknown_root'
    NAMING = 'naming_semantics_unavailable'
    PATH = 'invalid_target_path'
    LENGTH = 'target_path_or_component_too_long'
    SOURCE = 'source_unavailable_or_changed'
    OBSERVATION = 'filesystem_observation_required'
    SYMLINK = 'symlink_or_reparse_path_unavailable'
    OWNERSHIP = 'managed_folder_ownership_conflict'
    ASSOCIATIONS = 'destructive_reassociation_requires_review'
    STALE_ASSOCIATIONS = 'observed_associations_changed'
    METADATA = 'comicinfo_merge_unavailable'
    METADATA_UNSUPPORTED = 'comicinfo_write_unsupported'
    TARGET_EXISTS = 'filesystem_target_occupied'
    DB_TARGET = 'target_owned_by_another_db_file'
    SHARED_TARGET = 'multiple_plans_same_target'
    PATH_DEPENDENCY = 'target_is_another_plan_source'
    DUPLICATE_SOURCE = 'multiple_plans_same_source'
    CASE_ONLY = 'case_only_rename_requires_staged_apply'
    CROSS_DEVICE = 'cross_filesystem_relocation'
    DEVICE_UNKNOWN = 'cross_filesystem_status_unknown'


@dataclass(frozen=True)
class PlanDiagnostic:
    severity: Severity
    code: PlanCode
    detail: str = ''


class EffectKind(Enum):
    DIRECTORY = 'ensure_target_directory'
    RELOCATE = 'relocate_file'
    COMICINFO = 'write_comicinfo_at_destination'
    FILE_RECORD = 'register_or_update_file_record'
    VOLUME_FOLDER = 'record_previously_missing_volume_folder'
    ASSOCIATIONS = 'update_issue_associations'


@dataclass(frozen=True)
class PlannedEffect:
    kind: EffectKind
    reason: str
    depends_on: Tuple[EffectKind, ...] = ()


@dataclass(frozen=True)
class Precondition:
    name: str
    expected: Tuple[str, ...]
    validated_at_plan_time: bool
    revalidate_at_apply: bool = True


class MetadataMode(Enum):
    OFF = 'off'
    OPTIONAL = 'optional'
    REQUIRED = 'required'


@dataclass(frozen=True)
class PlanningPolicy:
    windows: bool = False
    case_sensitive: bool = True
    move: bool = True
    rename: bool = True
    associate: bool = True
    metadata: MetadataMode = MetadataMode.OFF
    max_path_length: Optional[int] = None
    folder: FolderPolicy = FolderPolicy()
    naming: RenamePolicy = RenamePolicy()


@dataclass(frozen=True)
class PlanningVolume:
    identity: LocalMatchVolume
    root_id: int
    folder: str
    custom_folder: bool
    comicvine_id: Optional[int]


@dataclass(frozen=True)
class PlanningIssue:
    identity: LocalMatchIssue
    title: Optional[str]
    date: Optional[str]
    description: Optional[str]
    comicvine_id: Optional[int]


@dataclass(frozen=True, order=True)
class AssociationLink:
    volume_id: int
    issue_id: int
    forced: bool = False


@dataclass(frozen=True)
class PlanningFile:
    id: int
    path: str
    links: Tuple[AssociationLink, ...]
    # General bindings are retained explicitly, never relabelled as issue 0.
    general_volumes: Tuple[int, ...] = ()
    general_links: Tuple[Tuple[int, bool, str], ...] = ()


@dataclass(frozen=True)
class PathObservation:
    path: str
    exists: Optional[bool]
    directory: bool = False
    unsafe_link: bool = False
    size: Optional[int] = None
    mtime_ns: Optional[int] = None
    device: Optional[int] = None


@dataclass(frozen=True)
class AssociationDelta:
    file_id: Optional[int]
    before: Tuple[AssociationLink, ...]
    after: Tuple[AssociationLink, ...]
    added: Tuple[AssociationLink, ...]
    removed: Tuple[AssociationLink, ...]


@dataclass(frozen=True)
class MetadataFieldDelta:
    field: str
    before: Optional[str]
    after: Optional[str]
    action: str


@dataclass(frozen=True)
class MetadataIntent:
    state: str
    fields: Tuple[MetadataFieldDelta, ...] = ()
    # Internal payload only; preview projection never exposes it.
    xml: Optional[bytes] = None
    source_digest: Optional[str] = None
    policy_id: str = MERGE_POLICY


@dataclass(frozen=True)
class OrganizationPlan:
    identification: IdentificationResult
    status: PlanStatus
    context_id: str
    policy: PlanningPolicy
    naming_settings: NamingSettings
    target_root: Optional[str] = None
    target_folder: Optional[str] = None
    target_path: Optional[str] = None
    folder_reason: Optional[str] = None
    associations: Optional[AssociationDelta] = None
    metadata: MetadataIntent = MetadataIntent('not_requested')
    preconditions: Tuple[Precondition, ...] = ()
    effects: Tuple[PlannedEffect, ...] = ()
    diagnostics: Tuple[PlanDiagnostic, ...] = ()
    policy_id: str = PLAN_POLICY
    naming_policy: str = NAMING_POLICY
    folder_policy: str = FOLDER_POLICY
    folder_decision: Optional[FolderDecision] = None
    rename_decision: Optional[RenameDecision] = None
    # Captured before application; not reconstructed from current metadata.
    database_fingerprint: Optional[str] = None
    volume_folder_before: Optional[str] = None

    @property
    def source_path(self) -> str:
        return self.identification.candidate.file.path


@dataclass(frozen=True)
class OrganizationBatch:
    plans: Tuple[OrganizationPlan, ...]

    @property
    def counts(self) -> Tuple[Tuple[str, int], ...]:
        return tuple((s.value, sum(p.status == s for p in self.plans)) for s in PlanStatus)

    @property
    def effect_counts(self) -> Tuple[Tuple[str, int], ...]:
        return tuple((k.value, sum(e.kind == k for p in self.plans for e in p.effects)) for k in EffectKind)
