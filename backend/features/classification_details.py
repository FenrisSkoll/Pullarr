"""Local read-only explanation; historical receipts are never re-evaluated."""

from backend.internals.classification_provenance import (InputScope,
                                                         decision_payload,
                                                         details, transaction)
from backend.internals.db import get_db


def classification_details(volume_id, evaluate=False):
    from backend.implementations.volumes import (
        Volume, evaluate_volume_classification)
    Volume(volume_id)
    cursor = get_db()
    with transaction(cursor):
        result = details(cursor, volume_id)
        if evaluate:
            try:
                candidate = evaluate_volume_classification(volume_id)
                result['current_evaluation'] = dict(decision_payload(candidate, InputScope.DURABLE),
                    status='evaluated', application=False)
            except ValueError:
                # Invalid legacy input is a diagnostic failure, not Normal and
                # not a reason to invalidate a successful historical receipt.
                result['current_evaluation'] = dict(status='invalid_durable_input', application=False,
                    input_scope=InputScope.DURABLE.value)
        return result
