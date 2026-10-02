"""Explicit single-operation coordination, delegating every effect to its owner.

Trusted worker API only. Roots/database are application configuration, never
part of a history confirmation. No queue, bulk undo or force/manual override.
"""

from contextlib import closing

from backend.base.maintenance_history import HistoryError
from backend.base.organization_job import OrganizationError
from backend.features.maintenance_history import MaintenanceHistory
from backend.features.organization_execution import OrganizationExecutor
from backend.internals.organization_reservations import load_reservations


class MaintenanceRecovery:
    def __init__(self, database: str, allowed_roots: tuple[str, ...]):
        self.database, self.allowed_roots = database, allowed_roots
        self.history = MaintenanceHistory(database)

    def _executor(self):
        return OrganizationExecutor(self.database, self.allowed_roots)

    def preview_recovery(self, domain, identifier):
        entry = self.history.get(domain, identifier)
        if domain != 'organization':
            return dict(entry=entry, capability='history_only', eligible=False,
                        reasons=['domain_owned_operation_required'])
        try:
            with closing(self._executor()) as executor:
                result = executor.preview_recovery(identifier)
        except OrganizationError as error:
            return dict(entry=entry, capability='recovery_blocked', eligible=False,
                        reasons=[error.code.value])
        result['entry'] = entry
        result['capability'] = 'recovery_available' if result['eligible'] else 'recovery_blocked'
        result['manual_inspection_required'] = any(reason in (
            'reconciliation_conflict', 'source_missing_or_changed', 'unsafe_path_or_symlink',
            'corrupt_or_unsupported_history') for reason in result['reasons'])
        return result

    def recover(self, identifier, digest, *, confirmed):
        self._confirmation(digest, confirmed)
        with closing(self._executor()) as executor:
            executor.apply_job(identifier, approved_recovery=digest)
        return self.history.get('organization', identifier)

    def preview_inverse(self, domain, identifier):
        entry = self.history.get(domain, identifier)
        if domain != 'organization':
            return dict(entry=entry, eligible=False, capability=entry['inverse_capability'],
                        reasons=['fresh_reverse_switch_required' if domain == 'provider_switch' else 'history_only'])
        try:
            with closing(self._executor()) as executor:
                preview = executor.preview_undo(identifier)
                reasons = list(preview.reasons)
                if preview.eligible:
                    index = load_reservations(executor.store.db.cursor())
                    if index.conflicts(preview.current_path) or index.conflicts(preview.restore_path):
                        reasons.append('executor_or_path_claimed')
                return dict(entry=entry, eligible=not reasons,
                    capability=('inverse_available' if not reasons else 'unsupported'
                        if 'lossless_metadata_undo_not_supported' in reasons else 'inverse_blocked'),
                    digest=preview.intent_digest if not reasons else None, reasons=reasons,
                    current_path=None if entry['internal_storage_hidden'] else preview.current_path,
                    restore_path=preview.restore_path,
                    internal_storage_hidden=entry['internal_storage_hidden'])
        except OrganizationError as error:
            return dict(entry=entry, eligible=False, capability='inverse_blocked', reasons=[error.code.value])

    def create_inverse(self, identifier, digest, *, confirmed):
        self._confirmation(digest, confirmed)
        with closing(self._executor()) as executor:
            inverse = executor.create_undo_job(identifier, digest)
        # Registration only. Normal task/executor execution remains explicit.
        return self.history.get('organization', inverse)

    @staticmethod
    def _confirmation(digest, confirmed):
        if (confirmed is not True or not isinstance(digest, str) or len(digest) != 64
                or any(char not in '0123456789abcdef' for char in digest)):
            raise HistoryError('exact_confirmation_required')
