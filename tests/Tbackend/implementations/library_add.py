"""Characterize real Library.add persistence and downstream behavior offline."""

import sqlite3
import unittest
from pathlib import Path

from aiohttp import ClientError
from fixtures.comicvine_fetch import (COVER, LibraryAddHarness, envelope,
                                      issue_response, issue_result)
from fixtures.comicvine_search import volume_response

from backend.base.custom_exceptions import (InvalidKeyValue,
                                            MetadataSourceRateLimitReached,
                                            VolumeAlreadyAdded,
                                            VolumeNotMatched)
from backend.base.definitions import MonitorScheme, SpecialVersion
from backend.implementations.volumes import Volume


class LibraryAdd(LibraryAddHarness, unittest.TestCase):
    def test_multi_issue_persistence_exact_rows_and_generated_folder(self):
        self.prepare_fetch(
            issues=[
                issue_response(
                    id=302,
                    issue_number='2'),
                issue_response()])
        identity = self.add_volume()
        expected_folder = self.root / 'Example Hero' / 'Volume 02 (2021)'
        self.assertEqual(
            self.rows('volumes'),
            [{'id': identity, 'comicvine_id': 2127, 'title': 'Example Hero',
              'alt_title': 'Alternate Hero', 'year': 2021,
              'publisher': 'Example Publisher', 'volume_number': 2,
              'description': '<p>An example series.</p>',
              'site_url': 'https://example.invalid/volume/4050-2127/',
              'monitored': True, 'monitor_new_issues': True, 'root_folder': 1,
              'folder': str(expected_folder),
              'custom_folder': False, 'last_cv_fetch': 1234567890,
              'special_version': None, 'special_version_locked': False,
              'metadata_provider': 'comicvine', 'authority_generation': 0}])
        self.assertEqual(self.rows('issues'), [
            dict(issue_result(comicvine_id=302, issue_number='2', calculated_issue_number=2.0),
                 id=1, volume_id=identity, monitored=True),
            dict(issue_result(), id=2, volume_id=identity, monitored=True)
        ])
        self.assertEqual(
            self.db.execute('SELECT volume_id, cover FROM volumes_covers').fetchall(), [
                (identity, COVER)])
        self.assertEqual(
            [i.comicvine_id for i in Volume(identity).get_issues()],
            [301, 302])
        self.assertTrue(expected_folder.is_dir())
        self.scan.assert_called_once_with(identity)
        self.process.assert_called_once_with(identity)
        self.assertEqual(self.db.execute(
            'PRAGMA integrity_check').fetchone(), ('ok',))

    def test_old_single_issue_becomes_tpb_without_title_rewrite(self):
        self.prepare_fetch(
            volume_response(
                count_of_issues=1), [
                issue_response(
                    name='TPB')])
        identity = self.add_volume()
        self.assertEqual(
            Volume(identity).get_data().special_version,
            SpecialVersion.TPB)
        self.assertEqual(self.rows('issues')[0]['title'], 'TPB')
        self.assertEqual(self.rows('volumes')[0]['title'], 'Example Hero')

    def test_single_issue_hardcover_detection(self):
        self.prepare_fetch(issues=[issue_response(name='HC')])
        self.assertEqual(Volume(self.add_volume()).get_data().special_version,
                         SpecialVersion.HARD_COVER)

    def test_custom_folder_and_locked_special_version(self):
        identity = self.add_volume(
            volume_folder='chosen',
            special_version=SpecialVersion.NORMAL)
        data = Volume(identity).get_data()
        self.assertEqual(data.folder, str(self.root / 'chosen'))
        self.assertTrue(data.custom_folder)
        self.assertTrue(data.special_version_locked)
        self.assertEqual(data.special_version, SpecialVersion.NORMAL)

    def test_no_empty_folder_setting_preserves_path_without_creating_directory(
            self):
        self.settings.create_empty_volume_folders = False
        identity = self.add_volume()
        self.assertFalse(Path(Volume(identity).get_data().folder).exists())
        self.scan.assert_not_called()
        self.process.assert_called_once_with(identity)

    def test_monitoring_policy_still_applies(self):
        identity = self.add_volume(
            monitor_scheme=MonitorScheme.NONE,
            monitor_new_issues=False)
        self.assertFalse(Volume(identity).get_data().monitor_new_issues)
        self.assertFalse(self.rows('issues')[0]['monitored'])

    def test_translated_volume_is_added_and_first_alias_persisted(self):
        description = '<p>French translation of Example Hero.</p>'
        self.prepare_fetch(volume_response(description=description))
        self.add_volume()
        self.assertEqual(self.rows('volumes')[0]['description'], description)
        self.assertEqual(self.rows('volumes')[0]['alt_title'], 'Alternate Hero')

    def test_null_optional_metadata_persists(self):
        self.prepare_fetch(
            volume_response(
                aliases=None, publisher=None, start_year=None, description=None), [
                issue_response(
                    name=None, description=None, cover_date=None)])
        self.session.get_content.return_value = None
        self.add_volume(special_version=SpecialVersion.NORMAL)
        volume, issue = self.rows('volumes')[0], self.rows('issues')[0]
        for key in ('alt_title', 'publisher', 'year', 'description'):
            self.assertIsNone(volume[key])
        for key in ('title', 'date', 'description'):
            self.assertIsNone(issue[key])
        self.assertIsNone(self.db.execute(
            'SELECT cover FROM volumes_covers').fetchone()[0])

    def test_duplicate_rejected_before_fetch_without_mutating_rows(self):
        self.add_volume()
        before = self.rows('volumes'), self.rows('issues')
        self.session.get.reset_mock()
        with self.assertRaises(VolumeAlreadyAdded):
            self.add_volume()
        self.session.get.assert_not_awaited()
        self.assertEqual((self.rows('volumes'), self.rows('issues')), before)

    def test_volume_fetch_errors_do_not_mutate_database_or_folders(self):
        for status, exception in (
            (101, VolumeNotMatched),
            (100, InvalidKeyValue),
                (107, MetadataSourceRateLimitReached)):
            with self.subTest(status=status):
                self.respond(None, status)
                with self.assertRaises(exception):
                    self.add_volume()
                self.assert_empty_library()

    def test_network_failure_does_not_mutate_database(self):
        self.session.get.side_effect = ClientError('offline')
        with self.assertRaises(MetadataSourceRateLimitReached):
            self.add_volume()
        self.assert_empty_library()

    def test_issue_key_failure_does_not_mutate_database(self):
        self.response.json.side_effect = [
            envelope(
                volume_response()), envelope(
                [], 100)]
        with self.assertRaises(InvalidKeyValue):
            self.add_volume()
        self.assert_empty_library()

    def test_issue_rate_limit_still_adds_volume_with_zero_issues(self):
        self.response.json.side_effect = [
            envelope(
                volume_response()), envelope(
                [], 107)]
        self.add_volume()
        self.assertEqual(len(self.rows('volumes')), 1)
        self.assertEqual(self.rows('issues'), [])

    def test_database_error_rolls_back_volume_and_issues(self):
        self.prepare_fetch(issues=[issue_response(), issue_response()])
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_volume()
        self.assert_empty_library()
