"""Pure issue-row presentation, never provider metadata or a file/book title."""

from dataclasses import dataclass
from typing import Literal, Optional

from backend.base.definitions import SV_TO_SHORT_TERM, SpecialVersion


@dataclass(frozen=True)
class IssueDisplayTitle:
    value: Optional[str]
    source: Literal['existing_issue_title', 'parent_volume_context', 'unknown']
    scope: Literal['issue'] = 'issue'


def issue_display_title(
    title: Optional[str], *, parent_title: str,
    special_version: SpecialVersion, issue_count: int
) -> IssueDisplayTitle:
    """Use full parent context only for a sole issue named exactly TPB or HC.

    Cardinality must describe the whole local volume, not a filtered page or
    file's issue set. No source provenance, physical binding or book title is
    inferred. All other nonblank mapped titles are returned verbatim.
    """
    if title is None or not title.strip():
        return IssueDisplayTitle(None, 'unknown')
    if (issue_count == 1
            and special_version in (SpecialVersion.TPB, SpecialVersion.HARD_COVER)
            and title.strip().casefold() == SV_TO_SHORT_TERM[special_version].casefold()
            and parent_title.strip()):
        return IssueDisplayTitle(parent_title, 'parent_volume_context')
    return IssueDisplayTitle(title, 'existing_issue_title')
