"""Selected-source publication intent/scope, distinct from physical binding."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from backend.base.definitions import SpecialVersion
from backend.internals.provider_identity import MetadataIdentityError


class PublicationKind(Enum):
    ONE_SHOT = 'one_shot'
    OMNIBUS = 'omnibus'


@dataclass(frozen=True)
class ProviderPublicationEvidence:
    provider: str
    provider_id: str
    source_field: str
    raw_value: str
    publication_kind: Optional[PublicationKind]


def validate_publication_evidence(evidence: Optional[ProviderPublicationEvidence],
                                  provider: str, provider_id: str) -> None:
    if evidence is not None and (evidence.provider, evidence.provider_id) != (provider, provider_id):
        raise MetadataIdentityError('Publication evidence differs from selected volume identity')


def single_issue_publication(evidence: Optional[ProviderPublicationEvidence]) -> Optional[SpecialVersion]:
    """Candidate only; the classifier owns lock/VAI/cardinality/conflict policy."""
    if evidence is None:
        return None
    return {
        PublicationKind.ONE_SHOT: SpecialVersion.ONE_SHOT,
        PublicationKind.OMNIBUS: SpecialVersion.OMNIBUS,
    }.get(evidence.publication_kind)
