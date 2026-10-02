"""Canonical issue evidence. Identity, arithmetic and presentation are distinct.

No provider, database, filename parser or clock is consulted here. Legacy DTOs
remain separate: these values are not an implicit extension of their JSON shape.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from re import fullmatch
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Tuple

NUMBER_POLICY = 'kapowarr-issue-number/v1'
DATE_POLICY = 'kapowarr-bibliographic-date/v1'
ORDER_POLICY = 'kapowarr-issue-presentation/v1'


def _is_type(value: object, kind: type) -> bool:
    return isinstance(value, kind)


def _qualified_reference(value: object) -> bool:
    return (isinstance(value, tuple) and len(value) == 2
            and all(isinstance(part, str) and bool(part) for part in value))


class NumberKind(Enum):
    NUMERIC = 'numeric'
    SUFFIXED = 'suffixed'
    OPAQUE = 'opaque'
    UNNUMBERED = 'unnumbered'
    ABSENT = 'absent'


def numeric_label(raw: Optional[str]) -> Optional[Decimal]:
    """The existing conservative unsigned ASCII decimal grammar, exactly."""
    if raw is None or len(raw) > 100 or not fullmatch(r'[0-9]+(?:\.[0-9]+)?', raw):
        return None
    return Decimal(raw)


def decimal_text(value: Decimal) -> str:
    """Context-independent canonical text, not Decimal.normalize rounding."""
    if not value.is_finite():
        raise ValueError('Finite decimal required')
    text = format(value, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return '0' if value == 0 else text


def number_kind(raw: Optional[str]) -> NumberKind:
    if raw is None:
        return NumberKind.ABSENT
    if raw.strip().casefold() in ('nn', '[nn]', 'unnumbered'):
        return NumberKind.UNNUMBERED
    if numeric_label(raw) is not None:
        return NumberKind.NUMERIC
    if fullmatch(r'[0-9]+(?:[.-]?[A-Za-z][A-Za-z0-9.-]*)', raw):
        return NumberKind.SUFFIXED
    return NumberKind.OPAQUE


@dataclass(frozen=True)
class IssueNumberFacts:
    raw_label: Optional[str]
    provenance: str
    source_field: str
    interpretation: NumberKind
    numeric_text: Optional[str] = None
    policy: str = NUMBER_POLICY

    def __post_init__(self) -> None:
        if (self.policy != NUMBER_POLICY or not self.provenance or not self.source_field
                or self.raw_label is not None and not isinstance(self.raw_label, str)):
            raise ValueError('Invalid issue number evidence')
        number = numeric_label(self.raw_label)
        expected = decimal_text(number) if number is not None else None
        if self.interpretation != number_kind(self.raw_label) or self.numeric_text != expected:
            raise ValueError('Inconsistent issue number interpretation')

    @classmethod
    def interpret(cls, raw: Optional[str], provenance: str, source_field: str) -> 'IssueNumberFacts':
        value = numeric_label(raw)
        return cls(raw, provenance, source_field, number_kind(raw),
                   decimal_text(value) if value is not None else None)

    @property
    def numeric(self) -> Optional[Decimal]:
        return Decimal(self.numeric_text) if self.numeric_text is not None else None


class DatePrecision(Enum):
    DAY = 'day'
    MONTH = 'month'
    YEAR = 'year'
    UNKNOWN = 'unknown'
    UNSUPPORTED_TEXT = 'unsupported_text'


class DateKind(Enum):
    COVER = 'cover'
    ON_SALE = 'on_sale'
    PUBLICATION = 'publication'
    LEGACY_SELECTED = 'legacy_selected_unknown'


def _date_components(raw: Optional[str], zero_placeholders: bool) -> Tuple[
    Optional[int], Optional[int], Optional[int], DatePrecision
]:
    if raw is None or raw == '':
        return None, None, None, DatePrecision.UNKNOWN
    if fullmatch(r'[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?', raw):
        parts = [int(p) for p in raw.split('-')]
        year = parts[0]
        month = parts[1] if len(parts) > 1 else None
        day = parts[2] if len(parts) > 2 else None
        if zero_placeholders:
            month, day = month or None, day or None
        try:
            if month is None and day is not None:
                raise ValueError('Day without month')
            # Defaults validate known components only; never become output facts.
            date(year, month if month is not None else 1, day if day is not None else 1)
            return year, month, day, (DatePrecision.DAY if day is not None else
                                      DatePrecision.MONTH if month is not None else DatePrecision.YEAR)
        except ValueError:
            pass
    return None, None, None, DatePrecision.UNSUPPORTED_TEXT


@dataclass(frozen=True)
class BibliographicDate:
    raw_value: Optional[str]
    kind: DateKind
    source_field: str
    provenance: str
    year: Optional[int]
    month: Optional[int]
    day: Optional[int]
    precision: DatePrecision
    zero_placeholders: bool = False
    uncertainty: Optional[str] = None
    policy: str = DATE_POLICY

    def __post_init__(self) -> None:
        if (self.policy != DATE_POLICY or not self.source_field or not self.provenance
                or not _is_type(self.kind, DateKind)
                or (self.year, self.month, self.day, self.precision)
                != _date_components(self.raw_value, self.zero_placeholders)):
            raise ValueError('Inconsistent bibliographic date')

    @classmethod
    def interpret(cls, raw: Optional[str], kind: DateKind, provenance: str,
                  source_field: str, *, zero_placeholders: bool = False,
                  uncertainty: Optional[str] = None) -> 'BibliographicDate':
        return cls(raw, kind, source_field, provenance,
                   *_date_components(raw, zero_placeholders), zero_placeholders, uncertainty)

    @property
    def exact_day(self) -> Optional[date]:
        if (self.precision == DatePrecision.DAY and self.uncertainty is None
                and self.year is not None and self.month is not None and self.day is not None):
            return date(self.year, self.month, self.day)
        return None

    @property
    def display(self) -> Optional[str]:
        if self.year is None:
            return self.raw_value
        text = f'{self.year:04d}'
        if self.month is not None:
            text += f'-{self.month:02d}'
        if self.day is not None:
            text += f'-{self.day:02d}'
        return text


@dataclass(frozen=True)
class IssueFacts:
    """Provider evidence companion, deliberately outside IssueMetadata.asdict."""
    number: IssueNumberFacts
    dates: Tuple[BibliographicDate, ...] = ()
    selected_date_field: Optional[str] = None
    provider_ordinal: Optional[int] = None
    ordinal_provenance: Optional[str] = None

    def __post_init__(self) -> None:
        if not _is_type(self.dates, tuple) or len({d.source_field for d in self.dates}) != len(self.dates):
            raise ValueError('Distinct immutable source date fields required')
        if self.selected_date_field is not None and self.selected_date_field not in {d.source_field for d in self.dates}:
            raise ValueError('Selected date must identify retained evidence')
        if ((self.provider_ordinal is None) != (self.ordinal_provenance is None)
                or self.provider_ordinal is not None and type(self.provider_ordinal) is not int):
            raise ValueError('Validated ordinal requires provenance')

    @property
    def operational_date(self) -> Optional[BibliographicDate]:
        return next((d for d in self.dates if d.source_field == self.selected_date_field), None)


@dataclass(frozen=True)
class VariantOf:
    """A separate qualified publication, never an identity alias."""
    provider: str
    provider_id: str
    provenance: str

    def __post_init__(self) -> None:
        if not self.provider or not self.provider_id or not self.provenance:
            raise ValueError('Qualified variant relationship required')


@dataclass(frozen=True)
class IssueRecord:
    id: int
    volume_id: int
    provider_identities: Tuple[Tuple[str, str], ...]
    facts: Optional[IssueFacts]
    legacy_label: str
    legacy_number: Optional[float]
    legacy_date: Optional[str]
    variant_of: Optional[VariantOf] = None

    def __post_init__(self) -> None:
        if type(self.id) is not int or type(self.volume_id) is not int or self.id <= 0 or self.volume_id <= 0:
            raise ValueError('Local issue identity required')
        if (not _is_type(self.provider_identities, tuple)
                or any(not _qualified_reference(ref) for ref in self.provider_identities)):
            raise ValueError('Immutable qualified identities required')


class SemanticState(Enum):
    SUPPORTED = 'supported'
    UNSUPPORTED = 'unsupported'
    AMBIGUOUS = 'ambiguous'


@dataclass(frozen=True)
class CandidateSet:
    issue_ids: Tuple[int, ...]
    state: SemanticState
    evidence: str


@dataclass(frozen=True)
class NumberCatalog:
    """Shared per-context indexes; missing persisted facts use explicit legacy labels."""
    raw: Mapping[str, Tuple[int, ...]]
    numeric: Mapping[Decimal, Tuple[int, ...]]

    @classmethod
    def build(cls, rows: Iterable[Tuple[int, str, Optional[IssueNumberFacts]]]) -> 'NumberCatalog':
        raw: dict[str, list[int]] = {}
        numeric: dict[Decimal, list[int]] = {}
        seen = set()
        for iid, legacy, facts in rows:
            if iid in seen:
                raise ValueError('Duplicate local issue identity')
            seen.add(iid)
            label = facts.raw_label if facts is not None else legacy
            value = facts.numeric if facts is not None else numeric_label(legacy)
            if label is not None:
                raw.setdefault(label, []).append(iid)
            if value is not None:
                numeric.setdefault(value, []).append(iid)
        return cls(MappingProxyType({k: tuple(sorted(v)) for k, v in raw.items()}),
                   MappingProxyType({k: tuple(sorted(v)) for k, v in numeric.items()}))

    def match(self, label: str, *, exact_first: bool = True,
              allow_unnumbered: bool = False) -> CandidateSet:
        if not allow_unnumbered and (not label.strip() or number_kind(label) == NumberKind.UNNUMBERED):
            return CandidateSet((), SemanticState.UNSUPPORTED, 'label_not_identity')
        raw, number = self.raw.get(label, ()), numeric_label(label)
        ids, evidence = ((raw, 'raw') if exact_first and raw else
                         (self.numeric.get(number, ()) if number is not None else (), 'numeric'))
        state = (SemanticState.AMBIGUOUS if len(ids) > 1 else SemanticState.SUPPORTED
                 if ids or number is not None else SemanticState.UNSUPPORTED)
        return CandidateSet(ids, state, evidence)

    def range_members(self, context: int, interval: 'NumericRange') -> CandidateSet:
        if context != interval.context:
            return CandidateSet((), SemanticState.UNSUPPORTED, 'numbering_context')
        groups = tuple(ids for n, ids in self.numeric.items() if interval.start <= n <= interval.end)
        return CandidateSet(tuple(sorted(i for ids in groups for i in ids)),
            SemanticState.AMBIGUOUS if any(len(ids) > 1 for ids in groups) else SemanticState.SUPPORTED,
            'numeric_range')


@dataclass(frozen=True)
class NumericRange:
    context: int
    start: Decimal
    end: Decimal

    def __post_init__(self) -> None:
        if (type(self.context) is not int or self.context <= 0 or not self.start.is_finite() or not self.end.is_finite()
                or self.start > self.end):
            raise ValueError('Valid finite contextual range required')


@dataclass(frozen=True)
class NumericResult:
    state: SemanticState
    value: Optional[int] = None


def compare_numeric(left: IssueNumberFacts, right: IssueNumberFacts) -> NumericResult:
    a, b = left.numeric, right.numeric
    if a is None or b is None:
        return NumericResult(SemanticState.UNSUPPORTED)
    return NumericResult(SemanticState.SUPPORTED, (a > b) - (a < b))


def in_numeric_range(facts: IssueNumberFacts, context: int, interval: NumericRange) -> NumericResult:
    value = facts.numeric
    if value is None or context != interval.context:
        return NumericResult(SemanticState.UNSUPPORTED)
    return NumericResult(SemanticState.SUPPORTED, int(interval.start <= value <= interval.end))


def match_candidates(label: str, records: Tuple[IssueRecord, ...], *, exact_first: bool = True,
                     allow_unnumbered: bool = False) -> CandidateSet:
    """Return all candidates, never a first-row identity or a numeric sentinel."""
    return NumberCatalog.build((r.id, r.legacy_label, r.facts.number if r.facts else None)
                               for r in records).match(label, exact_first=exact_first,
                                                      allow_unnumbered=allow_unnumbered)


def presentation_key(record: IssueRecord, *, legacy_order: bool = True) -> tuple:
    """Presentation only. Legacy date/float order is retained with an ID tie."""
    if legacy_order and record.legacy_number is not None:
        return (0, record.legacy_date or '', record.legacy_number, record.id)
    facts = record.facts
    if facts is not None and facts.provider_ordinal is not None:
        return (1, 0, facts.provider_ordinal, record.provider_identities, record.id)
    if facts is not None and facts.number.numeric is not None:
        return (1, 1, facts.number.numeric, record.provider_identities, record.id)
    return (1, 2, facts.number.raw_label or '' if facts else record.legacy_label,
            record.provider_identities, record.id)
