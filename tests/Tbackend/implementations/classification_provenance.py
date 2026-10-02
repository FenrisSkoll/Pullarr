"""Actual application paths: a pure candidate is never proof of application."""

import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fixtures.metadata_refresh import RefreshHarness
from Tbackend.implementations.classification_decisions import evaluate
from Tbackend.implementations.classification_parity import (PHYSICAL,
                                                            PUBLICATION)
from Tbackend.implementations.format_contract import FormatHarness
from Tbackend.implementations.gcd_lifecycle import GcdLifecycleHarness

from backend.base.definitions import SpecialVersion as SV
from backend.features.classification_details import classification_details
from backend.implementations import volumes
from backend.implementations.classification import ClassificationIssue
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.internals.classification_provenance import (
    ApplicationKind, ClassificationApplicationReceipt, InputScope, apply,
    control, decision_payload, details, revisions, summaries)


class ProvenanceLifecycle(FormatHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch('backend.features.classification_details.get_db', side_effect=self.db.cursor)

    def detail(self, volume):
        return details(self.db.cursor(), volume.id)

    def snapshot(self):
        # Python 3.13 iterdump changes cursor.row_factory. DBConnection caches
        # its cursor, so the test-only dump must restore that reader contract.
        cursor = self.db.cursor()
        factory = cursor.row_factory
        try:
            return tuple(self.db.iterdump())
        finally:
            cursor.row_factory = factory

    def refresh(self, volume, result):
        with patch.object(MetronMetadataProvider, 'fetch_volume_enriched', new=AsyncMock(return_value=result)):
            volumes.refresh_and_scan(volume.id)

    def test_auto_add_physical_and_explicit_same_value_control_separate(self):
        volume = self.add_result(self.mapped())
        original = self.detail(volume)['provenance']
        self.assertEqual(original['reason'], 'sole_issue_physical_evidence')
        physical, publication = original['evidence']
        self.assertEqual((physical['provider'], physical['raw_value'], physical['disposition']), ('metron', 'Hardcover', 'accepted'))
        self.assertEqual(publication['availability'], 'available_with_value')
        self.assertEqual(publication['disposition'], 'unmapped_value')
        for locked in (True, False):
            volume.update({'special_version_locked': locked})
            current = self.detail(volume)
            self.assertEqual(current['provenance'], original)
            self.assertEqual(current['last_control_action']['action'], 'lock' if locked else 'unlock')
        volume.update({'special_version': SV.HARD_COVER})
        explicit = self.detail(volume)['provenance']
        self.assertEqual(explicit['application_kind'], 'explicit_selection')
        self.assertIsNone(explicit['source'])
        self.assertIsNone(explicit['reason'])

    def test_same_value_automatic_refresh_replaces_cause(self):
        result = self.mapped('kickdown')
        volume = self.add_result(result)
        self.assertEqual(self.detail(volume)['provenance']['reason'], 'sole_issue_physical_evidence')
        self.refresh(volume, replace(result, format_evidence=None))
        current = self.detail(volume)['provenance']
        self.assertEqual(current['applied_value'], 'tpb')
        self.assertEqual(current['reason'], 'aged_single_issue_tpb')
        self.assertIsNotNone(current['issue_date'])

    def test_locked_production_refresh_preserves_receipt_despite_candidate(self):
        result = self.mapped()
        volume = self.add_result(result)
        volume.update({'special_version_locked': True})
        old = self.detail(volume)
        self.refresh(volume, replace(result, format_evidence=None))
        self.assertEqual(self.detail(volume), old)
        diagnostic = classification_details(volume.id, True)
        self.assertEqual(diagnostic['stored']['value'], 'hard-cover')
        self.assertEqual(diagnostic['current_evaluation']['value'], 'tpb')
        self.assertFalse(diagnostic['current_evaluation']['application'])

    def test_manual_write_during_fetch_wins_even_same_value_unlocked(self):
        result = self.mapped()
        volume = self.add_result(result)
        async def fetch(*_):
            volume.update({'special_version': SV.HARD_COVER})
            return result
        with patch.object(MetronMetadataProvider, 'fetch_volume_enriched', new=fetch):
            volumes.refresh_and_scan(volume.id)
        self.assertEqual(self.detail(volume)['provenance']['application_kind'], 'explicit_selection')

    def test_lock_during_fetch_wins(self):
        result = self.mapped()
        volume = self.add_result(result)
        before = self.detail(volume)['provenance']
        async def fetch(*_):
            volume.update({'special_version_locked': True})
            return result
        with patch.object(MetronMetadataProvider, 'fetch_volume_enriched', new=fetch):
            volumes.refresh_and_scan(volume.id)
        self.assertEqual(self.detail(volume)['provenance'], before)

    def test_direct_same_value_and_different_updates_invalidate_only_origin(self):
        volume = self.add_result(self.mapped())
        original = self.detail(volume)['provenance']
        self.db.execute('UPDATE volumes SET title=title,special_version_locked=1 WHERE id=?', (volume.id,))
        self.assertEqual(self.detail(volume)['provenance'], original)
        self.assertIsNone(self.detail(volume)['last_control_action'])
        for value in ('hard-cover', 'omnibus'):
            self.db.execute('UPDATE volumes SET special_version=? WHERE id=?', (value, volume.id))
            self.assertEqual(self.detail(volume)['provenance']['status'], 'invalidated')
            self.assertIsNone(self.detail(volume)['provenance']['reason'])

    def test_failed_receipt_rolls_back_manual_value_and_control(self):
        volume = self.add_result(self.mapped())
        old = self.detail(volume)
        self.db.execute("CREATE TEMP TRIGGER deny_receipt BEFORE INSERT ON classification_provenance BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            volume.update({'special_version_locked': True, 'special_version': SV.OMNIBUS})
        self.assertEqual(self.detail(volume), old)

    def test_failed_add_has_no_volume_or_receipt(self):
        self.db.execute("CREATE TEMP TRIGGER deny_receipt BEFORE INSERT ON classification_provenance BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_result(self.mapped())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM classification_provenance').fetchone()[0], 0)

    def test_failed_refresh_receipt_preserves_prior_origin(self):
        result = self.mapped()
        volume = self.add_result(result)
        old = self.detail(volume)
        self.db.execute("CREATE TEMP TRIGGER deny_receipt BEFORE INSERT ON classification_provenance BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.refresh(volume, replace(result, format_evidence=None))
        self.assertEqual(self.detail(volume), old)

    def test_current_evaluation_age_drift_read_only_not_history(self):
        result = replace(self.mapped(), format_evidence=None)
        result.metadata.title = 'Plain'
        result.metadata.description = None
        result.metadata.issues[0].title = 'Story'
        result.metadata.issues[0].date = '2025-12-31'
        volume = self.add_result(result)
        before = self.snapshot()
        with patch.object(volumes, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 3, 1)
            current = classification_details(volume.id, True)
        self.assertEqual(current['provenance']['reason'], 'no_rule_matched')
        self.assertEqual(current['current_evaluation']['reason'], 'aged_single_issue_tpb')
        self.assertEqual(current['current_evaluation']['evidence'][0]['availability'], 'not_available_in_scope')
        self.assertEqual(self.snapshot(), before)

    def test_explicit_and_legacy_default_add_kinds(self):
        result = self.mapped()
        with patch.object(volumes, 'fetch_volume_result', new=AsyncMock(return_value=result)), \
                patch.object(volumes, 'evaluate_special_version', side_effect=AssertionError('classifier bypass')):
            local = volumes.Library.add_metadata(volumes.ProviderVolumeIdentity('metron', result.metadata.provider_id),
                1, True, special_version=SV.NORMAL, legacy_default_classification=True)
        record = details(self.db.cursor(), local)
        self.assertEqual(record['provenance']['application_kind'], 'legacy_default_application')
        self.assertIsNone(record['provenance']['source'])
        self.assertEqual(record['stored'], {'value': None, 'locked': True})

    def test_future_policy_unknown_reason_remains_recorded(self):
        volume = self.add_result(self.mapped())
        old = self.detail(volume)
        with patch('backend.implementations.classification.POLICY_ID', 'test-next-policy'):
            self.assertEqual(self.detail(volume), old)
        self.db.execute("UPDATE classification_provenance SET reason='future_safe_reason' WHERE volume_id=?", (volume.id,))
        self.assertEqual(self.detail(volume)['provenance']['reason'], 'future_safe_reason')

    def test_two_connection_revision_guard_manual_wins(self):
        volume = self.add_result(self.mapped())
        candidate = volumes.evaluate_volume_classification(volume.id)
        self.db.commit()
        with TemporaryDirectory(prefix='kapowarr-classification-race-') as folder:
            path = Path(folder) / 'race.db'
            with closing(sqlite3.connect(path)) as a, closing(sqlite3.connect(path)) as b:
                self.db.backup(a)
                expected = revisions(a.cursor(), [volume.id])[volume.id]
                apply(b.cursor(), volume.id, SV.HARD_COVER)
                self.assertFalse(apply(a.cursor(), volume.id, candidate.value, decision=candidate, expected_revision=expected))
                self.assertEqual(details(a.cursor(), volume.id)['provenance']['application_kind'], 'explicit_selection')

    def test_batch_diagnostics_and_no_receipt_n_plus_one(self):
        volume = self.add_result(self.mapped())
        self.db.executemany('INSERT INTO volumes(id,title,root_folder) VALUES(?,?,1)',
                            ((n, 'Plain') for n in range(100, 1100)))
        observations = []
        for count in (1, 100, 1000):
            ids = tuple(range(100, 100 + count))
            queries = []
            self.db.set_trace_callback(queries.append)
            start = perf_counter()
            for local in ids:
                apply(self.db.cursor(), local, SV.NORMAL)
            duration = perf_counter() - start
            queries.clear()
            summaries(self.db.cursor(), ids)
            selects = sum(s.lstrip().upper().startswith('SELECT') for s in queries)
            self.db.set_trace_callback(None)
            self.assertEqual(selects, (count + 399) // 400)
            observations.append((count, round(duration, 4), selects))
        print('Classification applications/seconds/summary SELECTs:', observations)

    def test_api_default_add_and_local_details_are_authenticated_read_only(self):
        from fixtures.comicvine_search import FAKE_APP_KEY

        from backend.internals.server import Server
        self.start_patch('frontend.api.Settings').return_value.sv = self.settings
        self.start_patch('frontend.api.get_db', side_effect=self.db.cursor)
        self.start_patch('frontend.api.StartTypeHandlers.diffuse_timer')
        for name in ('WebSocket', 'SimpleQueue', 'MPWebSocketQueue'):
            self.start_patch('backend.internals.server.' + name)
        app = Server._create_app()
        app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
        client = app.test_client()
        result = self.mapped()
        with patch.object(volumes, 'fetch_volume_result', new=AsyncMock(return_value=result)):
            response = client.post('/api/volumes', query_string={'api_key': FAKE_APP_KEY}, json={
                'provider': 'metron', 'provider_id': result.metadata.provider_id,
                'root_folder_id': 1, 'auto_search': False})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        local = response.json['result']['id']
        self.assertEqual(details(self.db.cursor(), local)['provenance']['application_kind'], 'legacy_default_application')
        self.assertNotIn('provenance', response.json['result'])
        route = f'/api/volumes/{local}/classification'
        self.assertEqual(client.get(route).status_code, 401)
        self.assertEqual(client.post(route, query_string={'api_key': FAKE_APP_KEY}).status_code, 405)
        before = self.snapshot()
        with patch.object(volumes, 'fetch_volume_result', side_effect=AssertionError('No remote details')):
            response = client.get(route, query_string={'api_key': FAKE_APP_KEY, 'evaluate': 'true'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json['result']['schema'], 'classification-details/v1')
        self.assertEqual(self.snapshot(), before)
        self.assertIn(b'classification-details-body', client.get(f'/volumes/{local}').data)


class ReceiptProjectionTests(TestCase):
    def test_all_branch_receipts_retain_exact_policy_facts_and_axes(self):
        # Existing full oracle suite owns all precedence; receipt uses its result.
        physical = next(p for p in PHYSICAL if p is not None)
        publication = next(p for p in PUBLICATION if p is not None)
        decision = evaluate(format_evidence=physical, publication_evidence=publication,
                            issues=(ClassificationIssue('Story', '2000-01-01'),))
        payload = decision_payload(decision, InputScope.DECISION_TIME)
        self.assertEqual(payload['reason'], decision.reason.value)
        self.assertEqual(len(payload['evidence']), 2)
        self.assertTrue(all(e['disposition'] == 'declined_cross_axis_conflict' for e in payload['evidence']))
        self.assertEqual(payload['replay_status'], 'explanation_complete_replay_incomplete')
        unknown = replace(next(p for p in PHYSICAL if p is not None), raw_value='<script>' * 100, physical_format=None)
        payload = decision_payload(evaluate(format_evidence=unknown), InputScope.DECISION_TIME)
        self.assertEqual(len(payload['evidence'][0]['raw_value']), 512)
        self.assertTrue(payload['evidence'][0]['raw_truncated'])

    def test_receipt_immutable_and_clock_boundary_unchanged(self):
        record = ClassificationApplicationReceipt(1, None, ApplicationKind.EXPLICIT, 'explicit_selection', 'now')
        with self.assertRaises(FrozenInstanceError):
            record.volume_id = 2
        now = datetime(2026, 1, 31)
        for delta, value in ((timedelta(), SV.NORMAL), (timedelta(microseconds=1), SV.TPB)):
            d = evaluate(issues=(ClassificationIssue('Story', '2026-01-01'),), evaluated_at=now + delta)
            self.assertEqual(d.value, value)


class GcdProvenance(GcdLifecycleHarness, TestCase):
    def test_gcd_snapshot_receipt_no_bibliography_adoption_or_extra_http(self):
        local = self.add_gcd()
        before = len(self.fake.requests)
        receipt = details(self.db.cursor(), local)['provenance']
        self.assertEqual(receipt['application_kind'], 'automatic_decision')
        self.assertTrue(all(e['availability'] == 'available_but_absent' for e in receipt['evidence']))
        volumes.refresh_and_scan(local)
        self.assertEqual(len(self.fake.requests) - before, before)
        self.assertIsNone(details(self.db.cursor(), local)['provenance']['evidence'][0]['provider'])


class ComicVineProvenance(RefreshHarness, TestCase):
    def test_bulk_actual_application_and_lock_preservation(self):
        added = details(self.db.cursor(), self.volume_id)['provenance']
        self.assertEqual(added['application_kind'], 'automatic_decision')
        self.assertTrue(all(e['availability'] == 'available_but_absent' for e in added['evidence']))
        self.refresh()
        refreshed = details(self.db.cursor(), self.volume_id)['provenance']
        self.assertEqual(refreshed['input_scope'], 'durable_fields_only')
        self.assertTrue(all(e['availability'] == 'not_available_in_scope' for e in refreshed['evidence']))
        volumes.Volume(self.volume_id).update({'special_version_locked': True})
        self.prepare_refresh()
        self.refresh()
        self.assertEqual(details(self.db.cursor(), self.volume_id)['provenance'], refreshed)

    def test_bulk_final_application_failure_rolls_back_receipts_not_earlier_metadata(self):
        from fixtures.comicvine_search import volume_response
        old = details(self.db.cursor(), self.volume_id)['provenance']
        self.db.execute("CREATE TEMP TRIGGER deny_receipt BEFORE INSERT ON classification_provenance BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.db.commit()
        self.prepare_refresh([volume_response(name='Already committed metadata', count_of_issues=2)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.refresh()
        self.assertEqual(details(self.db.cursor(), self.volume_id)['provenance'], old)
        self.assertEqual(volumes.Volume(self.volume_id).get_data().title, 'Already committed metadata')
