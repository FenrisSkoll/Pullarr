"""Frozen c720e2d classifier oracle. Do not route through the new evaluator.

Original body and regexes are retained verbatim; dependency loading is mocked
by tests. Enum-to-candidate maps are frozen here too.
"""

from datetime import datetime, timedelta
from re import IGNORECASE, compile
from typing import Union

from backend.base.definitions import SpecialVersion
from backend.implementations.metadata.format_evidence import (
    PhysicalFormat, ProviderFormatEvidence)
from backend.implementations.metadata.publication_evidence import (
    ProviderPublicationEvidence, PublicationKind)
from backend.implementations.volumes import Volume

# autopep8: off
THIRTY_DAYS = timedelta(days=30)
split_regex = compile(r'(?<!vs)(?<!r\.i\.p)(?:(?<=[\.!\?])\s|(?<=[\.!\?]</p>)(?!$))', IGNORECASE)
remove_link_regex = compile(r'<a[^>]*>.*?</a>', IGNORECASE)
annual_regex = compile(r'\bannual\b', IGNORECASE)
omnibus_regex = compile(r'\bomnibus\b', IGNORECASE)
os_regex = compile(r'(?<!preceding\s)\bone[\- ]?shot\b(?!\scollections?)', IGNORECASE)
hc_regex = compile(r'(?<!preceding\s)\bhard[\- ]?cover\b(?!\scollections?)', IGNORECASE)
vol_regex = compile(r'^v(?:ol(?:ume)?)?\.?\s(?:\d+|(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)[-\s]{0,1})+)(?:\:\s|$)', IGNORECASE)
# autopep8: on


def single_issue_format(evidence):
    if evidence is None:
        return None
    return {
        PhysicalFormat.HARDCOVER: SpecialVersion.HARD_COVER,
        PhysicalFormat.TRADE_PAPERBACK: SpecialVersion.TPB,
    }.get(evidence.physical_format)


def single_issue_publication(evidence):
    if evidence is None:
        return None
    return {
        PublicationKind.ONE_SHOT: SpecialVersion.ONE_SHOT,
        PublicationKind.OMNIBUS: SpecialVersion.OMNIBUS,
    }.get(evidence.publication_kind)


def determine_special_version(
    volume_id: int, format_evidence: Union[ProviderFormatEvidence, None] = None,
    publication_evidence: Union[ProviderPublicationEvidence, None] = None
) -> SpecialVersion:
    """Determine what Special Version a volume is, if any.

    Args:
        volume_id (int): The ID of the volume to determine for.
        format_evidence: Optional evidence already validated against the
            selected fetch identity. Locks and VAI take precedence; physical
            hints apply only to a single local issue. Omission retains the
            historical heuristic behavior.
        publication_evidence: Independently validated publication intent/scope.
            Recognized binding plus publication is ambiguous in SpecialVersion;
            neither hint wins and the existing heuristics remain the fallback.

    Returns:
        SpecialVersion: The result.
    """
    volume = Volume(volume_id)
    volume_data = volume.get_data()
    issues = volume.get_issues()
    one_issue = len(issues) == 1

    # No-evidence calls retain the historical heuristic contract (including CV).
    if (
        (format_evidence is not None or publication_evidence is not None)
        and volume_data.special_version_locked
    ):
        return volume_data.special_version

    if issues and all(
        vol_regex.search(i.title or '')
        for i in issues
    ):
        return SpecialVersion.VOLUME_AS_ISSUE

    if one_issue:
        physical = single_issue_format(format_evidence)
        publication = single_issue_publication(publication_evidence)
        # SpecialVersion cannot express both axes. Decline automatic collapse.
        if physical is not None and publication is None:
            return physical
        if publication is not None and physical is None:
            return publication

        if omnibus_regex.search(volume_data.title):
            return SpecialVersion.OMNIBUS

        if os_regex.search(volume_data.title):
            return SpecialVersion.ONE_SHOT

        if hc_regex.search(volume_data.title):
            return SpecialVersion.HARD_COVER

        issue_title = (issues[0].title or '').lower().replace(' ', '')

        if issue_title == 'omnibus':
            return SpecialVersion.OMNIBUS

        if issue_title in ('hc', 'hard-cover', 'hardcover'):
            return SpecialVersion.HARD_COVER

        if issue_title in ('os', 'one-shot', 'oneshot'):
            return SpecialVersion.ONE_SHOT

    if 'annual' in volume_data.title.lower():
        # Volume is annual
        return SpecialVersion.NORMAL

    if volume_data.description:
        # Look for Special Version in first sentence of description. Only first
        # sentence as to avoid false hits, like referring to another volume that
        # is a Special Version in the description (e.g. "Included in the TPB")
        first_sentence = split_regex.split(volume_data.description)[0]
        first_sentence = remove_link_regex.sub('', first_sentence)

        if one_issue and omnibus_regex.search(first_sentence):
            return SpecialVersion.OMNIBUS

        if one_issue and os_regex.search(first_sentence):
            return SpecialVersion.ONE_SHOT

        if one_issue and hc_regex.search(first_sentence):
            return SpecialVersion.HARD_COVER

        if annual_regex.search(first_sentence):
            return SpecialVersion.NORMAL

    if one_issue and issues[0].date:
        thirty_plus_days_ago = (
            datetime.now() - datetime.strptime(issues[0].date, "%Y-%m-%d")
            > THIRTY_DAYS
        )

        # The volume only has one issue. If the issue was released in the last
        # month, then we'll assume it's just a new volume that has only released
        # one issue up to this point. If the issue was released more than a
        # month ago, then we'll assume it's a TPB.
        if thirty_plus_days_ago:
            return SpecialVersion.TPB

    return SpecialVersion.NORMAL
