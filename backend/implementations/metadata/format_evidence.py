"""Selected-source physical evidence, not a canonical SpecialVersion."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from backend.base.definitions import SpecialVersion
from backend.internals.provider_identity import MetadataIdentityError


class PhysicalFormat(Enum):
    HARDCOVER = 'hardcover'
    TRADE_PAPERBACK = 'trade_paperback'


@dataclass(frozen=True)
class ProviderFormatEvidence:
    provider: str
    provider_id: str
    source_field: str
    raw_value: str
    physical_format: Optional[PhysicalFormat]


def validate_format_evidence(evidence: Optional[ProviderFormatEvidence],
                             provider: str, provider_id: str) -> None:
    if evidence is not None and (evidence.provider, evidence.provider_id) != (provider, provider_id):
        raise MetadataIdentityError('Format evidence differs from selected volume identity')


def single_issue_format(evidence: Optional[ProviderFormatEvidence]) -> Optional[SpecialVersion]:
    """Application mapping; callers must preserve locks, VAI and cardinality."""
    if evidence is None:
        return None
    return {
        PhysicalFormat.HARDCOVER: SpecialVersion.HARD_COVER,
        PhysicalFormat.TRADE_PAPERBACK: SpecialVersion.TPB,
    }.get(evidence.physical_format)
