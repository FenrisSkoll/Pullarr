"""Immutable bounded review values. No durable or executable switch intent."""

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date
from enum import Enum
from hashlib import sha256


class SwitchReviewError(ValueError):
    """Controlled diagnostic code; never a remote body or credential."""


def json_value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class FrozenReviewData:
    """Owned immutable bytes, with detached mutable transport projections."""
    payload: bytes

    @classmethod
    def create(cls, value, limit=16 * 1024 * 1024):
        chunks = []
        size = 0
        encoder = json.JSONEncoder(sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        for chunk in encoder.iterencode(json_value(value)):
            encoded = chunk.encode('ascii')
            size += len(encoded)
            if size > limit:
                raise SwitchReviewError('review_size_limit')
            chunks.append(encoded)
        return cls(b''.join(chunks))

    @property
    def digest(self):
        return sha256(self.payload).hexdigest()

    def view(self):
        return json.loads(self.payload)
