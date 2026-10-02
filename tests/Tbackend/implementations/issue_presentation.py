"""Narrow presentation policy; real source evidence and explicit synthetics."""

from dataclasses import asdict
from unittest import TestCase

from fixtures.collected_titles import record

from backend.base.definitions import DateType, SpecialVersion
from backend.implementations.issue_presentation import issue_display_title
from backend.implementations.metadata.metron import MetronMetadataProvider


class IssuePresentation(TestCase):
    def resolve(self, title, special=SpecialVersion.TPB, count=1, parent='Full: Parent'):
        return issue_display_title(title, parent_title=parent,
                                   special_version=special, issue_count=count)

    def test_confirmed_cyberpunk_tpb(self):
        parent = record('cyberpunk-cv-collected-volume')['name']
        raw = record('cyberpunk-cv-collected-issue')['name']
        result = self.resolve(raw, parent=parent)
        self.assertEqual(asdict(result), {'value': parent, 'source': 'parent_volume_context', 'scope': 'issue'})
        self.assertEqual(raw, 'TPB')

    def test_confirmed_hc_details_and_rai_enumeration(self):
        for label in ('halloween-hc', 'overture-deluxe', 'rai'):
            volume = record(label + '-cv-volume')
            raw = volume['issues'][0]['name']
            self.assertEqual(raw, 'HC')
            result = self.resolve(raw, SpecialVersion.HARD_COVER, parent=volume['name'])
            self.assertEqual((result.value, result.source), (volume['name'], 'parent_volume_context'))

    def test_real_meaningful_cv_titles(self):
        for label in ('halloween-floppy', 'halloween-floppy2', 'saga-trade1',
                      'saga-trade2', 'gideon1', 'gideon2', 'batman-deluxe1'):
            title = record(label + '-cv-issue')['name']
            self.assertEqual(self.resolve(title).value, title)
            self.assertEqual(self.resolve(title).source, 'existing_issue_title')

    def test_metron_explicit_collection_titles(self):
        for label, parent in (('saga-trade1', '3892'), ('saga-trade2', '3892'), ('invincible1', '5527')):
            mapped = MetronMetadataProvider.issue_metadata(record(label + '-metron-issue'), parent, DateType.COVER_DATE)
            self.assertEqual(self.resolve(mapped.title).value, mapped.title)
            self.assertEqual(self.resolve(mapped.title).source, 'existing_issue_title')

    def test_metron_thirteen_stories_unchanged_without_heuristics(self):
        raw = record('halloween-hc-metron-issue')
        mapped = MetronMetadataProvider.issue_metadata(raw, '17376', DateType.COVER_DATE)
        self.assertEqual(len(raw['name']), 13)
        self.assertEqual(self.resolve(mapped.title).value, '; '.join(raw['name']))
        self.assertEqual(self.resolve(mapped.title).source, 'existing_issue_title')

    def test_real_adventure_null_remains_unknown(self):
        result = self.resolve(record('adventure1-cv-issue')['name'])
        self.assertIsNone(result.value)
        self.assertEqual(result.source, 'unknown')

    def test_synthetic_bare_numbers_not_generic(self):
        for title in ('1', '2', '01'):
            self.assertEqual(asdict(self.resolve(title)),
                             {'value': title, 'source': 'existing_issue_title', 'scope': 'issue'})

    def test_synthetic_ordinals_and_semicolons_unchanged(self):
        for title in ('Book One', 'Book 1', 'Book 4', 'Volume 1', 'Crime; Thanksgiving', '  Meaningful  '):
            self.assertEqual(self.resolve(title).value, title)

    def test_zero_or_multiple_issues_never_substitute(self):
        for count in (0, 2, 1000):
            self.assertEqual(self.resolve('TPB', count=count).value, 'TPB')
            self.assertEqual(self.resolve('HC', SpecialVersion.HARD_COVER, count).value, 'HC')

    def test_other_formats_never_substitute(self):
        for special in (SpecialVersion.NORMAL, SpecialVersion.ONE_SHOT,
                        SpecialVersion.OMNIBUS, SpecialVersion.VOLUME_AS_ISSUE):
            for title in ('TPB', 'HC'):
                self.assertEqual(self.resolve(title, special).source, 'existing_issue_title')

    def test_mismatch_never_substitutes(self):
        self.assertEqual(self.resolve('HC').value, 'HC')
        self.assertEqual(self.resolve('TPB', SpecialVersion.HARD_COVER).value, 'TPB')

    def test_only_tested_short_labels_case_and_surrounding_space(self):
        for title in ('tpb', ' TPB '):
            self.assertEqual(self.resolve(title).source, 'parent_volume_context')
        for title in ('hc', ' HC '):
            self.assertEqual(self.resolve(title, SpecialVersion.HARD_COVER).source, 'parent_volume_context')
        for title in ('Hardcover', 'Hard-Cover', 'Trade Paperback', 'TPB: A Story', 'HC Book 1'):
            self.assertEqual(self.resolve(title, SpecialVersion.HARD_COVER).value, title)

    def test_null_empty_or_missing_parent(self):
        for title in (None, '', ' '):
            self.assertEqual(self.resolve(title).source, 'unknown')
        for parent in ('', ' '):
            self.assertEqual(self.resolve('TPB', parent=parent).value, 'TPB')

    def test_no_punctuation_splitting(self):
        for parent in ('Batman: Detective Comics', 'Star Wars: Darth Vader', 'Cyberpunk 2077: You Have My Word'):
            self.assertEqual(self.resolve('TPB', parent=parent).value, parent)
