"""Explicit read-only ComicInfo enrichment. No selection, search or reassignment."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from re import fullmatch
from typing import Optional

from backend.base.comicinfo import ComicInfoCode
from backend.base.import_candidate import (CandidateDiagnostic,
                                           ComicInfoObservation,
                                           DiagnosticCode, DiagnosticKind,
                                           EvidenceSource, ImportCandidate,
                                           InspectionState, Provenance)
from backend.implementations.comicinfo import comicinfo_claims
from backend.implementations.comicinfo_archive import (ComicInfoInspection,
                                                       inspect_comicinfo)
from backend.implementations.identification import title_key


def enrich_comicinfo(candidate: ImportCandidate,
                    inspection: Optional[ComicInfoInspection] = None) -> ImportCandidate:
    """Supply an inspection to reuse its parsed document within one operation.

    Existing candidate/filename/local authority remain untouched. Comparisons
    only diagnose discrepancies; they never identify or select a publication.
    """
    result = inspection or inspect_comicinfo(candidate.file.path, candidate.file)
    if result.path != candidate.file.path:
        raise ValueError('Inspection belongs to another candidate path')
    if result.state != InspectionState.FAILED and result.stamp is not None and (candidate.file.size, candidate.file.mtime_ns) != (
        result.stamp.size, result.stamp.mtime_ns
    ):
        raise ValueError('Inspection does not match candidate freshness')
    origin = Provenance(EvidenceSource.COMICINFO, result.member or 'ComicInfo.xml',
                        'kapowarr-comicinfo/v1', datetime.now(timezone.utc))
    # Reinspection replaces this layer, not other sources or DB associations.
    diagnostics = [d for d in candidate.diagnostics if d.provenance.source != EvidenceSource.COMICINFO]
    claims = tuple(c for c in candidate.claims if c.provenance.source != EvidenceSource.COMICINFO)
    document = result.document
    if result.state == InspectionState.FAILED:
        unavailable = all(d.code == ComicInfoCode.UNSUPPORTED_FORMAT for d in result.diagnostics)
        diagnostics.append(CandidateDiagnostic(
            DiagnosticKind.UNAVAILABLE if unavailable else DiagnosticKind.FATAL,
            DiagnosticCode.COMICINFO_FAILED, origin))
    if document is not None:
        embedded = comicinfo_claims(document, origin)
        claims += embedded
        diagnostics.extend(CandidateDiagnostic(DiagnosticKind.WARNING, DiagnosticCode.COMICINFO_FIELD,
                                               replace(origin, locator=origin.locator + '/' + (d.field or '')))
                           for d in document.diagnostics)
        disagreements = []
        if candidate.filename:
            filename = candidate.filename
            if document.series and filename.series and title_key(document.series) != title_key(filename.series):
                disagreements.append('Series/filename')
            if document.date.year is not None and filename.year is not None and document.date.year != filename.year:
                disagreements.append('Year/filename')
            # Only diagnose ordinary finite decimal values. Never pass Number
            # through the legacy alphabet/fraction/range parser.
            if (document.number and fullmatch(r'[0-9]+(?:\.[0-9]+)?', document.number)
                    and isinstance(filename.legacy_issue_number, (float, int))):
                try:
                    number = Decimal(document.number)
                    if number.is_finite() and number != Decimal(str(filename.legacy_issue_number)):
                        disagreements.append('Number/filename')
                except InvalidOperation:
                    pass
        if candidate.existing:
            associations = candidate.existing.associations
            titles = {a.volume_title for a in associations if a.volume_title is not None}
            numbers = {a.issue_number for a in associations if a.issue_number is not None}
            if document.series and titles and title_key(document.series) not in {title_key(t) for t in titles}:
                disagreements.append('Series/local_association')
            if document.number is not None and numbers and document.number not in numbers:
                # Literal disagreement, not proof two bibliographic issues differ.
                disagreements.append('Number/local_association')
            known_issues = {a.selected_issue for a in associations if a.selected_issue is not None}
            for claim in embedded:
                if claim.reference.kind.value != 'issue' or claim.reference in known_issues:
                    continue
                same_provider = {r for r in known_issues if r.provider == claim.reference.provider}
                code = (DiagnosticCode.IDENTITY_DISAGREEMENT if same_provider
                        else DiagnosticCode.UNVERIFIED_IDENTITY_RELATION)
                diagnostics.append(CandidateDiagnostic(DiagnosticKind.CONFLICT, code, claim.provenance))
        diagnostics.extend(CandidateDiagnostic(DiagnosticKind.CONFLICT,
                                               DiagnosticCode.BIBLIOGRAPHIC_DISAGREEMENT,
                                               replace(origin, locator=origin.locator + '/' + field))
                           for field in disagreements)
    envelope = ComicInfoObservation(
        result.state,
        tuple((f.name, f.text) for f in document.fields) if document else (),
        document.raw_bytes if document else result.failed_xml, origin, document, result.diagnostics)
    return replace(candidate, comicinfo=envelope, claims=claims,
                   diagnostics=tuple(diagnostics), archive_state=(InspectionState.FAILED
                   if result.state == InspectionState.FAILED else InspectionState.PRESENT))
