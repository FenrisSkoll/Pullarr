"""Bibliographic observations, never identity, classification or file coverage."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional, Tuple

POLICY = 'kapowarr-bibliography/v1'
MAX_STORIES = 1000
MAX_TEXT = 8192
MAX_ISSUE_TEXT_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_TEXT_BYTES = 32 * 1024 * 1024
ROLES = ('script', 'pencils', 'inks', 'colors', 'letters', 'editing')


def bounded_texts(values):
    if any(v is not None and (not isinstance(v, str) or len(v) > MAX_TEXT) for v in values):
        raise ValueError('Invalid bibliography text')


def immutable_values(*values):
    if any(not isinstance(value, tuple) for value in values):
        raise ValueError('Bibliography collections must be immutable tuples')


def numeric_text(value, *, signed=False):
    if value is None:
        return
    try:
        if not isinstance(value, str) or len(value) > 32:
            raise ValueError('Invalid bibliography decimal')
        number = Decimal(value)
        if not number.is_finite() or (not signed and number < 0):
            raise ValueError('Invalid bibliography decimal')
    except InvalidOperation:
        raise ValueError('Invalid bibliography decimal') from None


class StoryMode(str, Enum):
    IDENTIFIED = 'identified_story'
    OBSERVATION = 'issue_scoped_story_observation'


@dataclass(frozen=True)
class EditionFacts:
    isbn: Optional[str] = None
    isbn_normalized: Optional[str] = None
    isbn_validity: str = 'unknown'
    barcode: Optional[str] = None
    page_count: Optional[str] = None
    page_count_numeric: Optional[str] = None
    variant_name: Optional[str] = None
    indicia_publisher: Optional[str] = None
    indicia_printer: Optional[str] = None
    brand: Optional[str] = None
    rating: Optional[str] = None
    indicia_frequency: Optional[str] = None
    cover_reference: Optional[str] = None
    # Missing/malformed optional fields cannot erase previously acquired facts.
    supplied: Tuple[str, ...] = ()

    def __post_init__(self):
        immutable_values(self.supplied)
        bounded_texts((self.isbn, self.barcode, self.page_count, self.variant_name,
            self.indicia_publisher, self.indicia_printer, self.brand, self.rating,
            self.indicia_frequency, self.cover_reference, self.isbn_normalized))
        numeric_text(self.page_count_numeric)
        if self.isbn_validity not in ('valid', 'invalid', 'unknown'):
            raise ValueError('Invalid ISBN observation')


@dataclass(frozen=True)
class PublicationFacts:
    binding: Optional[str] = None
    publishing_format: Optional[str] = None
    color: Optional[str] = None
    dimensions: Optional[str] = None
    paper_stock: Optional[str] = None
    supplied: Tuple[str, ...] = ()
    diagnostics: Tuple[str, ...] = ()

    def __post_init__(self):
        immutable_values(self.supplied, self.diagnostics)
        bounded_texts((self.binding, self.publishing_format, self.color, self.dimensions, self.paper_stock))


@dataclass(frozen=True)
class CreditObservation:
    role: str
    text: Optional[str]

    def __post_init__(self):
        bounded_texts((self.text,))
        if self.role not in ROLES:
            raise ValueError('Invalid credit observation')


@dataclass(frozen=True)
class StoryObservation:
    source_position: int
    sequence: Optional[str]
    story_type: Optional[str]
    title: Optional[str]
    feature: Optional[str]
    page_count: Optional[str]
    page_count_numeric: Optional[str]
    characters: Optional[str]
    genre: Optional[str]
    credits: Tuple[CreditObservation, ...] = ()
    mode: StoryMode = StoryMode.OBSERVATION
    provider_story_id: Optional[str] = None

    def __post_init__(self):
        immutable_values(self.credits)
        numeric_text(self.sequence, signed=True)
        numeric_text(self.page_count_numeric)
        bounded_texts((self.title, self.feature, self.page_count, self.characters, self.genre,
                       self.story_type, self.provider_story_id))
        if (not isinstance(self.mode, StoryMode) or (self.mode == StoryMode.IDENTIFIED) != bool(self.provider_story_id)
                or type(self.source_position) is not int or self.source_position < 0
                or len({c.role for c in self.credits}) != len(self.credits)):
            raise ValueError('Invalid story identity/observation')


@dataclass(frozen=True)
class IssueBibliography:
    provider: str
    edition: EditionFacts
    stories: Tuple[StoryObservation, ...]
    diagnostics: Tuple[str, ...] = ()
    policy: str = POLICY
    story_scope: str = 'reported_set_active_completeness_unproven'

    def __post_init__(self):
        immutable_values(self.stories, self.diagnostics)
        if (not self.provider or len(self.stories) > MAX_STORIES
                or len({s.source_position for s in self.stories}) != len(self.stories)):
            raise ValueError('Invalid bibliography scope')
        if self.text_bytes > MAX_ISSUE_TEXT_BYTES:
            raise ValueError('Bibliography text limit')

    @property
    def text_bytes(self):
        edition = (self.edition.isbn, self.edition.barcode, self.edition.page_count,
            self.edition.variant_name, self.edition.indicia_publisher, self.edition.indicia_printer,
            self.edition.brand, self.edition.rating, self.edition.indicia_frequency, self.edition.cover_reference)
        return sum(len(v.encode('utf8')) for v in edition if v is not None) + sum(
            len(v.encode('utf8')) for s in self.stories for v in
            (s.title, s.feature, s.story_type, s.page_count, s.characters, s.genre,
             *(c.text for c in s.credits)) if v is not None)
