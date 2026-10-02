"""Pure candidate classification, not proof of application or historical origin.

Admission/ownership validation and SQL lock preservation stay with callers.
All inputs, including the naive local comparison clock, are explicit.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from re import IGNORECASE, compile
from typing import Generic, Optional, Sequence, TypeVar

from backend.base.definitions import SpecialVersion
from backend.implementations.metadata.format_evidence import (
    ProviderFormatEvidence, single_issue_format)
from backend.implementations.metadata.publication_evidence import (
    ProviderPublicationEvidence, single_issue_publication)

POLICY_ID = 'kapowarr-special-version/v1'

# These are the existing classifier patterns, not a new normalization policy.
# autopep8: off
split_regex = compile(r'(?<!vs)(?<!r\.i\.p)(?:(?<=[\.!\?])\s|(?<=[\.!\?]</p>)(?!$))', IGNORECASE)
remove_link_regex = compile(r'<a[^>]*>.*?</a>', IGNORECASE)
annual_regex = compile(r'\bannual\b', IGNORECASE)
omnibus_regex = compile(r'\bomnibus\b', IGNORECASE)
os_regex = compile(r'(?<!preceding\s)\bone[\- ]?shot\b(?!\scollections?)', IGNORECASE)
hc_regex = compile(r'(?<!preceding\s)\bhard[\- ]?cover\b(?!\scollections?)', IGNORECASE)
vol_regex = compile(r'^v(?:ol(?:ume)?)?\.?\s(?:\d+|(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)[-\s]{0,1})+)(?:\:\s|$)', IGNORECASE)
# autopep8: on


class ClassificationSource(Enum):
    CURRENT_LOCK = 'current_lock'
    LOGICAL_VAI = 'logical_vai'
    PROVIDER_PHYSICAL_FORMAT = 'provider_physical_format'
    PROVIDER_PUBLICATION_KIND = 'provider_publication_kind'
    LEGACY_VOLUME_TITLE = 'legacy_volume_title'
    LEGACY_ISSUE_LABEL = 'legacy_issue_label'
    LEGACY_DESCRIPTION = 'legacy_description'
    LEGACY_ANNUAL_EXCLUSION = 'legacy_annual_exclusion'
    LEGACY_AGE_COUNT = 'legacy_age_count'
    DEFAULT_NORMAL = 'default_normal'


class ClassificationReason(Enum):
    CURRENT_LOCK_PRESERVED = 'current_lock_preserved'
    ALL_ISSUE_TITLES_VOLUME_NUMBERED = 'all_issue_titles_volume_numbered'
    SOLE_ISSUE_PHYSICAL_EVIDENCE = 'sole_issue_physical_evidence'
    SOLE_ISSUE_PUBLICATION_EVIDENCE = 'sole_issue_publication_evidence'
    VOLUME_TITLE_OMNIBUS_MARKER = 'volume_title_omnibus_marker'
    VOLUME_TITLE_ONE_SHOT_MARKER = 'volume_title_one_shot_marker'
    VOLUME_TITLE_HARDCOVER_MARKER = 'volume_title_hardcover_marker'
    ISSUE_TITLE_OMNIBUS_LABEL = 'issue_title_omnibus_label'
    ISSUE_TITLE_HARDCOVER_LABEL = 'issue_title_hardcover_label'
    ISSUE_TITLE_ONE_SHOT_LABEL = 'issue_title_one_shot_label'
    VOLUME_TITLE_ANNUAL_EXCLUSION = 'volume_title_annual_exclusion'
    DESCRIPTION_OMNIBUS_MARKER = 'description_omnibus_marker'
    DESCRIPTION_ONE_SHOT_MARKER = 'description_one_shot_marker'
    DESCRIPTION_HARDCOVER_MARKER = 'description_hardcover_marker'
    DESCRIPTION_ANNUAL_EXCLUSION = 'description_annual_exclusion'
    AGED_SINGLE_ISSUE_TPB = 'aged_single_issue_tpb'
    NO_RULE_MATCHED = 'no_rule_matched'


class EvidenceDisposition(Enum):
    ACCEPTED = 'accepted'
    NOT_EVALUATED_DUE_TO_LOCK = 'not_evaluated_due_to_lock'
    NOT_EVALUATED_DUE_TO_VAI = 'not_evaluated_due_to_vai'
    INAPPLICABLE_ZERO_ISSUES = 'inapplicable_zero_issues'
    INAPPLICABLE_MULTIPLE_ISSUES = 'inapplicable_multiple_issues'
    UNMAPPED_VALUE = 'unmapped_value'
    DECLINED_CROSS_AXIS_CONFLICT = 'declined_cross_axis_conflict'
    NOT_SUPPLIED = 'not_supplied'


@dataclass(frozen=True)
class ClassificationIssue:
    title: Optional[str]
    date: Optional[str]


Evidence = TypeVar('Evidence', ProviderFormatEvidence, ProviderPublicationEvidence)


@dataclass(frozen=True)
class EvidenceAssessment(Generic[Evidence]):
    evidence: Optional[Evidence]
    disposition: EvidenceDisposition


@dataclass(frozen=True)
class ClassificationFacts:
    issue_count: int
    # None means lock short-circuited the title-pattern check.
    volume_numbered_count: Optional[int]
    # None means no dated sole-issue fallback was evaluated, not age zero.
    issue_age: Optional[timedelta]


@dataclass(frozen=True)
class ClassificationDecision:
    value: SpecialVersion
    source: ClassificationSource
    reason: ClassificationReason
    policy_id: str
    evaluated_at: datetime
    locked: bool
    facts: ClassificationFacts
    physical_evidence: EvidenceAssessment[ProviderFormatEvidence]
    publication_evidence: EvidenceAssessment[ProviderPublicationEvidence]


def evaluate_special_version(
    *, title: str, description: Optional[str],
    issues: Sequence[ClassificationIssue], stored_value: SpecialVersion,
    locked: bool, evaluated_at: datetime,
    format_evidence: Optional[ProviderFormatEvidence] = None,
    publication_evidence: Optional[ProviderPublicationEvidence] = None
) -> ClassificationDecision:
    """Evaluate admitted inputs without IO, mutation, or implicit clock access.

    This is always a candidate, never an application receipt. The caller owns
    snapshot completeness and evidence ownership; their failures are not Normal.
    Supply the historical naive-local clock convention, not a converted date.
    No-evidence calls intentionally retain the old lock-ignoring behavior.
    """
    count = len(issues)
    one_issue = count == 1
    numbered: Optional[int] = None
    age: Optional[timedelta] = None
    physical_disposition = EvidenceDisposition.NOT_SUPPLIED
    publication_disposition = EvidenceDisposition.NOT_SUPPLIED

    def disposition_for_supplied(disposition: EvidenceDisposition) -> None:
        nonlocal physical_disposition, publication_disposition
        if format_evidence is not None:
            physical_disposition = disposition
        if publication_evidence is not None:
            publication_disposition = disposition

    def decision(value: SpecialVersion, source: ClassificationSource,
                 reason: ClassificationReason) -> ClassificationDecision:
        return ClassificationDecision(
            value, source, reason, POLICY_ID, evaluated_at, locked,
            ClassificationFacts(count, numbered, age),
            EvidenceAssessment(format_evidence, physical_disposition),
            EvidenceAssessment(publication_evidence, publication_disposition))

    if (format_evidence is not None or publication_evidence is not None) and locked:
        disposition_for_supplied(EvidenceDisposition.NOT_EVALUATED_DUE_TO_LOCK)
        return decision(stored_value, ClassificationSource.CURRENT_LOCK,
                        ClassificationReason.CURRENT_LOCK_PRESERVED)

    numbered = sum(bool(vol_regex.search(issue.title or '')) for issue in issues)
    if count and numbered == count:
        disposition_for_supplied(EvidenceDisposition.NOT_EVALUATED_DUE_TO_VAI)
        return decision(SpecialVersion.VOLUME_AS_ISSUE, ClassificationSource.LOGICAL_VAI,
                        ClassificationReason.ALL_ISSUE_TITLES_VOLUME_NUMBERED)

    if one_issue:
        physical = single_issue_format(format_evidence)
        publication = single_issue_publication(publication_evidence)
        disposition_for_supplied(EvidenceDisposition.UNMAPPED_VALUE)
        if physical is not None and publication is None:
            physical_disposition = EvidenceDisposition.ACCEPTED
            return decision(physical, ClassificationSource.PROVIDER_PHYSICAL_FORMAT,
                            ClassificationReason.SOLE_ISSUE_PHYSICAL_EVIDENCE)
        if publication is not None and physical is None:
            publication_disposition = EvidenceDisposition.ACCEPTED
            return decision(publication, ClassificationSource.PROVIDER_PUBLICATION_KIND,
                            ClassificationReason.SOLE_ISSUE_PUBLICATION_EVIDENCE)
        if physical is not None and publication is not None:
            disposition_for_supplied(EvidenceDisposition.DECLINED_CROSS_AXIS_CONFLICT)

        if omnibus_regex.search(title):
            return decision(SpecialVersion.OMNIBUS, ClassificationSource.LEGACY_VOLUME_TITLE,
                            ClassificationReason.VOLUME_TITLE_OMNIBUS_MARKER)
        if os_regex.search(title):
            return decision(SpecialVersion.ONE_SHOT, ClassificationSource.LEGACY_VOLUME_TITLE,
                            ClassificationReason.VOLUME_TITLE_ONE_SHOT_MARKER)
        if hc_regex.search(title):
            return decision(SpecialVersion.HARD_COVER, ClassificationSource.LEGACY_VOLUME_TITLE,
                            ClassificationReason.VOLUME_TITLE_HARDCOVER_MARKER)

        issue_title = (issues[0].title or '').lower().replace(' ', '')
        if issue_title == 'omnibus':
            return decision(SpecialVersion.OMNIBUS, ClassificationSource.LEGACY_ISSUE_LABEL,
                            ClassificationReason.ISSUE_TITLE_OMNIBUS_LABEL)
        if issue_title in ('hc', 'hard-cover', 'hardcover'):
            return decision(SpecialVersion.HARD_COVER, ClassificationSource.LEGACY_ISSUE_LABEL,
                            ClassificationReason.ISSUE_TITLE_HARDCOVER_LABEL)
        if issue_title in ('os', 'one-shot', 'oneshot'):
            return decision(SpecialVersion.ONE_SHOT, ClassificationSource.LEGACY_ISSUE_LABEL,
                            ClassificationReason.ISSUE_TITLE_ONE_SHOT_LABEL)
    else:
        disposition_for_supplied(EvidenceDisposition.INAPPLICABLE_ZERO_ISSUES if not count
                                 else EvidenceDisposition.INAPPLICABLE_MULTIPLE_ISSUES)

    if 'annual' in title.lower():
        return decision(SpecialVersion.NORMAL, ClassificationSource.LEGACY_ANNUAL_EXCLUSION,
                        ClassificationReason.VOLUME_TITLE_ANNUAL_EXCLUSION)

    if description:
        first_sentence = split_regex.split(description)[0]
        first_sentence = remove_link_regex.sub('', first_sentence)
        if one_issue and omnibus_regex.search(first_sentence):
            return decision(SpecialVersion.OMNIBUS, ClassificationSource.LEGACY_DESCRIPTION,
                            ClassificationReason.DESCRIPTION_OMNIBUS_MARKER)
        if one_issue and os_regex.search(first_sentence):
            return decision(SpecialVersion.ONE_SHOT, ClassificationSource.LEGACY_DESCRIPTION,
                            ClassificationReason.DESCRIPTION_ONE_SHOT_MARKER)
        if one_issue and hc_regex.search(first_sentence):
            return decision(SpecialVersion.HARD_COVER, ClassificationSource.LEGACY_DESCRIPTION,
                            ClassificationReason.DESCRIPTION_HARDCOVER_MARKER)
        if annual_regex.search(first_sentence):
            return decision(SpecialVersion.NORMAL, ClassificationSource.LEGACY_ANNUAL_EXCLUSION,
                            ClassificationReason.DESCRIPTION_ANNUAL_EXCLUSION)

    if one_issue and issues[0].date:
        from backend.base.issue_facts import (BibliographicDate,
                                              DateKind, DatePrecision)
        evidence = BibliographicDate.interpret(issues[0].date, DateKind.LEGACY_SELECTED,
                                               'legacy_mapped', 'date', zero_placeholders=True)
        if evidence.precision == DatePrecision.UNSUPPORTED_TEXT:
            # Retain the established malformed legacy-input exception. Known
            # partial precision is different and abstains without this parser.
            datetime.strptime(issues[0].date, '%Y-%m-%d')
        day = evidence.exact_day
        if day is None:
            return decision(SpecialVersion.NORMAL, ClassificationSource.DEFAULT_NORMAL,
                            ClassificationReason.NO_RULE_MATCHED)
        age = evaluated_at - datetime.combine(day, datetime.min.time())
        if age > timedelta(days=30):
            return decision(SpecialVersion.TPB, ClassificationSource.LEGACY_AGE_COUNT,
                            ClassificationReason.AGED_SINGLE_ISSUE_TPB)
    return decision(SpecialVersion.NORMAL, ClassificationSource.DEFAULT_NORMAL,
                    ClassificationReason.NO_RULE_MATCHED)
