"""Sanitized exact-resource snapshots; no live test requests."""

import json
from pathlib import Path

from backend.base.definitions import DateType
from backend.implementations.metadata.metron import MetronMetadataProvider


def snapshot(identity):
    path = Path(__file__).with_suffix('') / (str(identity) + '.json')
    return json.loads(path.read_text(encoding='utf-8'))


def mapped(identity):
    data = snapshot(identity)
    return MetronMetadataProvider.volume_result(data['series'], data['issues'], DateType.COVER_DATE)
