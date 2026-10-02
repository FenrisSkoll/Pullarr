"""Current classifier exits and persistence boundaries, not an explanation API."""

from dataclasses import replace
from datetime import datetime
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fixtures.comicvine_fetch import issue_response
from fixtures.comicvine_search import volume_response
from fixtures.publication_kinds import mapped
from Tbackend.implementations.collected_titles import cv_mapped
from Tbackend.implementations.format_contract import FormatHarness

from backend.base.definitions import SpecialVersion
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.format_evidence import (
    PhysicalFormat, ProviderFormatEvidence, single_issue_format)
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.implementations.volumes import (determine_special_version,
                                             refresh_and_scan)


class ClassificationPaths(FormatHarness, TestCase):
    def cv(self, title='Unmarked', issue_title=None, description=None, date=None):
        identity = 8000 + self.db.execute('SELECT COUNT(*) FROM volumes').fetchone()[0]
        metadata = cv_mapped(volume_response(id=identity, name=title, description=description), [
            issue_response(id=identity + 1000, volume={'id': identity}, name=issue_title, cover_date=date)])
        return self.add_result(VolumeFetchResult(metadata, ()))

    def test_explicit_locked_normal(self):
        result = mapped(1530)
        volume = self.add_result(result, SpecialVersion.NORMAL)
        self.assertEqual(determine_special_version(volume.id, result.format_evidence,
                                                  result.publication_evidence), SpecialVersion.NORMAL)
        self.assertTrue(volume.get_data().special_version_locked)

    def test_explicit_locked_tpb(self):
        result = self.mapped()
        volume = self.add_result(result, SpecialVersion.TPB)
        self.assertEqual(determine_special_version(volume.id, result.format_evidence), SpecialVersion.TPB)

    def test_explicit_locked_hc(self):
        result = mapped(17494)
        volume = self.add_result(result, SpecialVersion.HARD_COVER)
        self.assertEqual(determine_special_version(volume.id, result.format_evidence,
                                                  result.publication_evidence), SpecialVersion.HARD_COVER)

    def test_evidence_free_candidate_ignores_lock_but_refresh_cannot_write_it(self):
        result = mapped(1530)
        volume = self.add_result(result, SpecialVersion.HARD_COVER)
        self.assertEqual(determine_special_version(volume.id), SpecialVersion.TPB)
        with patch.object(MetronMetadataProvider, 'fetch_volume_enriched',
                          new=AsyncMock(return_value=replace(result, format_evidence=None, publication_evidence=None))):
            refresh_and_scan(volume.id)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.HARD_COVER)

    def test_manual_update_and_unlock_do_not_recalculate_or_record_origin(self):
        volume = self.cv(date='2000-01-01')
        volume.update({'special_version': SpecialVersion.OMNIBUS}, from_public=True)
        self.assertFalse(volume.get_data().special_version_locked)
        volume.update({'special_version_locked': True}, from_public=True)
        volume.update({'special_version_locked': False}, from_public=True)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.OMNIBUS)
        self.assertEqual(determine_special_version(volume.id), SpecialVersion.TPB)

    def test_vai_requires_nonempty_all_matching_titles(self):
        for title in ('Volume 1', 'Vol. Two', 'V 3: Collected'):
            self.assertEqual(self.cv(issue_title=title).get_data().special_version, SpecialVersion.VOLUME_AS_ISSUE)
        for title in (None, 'Book One', 'Volume 1 subtitle', 'Preface Volume 1'):
            self.assertEqual(self.cv(issue_title=title).get_data().special_version, SpecialVersion.NORMAL)

    def test_metron_hardcover_evidence_precedes_title_markers(self):
        result = self.mapped()
        result.metadata.title = 'Synthetic Omnibus'
        self.assertEqual(self.add_result(result).get_data().special_version, SpecialVersion.HARD_COVER)

    def test_metron_trade_paperback_evidence_precedes_annual(self):
        result = self.mapped('kickdown')
        result.metadata.title = 'Synthetic Annual'
        self.assertEqual(self.add_result(result).get_data().special_version, SpecialVersion.TPB)

    def test_metron_one_shot_evidence_not_age_fallback(self):
        result = mapped(1530)
        result.metadata.issues[0].date = None
        self.assertEqual(self.add_result(result).get_data().special_version, SpecialVersion.ONE_SHOT)

    def test_metron_omnibus_evidence_not_title_marker(self):
        result = mapped(17494)
        result.metadata.issues[0].date = None
        self.assertEqual(self.add_result(result).get_data().special_version, SpecialVersion.OMNIBUS)

    def test_multi_book_omnibus_vai_short_circuits_evidence_evaluation(self):
        result = mapped(14110)
        volume = self.add_result(result)
        with patch('backend.implementations.classification.single_issue_format', wraps=single_issue_format) as physical:
            self.assertEqual(determine_special_version(volume.id, result.format_evidence,
                             result.publication_evidence), SpecialVersion.VOLUME_AS_ISSUE)
            physical.assert_not_called()

    def test_multi_book_tpb_no_provider_classification(self):
        result = self.mapped('invincible', ['invincible%d-metron-issue' % i for i in (1, 2, 3)])
        self.assertEqual(self.add_result(result).get_data().special_version, SpecialVersion.NORMAL)

    def test_conflict_is_not_an_exit_heuristic_still_decides(self):
        result = mapped(1530)
        result = replace(result, format_evidence=ProviderFormatEvidence(
            'metron', '1530', 'synthetic.binding', 'Hardcover', PhysicalFormat.HARDCOVER))
        volume = self.add_result(result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)
        volume.update({'title': 'Synthetic Omnibus'})
        self.assertEqual(determine_special_version(volume.id, result.format_evidence,
                                                  result.publication_evidence), SpecialVersion.OMNIBUS)

    def test_cv_volume_markers_order_omnibus_one_shot_hc(self):
        for title, expected in (('Omnibus One-Shot Hardcover', SpecialVersion.OMNIBUS),
                                ('One-Shot Hardcover', SpecialVersion.ONE_SHOT),
                                ('Hardcover', SpecialVersion.HARD_COVER)):
            self.assertEqual(self.cv(title=title).get_data().special_version, expected)

    def test_cv_issue_exact_markers_after_volume_markers(self):
        for title, expected in (('OMNIBUS', SpecialVersion.OMNIBUS), ('HC', SpecialVersion.HARD_COVER),
                                ('Hard Cover', SpecialVersion.HARD_COVER), ('OS', SpecialVersion.ONE_SHOT)):
            self.assertEqual(self.cv(issue_title=title).get_data().special_version, expected)
        self.assertEqual(self.cv(title='Hardcover', issue_title='Omnibus').get_data().special_version, SpecialVersion.HARD_COVER)
        self.assertEqual(self.cv(issue_title=' HC ').get_data().special_version, SpecialVersion.HARD_COVER)
        volume = self.cv()
        # The CV adapter trims tabs; inject a durable synthetic value to isolate
        # the classifier's own literal-space normalization, not adapter cleanup.
        self.db.execute('UPDATE issues SET title=? WHERE volume_id=?', ('HC\t', volume.id))
        self.assertEqual(determine_special_version(volume.id), SpecialVersion.NORMAL)

    def test_tpb_title_is_not_a_classifier_marker(self):
        for date, expected in ((None, SpecialVersion.NORMAL), ('2025-12-31', SpecialVersion.NORMAL),
                                ('2000-01-01', SpecialVersion.TPB)):
            self.assertEqual(self.cv(title='TPB', issue_title='TPB', description='TPB', date=date).get_data().special_version, expected)

    def test_description_markers_order_and_first_sentence_only(self):
        for description, expected in (('Omnibus Hardcover.', SpecialVersion.OMNIBUS),
                                      ('One-Shot Hardcover.', SpecialVersion.ONE_SHOT),
                                      ('Hardcover.', SpecialVersion.HARD_COVER),
                                      ('A book. Hardcover.', SpecialVersion.NORMAL),
                                      ('<a href="https://example.invalid">Hardcover</a> book.', SpecialVersion.NORMAL)):
            self.assertEqual(self.cv(description=description).get_data().special_version, expected)

    def test_annual_title_exclusion_after_markers_before_description(self):
        self.assertEqual(self.cv(title='Semiannual', description='Hardcover', date='2000-01-01').get_data().special_version, SpecialVersion.NORMAL)
        self.assertEqual(self.cv(title='Hardcover Annual').get_data().special_version, SpecialVersion.HARD_COVER)

    def test_annual_description_exclusion_after_special_markers(self):
        self.assertEqual(self.cv(description='Annual collection.', date='2000-01-01').get_data().special_version, SpecialVersion.NORMAL)
        self.assertEqual(self.cv(description='Hardcover annual.').get_data().special_version, SpecialVersion.HARD_COVER)

    def test_age_strict_thirty_days_boundary_and_future(self):
        # Harness pins now to 2026-01-01 00:00:00 (host-local naive clock).
        for date, expected in (('2025-12-01', SpecialVersion.TPB), ('2025-12-02', SpecialVersion.NORMAL),
                                ('2025-12-31', SpecialVersion.NORMAL), ('2026-02-01', SpecialVersion.NORMAL)):
            self.assertEqual(self.cv(date=date).get_data().special_version, expected)

    def test_current_recomputation_changes_with_time_not_stored_value(self):
        volume = self.cv(date='2025-12-02')
        with patch('backend.implementations.volumes.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 1, 2)
            self.assertEqual(determine_special_version(volume.id), SpecialVersion.TPB)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.NORMAL)

    def test_unknown_and_graphic_novel_do_not_supply_reason(self):
        for label in ('Unknown', 'Graphic Novel'):
            result = self.mapped()
            result = replace(result, format_evidence=replace(result.format_evidence, raw_value=label, physical_format=None))
            volume = self.add_result(result)
            self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)
            self.db.execute('DELETE FROM volumes')
            self.db.commit()

    def test_zero_issues_no_vacuous_vai_or_provider_acceptance(self):
        result = mapped(1530)
        result.metadata.issues = []
        result.metadata.issue_count = 0
        result = replace(result, enrichment=tuple(e for e in result.enrichment if e.entity == 'volume'), issue_facts=())
        self.assertEqual(self.add_result(result).get_data().special_version, SpecialVersion.NORMAL)

    def test_malformed_date_is_failure_not_default_normal(self):
        volume = self.cv()
        self.db.execute("UPDATE issues SET date='not-a-date' WHERE volume_id=?", (volume.id,))
        with self.assertRaises(ValueError):
            determine_special_version(volume.id)

    def test_same_stored_value_cannot_reveal_provider_or_heuristic_source(self):
        result = self.mapped('kickdown')
        volume = self.add_result(result)
        before = self.state()
        # TPB is both the provider result and the no-evidence current fallback.
        self.assertEqual(determine_special_version(volume.id), SpecialVersion.TPB)
        self.assertEqual(self.state(), before)
        self.assertNotIn('classification_source', volume.get_public_data())

    def test_current_classifier_read_cost_and_no_write(self):
        volume = self.cv(date='2000-01-01')
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            self.assertEqual(determine_special_version(volume.id), SpecialVersion.TPB)
        finally:
            self.db.set_trace_callback(None)
        self.assertEqual(len(statements), 3)
        self.assertTrue(all(s.lstrip().upper().startswith('SELECT') for s in statements))
