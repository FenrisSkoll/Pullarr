"""Explicit same-authority repair fields. Observation is never permission."""

from dataclasses import dataclass
from enum import Enum
from typing import Tuple

from backend.base.switch_review import FrozenReviewData

POLICY = 'kapowarr-metadata-repair/v1'


class RepairError(ValueError):
    """Controlled reason code; do not expose provider or filesystem exceptions."""


class Field(str, Enum):
    TITLE = 'title'
    YEAR = 'year'
    PUBLISHER = 'publisher'
    DESCRIPTION = 'description'
    SITE_URL = 'site_url'
    VOLUME_NUMBER = 'volume_number'
    ALT_TITLE = 'alt_title'
    FACTS = 'canonical_facts'
    BIBLIOGRAPHY = 'bibliography'


@dataclass(frozen=True)
class FieldSelection:
    scope: str
    local_id: int
    field: Field

    def __post_init__(self):
        if (self.scope not in ('volume', 'issue') or type(self.local_id) is not int
                or self.local_id <= 0 or not isinstance(self.field, Field)):
            raise RepairError('invalid_repair_field')

    @property
    def key(self):
        return f'{self.scope}:{self.local_id}:{self.field.value}'


@dataclass(frozen=True)
class RepairField:
    selection: FieldSelection
    values: FrozenReviewData
    support: str
    change: str
    reason: str = ''
    preservation: str = 'Excluded fields remain unchanged; no permanent override lock.'
    classification_input: bool = False

    def view(self):
        return dict(key=self.selection.key, scope=self.selection.scope,
                    local_id=self.selection.local_id, field=self.selection.field.value,
                    **self.values.view(), support=self.support, change=self.change,
                    reason=self.reason, preservation=self.preservation,
                    classification_input=self.classification_input)


def validate_selection(fields: Tuple[RepairField, ...], selected: Tuple[FieldSelection, ...]):
    if type(selected) is not tuple or len(selected) > 40000 or any(not isinstance(s, FieldSelection) for s in selected):
        raise RepairError('invalid_repair_selection')
    keys = tuple(s.key for s in selected)
    if len(keys) != len(set(keys)):
        raise RepairError('duplicate_repair_field')
    allowed = {f.selection.key: f for f in fields}
    if any(k not in allowed or allowed[k].support != 'supported' for k in keys):
        raise RepairError('unsupported_repair_field')
    return tuple(sorted(keys))
