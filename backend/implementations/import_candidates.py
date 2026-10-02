"""Read-only candidate observation and explicit legacy filename projection.

Callers load existing identity separately, in batches. This module never opens
archives, searches providers, selects a match or generates destination paths.
"""

from datetime import datetime, timezone
from os import stat
from os.path import abspath, basename, dirname, relpath, splitext
from stat import S_ISREG
from typing import Dict, Mapping, Optional
from uuid import uuid4

from backend.base.definitions import FilenameData
from backend.base.file_extraction import extract_filename_data
from backend.base.import_candidate import (CandidateDiagnostic,
                                           CoverageHypothesis, CoverageKind,
                                           DiagnosticCode, DiagnosticKind,
                                           DiscoveryScope, EvidenceSource,
                                           ExistingFileIdentity,
                                           FilenameObservation,
                                           FileObservation, FolderObservation,
                                           ImportCandidate, InspectionState,
                                           Provenance)


def observe_import_candidate(
    path: str, scope: DiscoveryScope, *,
    parsed: Optional[FilenameData] = None,
    existing: Optional[ExistingFileIdentity] = None,
    observed_at: Optional[datetime] = None,
    candidate_id: Optional[str] = None,
    prefer_folder_year: bool = True
) -> ImportCandidate:
    """Observe a file, or retain an explicitly failed path observation.

    Stat failure is blocking evidence, not proof of deletion. A parser failure
    is a warning: other explicit evidence may still identify the observed file.
    Directory/group enumeration and its completeness remain the caller's job.
    A caller with authoritative associations need not run the filename parser.
    """
    clock = observed_at if observed_at is not None else datetime.now(timezone.utc)
    origin = Provenance(EvidenceSource.FILESYSTEM, path, observed_at=clock)
    diagnostics = []
    try:
        stats = stat(path)
        if not S_ISREG(stats.st_mode):
            raise IsADirectoryError(path)
        file = FileObservation(path, clock, stats.st_size, stats.st_mtime_ns,
                               InspectionState.PRESENT)
    except OSError as error:
        file = FileObservation(path, clock, None, None, InspectionState.FAILED)
        code = (DiagnosticCode.PATH_MISSING if isinstance(error, FileNotFoundError)
                else DiagnosticCode.PATH_UNREADABLE if isinstance(error, PermissionError)
                else DiagnosticCode.PATH_NOT_FILE if isinstance(error, IsADirectoryError)
                else DiagnosticCode.STAT_FAILED)
        diagnostics.append(CandidateDiagnostic(
            DiagnosticKind.FATAL, code, origin))

    parent = dirname(path)
    relative = relpath(path, scope.root) if scope.root is not None else None
    folder = FolderObservation(
        parent, relative,
        abspath(parent) == abspath(scope.root) if scope.root is not None else None,
        provenance=Provenance(EvidenceSource.FILESYSTEM, parent, observed_at=clock))
    filename = None
    coverage = ()
    if parsed is None and not (existing and existing.associations):
        # Preserve parser exceptions in the legacy caller (which supplies its
        # already parsed DTO). New observation clients get a typed diagnostic.
        try:
            parsed = extract_filename_data(path, prefer_folder_year=prefer_folder_year)
        except (ValueError, IndexError):
            diagnostics.append(CandidateDiagnostic(
                DiagnosticKind.WARNING, DiagnosticCode.PARSE_FAILED,
                Provenance(EvidenceSource.LEGACY_PARSER, path, observed_at=clock)))
    if parsed is not None:
        name = basename(path)
        stem, extension = splitext(name)
        provenance = Provenance(
            EvidenceSource.LEGACY_PARSER, path,
            'kapowarr-filename/v1;prefer_folder_year=' + str(prefer_folder_year), clock)
        filename = FilenameObservation(
            name, stem, extension, parsed['series'], parsed['year'],
            parsed['volume_number'], parsed['special_version'],
            parsed['issue_number'], parsed['annual'], provenance)
        number = parsed['issue_number']
        coverage = (CoverageHypothesis(
            CoverageKind.LEGACY_RANGE if isinstance(number, tuple)
            else CoverageKind.SINGLE if number is not None else CoverageKind.UNKNOWN,
            provenance, legacy_endpoints=number if isinstance(number, tuple) else None),)
    return ImportCandidate(
        candidate_id or uuid4().hex, scope, file, folder, filename,
        existing=existing, coverage=coverage, diagnostics=tuple(diagnostics))


def legacy_filename_inputs(
    candidates: Mapping[str, ImportCandidate]
) -> Dict[str, FilenameData]:
    """Projection for existing grouping only; keys may be image-folder paths.

    This does NOT authorize apply, interpret bibliography, or consume candidate
    hypotheses as matches. Original Library Import selection/limits remain in
    its caller. It always supplies a successful legacy parse before observation.
    """
    result = {}
    for proposal_path, candidate in candidates.items():
        if candidate.filename is None:
            raise ValueError('Legacy import requires its original filename parse')
        result[proposal_path] = candidate.filename.to_legacy()
    return result
