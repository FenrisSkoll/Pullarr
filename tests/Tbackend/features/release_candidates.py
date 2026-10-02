"""Offline acquisition contract, parser safety and real DDL adapter parity."""

import json
from asyncio import run
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from backend.base.file_extraction import extract_filename_data
from backend.base.release_candidate import (AcquisitionMechanism as Mechanism,
                                            AcquisitionReference, CoverageKind,
                                            LocatorKind, ObservationOrigin,
                                            PackKind, ReleaseCandidate,
                                            ReleaseCoverage,
                                            ReleaseDiagnosticCode as Code,
                                            ReleaseObservation, ReleaseSource,
                                            SourceKind, preview_release)
from backend.implementations.indexer_clients.ddl.GetComics import \
    GetComicsIndexer
from backend.implementations.release_candidates import (adapt_ddl_result,
                                                        adapt_ddl_results,
                                                        normalize_release,
                                                        parse_release_title,
                                                        resolver_key)


def ddl(title='Batman #001 (2016)', **overrides):
    return {
        **extract_filename_data(title, assume_volume_number=False, fix_year=True),
        'display_title': title, 'link': 'https://getcomics.example/batman/',
        'size': 12000000, 'indexer_id': 1, 'indexer_title': 'GetComics',
        **overrides
    }


