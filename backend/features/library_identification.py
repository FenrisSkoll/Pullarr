"""Explicit read-only organizer service; legacy proposals remain unchanged.

External acquisition is opt-in and injected. The adapter must own provider HTTP
budgets; this service's limit counts acquisition operations, not wire requests.
No production provider adapter is implicitly selected or instantiated here.
"""

from dataclasses import dataclass, replace
from typing import Iterable, Optional, Protocol, Tuple

from backend.base.identification import (BulkIdentification,
                                         IdentificationResult,
                                         MatchContribution, MatchReason,
                                         MatchState, PublicationMatch)
from backend.base.import_candidate import (ClaimRole, ImportCandidate,
                                           ProviderReference, ResourceKind)
from backend.implementations.identification import MatchingSnapshot, identify
from backend.internals.identification import load_matching_records


@dataclass(frozen=True)
class ProviderRequest:
    provider: str
    reference: Optional[ProviderReference]
    query: Optional[str]


@dataclass(frozen=True)
class ProviderPublication:
    reference: ProviderReference
    title: str
    year: Optional[int]


@dataclass(frozen=True)
class ProviderReply:
    publications: Tuple[ProviderPublication, ...] = ()
    unavailable: bool = False
    # Safe machine-readable category; no credentials/exception payloads.
    failure: Optional[str] = None
    resolved_reference: Optional[ProviderReference] = None


class ProviderAcquisition(Protocol):
    def acquire(self, request: ProviderRequest) -> ProviderReply:
        """Implementations must enforce existing provider wire-request budgets."""
        ...


def local_matching_snapshot(registered_providers: Tuple[str, ...], *, volume_ids=None) -> MatchingSnapshot:
    records = (load_matching_records(registered_providers) if volume_ids is None
               else load_matching_records(registered_providers, volume_ids=volume_ids))
    return MatchingSnapshot.build(*records)


def identify_many(
    candidates: Iterable[ImportCandidate], snapshot: MatchingSnapshot,
    *, acquisition: Optional[ProviderAcquisition] = None,
    provider: Optional[str] = None, max_operations: int = 0
) -> BulkIdentification:
    """Default is offline. Provider results are review alternatives, never adds.

    An explicit provider and adapter are both required. One cache per operation
    prevents duplicate lookups; exhausted/failed acquisition is not 'no match'.
    """
    if max_operations < 0 or (acquisition is not None and not provider):
        raise ValueError('Explicit provider and nonnegative operation budget required')
    cache = {}
    results = []
    operations = 0
    for candidate in candidates:
        matched = identify(candidate, snapshot)
        if matched.state != MatchState.UNRESOLVED or acquisition is None or provider is None:
            results.append(matched)
            continue
        claims = tuple(sorted({c.reference for c in candidate.claims
                               if c.role == ClaimRole.EMBEDDED},
                              key=lambda r: (r.provider, r.kind.value, r.provider_id)))
        # Never search around an unproven identity in another namespace.
        if claims and (len(claims) != 1 or claims[0].provider != provider):
            results.append(replace(matched, state=MatchState.REVIEW,
                                   reasons=(MatchReason.UNKNOWN_IDENTITY,)))
            continue
        document = candidate.comicinfo.document
        query = document.series if document and document.series else candidate.filename.series if candidate.filename else None
        if not claims and not query:
            results.append(matched)
            continue
        request = ProviderRequest(provider, claims[0] if claims else None, None if claims else query)
        if request not in cache:
            if operations >= max_operations:
                cache[request] = ProviderReply(unavailable=True, failure='operation_budget')
            else:
                operations += 1
                # Adapter translates provider/auth/rate failures to ProviderReply.
                # Programming/systemic errors propagate rather than hiding bugs.
                cache[request] = acquisition.acquire(request)
        reply = cache[request]
        if reply.unavailable or reply.failure:
            results.append(replace(matched, state=MatchState.BLOCKED,
                                   reasons=(MatchReason.PROVIDER_UNAVAILABLE,),
                                   acquisition_failure=reply.failure or 'unavailable'))
            continue
        if any(p.reference.provider != provider or p.reference.kind != ResourceKind.VOLUME
               for p in reply.publications):
            raise ValueError('Acquisition returned invalid attributed publication')
        if request.reference is not None and reply.publications:
            if (request.reference.kind == ResourceKind.VOLUME
                    and any(p.reference != request.reference for p in reply.publications)) or (
                    request.reference.kind == ResourceKind.ISSUE
                    and reply.resolved_reference != request.reference):
                raise ValueError('Direct lookup did not prove requested identity')
        publications = sorted(set(reply.publications), key=lambda p: (p.reference.provider, p.reference.provider_id, p.title, p.year or 0))
        options = tuple(PublicationMatch(None, p.reference, (), (
            MatchContribution(MatchReason.PROVIDER_RESULT, 'provider:' + provider),),
            title=p.title, year=p.year) for p in publications)
        results.append(replace(matched, state=MatchState.REVIEW if options else MatchState.UNRESOLVED,
                               alternatives=options, reasons=(MatchReason.PROVIDER_RESULT,) if options else matched.reasons))
    return BulkIdentification(tuple(results))
