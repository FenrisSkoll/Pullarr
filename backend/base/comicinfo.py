"""Immutable embedded metadata values; not provider metadata or identity authority."""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional, Tuple


class ComicInfoCode(Enum):
    UNSUPPORTED_FORMAT = 'unsupported_format'
    ARCHIVE_UNREADABLE = 'archive_unreadable'
    UNSAFE_MEMBER = 'unsafe_member'
    DUPLICATE_MEMBER = 'duplicate_member'
    MULTIPLE_DOCUMENTS = 'multiple_documents'
    ENCRYPTED = 'encrypted'
    LIMIT_EXCEEDED = 'limit_exceeded'
    XML_MALFORMED = 'xml_malformed'
    XML_UNSAFE = 'xml_unsafe'
    UNSUPPORTED_ROOT = 'unsupported_root'
    UNKNOWN_VERSION = 'unknown_version'
    INVALID_FIELD = 'invalid_field'
    DUPLICATE_FIELD = 'duplicate_field'
    STALE = 'stale_observation'
    WRITE_UNSUPPORTED = 'write_unsupported'
    WRITE_FAILED = 'write_failed'
    MERGE_AMBIGUOUS = 'merge_ambiguous'


@dataclass(frozen=True)
class ComicInfoDiagnostic:
    code: ComicInfoCode
    field: Optional[str] = None


class ComicInfoError(Exception):
    def __init__(self, code: ComicInfoCode, *, os_error: Optional[int] = None):
        self.code = code
        self.os_error = os_error
        # Never include raw XML, paths, URLs or backend exception text.
        super().__init__(code.value)


class DatePrecision(Enum):
    UNKNOWN = 'unknown'
    YEAR = 'year'
    MONTH = 'month'
    DAY = 'day'
    INVALID = 'invalid'


@dataclass(frozen=True)
class ComicInfoDate:
    year: Optional[int]
    month: Optional[int]
    day: Optional[int]
    precision: DatePrecision

    @property
    def complete_date(self) -> Optional[str]:
        if (self.precision == DatePrecision.DAY and self.year is not None
                and self.month is not None and self.day is not None):
            return date(self.year, self.month, self.day).isoformat()
        return None


@dataclass(frozen=True)
class XmlField:
    name: str  # expanded XML name, preserving namespace identity
    text: Optional[str]
    attributes: Tuple[Tuple[str, str], ...] = ()
    structured: bool = False


@dataclass(frozen=True)
class ComicInfoDocument:
    raw_bytes: bytes
    fields: Tuple[XmlField, ...]
    date: ComicInfoDate
    diagnostics: Tuple[ComicInfoDiagnostic, ...] = ()
    declared_encoding: Optional[str] = None

    def values(self, name: str) -> Tuple[XmlField, ...]:
        return tuple(f for f in self.fields if f.name == name)

    def text(self, name: str) -> Optional[str]:
        """No arbitrary first-wins for duplicate/structured scalar fields.

        Absent/ambiguous returns None; an empty element returns ''. The fields
        tuple and source bytes retain the distinction in all cases.
        """
        found = self.values(name)
        return (found[0].text or '') if len(found) == 1 and not found[0].structured else None

    @property
    def series(self) -> Optional[str]:
        return self.text('Series')

    @property
    def number(self) -> Optional[str]:
        return self.text('Number')

    @property
    def title(self) -> Optional[str]:
        return self.text('Title')

    @property
    def publisher(self) -> Optional[str]:
        return self.text('Publisher')

    @property
    def volume(self) -> Optional[str]:
        return self.text('Volume')
