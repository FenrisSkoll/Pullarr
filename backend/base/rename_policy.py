"""Immutable filename intent, independent of folder selection and execution."""

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Tuple

from backend.base.identification import LocalMatchIssue, LocalMatchVolume
from backend.base.import_candidate import ProviderReference
from backend.base.issue_facts import NumberCatalog
from backend.base.naming_policy import NamingSettings

POLICY_ID = 'kapowarr-rename-policy/v1'


class RenameMode(Enum):
    PRESERVE_EXISTING = 'preserve_existing'
    CANONICAL = 'canonical'


class RenameStatus(Enum):
    UNCHANGED = 'unchanged'
    CALCULATED = 'calculated'
    REVIEW = 'review_required'
    BLOCKED = 'blocked'


class RenameCode(Enum):
    TEMPLATE = 'invalid_template'
    MISSING = 'missing_required_token'
    FALLBACK = 'legacy_missing_value_fallback'
    COVERAGE = 'issue_coverage_unrepresentable'
    IDENTITY = 'invalid_issue_parent_or_authority'
    COLLISION = 'issue_display_collision'
    RAW_LABEL = 'raw_label_without_numeric_semantics'
    LABEL_CLEANED = 'issue_label_cleaning_requires_review'
    DATE = 'required_date_precision_unavailable'
    PATH = 'unsafe_or_empty_basename'
    RESERVED = 'reserved_filename'
    LENGTH = 'filename_or_path_too_long'
    CASE = 'case_only_rename'
    FORMAT = 'general_file_or_image_group_unsupported'
    TITLELESS = 'legacy_titleless_template_fallback'


@dataclass(frozen=True)
class RenameDiagnostic:
    code: RenameCode
    field: str = ''
    blocking: bool = False


@dataclass(frozen=True)
class RenamePolicy:
    mode: RenameMode = RenameMode.CANONICAL
    strict: bool = False


@dataclass(frozen=True)
class RenameIssue:
    identity: LocalMatchIssue
    title: Optional[str]
    date: Optional[str]
    comicvine_id: Optional[int]


@dataclass(frozen=True)
class RenameCatalog:
    volume_id: int
    issues: Mapping[int, LocalMatchIssue]
    display_counts: Mapping[str, int]
    padding: int
    fingerprint: str
    numbers: NumberCatalog


@dataclass(frozen=True)
class NamingContext:
    publication: LocalMatchVolume
    issues: Tuple[RenameIssue, ...]
    # Complete supplied local coverage universe, not an inferred integer list.
    catalog: RenameCatalog
    settings: NamingSettings
    current_filename: str
    comicvine_id: Optional[int] = None
    target_folder: Optional[str] = None
    windows: bool = False
    case_sensitive: bool = True
    max_path_length: Optional[int] = None


@dataclass(frozen=True)
class RenameToken:
    name: str
    value: str
    source: str


@dataclass(frozen=True)
class RenameDecision:
    current_filename: str
    target_filename: Optional[str]
    raw_basename: Optional[str]
    safe_basename: Optional[str]
    extension: str
    issue_ids: Tuple[int, ...]
    raw_labels: Tuple[str, ...]
    authority: ProviderReference
    template: Optional[str]
    mode: RenameMode
    status: RenameStatus
    fingerprint: str
    tokens: Tuple[RenameToken, ...] = ()
    reasons: Tuple[str, ...] = ()
    diagnostics: Tuple[RenameDiagnostic, ...] = ()
    policy_id: str = POLICY_ID


@dataclass(frozen=True)
class RenameBatch:
    decisions: Tuple[RenameDecision, ...]

    @property
    def counts(self) -> Tuple[Tuple[str, int], ...]:
        return tuple((s.value, sum(d.status == s for d in self.decisions)) for s in RenameStatus)
