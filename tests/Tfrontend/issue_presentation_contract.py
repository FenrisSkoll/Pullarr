"""Freeze stored/legacy resource titles before adding presentation fields."""

from copy import deepcopy
from unittest import TestCase

from fixtures.collected_titles import record
from fixtures.comicvine_fetch import LibraryAddHarness

from backend.implementations.naming import generate_issue_name
from backend.implementations.volumes import Library, Volume
from frontend.metadata import issue_identity_result, volume_identity_results


class PresentationHarness(LibraryAddHarness):
    def add_fixture(self):
        self.prepare_fetch(record('cyberpunk-cv-collected-volume'),
                           [record('cyberpunk-cv-collected-issue')])
        return Volume(Library.add(150046, 1, True))


class PresentationCompatibility(PresentationHarness, TestCase):
    def test_legacy_volume_is_exactly_the_original_public_dictionary(self):
        volume = self.add_fixture()
        raw = volume.get_public_data()
        before = deepcopy(raw)
        self.assertEqual(volume_identity_results([raw], False), [before])
        self.assertEqual(raw, before)
        self.assertEqual(raw['issues'][0]['title'], 'TPB')
        self.assertNotIn('display_title', raw['issues'][0])

    def test_legacy_issue_returns_original_data_without_new_fields(self):
        issue = self.add_fixture().get_issues()[0]
        before = issue.todict()
        self.assertIs(issue_identity_result(issue, False), issue)
        self.assertEqual(issue.todict(), before)
        self.assertNotIn('display_title', before)

    def test_metadata_reads_do_not_write_or_change_legacy_naming(self):
        volume = self.add_fixture()
        issue = volume.get_issues()[0]
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            self.assertEqual(issue_identity_result(issue, True)['title'], 'TPB')
            self.assertEqual(volume_identity_results([volume.get_public_data()], True)[0]['issues'][0]['title'], 'TPB')
        finally:
            self.db.set_trace_callback(None)
        self.assertTrue(all(s.lstrip().upper().startswith('SELECT') for s in statements))
        self.assertEqual(volume.get_issues()[0].title, 'TPB')
        self.assertEqual(generate_issue_name(volume.get_data(), 1.0),
                         'Cyberpunk 2077 - You Have My Word (2023) Volume 01 TPB')
