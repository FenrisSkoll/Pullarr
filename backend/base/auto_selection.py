"""Versioned automation authorization, separate from release compatibility."""

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Optional, Tuple

from backend.base.release_evaluation import ReleaseEvaluation

AUTO_SELECTION_POLICY = 'kapowarr-auto-selection/v1'


class SelectionReason(Enum):
    UNIQUE_BEST = 'unique_best'
    SEARCH_INCOMPLETE = 'search_incomplete'
    NO_ACCEPTABLE_RELEASE = 'no_acceptable_release'
    OPERATIONALLY_BLOCKED = 'operationally_blocked'
    QUALITY_TIE = 'quality_tie'


@dataclass(frozen=True)
class AutoSelectionPolicy:
    """V1 deliberately requires a complete bounded search, not worldwide best.

    Compatible exact/containing coverage is eligible. Claimed packs are not.
    Points are preferences, never confidence. No additional score threshold.
    """

    version: str = AUTO_SELECTION_POLICY

    def __post_init__(self):
        if self.version != AUTO_SELECTION_POLICY:
            raise ValueError('Unsupported automatic selection policy')

    @property
    def fingerprint(self) -> str:
        return sha256((self.version + '|complete|exact,containing|unique-quality|no-threshold').encode()).hexdigest()


@dataclass(frozen=True)
class SelectionDecision:
    reason: SelectionReason
    selected: Optional[ReleaseEvaluation]
    alternatives: Tuple[str, ...]
    policy_fingerprint: str