class ReleaseCandidateTests(TestCase):
    def setUp(self):
        self.source = ReleaseSource(
            SourceKind.NEWZNAB,
            'indexer:opaque',
            'Example',
            'Prowlarr')
        self.reference = AcquisitionReference(
            Mechanism.NZB, LocatorKind.SOURCE_RECORD,
            resolver_key(self.source, 'opaque-guid'))

    def candidate(self, **kwargs):
        return normalize_release(
            self.source,
            'Batman #01 (2016)',
            self.reference,
            **kwargs)

    def test_basic(self):
        c = self.candidate(result_id='001-A', size=123)
        self.assertEqual(c.raw_title, 'Batman #01 (2016)')
        self.assertEqual(c.result_id, '001-A')
        self.assertEqual(c.size_bytes, 123)
        self.assertEqual(c.observations[0].coverage.labels, ('01',))

    def test_optional_unknown_not_false(self):
        c = normalize_release(self.source, 'Unparseable title', self.reference)
        self.assertIsNone(c.size_bytes)
        self.assertIsNone(c.published_at)
        self.assertIsNone(c.observations[0].language)
        self.assertEqual(c.observations[0].pack, PackKind.UNKNOWN)

    def test_source_namespaces(self):
        c = self.candidate(result_id='same')
        self.assertNotEqual(
            c.candidate_id, replace(
                c, source=replace(
                    self.source, key='other')).candidate_id)
        self.assertNotEqual(
            c.candidate_id,
            replace(
                c,
                source=replace(
                    self.source,
                    kind=SourceKind.TORZNAB)).candidate_id)
        self.assertNotEqual(
            c.candidate_id, replace(
                c, result_id='different').candidate_id)

    def test_identity_not_title_or_query(self):
        c = self.candidate(result_id='stable')
        self.assertEqual(
            c.candidate_id, replace(
                c, raw_title='Corrected title').candidate_id)
        self.assertNotEqual(c, replace(c, raw_title='Corrected title'))
        self.assertEqual(
            c.candidate_id,
            replace(
                c,
                acquisition=replace(
                    self.reference,
                    key='a' *
                    64)).candidate_id)

    def test_no_id_locator_fallback(self):
        c = self.candidate()
        self.assertEqual(
            c.candidate_id, replace(
                c, raw_title='Other display').candidate_id)
        self.assertNotEqual(
            c.candidate_id,
            replace(
                c,
                acquisition=replace(
                    self.reference,
                    key='a' *
                    64)).candidate_id)

    def test_title_only_not_unique_release_claim(self):
        ref = AcquisitionReference(Mechanism.OTHER, LocatorKind.UNAVAILABLE)
        c = normalize_release(self.source, 'Title only', ref)
        self.assertIsNone(c.candidate_id)
        self.assertIn(Code.LOCATOR_UNAVAILABLE, [d.code for d in c.diagnostics])
        self.assertEqual(c.acquisition.kind, LocatorKind.UNAVAILABLE)

    def test_synthetic_source_neutrality(self):
        for kind, mechanism in (
            (SourceKind.DIRECT_DOWNLOAD, Mechanism.DIRECT_DOWNLOAD),
            (SourceKind.NEWZNAB, Mechanism.NZB),
            (SourceKind.TORZNAB, Mechanism.TORRENT),
                (SourceKind.EXTERNAL, Mechanism.OTHER)):
            with self.subTest(kind=kind):
                source = replace(self.source, kind=kind)
                c = normalize_release(
                    source, 'Batman #1', replace(
                        self.reference, mechanism=mechanism))
                self.assertEqual(c.acquisition.mechanism, mechanism)

    def test_raw_labels(self):
        for label in ('1', '01', '1.5', '1A', '[nn]', 'Annual', 'Special', '½'):
            with self.subTest(label=label):
                o, _ = parse_release_title(f'Batman #{label} (2016)')
                self.assertEqual(o.coverage.labels, (label,))
                self.assertNotIn('calculated_issue_number', asdict(o))

    def test_no_suffix_projection_collision(self):
        first, _ = parse_release_title('Batman #1A')
        second, _ = parse_release_title('Batman #1.01')
        self.assertNotEqual(first.coverage, second.coverage)

    def test_ranges(self):
        for raw, expected in (
            ('1-4', ('1', '4')),
            ('001-004', ('001', '004')),
                ('1.5-3', ('1.5', '3'))):
            with self.subTest(raw=raw):
                o, _ = parse_release_title(f'Batman #{raw} (2016)')
                self.assertEqual(o.coverage.kind, CoverageKind.RANGE)
                self.assertEqual(o.coverage.labels, expected)

    def test_set_not_range(self):
        o, _ = parse_release_title('Batman #1,3,5 (2016)')
        self.assertEqual(o.coverage.kind, CoverageKind.SET)
        self.assertEqual(o.coverage.labels, ('1', '3', '5'))

    def test_opaque_set(self):
        o, _ = parse_release_title('Batman #1A,1B,[nn] (2016)')
        self.assertEqual(o.coverage.labels, ('1A', '1B', '[nn]'))
        self.assertEqual(o.coverage.kind, CoverageKind.SET)

    def test_ambiguous_ranges(self):
        for raw in ('4-1', '1A-1B', '1-4-8', '1,3-5'):
            with self.subTest(raw=raw):
                o, d = parse_release_title(f'Batman #{raw}')
                self.assertEqual(o.coverage.kind, CoverageKind.UNKNOWN)
                self.assertIn(Code.AMBIGUOUS_COVERAGE, [n.code for n in d])

    def test_multiple_claims_not_arbitrary_winner(self):
        o, d = parse_release_title('Batman #1 and #5')
        self.assertEqual(o.coverage.kind, CoverageKind.UNKNOWN)
        self.assertIn(Code.AMBIGUOUS_COVERAGE, [n.code for n in d])

    def test_duplicate_labels_diagnosed(self):
        o, d = parse_release_title('Batman #1,1,3')
        self.assertEqual(o.coverage.labels, ('1', '1', '3'))
        self.assertIn(Code.AMBIGUOUS_COVERAGE, [n.code for n in d])

    def test_packs_no_fake_endpoints(self):
        for text, kind in (
            ('Complete Series', PackKind.SERIES),
            ('Volume Pack', PackKind.VOLUME),
                ('Pack', PackKind.MULTI_ISSUE)):
            o, _ = parse_release_title(f'Batman {text}')
            self.assertEqual(o.pack, kind)
            self.assertEqual(o.coverage.kind, CoverageKind.PACK)
            self.assertEqual(o.coverage.labels, ())

    def test_special_axes(self):
        for text in ('TPB', 'Hardcover', 'Omnibus', 'One-Shot'):
            with self.subTest(text=text):
                o, _ = parse_release_title(f'Batman {text} (2016)')
                self.assertEqual(o.coverage.kind, CoverageKind.COLLECTION)
                self.assertEqual(
                    o.physical_format is not None, text in (
                        'TPB', 'Hardcover'))
                self.assertEqual(
                    o.publication_kind is not None, text in (
                        'Omnibus', 'One-Shot'))

    def test_vai_volume_evidence_not_target_classification(self):
        o, _ = parse_release_title('Batman Volume 02 (2016)')
        self.assertEqual(o.volume, '02')
        self.assertEqual(o.coverage.kind, CoverageKind.UNKNOWN)
        self.assertIsNone(o.special_version)

    def test_legacy_implicit_tpb_not_fact(self):
        result = ddl('Batman (2016)')
        self.assertEqual(result['special_version'], 'tpb')
        c = adapt_ddl_result(result)
        self.assertTrue(all(o.special_version is None for o in c.observations))

    def test_size(self):
        for size in (0, 1, 2 ** 50):
            self.assertEqual(self.candidate(size=size).size_bytes, size)
        for size in (-1, '1.3 GB', True, float('nan')):
            c = self.candidate(size=size)
            self.assertIsNone(c.size_bytes)
            self.assertIn(Code.INVALID_SIZE, [d.code for d in c.diagnostics])

    def test_published_time(self):
        value = datetime(2024, 1, 2, 5, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(self.candidate(published=value).published_at.hour, 3)
        self.assertEqual(
            self.candidate(
                published='2024-01-02T03:00:00Z').published_at.hour,
            3)
        self.assertEqual(
            self.candidate(
                published=value).observations[0].year,
            2016)

    def test_naive_time_requires_source_semantics(self):
        value = datetime(2024, 1, 2)
        self.assertIsNone(self.candidate(published=value).published_at)
        self.assertIsNotNone(
            self.candidate(
                published=value,
                source_timezone=timezone.utc).published_at)
        for value in ('not a date', '2024-99-99', 12):
            self.assertIn(
                Code.INVALID_TIME, [
                    d.code for d in self.candidate(
                        published=value).diagnostics])

    def test_structured_conflicts_preserved(self):
        o = ReleaseObservation(
            ObservationOrigin.STRUCTURED,
            'item',
            series='Other',
            year=2017,
            extension='.cbr')
        c = normalize_release(
            self.source,
            'Batman #1 (2016).cbz',
            self.reference,
            structured=(
                o,
            ))
        self.assertEqual(c.observations[0], o)
        self.assertEqual(c.observations[1].series, 'Batman')
        self.assertEqual(
            {d.field for d in c.diagnostics if d.code == Code.CONFLICT},
            {'series', 'year', 'extension'})

    def test_group_uploader_language_independent(self):
        o = ReleaseObservation(
            ObservationOrigin.STRUCTURED,
            'item',
            language='fr',
            release_group='Empire',
            uploader='Alice',
            tags=(
                'Digital',
                'Variant cover'))
        c = self.candidate(structured=(o,))
        self.assertEqual(c.observations[0], o)
        parsed, _ = parse_release_title('Series-NotAGroup')
        self.assertIsNone(parsed.release_group)

    def test_extension_not_mechanism(self):
        self.assertIsNone(self.candidate().observations[0].extension)
        for extension in ('.cbz', '.CBR', '.zip', '.rar', '.pdf'):
            o, _ = parse_release_title('Batman #1' + extension)
            self.assertEqual(o.extension, extension)

    def test_decimal_label_is_not_extension(self):
        observation, _ = parse_release_title('Batman #1.5')
        self.assertIsNone(observation.extension)
        self.assertEqual(observation.coverage.labels, ('1.5',))
        for extension in ('.7z', '.tar.gz'):
            observation, _ = parse_release_title('Batman #1' + extension)
            self.assertEqual(observation.extension, extension)
            self.assertEqual(observation.coverage.labels, ('1',))

    def test_unparseable_keeps_source(self):
        c = adapt_ddl_result(ddl('???'))
        self.assertEqual(c.raw_title, '???')
        self.assertEqual(c.acquisition.kind, LocatorKind.SOURCE_PAGE)
        self.assertIn(
            Code.COVERAGE_UNAVAILABLE, [
                d.code for d in c.diagnostics])

    def test_credentials_not_retained(self):
        for link in ('https://user:password@example.test/api?apikey=SECRET',
                     'https://example.test/SECRET/download#cookie',
                     'magnet:?xt=urn:btih:SECRET'):
            c = adapt_ddl_result(
                ddl(link=link, authorization='Bearer SECRET', cookies='SECRET'))
            for rendered in (
                repr(c), json.dumps(
                    asdict(c), default=str), json.dumps(
                    preview_release(c))):
                self.assertNotIn('SECRET', rendered)
                self.assertNotIn('password', rendered)
                self.assertNotIn(link, rendered)

    def test_invalid_locator_retains_candidate(self):
        for value in (
            '',
            'javascript:alert(1)',
            '/local/path',
                'https://[broken'):
            c = adapt_ddl_result(ddl(link=value))
            self.assertEqual(c.acquisition.kind, LocatorKind.UNAVAILABLE)

    def test_locator_model_rejects_urls(self):
        with self.assertRaises(ValueError):
            AcquisitionReference(
                Mechanism.NZB,
                LocatorKind.SOURCE_RECORD,
                'https://secret')

    def test_url_guid_is_not_a_secret_channel(self):
        c = self.candidate(result_id='https://example.test/get?apikey=SECRET')
        self.assertTrue(c.result_id.startswith('url-sha256:'))
        self.assertNotIn('SECRET', repr(c))
        self.assertNotIn('SECRET', json.dumps(preview_release(c)))
        with self.assertRaises(ValueError):
            replace(c, result_id='https://example.test/get?apikey=SECRET')

    def test_numeric_equivalent_set_diagnostic(self):
        observation, diagnostics = parse_release_title('Batman #1,01,3')
        self.assertEqual(observation.coverage.labels, ('1', '01', '3'))
        self.assertIn(Code.AMBIGUOUS_COVERAGE, [d.code for d in diagnostics])

    def test_bounded_source_tags(self):
        with self.assertRaises(ValueError):
            ReleaseObservation(
                ObservationOrigin.STRUCTURED,
                'item',
                tags=(
                    'x',
                ) * 33)

    def test_unknown_extension_diagnostic(self):
        observation, diagnostics = parse_release_title('Batman #1.xyz')
        self.assertEqual(observation.extension, '.xyz')
        self.assertIn(Code.UNKNOWN_EXTENSION, [d.code for d in diagnostics])

    def test_malformed_numeric_token_not_partially_admitted(self):
        for token in ('1.5.2', '1/2'):
            observation, _ = parse_release_title(f'Batman #{token}')
            self.assertEqual(observation.coverage.kind, CoverageKind.UNKNOWN)

    def test_physical_and_publication_evidence_coexist(self):
        observation, diagnostics = parse_release_title('Batman HC Omnibus (2016)')
        self.assertEqual(observation.physical_format, 'HC')
        self.assertEqual(observation.publication_kind, 'Omnibus')
        self.assertIsNone(observation.special_version)
        self.assertIn(Code.CONFLICT, [d.code for d in diagnostics])

    def test_immutable_and_hashable(self):
        c = self.candidate()
        self.assertEqual(hash(c), hash(self.candidate()))
        with self.assertRaises(FrozenInstanceError):
            c.raw_title = 'changed'
        with self.assertRaises(ValueError):
            replace(c, observations=[])
        with self.assertRaises(ValueError):
            ReleaseCoverage(CoverageKind.SET, ['1', '2'])
        with self.assertRaises(ValueError):
            ReleaseObservation(ObservationOrigin.STRUCTURED, 'item', tags=[])

    def test_cardinality_and_typed_fields(self):
        for kind, labels in (
            (CoverageKind.SINGLE, ()),
            (CoverageKind.RANGE, ('1',)),
                (CoverageKind.UNKNOWN, ('1',))):
            with self.assertRaises(ValueError):
                ReleaseCoverage(kind, labels)
        with self.assertRaises(ValueError):
            self.candidate(result_id=123)

    def test_deterministic_mapping_order(self):
        result = ddl()
        self.assertEqual(
            adapt_ddl_result(result),
            adapt_ddl_result(
                dict(
                    reversed(
                        tuple(
                            result.items())))))

    def test_input_not_mutated(self):
        result = ddl()
        original = dict(result)
        candidate = adapt_ddl_result(result)
        self.assertEqual(result, original)
        result['display_title'] = 'changed'
        self.assertNotEqual(candidate.raw_title, result['display_title'])

    def test_no_scoring_fields(self):
        result = ddl(
            match=False,
            match_issue='wrong volume',
            score=100,
            winner=True)
        c = adapt_ddl_result(result)
        self.assertEqual(c, adapt_ddl_result(ddl()))
        for field in (
            'score',
            'match',
            'winner',
            'accepted',
            'rejected',
                'target'):
            self.assertNotIn(field, preview_release(c))

    def test_bulk_error_isolation(self):
        batch = adapt_ddl_results(
            (ddl(), ddl(
                indexer_id=None), ddl(
                display_title=''), ddl('Batman #2')))
        self.assertEqual(len(batch.candidates), 2)
        self.assertEqual(tuple(f.position for f in batch.failures), (1, 2))

    def test_bulk_does_not_hide_programming_failure(self):
        with patch('backend.implementations.release_candidates.adapt_ddl_result', side_effect=RuntimeError('bug')):
            with self.assertRaises(RuntimeError):
                adapt_ddl_results((ddl(),))

    def test_bulk_no_io_or_dedup(self):
        result = ddl()
        with patch('socket.socket', side_effect=AssertionError('network')), \
                patch('sqlite3.connect', side_effect=AssertionError('DB')), \
                patch('builtins.open', side_effect=AssertionError('file IO')):
            batch = adapt_ddl_results(result for _ in range(1000))
        self.assertEqual(len(batch.candidates), 1000)
        self.assertEqual(batch.failures, ())
        self.assertEqual(len(set(batch.candidates)), 1)

    def test_transport_json_no_html_execution(self):
        c = normalize_release(
            self.source,
            '<script>alert(1)</script>',
            self.reference)
        dto = preview_release(c)
        self.assertEqual(json.loads(json.dumps(dto)), dto)
        self.assertEqual(dto['raw_title'], c.raw_title)
        self.assertNotIn(self.reference.key, json.dumps(dto))

    def test_title_bound(self):
        with self.assertRaises(ValueError):
            normalize_release(self.source, 'a' * 16385, self.reference)

    def test_real_ddl_search_output_parity(self):
        indexer = object.__new__(GetComicsIndexer)
        indexer._id, indexer._title, indexer._url = 1, 'GetComics', 'https://getcomics.example'
        indexer.request_count = 0
        titles = (
            'Batman #001 (2016)',
            'Batman #001-004 (2016)',
            'Batman HC (2016)',
            'Batman (2016)',
            '???')
        page = ''.join(
            f'<article class="post"><h1 class="post-title"><a href="https://getcomics.example/{i}/">{title}</a></h1><div><p>Size : 12 MB</p></div><a class="post-category">Comics</a></article>'
            for i, title in enumerate(titles))
        indexer.session = AsyncMock()
        indexer.session.get_text.return_value = page
        results = run(
            indexer.search(
                {'query': 'Batman', 'page': 1,
             'total_available_variations': 1}))
        self.assertEqual(len(results.results), 5)
        before = tuple(dict(r) for r in results.results)
        batch = adapt_ddl_results(results.results)
        self.assertEqual(tuple(c.raw_title for c in batch.candidates), titles)
        self.assertEqual(tuple(results.results), before)
        for c, result in zip(batch.candidates, results.results):
            self.assertEqual(c.size_bytes, result['size'])
            self.assertEqual(c.source.name, result['indexer_title'])
            self.assertEqual(c.observations[0].series, result['series'])
            self.assertEqual(
                c.acquisition.key, resolver_key(
                    c.source, result['link']))
        self.assertEqual(batch.failures, ())
