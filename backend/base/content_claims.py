"""Collected-content assertions: evidence, authority and file use are separate."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

POLICY = 'kapowarr-collected-content/v1'


class ContentConflict(ValueError):
    """An exact identity, preview or applicability precondition changed."""


class ClaimKind(str, Enum):
    PARTIAL = 'partial_issue_content'
    COMPLETE = 'complete_issue_containment'


class EvidenceOutcome(str, Enum):
    NONE = 'no_graph_evidence'
    MATERIAL = 'material_from_issue'
    STORY = 'specific_story_reprint'
    MULTIPLE = 'multiple_reprint_edges'


@dataclass(frozen=True)
class PublicationRef:
    provider: str
    provider_id: str

    def __post_init__(self):
        if not self.provider or not self.provider_id:
            raise ValueError('Publication identity is required')


@dataclass(frozen=True)
class EvidenceReceipt:
    provider: str
    edge_id: str
    snapshot_id: str
    origin_issue: str
    target_issue: str
    origin_story: Optional[str]
    target_story: Optional[str]
    active: bool

    @property
    def shape(self) -> str:
        return ('story' if self.origin_story else 'issue') + '_to_' + (
            'story' if self.target_story else 'issue')


@dataclass(frozen=True)
class ContentEvidenceEvaluation:
    source: PublicationRef
    target: PublicationRef
    edges: Tuple[EvidenceReceipt, ...]

    def __post_init__(self):
        if self.source == self.target or not isinstance(self.edges, tuple):
            raise ValueError('Distinct publications and immutable evidence required')
        keys = {(edge.provider, edge.edge_id) for edge in self.edges}
        if len(keys) != len(self.edges):
            raise ValueError('Duplicate evidence identity')
        for edge in self.edges:
            if (PublicationRef(edge.provider, edge.origin_issue) != self.source
                    or PublicationRef(edge.provider, edge.target_issue) != self.target):
                raise ValueError('Evidence endpoints differ from reviewed publications')

    @property
    def outcome(self) -> EvidenceOutcome:
        # No inventory, indexing flag, note or page total can upgrade this to
        # completeness. Even all known story edges remain only reprint evidence.
        if not self.edges:
            return EvidenceOutcome.NONE
        if len(self.edges) > 1:
            return EvidenceOutcome.MULTIPLE
        if self.edges[0].origin_story is not None:
            return EvidenceOutcome.STORY
        return EvidenceOutcome.MATERIAL


@dataclass(frozen=True)
class CollectedContentClaim:
    id: str
    target: PublicationRef
    source: PublicationRef
    kind: ClaimKind
    created_at: float
    retired_at: Optional[float] = None
    supersedes: Optional[str] = None
    authority: str = 'operator_confirmed'
    policy: str = POLICY

    def __post_init__(self):
        if self.target == self.source or self.authority != 'operator_confirmed':
            raise ValueError('Exact distinct publications and operator authority required')


@dataclass(frozen=True)
class FileContentCoverage:
    id: str
    file_id: int
    target_issue_id: int
    source_issue_id: int
    claim_id: str
    created_at: float
    retired_at: Optional[float] = None
    policy: str = POLICY
