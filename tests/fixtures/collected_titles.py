"""Offline access to the small reviewed corpus; no live API access."""

import json
from copy import deepcopy
from pathlib import Path

CORPUS = json.loads((Path(__file__).parent / 'collected_editions/live.json').read_text(encoding='utf-8'))


def record(label):
    return deepcopy(CORPUS['records'][label]['data'])


def cv_issue_snapshot(volume_label, captured_labels):
    """Complete title enumeration, with synthetic NULL non-title fields.

    CV volume responses contain all issue IDs/names/numbers. Only explicitly
    captured details have real dates/descriptions. Do not call these synthesized
    rows complete live issue responses.
    """
    volume = record(volume_label)
    captured = {record(label)['id']: record(label) for label in captured_labels}
    return [captured.get(issue['id'], dict(issue, volume={'id': volume['id']},
                                         cover_date=None, store_date=None, description=None))
            for issue in volume['issues']]
