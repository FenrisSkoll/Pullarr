"""Rich observations are neither stable story identity nor classification."""

from dataclasses import FrozenInstanceError, replace
from unittest import TestCase

from backend.base.bibliography import MAX_TEXT, CreditObservation, StoryMode
from backend.implementations.metadata.gcd_bibliography import (bibliography,
                                                               isbn, numeric,
                                                               publication)
from backend.implementations.metadata.gcd_client import GcdError


def story(sequence=1, **values):
    return dict(type='comic story', title='Beginning', feature='Hero', sequence_number=sequence,
                page_count='12.50', script='Alice', pencils='Bob', inks='Carol', colors='D',
                letters='E', editing='F', characters='Hero; Friend', genre='adventure', **values)


class BibliographyTests(TestCase):
    def test_isbn_raw_checksum_no_correction(self):
        for raw, normalized, validity in (
            ('0-306-40615-2', '0306406152', 'valid'),
            ('978-0-306-40615-7', '9780306406157', 'valid'),
            ('9780306406158', None, 'invalid'), ('not an ISBN', None, 'invalid'),
            ('', None, 'unknown'), (None, None, 'unknown')):
            with self.subTest(raw=raw):
                self.assertEqual(isbn(raw), (normalized, validity))
                item = bibliography(dict(isbn=raw, story_set=[])).edition
                self.assertEqual(item.isbn, raw)

    def test_exact_page_counts_and_literal_barcodes(self):
        for raw, expected in (('32', '32'), ('0', '0'), ('000', '0'), ('10.00', '10'),
                              ('0.50', '0.5'), ('?', None), ('-2', None), (None, None)):
            self.assertEqual(numeric(raw), expected)
        for barcode in ('012345', 'abc-123', '', None):
            self.assertEqual(bibliography(dict(barcode=barcode, story_set=[])).edition.barcode, barcode)

    def test_modes_immutable_and_no_fake_identity(self):
        value = bibliography(dict(story_set=[story()]))
        row = value.stories[0]
        self.assertEqual(row.mode, StoryMode.OBSERVATION)
        self.assertIsNone(row.provider_story_id)
        with self.assertRaises(FrozenInstanceError):
            value.provider = 'other'
        with self.assertRaises(ValueError):
            replace(row, provider_story_id='invented')
        with self.assertRaises(ValueError):
            replace(row, mode=StoryMode.IDENTIFIED)
        self.assertEqual(replace(row, mode=StoryMode.IDENTIFIED, provider_story_id='42').provider_story_id, '42')
        with self.assertRaises(ValueError):
            CreditObservation('creator', 'Alice')
        for change in (dict(credits=[]), dict(page_count_numeric='NaN'), dict(sequence='Infinity')):
            with self.assertRaises(ValueError):
                replace(row, **change)

    def test_order_duplicates_missing_and_role_separation(self):
        rows = [story(3), story(1), story(1), story(None)]
        rows[0]['type'] = 'advertisement'
        rows[2]['title'] = ''
        result = bibliography(dict(story_set=rows))
        self.assertEqual([s.source_position for s in result.stories], [1, 2, 0, 3])
        self.assertIn('story[2].sequence_number:duplicate', result.diagnostics)
        self.assertEqual(len(result.stories[0].credits), 6)
        self.assertEqual(result.stories[0].page_count_numeric, '12.5')
        self.assertEqual(result.stories[1].title, '')
        self.assertEqual(result.stories[2].story_type, 'advertisement')

    def test_optional_rejection_and_critical_container(self):
        row = story()
        row.update(title=['bad'], synopsis='x' * (MAX_TEXT + 1), script='x' * (MAX_TEXT + 1))
        value = bibliography(dict(story_set=[row], isbn=['bad']))
        self.assertIsNone(value.stories[0].title)
        self.assertNotIn('isbn', value.edition.supplied)
        self.assertNotIn('script', [c.role for c in value.stories[0].credits])
        self.assertTrue(any('title:invalid' in code for code in value.diagnostics))
        for container in (None, {}, [None], [story()] * 1001):
            with self.assertRaises(GcdError):
                bibliography(dict(story_set=container))

    def test_bounds_and_literal_hostile_text(self):
        row = story()
        row['title'] = '<img src=x onerror=alert(1)>雪'
        self.assertEqual(bibliography(dict(story_set=[row])).stories[0].title, row['title'])
        row['title'] = '雪' * MAX_TEXT
        with self.assertRaisesRegex(ValueError, 'text limit'):
            bibliography(dict(story_set=[row] * 100))

    def test_cover_references_are_not_identity_or_fetch_authority(self):
        good = 'https://images.comics.org/img/gcd/covers_by_id/1/w400/123.jpg'
        self.assertEqual(bibliography(dict(story_set=[], cover=good)).edition.cover_reference, good)
        for bad in ('https://evil.invalid/image', 'javascript:alert(1)',
                    good + '?password=secret', 'https://user:secret@images.comics.org/img/gcd/covers_by_id/a'):
            result = bibliography(dict(story_set=[], cover=bad))
            self.assertIsNone(result.edition.cover_reference)
            self.assertNotIn('cover_reference', result.edition.supplied)
            self.assertIn('cover:unsafe_reference', result.diagnostics)

    def test_binding_observation_not_special_version(self):
        value = publication(dict(binding='Hardcover', publishing_format='Omnibus'))
        self.assertEqual(value.binding, 'Hardcover')
        self.assertFalse(hasattr(value, 'special_version'))
        self.assertIn('color:unavailable', value.diagnostics)
