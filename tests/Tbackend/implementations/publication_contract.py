"""Freeze pre-publication classification and downstream modes for real records."""

from unittest import TestCase

from fixtures.publication_kinds import mapped, snapshot
from Tbackend.implementations.format_contract import FormatHarness

from backend.base.definitions import SpecialVersion
from backend.implementations.matching import match_special_version
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.naming import _determine_format


class PublicationContract(FormatHarness, TestCase):
    def old_transport(self, identity):
        result = mapped(identity)
        # Explicit pre-publication envelope preserves this historical baseline.
        return VolumeFetchResult(result.metadata, result.enrichment, result.format_evidence)

    def test_real_one_shot_description_heuristic(self):
        result = self.old_transport(10307)
        self.assertIsNone(getattr(result, 'publication_evidence', None))
        self.assertIsNone(result.format_evidence.physical_format)
        self.assertEqual(result.metadata.issue_count, 1)
        volume = self.add_result(result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.ONE_SHOT)
        self.assertEqual(volume.get_issues()[0].title, None)

    def test_real_one_shot_without_heuristic_is_tpb(self):
        result = self.old_transport(1530)
        self.assertEqual(snapshot(1530)['series']['series_type']['name'], 'One-Shot')
        volume = self.add_result(result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)
        self.assertEqual(_determine_format(volume.get_data(), 1.0)[0], self.settings.file_naming_special_version)

    def test_real_sole_omnibus_without_title_marker_is_tpb(self):
        result = self.old_transport(17494)
        self.assertIsNone(getattr(result, 'publication_evidence', None))
        self.assertEqual(snapshot(17494)['series']['series_type']['name'], 'Omnibus')
        self.assertEqual(result.metadata.issue_count, 1)
        volume = self.add_result(result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)
        self.assertEqual(volume.get_issues()[0].title, 'The Complete Cartoon Epic In One Volume')

    def test_real_multi_book_omnibus_is_vai(self):
        result = self.old_transport(14110)
        self.assertEqual(result.metadata.issue_count, 3)
        volume = self.add_result(result)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.VOLUME_AS_ISSUE)
        self.assertEqual([i.issue_number for i in volume.get_issues()], ['1', '2', '3'])
        self.assertEqual(_determine_format(volume.get_data(), 2.0)[0], self.settings.file_naming_vai)

    def test_existing_whole_item_matching_contract(self):
        for mode in (SpecialVersion.ONE_SHOT, SpecialVersion.OMNIBUS):
            self.assertTrue(match_special_version(mode, SpecialVersion.NORMAL, 'Book', 1.0))
            self.assertFalse(match_special_version(mode, SpecialVersion.NORMAL, 'Book', 2.0))
            self.assertTrue(match_special_version(mode, SpecialVersion.TPB, 'Book'))
