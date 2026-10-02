"""Candidate/application distinction without changing existing write paths."""

from dataclasses import replace
from datetime import datetime
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fixtures import classifier_legacy as old
from Tbackend.implementations.format_contract import FormatHarness

from backend.base.definitions import SpecialVersion as SV
from backend.implementations import volumes
from backend.implementations.classification import (ClassificationSource,
                                                    evaluate_special_version)
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.internals.db_migration import DatabaseMigrationHandler


class ClassificationApplicationParity(FormatHarness, TestCase):
    def test_explicit_add_and_control_updates_bypass_evaluator(self):
        with patch.object(volumes, 'evaluate_special_version', side_effect=AssertionError('automatic evaluation')):
            volume = self.add_result(self.mapped(), SV.NORMAL)
            self.assertTrue(volume.get_data().special_version_locked)
            volume.update({'special_version': SV.OMNIBUS}, from_public=True)
            volume.update({'special_version_locked': False}, from_public=True)
        self.assertEqual(volume.get_data().special_version, SV.OMNIBUS)

    def test_unapplied_refresh_candidate_does_not_claim_lock_provenance(self):
        result = self.mapped()
        volume = self.add_result(result, SV.HARD_COVER)
        decisions = []

        def observe(**inputs):
            decision = evaluate_special_version(**inputs)
            decisions.append(decision)
            return decision

        with patch.object(volumes, 'evaluate_special_version', side_effect=observe), \
                patch.object(MetronMetadataProvider, 'fetch_volume_enriched', new=AsyncMock(
                    return_value=replace(result, format_evidence=None, publication_evidence=None))):
            volumes.refresh_and_scan(volume.id)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].value, SV.TPB)
        self.assertEqual(decisions[0].source, ClassificationSource.LEGACY_AGE_COUNT)
        self.assertTrue(decisions[0].locked)
        self.assertEqual(volume.get_data().special_version, SV.HARD_COVER)

    def test_historical_migration_classifier_call_contract(self):
        # Exercise actual migration call sites and generated UPDATE parameters.
        # DDL is a sink, not an attempted replay of obsolete schemas on schema 53.
        volume = self.add_result(self.mapped(), SV.OMNIBUS)
        for version in (7, 10):
            outputs = []
            for classifier in (old.determine_special_version, volumes.determine_special_version):
                with patch('backend.internals.db_migration.get_db') as db, \
                        patch.object(volumes.Library, 'get_volumes', return_value=[volume.id]), \
                        patch.object(volumes, 'determine_special_version', classifier), \
                        patch.object(old, 'datetime', wraps=datetime) as clock:
                    clock.now.return_value = datetime(2026, 1, 1)
                    db.return_value.executemany.side_effect = lambda sql, rows: outputs.append((sql, tuple(rows)))
                    DatabaseMigrationHandler.handlers[version]()
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[1][1], ((SV.TPB, volume.id),))
        # Read-only classifier; fake migration UPDATE was deliberately not applied.
        self.assertEqual(volume.get_data().special_version, SV.OMNIBUS)

    def test_thousand_issue_wrapper_query_parity(self):
        result = self.mapped()
        result.metadata.issues = [replace(result.metadata.issues[0], provider_id=str(900000 + number),
                                          issue_number=str(number), calculated_issue_number=float(number),
                                          title='Story %d' % number) for number in range(1000)]
        result.metadata.issue_count = 1000
        result = replace(result, enrichment=(), issue_facts=())
        volume = self.add_result(result)
        counts = []
        before = self.state()
        for classifier in (old.determine_special_version, volumes.determine_special_version):
            statements = []
            self.db.set_trace_callback(statements.append)
            try:
                self.assertEqual(classifier(volume.id, result.format_evidence), SV.NORMAL)
            finally:
                self.db.set_trace_callback(None)
            self.assertTrue(all(sql.lstrip().upper().startswith('SELECT') for sql in statements))
            counts.append(len(statements))
        self.assertEqual(counts, [3, 3])
        self.assertEqual(self.state(), before)
