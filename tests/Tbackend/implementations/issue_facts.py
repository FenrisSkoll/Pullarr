"""Exact canonical facts, explicit abstention and historical DTO parity."""

import sqlite3
from dataclasses import FrozenInstanceError, asdict, replace
from decimal import Decimal, localcontext
from unittest import TestCase

from backend.base.issue_facts import (BibliographicDate, DateKind,
                                      DatePrecision, IssueFacts,
                                      IssueNumberFacts, IssueRecord,
                                      NumberKind, NumericRange, SemanticState,
                                      compare_numeric, decimal_text,
                                      in_numeric_range, match_candidates,
                                      presentation_key)
from backend.implementations.metadata.models import IssueMetadata
from backend.internals.db import DB_SCHEMA
from backend.internals.issue_facts import (load_records,
                                           mapped_facts, write_facts)
from backend.internals.issue_facts_schema import SCHEMA


def number(raw):
    return IssueNumberFacts.interpret(raw, 'fixture', 'number')


def record(i, raw, legacy=None):
    return IssueRecord(i, 1, (('fixture', str(i)),), IssueFacts(number(raw)), raw or '', legacy, None)


class CanonicalIssueFactsTests(TestCase):
    def test_number_taxonomy_and_raw_spelling(self):
        for raw, kind in [('1', NumberKind.NUMERIC), ('01', NumberKind.NUMERIC),
                          ('1.0', NumberKind.NUMERIC), ('1.5', NumberKind.NUMERIC),
                          ('0', NumberKind.NUMERIC), ('1A', NumberKind.SUFFIXED),
                          ('080.BEY', NumberKind.SUFFIXED), ('1.01', NumberKind.NUMERIC),
                          ('[nn]', NumberKind.UNNUMBERED), ('Annual', NumberKind.OPAQUE),
                          ('Special', NumberKind.OPAQUE), ('', NumberKind.OPAQUE),
                          (None, NumberKind.ABSENT)]:
            with self.subTest(raw=raw):
                self.assertEqual(number(raw).interpretation, kind)
                self.assertEqual(number(raw).raw_label, raw)

    def test_exact_decimal_is_not_context_rounded(self):
        raw = '123456789012345678901234567890.123456789'
        with localcontext() as ctx:
            ctx.prec = 3
            self.assertEqual(number(raw).numeric, Decimal(raw))
            self.assertEqual(decimal_text(Decimal(raw)), raw)
        self.assertEqual(number('0001.000').numeric_text, '1')

    def test_invalid_numeric_grammar_never_gains_arithmetic(self):
        for raw in ('NaN', 'Infinity', '1e3', '-1', '+1', ' 1', '1/2', '½', '１'):
            self.assertIsNone(number(raw).numeric)

    def test_value_immutable_and_consistent(self):
        with self.assertRaises(FrozenInstanceError):
            number('01').raw_label = '1'
        with self.assertRaises(ValueError):
            replace(number('1A'), numeric_text='1.01')
        with self.assertRaises(ValueError):
            replace(number('1'), policy='future')

    def test_numeric_candidate_ambiguity_and_exact_precedence(self):
        rows = tuple(record(i, raw) for i, raw in enumerate(('1', '01', '1.0'), 1))
        self.assertEqual(match_candidates('1.00', rows).state, SemanticState.AMBIGUOUS)
        self.assertEqual(match_candidates('1.00', rows).issue_ids, (1, 2, 3))
        self.assertEqual(match_candidates('01', rows).issue_ids, (2,))
        self.assertEqual(match_candidates('01', rows, exact_first=False).state, SemanticState.AMBIGUOUS)

    def test_suffix_and_decimal_do_not_match(self):
        rows = (record(1, '1A'), record(2, '1.01'))
        self.assertEqual(match_candidates('1A', rows).issue_ids, (1,))
        self.assertEqual(match_candidates('1.010', rows).issue_ids, (2,))
        self.assertEqual(compare_numeric(number('1A'), number('1.01')).state, SemanticState.UNSUPPORTED)

    def test_unnumbered_is_not_local_identity(self):
        for raw in ('[nn]', '', 'unnumbered'):
            self.assertEqual(match_candidates(raw, (record(1, raw),)).state, SemanticState.UNSUPPORTED)

    def test_exact_duplicate_labels_are_ambiguous(self):
        self.assertEqual(match_candidates('Annual', (record(2, 'Annual'), record(1, 'Annual'))).issue_ids, (1, 2))
        self.assertEqual(match_candidates('Annual', (record(2, 'Annual'), record(1, 'Annual'))).state, SemanticState.AMBIGUOUS)

    def test_range_context_and_explicit_unsupported(self):
        interval = NumericRange(1, Decimal('1.5'), Decimal('3'))
        self.assertEqual(in_numeric_range(number('2'), 1, interval).value, 1)
        self.assertEqual(in_numeric_range(number('1'), 1, interval).value, 0)
        for raw in ('1A', '[nn]', 'Annual'):
            self.assertEqual(in_numeric_range(number(raw), 1, interval).state, SemanticState.UNSUPPORTED)
        self.assertEqual(in_numeric_range(number('2'), 2, interval).state, SemanticState.UNSUPPORTED)
        with self.assertRaises(ValueError):
            NumericRange(1, Decimal('3'), Decimal('1'))

    def test_noncontiguous_set_is_not_endpoint_coverage(self):
        rows = (record(1, '1'), record(3, '3'), record(5, '5'))
        self.assertEqual(match_candidates('2', rows).issue_ids, ())

    def test_date_precision_and_truthful_display(self):
        for raw, precision, display in [('2021-12-31', DatePrecision.DAY, '2021-12-31'),
                                        ('2021-12-00', DatePrecision.MONTH, '2021-12'),
                                        ('2021-00-00', DatePrecision.YEAR, '2021'),
                                        (None, DatePrecision.UNKNOWN, None),
                                        ('Winter 2021', DatePrecision.UNSUPPORTED_TEXT, 'Winter 2021')]:
            with self.subTest(raw=raw):
                value = BibliographicDate.interpret(raw, DateKind.COVER, 'fixture', 'date', zero_placeholders=True)
                self.assertEqual(value.precision, precision)
                self.assertEqual(value.display, display)
                self.assertEqual(value.exact_day is not None, precision == DatePrecision.DAY)

    def test_zero_date_convention_must_be_explicit(self):
        value = BibliographicDate.interpret('2021-12-00', DateKind.COVER, 'fixture', 'date')
        self.assertEqual(value.precision, DatePrecision.UNSUPPORTED_TEXT)

    def test_invalid_dates_and_uncertainty_abstain(self):
        for raw in ('2021-02-29', '2021-13-00', '0000-00-00', '2021-00-01'):
            self.assertEqual(BibliographicDate.interpret(raw, DateKind.COVER, 'fixture', 'date',
                zero_placeholders=True).precision, DatePrecision.UNSUPPORTED_TEXT)
        self.assertIsNone(BibliographicDate.interpret('2020-02-29', DateKind.COVER, 'fixture', 'date',
            uncertainty='source uncertain').exact_day)

    def test_multiple_dates_and_selected_projection(self):
        cover = BibliographicDate.interpret('2021-12-00', DateKind.COVER, 'fixture', 'cover', zero_placeholders=True)
        sale = BibliographicDate.interpret('2021-11-10', DateKind.ON_SALE, 'fixture', 'sale')
        facts = IssueFacts(number('01'), (cover, sale), 'cover')
        self.assertIsNone(facts.operational_date.exact_day)
        self.assertEqual(len(facts.dates), 2)
        self.assertEqual(replace(facts, selected_date_field='sale').operational_date.exact_day.isoformat(), '2021-11-10')
        with self.assertRaises(ValueError):
            replace(facts, selected_date_field='not_present')

    def test_ordering_does_not_establish_equality_or_range(self):
        rows = (record(2, 'Special'), record(1, 'Annual'))
        self.assertEqual(sorted(rows, key=presentation_key), sorted(reversed(rows), key=presentation_key))
        self.assertEqual(compare_numeric(rows[0].facts.number, rows[1].facts.number).state, SemanticState.UNSUPPORTED)

    def test_legacy_order_and_dto_golden_unchanged(self):
        rows = (replace(record(1, '1A', 1.01), legacy_date='2022-01-01'),
                record(2, '1.01', 1.01), replace(record(3, '2', 2.0), legacy_date='2021-01-01'))
        self.assertEqual([r.id for r in sorted(rows, key=presentation_key)], [2, 3, 1])
        for provider in ('comicvine', 'metron'):
            self.assertEqual(asdict(IssueMetadata(provider, '10', '1', '01', 1.0, None, '2021-12-25', None)),
                dict(provider=provider, provider_id='10', volume_provider_id='1', issue_number='01',
                     calculated_issue_number=1.0, title=None, date='2021-12-25', description=None))


class CanonicalIssueStoreTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(DB_SCHEMA + SCHEMA)
        self.db.execute("INSERT INTO root_folders VALUES(1,'/library')")
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder) VALUES(1,101,'Series',1)")
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(1,1,201,'01',1)")

    def test_missing_facts_is_legacy_mode_without_write(self):
        before = self.db.total_changes
        self.assertIsNone(load_records(self.db.cursor(), volume_id=1)[0].facts)
        self.assertEqual(self.db.total_changes, before)

    def test_roundtrip_retains_provenance_and_identity(self):
        facts = mapped_facts('01', '2021-12-25', 'legacy_mapped')
        write_facts(self.db.cursor(), 1, facts)
        loaded = load_records(self.db.cursor(), issue_ids=(1,))[0]
        self.assertEqual(loaded.facts, facts)
        self.assertEqual(loaded.provider_identities, (('comicvine', '201'),))
        self.assertEqual(loaded.legacy_number, 1.0)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_loader_three_reads_not_per_issue(self):
        from time import perf_counter

        from backend.base.issue_facts import NumberCatalog
        previous, diagnostics = 1, []
        write_facts(self.db.cursor(), 1, mapped_facts('01', None, 'fixture'))
        for count in (100, 1000, 10000):
            self.db.executemany('''INSERT INTO issues(id,volume_id,issue_number,calculated_issue_number)
                VALUES(?,1,?,?)''', ((i, str(i), i) for i in range(previous + 1, count + 1)))
            for i in range(previous + 1, count + 1):
                write_facts(self.db.cursor(), i, mapped_facts(str(i), None, 'fixture'))
            queries = []
            self.db.set_trace_callback(queries.append)
            started = perf_counter()
            records = load_records(self.db.cursor(), volume_id=1)
            catalog = NumberCatalog.build((r.id, r.legacy_label, r.facts.number) for r in records)
            self.assertEqual(catalog.match(str(count)).issue_ids, (count,))
            elapsed = perf_counter() - started
            self.db.set_trace_callback(None)
            self.assertEqual(len(records), count)
            reads = sum(q.lstrip().upper().startswith('SELECT') for q in queries)
            self.assertEqual(reads, 3)
            diagnostics.append((count, reads, round(elapsed, 5)))
            previous = count
        print('Canonical issue facts rows/SELECTs/load+index seconds:', diagnostics)

    def test_legacy_update_invalidates_not_silently_reinterprets(self):
        write_facts(self.db.cursor(), 1, mapped_facts('01', '2021-12-25', 'legacy_mapped'))
        self.db.execute("UPDATE issues SET issue_number='2' WHERE id=1")
        self.assertIsNone(load_records(self.db.cursor(), volume_id=1)[0].facts)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issue_date_facts').fetchone()[0], 0)

    def test_id_batches_share_one_read_snapshot(self):
        self.db.executemany('''INSERT INTO issues(id,volume_id,issue_number,calculated_issue_number)
            VALUES(?,1,?,?)''', ((i, str(i), i) for i in range(2, 1001)))
        queries = []
        self.db.set_trace_callback(queries.append)
        records = load_records(self.db.cursor(), issue_ids=tuple(range(1, 1001)))
        self.db.set_trace_callback(None)
        self.assertEqual(len(records), 1000)
        self.assertEqual(sum(q.startswith('SELECT') for q in queries), 6)
        self.assertEqual(queries[0], 'SAVEPOINT issue_facts_batch_read')
        self.assertEqual(queries[-1], 'RELEASE issue_facts_batch_read')

    def test_variant_relationship_never_creates_identity_alias(self):
        self.db.execute("INSERT INTO issue_variant_of VALUES(1,'gcd','base-100','fixture')")
        loaded = load_records(self.db.cursor(), volume_id=1)[0]
        self.assertEqual(loaded.variant_of.provider_id, 'base-100')
        self.assertEqual(loaded.provider_identities, (('comicvine', '201'),))
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_rich_only_record_has_no_legacy_dto_projection(self):
        from backend.base.definitions import IssueData, UnsupportedLegacyIssue
        self.db.execute("UPDATE issues SET calculated_issue_number=NULL,issue_number='[nn]' WHERE id=1")
        write_facts(self.db.cursor(), 1, mapped_facts('[nn]', None, 'fixture'))
        value = load_records(self.db.cursor(), volume_id=1)[0]
        self.assertIsNone(value.legacy_number)
        self.assertEqual(value.facts.number.interpretation, NumberKind.UNNUMBERED)
        with self.assertRaises(UnsupportedLegacyIssue):
            IssueData(1, 1, 201, '[nn]', None, None, None, '', True, [])


