"""Provider graph evidence. Deliberately no containment or ownership operation."""

from dataclasses import dataclass
from typing import Optional, Tuple

POLICY = 'kapowarr-reprint-graph/v1'


class GraphConflict(ValueError):
    """Local graph/authority changed after catalog acquisition began."""


@dataclass(frozen=True)
class IssueReference:
    provider_id: str
    series_id: str
    series_name: str
    number: str
    title: str
    deleted: bool


@dataclass(frozen=True)
class StoryEntity:
    provider_id: str
    issue_id: str
    title: str
    sequence: int
    deleted: bool


@dataclass(frozen=True)
class CreatorEntity:
    provider_id: str
    name: str
    deleted: bool


@dataclass(frozen=True)
class CreatorName:
    provider_id: str
    creator_id: str
    name: str
    official: bool
    name_type_id: Optional[str]
    deleted: bool


@dataclass(frozen=True)
class StoryCredit:
    provider_id: str
    story_id: str
    name_id: str
    role_id: str
    role: str
    credited_as: str
    signed_as: str
    credited: bool
    signed: bool
    uncertain: bool
    deleted: bool


@dataclass(frozen=True)
class ReprintEdge:
    provider_id: str
    origin_issue: str
    target_issue: str
    origin_story: Optional[str]
    target_story: Optional[str]
    notes: str
    modified: str

    def __post_init__(self):
        if not self.provider_id or not self.origin_issue or not self.target_issue:
            raise ValueError('Missing graph identity')
        if self.origin_issue == self.target_issue:
            raise ValueError('Reprint endpoints must belong to different issues')

    @property
    def shape(self) -> str:
        return ('story' if self.origin_story else 'issue') + '_to_' + (
            'story' if self.target_story else 'issue')


@dataclass(frozen=True)
class GraphSnapshot:
    provider: str
    source_policy: str
    source_fingerprint: str
    observed_at: float
    seeds: Tuple[str, ...]
    issues: Tuple[IssueReference, ...]
    stories: Tuple[StoryEntity, ...]
    creators: Tuple[CreatorEntity, ...]
    names: Tuple[CreatorName, ...]
    credits: Tuple[StoryCredit, ...]
    edges: Tuple[ReprintEdge, ...]
    select_count: int

    def __post_init__(self):
        if not self.provider or not self.source_policy or not self.source_fingerprint:
            raise ValueError('Missing graph provenance')
        for records in (self.issues, self.stories, self.creators, self.names, self.credits, self.edges):
            if not isinstance(records, tuple) or len({r.provider_id for r in records}) != len(records):
                raise ValueError('Duplicate graph identity or mutable snapshot')
        issues = {r.provider_id: r for r in self.issues}
        stories = {r.provider_id: r for r in self.stories}
        creators = {r.provider_id for r in self.creators}
        names = {r.provider_id: r for r in self.names}
        if not set(self.seeds) <= issues.keys():
            raise ValueError('Missing seed identity')
        if any(r.issue_id not in issues for r in self.stories):
            raise ValueError('Missing story parent')
        if any(r.creator_id not in creators for r in self.names):
            raise ValueError('Missing creator parent')
        if any(r.story_id not in stories or r.name_id not in names for r in self.credits):
            raise ValueError('Missing credit endpoint')
        for edge in self.edges:
            for iid, sid in ((edge.origin_issue, edge.origin_story), (edge.target_issue, edge.target_story)):
                if iid not in issues or (sid is not None and (sid not in stories or stories[sid].issue_id != iid)):
                    raise ValueError('Inconsistent reprint endpoint')

    def evidence_for_target_issue(self, provider_id: str) -> Tuple[ReprintEdge, ...]:
        """Direct explicit material edges, never transitive or complete coverage."""
        return tuple(edge for edge in self.edges if edge.target_issue == provider_id)
