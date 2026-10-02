"""Immutable presentation values, never acquisition or compatibility authority."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from backend.base.release_evaluation import (Compatibility, CoverageBand,
                                             RankingTier, Rule, RuleOutcome)

EXPLANATION_POLICY = 'kapowarr-release-explanation/v1'


class EntryKind(Enum):
    POSITIVE = 'positive'
    PENALTY = 'penalty'
    REJECTION = 'rejection'
    REVIEW = 'review'
    UNDETERMINED = 'undetermined'
    NEUTRAL = 'neutral'
    TIE_BREAK = 'tie_break'


class UnsupportedExplanation(ValueError):
    """Caller must show explanation unavailable, not invent unknown semantics."""


class InvalidEvaluationReceipt(ValueError):
    """Programming/transport invariant failure; never silently repair a score."""


@dataclass(frozen=True)
class EvidenceSummary:
    reference: str
    origin: str
    field: str
    label: str
    values: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ExplanationEntry:
    key: str
    rule: Rule
    axis: str
    outcome: RuleOutcome
    kind: EntryKind
    points: int
    gate: Optional[Compatibility]
    message: str
    evidence: Tuple[EvidenceSummary, ...]


@dataclass(frozen=True)
class ReleaseExplanation:
    evaluation_id: str
    state: Compatibility
    band: CoverageBand
    headline: str
    coverage: str
    score: Optional[int]
    entries: Tuple[ExplanationEntry, ...]
    source_priority: int
    scoring_policy: str
    scoring_fingerprint: str
    # Owned JSON strings are immutable snapshots of explicit allowlisted DTOs,
    # not mutable dictionaries or retained candidate/acquisition objects.
    candidate_json: str
    target_json: str
    policy: str = EXPLANATION_POLICY


@dataclass(frozen=True)
class RankingExplanation:
    left_id: str
    right_id: str
    order: int
    tier: Optional[RankingTier]
    key: str
    message: str
    policy: str = EXPLANATION_POLICY