class CanonicalConsumerTests(TestCase):
    def test_metron_dates_coexist_without_changing_metadata_dto(self):
        from fixtures.metron import SERIES, issue

        from backend.base.definitions import DateType
        from backend.implementations.metadata.metron import \
            MetronMetadataProvider
        for raw in ('1', '01', '1.0', '1.5', '0', '1A', '1.01', '[nn]', 'Annual', 'Special'):
            row = issue(number=raw)
            old = MetronMetadataProvider.issue_metadata(row, '700', DateType.COVER_DATE)
            result = MetronMetadataProvider.volume_result(SERIES, [row], DateType.COVER_DATE)
            self.assertEqual(asdict(result.metadata.issues[0]), asdict(old))
            facts = result.issue_facts[0].facts
            self.assertEqual(facts.number.raw_label, raw)
            self.assertEqual(facts.number.provenance, 'metron_field')
            self.assertEqual([d.kind for d in facts.dates], [DateKind.COVER, DateKind.ON_SALE])
            self.assertEqual(facts.operational_date.raw_value, old.date)

    def test_comicvine_companion_is_honestly_mapped_and_not_serialized(self):
        from asyncio import run
        from unittest.mock import AsyncMock, patch

        from backend.implementations.metadata.comicvine import \
            ComicVineMetadataProvider
        from backend.implementations.metadata.models import VolumeMetadata
        metadata = VolumeMetadata('comicvine', '101', 'Series', 2021, 1, None, None,
            None, None, [], None, 1, False,
            [IssueMetadata('comicvine', '201', '101', '01', 1.0, None, '2021-12-25', None)])
        before = asdict(metadata)
        with patch.object(ComicVineMetadataProvider, 'fetch_volume', new=AsyncMock(return_value=metadata)):
            result = run(ComicVineMetadataProvider().fetch_volume_enriched('101'))
        self.assertEqual(asdict(result.metadata), before)
        self.assertEqual(result.issue_facts[0].facts.number.provenance, 'comicvine_mapped')
        self.assertEqual(result.issue_facts[0].facts.dates[0].kind, DateKind.LEGACY_SELECTED)

    def test_rich_provider_ordinal_is_only_explicit_presentation(self):
        a, b = record(1, '1'), record(2, '2')
        a = replace(a, facts=replace(a.facts, provider_ordinal=2, ordinal_provenance='fixture'))
        b = replace(b, facts=replace(b.facts, provider_ordinal=1, ordinal_provenance='fixture'))
        self.assertEqual([r.id for r in sorted((a, b), key=lambda r: presentation_key(r, legacy_order=False))], [2, 1])
        self.assertEqual(match_candidates('1', (a, b)).issue_ids, (1,))

    def test_rich_decimal_identification_and_ambiguity(self):
        from backend.base.identification import MatchReason
        from backend.implementations.identification import (MatchingSnapshot,
                                                            _coverage)
        from tests.Tbackend.features.identification import (candidate, comic,
                                                            issue, volume)
        parent = volume()
        rows = (issue(1, '1.1', None, number_facts=number('1.1')),)
        snapshot = MatchingSnapshot.build((parent,), rows)
        local = comic(candidate(), '<Series>Batman</Series><Number>1.10</Number>')
        self.assertEqual(_coverage(local, parent, snapshot), ((1,), MatchReason.ISSUE_NUMERIC))
        snapshot = MatchingSnapshot.build((parent,), rows + (issue(2, '01.1', None, number_facts=number('01.1')),))
        self.assertEqual(_coverage(local, parent, snapshot), ((), MatchReason.ISSUE_AMBIGUOUS))

    def test_rich_range_never_acquires_suffix_member(self):
        from backend.implementations.identification import (MatchingSnapshot,
                                                            _coverage)
        from tests.Tbackend.features.identification import (candidate,
                                                            issue, volume)
        parent = volume()
        rows = tuple(issue(i, raw, None, number_facts=number(raw))
                     for i, raw in enumerate(('1', '1A', '2'), 1))
        snapshot = MatchingSnapshot.build((parent,), rows)
        ids, _ = _coverage(candidate(number=(1.0, 2.0), stem='Batman 1-2 (2020)'), parent, snapshot)
        self.assertEqual(ids, (1, 3))

    def test_scoring_shadow_canonical_facts_preserve_receipt(self):
        from backend.implementations.release_scoring import (
            evaluate_release, preview_evaluation)
        from tests.Tbackend.features.release_scoring import candidate, target
        old = target()
        rich = replace(old, catalog=tuple(replace(i, number_facts=number(i.raw_number)) for i in old.catalog))
        self.assertEqual(preview_evaluation(evaluate_release(old, candidate())),
                         preview_evaluation(evaluate_release(rich, candidate())))

    def test_partial_date_cannot_set_file_timestamp(self):
        from unittest.mock import patch

        from backend.base.files import set_file_date
        for raw in ('2021-12-00', '2021-00-00', 'unknown'):
            with patch('backend.base.files.utime', side_effect=AssertionError), \
                    patch('backend.base.files.__set_windows_times', side_effect=AssertionError):
                set_file_date('/never-accessed', raw)

    def test_gcd_offline_partial_and_variant_identity(self):
        from backend.implementations.metadata.gcd_issue_facts import \
            canonical_issue
        from backend.implementations.metadata.gcd_staging import (
            GcdIssueNumber, GcdIssueSnapshot,
            GcdPartialDate, GcdVariantRelation, admit_issue)
        snapshot = GcdIssueSnapshot('101', '10', GcdIssueNumber('[nn]'), None,
            GcdPartialDate('2021-12-00'), GcdPartialDate('2021-00-00'),
            GcdVariantRelation('101', '100', 'fixture'))
        result = canonical_issue(snapshot, 'key_date')
        self.assertEqual(result.provider_id, '101')
        self.assertEqual(result.variant_of.provider_id, '100')
        self.assertIsNone(result.facts.number.numeric)
        self.assertEqual(result.facts.operational_date.precision, DatePrecision.MONTH)
        self.assertFalse(admit_issue(snapshot, 'key_date').neutral_admissible)
        self.assertFalse(admit_issue(snapshot, 'key_date').destructive_complete)
