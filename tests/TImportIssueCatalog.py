"""Conservative provider-catalogue suggestions; no fabricated issue identities."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from backend.base.import_candidate import DiscoveryScope
from backend.features.import_issue_catalog import _suggest
from backend.implementations.import_candidates import observe_import_candidate


class IssueSuggestionTests(TestCase):
    def suggestion(self, filename, issues):
        with TemporaryDirectory() as temporary:
            path = Path(temporary)/filename
            path.write_bytes(b'local fixture')
            candidate = observe_import_candidate(str(path), DiscoveryScope('fixture', temporary))
            return _suggest(candidate, issues, 'comicvine', '2127')

    @staticmethod
    def issue(identifier, number, title):
        return dict(id=identifier, number=number, title=title, date=None)

    def test_decimal_subissues_remain_distinct(self):
        issues = [self.issue('a', '6.1', 'Part A'), self.issue('b', '6.2', 'Part B')]
        value = self.suggestion('Series #6.1.cbz', issues)
        self.assertEqual(value['issue_ids'], ['a'])
        self.assertEqual(value['confidence'], 'strong')

    def test_decimal_spelling_does_not_merge_provider_labels(self):
        issues = [self.issue('a', '6.1', 'First'), self.issue('b', '6.10', 'Tenth')]
        self.assertEqual(self.suggestion('Series #6.10.cbz', issues)['issue_ids'], ['b'])

    def test_volume_notation_requires_catalogue_semantics(self):
        generic = [self.issue('a', '1', 'The Beginning')]
        self.assertEqual(self.suggestion('Series v001.cbz', generic)['confidence'], 'review_needed')
        collected = [self.issue('a', '1', 'Book 1'), self.issue('b', '2', 'Book 2')]
        self.assertEqual(self.suggestion('Series v001.cbz', collected)['issue_ids'], ['a'])

    def test_book_title_is_evidence_only_in_selected_catalogue(self):
        collected = [self.issue('a', '1', 'Book 1')]
        singles = [self.issue('b', '1', 'Opening Night')]
        self.assertEqual(self.suggestion('Series Book 1.cbz', collected)['issue_ids'], ['a'])
        self.assertEqual(self.suggestion('Series Book 1.cbz', singles)['confidence'], 'review_needed')

    def test_tpb_is_not_automatically_every_issue(self):
        singles = [self.issue('a', '1', 'Opening'), self.issue('b', '2', 'Closing')]
        self.assertEqual(self.suggestion('Series TPB.cbz', singles)['issue_ids'], [])
