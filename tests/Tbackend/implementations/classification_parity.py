"""Independent pre-extraction oracle versus the production compatibility entry."""

from datetime import datetime
from itertools import product
from random import Random
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fixtures import classifier_legacy as old

from backend.base.definitions import SpecialVersion as SV
from backend.implementations import volumes
from backend.implementations.metadata.format_evidence import (
    PhysicalFormat, ProviderFormatEvidence)
from backend.implementations.metadata.publication_evidence import (
    ProviderPublicationEvidence, PublicationKind)

NOW = datetime(2026, 1, 31)
PHYSICAL = (None,) + tuple(ProviderFormatEvidence('metron', '1', 'series_type.name', raw, kind)
                          for raw, kind in (('Hardcover', PhysicalFormat.HARDCOVER),
                                            ('Trade Paperback', PhysicalFormat.TRADE_PAPERBACK),
                                            ('Graphic Novel', None)))
PUBLICATION = (None,) + tuple(ProviderPublicationEvidence('metron', '1', 'series_type.name', raw, kind)
                             for raw, kind in (('One-Shot', PublicationKind.ONE_SHOT),
                                               ('Omnibus', PublicationKind.OMNIBUS),
                                               ('Single Issue', None)))


def case(title='Unmarked', issue_title=None, count=1, date=None, description=None,
         locked=False, stored=SV.NORMAL, physical=None, publication=None, now=NOW):
    data = SimpleNamespace(title=title, description=description,
                           special_version_locked=locked, special_version=stored)
    issues = [SimpleNamespace(title=issue_title, date=date) for _ in range(count)]
    return data, issues, physical, publication, now


def branch_cases():
    yield case(count=0)
    yield case(count=2)
    for title in (None, 'Volume 1', 'Vol. Two', 'V 3: Collected', 'Volume 1 subtitle',
                  'Vol.Two', 'Book One', 'TPB', 'HC', 'hard-cover', 'hard cover',
                  'omnibus', 'OS', 'one-shot', 'one shot', 'HC\t'):
        yield case(issue_title=title)
    for title in ('Omnibus', 'One-Shot', 'Hardcover', 'Annual', 'Semiannual',
                  'Omnibus One-Shot Hardcover Annual', 'preceding hardcover',
                  'One Shot Collection'):
        yield case(title=title, date='2000-01-01')
    for description in ('An omnibus.', 'A one-shot.', 'A hardcover.', 'An annual.',
                        'Semiannual.', 'Nothing. A hardcover.',
                        '<a href="public">Hardcover</a> here.', 'vs. hardcover',
                        'r.i.p. omnibus', 'An omnibus annual.'):
        yield case(description=description, date='2000-01-01')
    for date in (None, '', '2026-01-31', '2026-01-01', '2025-12-31', 'invalid'):
        yield case(date=date)
        yield case(date=date, now=NOW.replace(microsecond=1))
        yield case(date=date, title='Omnibus')  # early exit must not parse bad date
    for physical, publication, count, locked in product(PHYSICAL, PUBLICATION, (0, 1, 2), (False, True)):
        yield case(physical=physical, publication=publication, count=count,
                   locked=locked, stored=SV.HARD_COVER, date='2000-01-01')
        yield case(physical=physical, publication=publication, count=count,
                   locked=locked, stored=SV.NORMAL, issue_title='Volume 1')


def generated_cases():
    random = Random(20260925)
    for _ in range(2048):
        yield case(
            title=random.choice(('Unmarked', 'Omnibus', 'One-Shot', 'Hardcover', 'Annual', 'TPB')),
            issue_title=random.choice((None, 'Volume 1', 'Vol. Two', 'Volume 1 subtitle',
                                       'HC', 'OS', 'Omnibus', 'TPB', '1', 'Story')),
            count=random.choice((0, 1, 2, 3)), date=random.choice((None, '2000-01-01',
                                                                            '2026-01-01', '2026-01-31', 'bad')),
            description=random.choice((None, 'An omnibus.', 'A hardcover.', 'Annual.',
                                       'A one-shot.', 'Normal. Omnibus.')),
            locked=random.choice((False, True)), stored=random.choice(tuple(SV)),
            physical=random.choice(PHYSICAL), publication=random.choice(PUBLICATION),
            now=random.choice((NOW, NOW.replace(microsecond=1))))


class FrozenClassifierParity(TestCase):
    def compare(self, cases):
        with patch.object(old, 'Volume') as old_volume, patch.object(volumes, 'Volume') as new_volume, \
                patch.object(old, 'datetime', wraps=datetime) as old_clock, \
                patch.object(volumes, 'datetime', wraps=datetime) as new_clock:
            for index, (data, issues, physical, publication, now) in enumerate(cases):
                with self.subTest(index=index, data=data, issues=issues, physical=physical, publication=publication, now=now):
                    for loader in (old_volume, new_volume):
                        loader.return_value.get_data.return_value = data
                        loader.return_value.get_issues.return_value = issues
                    old_clock.now.return_value = new_clock.now.return_value = now
                    try:
                        expected = old.determine_special_version(1, physical, publication)
                    except ValueError as error:
                        with self.assertRaises(ValueError) as actual:
                            volumes.determine_special_version(1, physical, publication)
                        self.assertEqual(str(actual.exception), str(error))
                    else:
                        self.assertEqual(volumes.determine_special_version(1, physical, publication), expected)

    def test_all_branch_families(self):
        self.compare(branch_cases())

    def test_bounded_generated_parity(self):
        self.compare(generated_cases())

    def test_strict_naive_local_boundary_and_no_tpb_marker(self):
        with patch.object(old, 'Volume') as volume, patch.object(old, 'datetime', wraps=datetime) as clock:
            data, issues, _, _, _ = case(issue_title='TPB', date='2026-01-01')
            volume.return_value.get_data.return_value = data
            volume.return_value.get_issues.return_value = issues
            clock.now.return_value = NOW
            self.assertEqual(old.determine_special_version(1), SV.NORMAL)
            clock.now.return_value = NOW.replace(microsecond=1)
            self.assertEqual(old.determine_special_version(1), SV.TPB)
