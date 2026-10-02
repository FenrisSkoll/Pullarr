"""Offline, disposable ComicInfo security, preservation and organizer contracts."""

import os
import warnings
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from backend.base.comicinfo import ComicInfoCode, ComicInfoError, DatePrecision
from backend.base.import_candidate import (DiscoveryScope, EvidenceSource,
                                           ExistingFileIdentity,
                                           InspectionState, LocalAssociation,
                                           Provenance, ProviderReference,
                                           ResourceKind, ReviewState)
from backend.implementations.comicinfo import (IDENTITY_NS, MAX_XML,
                                               comicinfo_claims,
                                               parse_comicinfo, provider_url)
from backend.implementations.comicinfo_archive import (inspect_comicinfo,
                                                       write_comicinfo)
from backend.implementations.comicinfo_candidate import enrich_comicinfo
from backend.implementations.comicinfo_merge import (ComicInfoUpdates,
                                                     merge_comicinfo,
                                                     selected_metadata_updates)
from backend.implementations.import_candidates import observe_import_candidate
from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata)

AUTHORITY = ProviderReference('metron', ResourceKind.VOLUME, 'ABC')
ORIGIN = Provenance(EvidenceSource.COMICINFO, 'ComicInfo.xml')


def xml(body=''):
    return ('<ComicInfo>' + body + '</ComicInfo>').encode()


def updates(values=(('Title', 'New'),)):
    return ComicInfoUpdates(AUTHORITY, values, (
        AUTHORITY, ProviderReference('metron', ResourceKind.ISSUE, '001-A'),
        ProviderReference('comicvine', ResourceKind.VOLUME, 'ABC')))


class ComicInfoParser(TestCase):
    def test_minimal_and_missing(self):
        document = parse_comicinfo(xml())
        self.assertIsNone(document.series)
        self.assertEqual(document.date.precision, DatePrecision.UNKNOWN)

    def test_raw_numbers_never_use_legacy_parser(self):
        for number in ('1', '01', '1.5', '1A', '[nn]', 'Annual', 'Special', '', ' 01 '):
            with self.subTest(number=number), patch(
                    'backend.base.file_extraction.extract_issue_number', side_effect=AssertionError):
                document = parse_comicinfo(xml('<Number>' + number + '</Number>'))
                self.assertEqual(document.number, number)
                self.assertFalse(hasattr(document, 'calculated_issue_number'))

    def test_partial_dates(self):
        for fields, precision, complete in (
            ('<Year>2021</Year>', DatePrecision.YEAR, None),
            ('<Year>2021</Year><Month>12</Month>', DatePrecision.MONTH, None),
            ('<Year>2021</Year><Month>12</Month><Day>25</Day>', DatePrecision.DAY, '2021-12-25'),
            ('<Year>-1</Year>', DatePrecision.UNKNOWN, None),
        ):
            document = parse_comicinfo(xml(fields))
            self.assertEqual(document.date.precision, precision)
            self.assertEqual(document.date.complete_date, complete)

    def test_invalid_fields_preserve_other_data(self):
        document = parse_comicinfo(xml('<Year>banana</Year><Month>13</Month><PageCount>-4</PageCount><Series>Hero</Series>'))
        self.assertEqual(document.series, 'Hero')
        self.assertEqual(document.text('Year'), 'banana')
        self.assertEqual(document.date.precision, DatePrecision.INVALID)
        self.assertIsNone(document.date.complete_date)
        self.assertTrue(any(d.field == 'PageCount' for d in document.diagnostics))

    def test_impossible_and_unanchored_dates(self):
        for fields in ('<Day>2</Day>', '<Month>12</Month>',
                       '<Year>2021</Year><Month>2</Month><Day>29</Day>',
                       '<Year>2021</Year><Month>0</Month>'):
            document = parse_comicinfo(xml(fields))
            self.assertEqual(document.date.precision, DatePrecision.INVALID)
            self.assertIsNone(document.date.complete_date)

    def test_missing_empty_whitespace_distinct(self):
        document = parse_comicinfo(xml('<Publisher/><Title> </Title>'))
        self.assertEqual(document.publisher, '')
        self.assertEqual(document.title, ' ')
        self.assertIsNone(document.series)
        self.assertEqual(document.values('Publisher')[0].text, None)

    def test_unicode_encoding_and_unknown_fields(self):
        raw = '<?xml version="1.0" encoding="UTF-16"?><ComicInfo><Series>風</Series><Custom x="1">é</Custom></ComicInfo>'.encode('utf-16')
        document = parse_comicinfo(raw)
        self.assertEqual(document.series, '風')
        self.assertEqual(document.raw_bytes, raw)
        self.assertEqual(document.declared_encoding, 'UTF-16 BOM')
        self.assertEqual(document.values('Custom')[0].attributes, (('x', '1'),))

    def test_lists_and_alternate_fields_not_reinterpreted(self):
        document = parse_comicinfo(xml('<Writer>Doe, Jane, Someone Else</Writer><AlternateSeries>Arc</AlternateSeries><Format>Omnibus</Format>'))
        self.assertEqual(document.text('Writer'), 'Doe, Jane, Someone Else')
        self.assertIsNone(document.series)
        self.assertEqual(document.text('Format'), 'Omnibus')
        self.assertFalse(hasattr(document, 'special_version'))

    def test_duplicate_and_structured_fields_not_first_wins(self):
        document = parse_comicinfo(xml('<Number>1</Number><Number>2</Number><Series><b>Hero</b></Series>'))
        self.assertIsNone(document.number)
        self.assertIsNone(document.series)
        self.assertEqual(len(document.diagnostics), 2)

    def test_malformed_and_wrong_root(self):
        for raw, code in ((b'<ComicInfo>', ComicInfoCode.XML_MALFORMED),
                          (b'<Other/>', ComicInfoCode.UNSUPPORTED_ROOT),
                          (b'<ComicInfo>\xff</ComicInfo>', ComicInfoCode.XML_MALFORMED)):
            with self.assertRaises(ComicInfoError) as caught:
                parse_comicinfo(raw)
            self.assertEqual(caught.exception.code, code)

    def test_dtd_and_entities_rejected_without_network(self):
        for declaration in ('<!DOCTYPE ComicInfo SYSTEM "https://example.invalid/x">',
                            '<!DOCTYPE ComicInfo [<!ENTITY x "boom">]>'):
            for encoding in ('utf-8', 'utf-16'):
                raw = ('<?xml version="1.0" encoding="' + encoding + '"?>' + declaration + '<ComicInfo/>').encode(encoding)
                with patch('socket.create_connection', side_effect=AssertionError), self.assertRaises(ComicInfoError) as caught:
                    parse_comicinfo(raw)
                self.assertEqual(caught.exception.code, ComicInfoCode.XML_UNSAFE)

    def test_xml_bounds(self):
        for raw in (b' ' * (MAX_XML + 1), xml('<x>' * 70 + '</x>' * 70)):
            with self.assertRaises(ComicInfoError) as caught:
                parse_comicinfo(raw)
            self.assertEqual(caught.exception.code, ComicInfoCode.LIMIT_EXCEEDED)

    def test_unknown_version_readable_but_not_rewritable(self):
        document = parse_comicinfo(b'<ComicInfo version="99"><Series>Hero</Series></ComicInfo>')
        self.assertEqual(document.series, 'Hero')
        self.assertEqual(document.diagnostics[0].code, ComicInfoCode.UNKNOWN_VERSION)
        with self.assertRaises(ComicInfoError):
            merge_comicinfo(document, updates())

    def test_exact_provider_url_grammars(self):
        for url, provider, kind, identity in (
            ('https://comicvine.gamespot.com/hero/4050-123/', 'comicvine', ResourceKind.VOLUME, '123'),
            ('https://comicvine.gamespot.com/4000-123/', 'comicvine', ResourceKind.ISSUE, '123'),
            ('https://metron.cloud/series/123/', 'metron', ResourceKind.VOLUME, '123'),
            ('https://metron.cloud/issue/123/', 'metron', ResourceKind.ISSUE, '123'),
            ('https://www.comics.org/issue/123/', 'gcd', ResourceKind.ISSUE, '123'),
        ):
            self.assertEqual(provider_url(url), ProviderReference(provider, kind, identity))

    def test_url_lookalikes_and_ambiguous_urls_rejected(self):
        for url in ('https://metron.cloud.evil/series/1/', 'https://evil/metron.cloud/series/1/',
                    'https://user@metron.cloud/series/1/', 'https://metron.cloud:44/series/1/',
                    'https://metron.cloud/series/1/?id=2', 'https://metron.cloud/series/nope/',
                    'https://[broken', 'https://example.com/issue/1/', '/series/1/'):
            self.assertIsNone(provider_url(url), url)

    def test_web_multiple_urls_and_notes_not_guessed(self):
        document = parse_comicinfo(xml('<Web>https://metron.cloud/series/1/ https://comics.org/series/1/</Web><Notes>ComicVine:123</Notes>'))
        claims = comicinfo_claims(document, ORIGIN)
        self.assertEqual({c.reference.provider for c in claims}, {'metron', 'gcd'})
        self.assertEqual({c.reference.provider_id for c in claims}, {'1'})


class ComicInfoMerge(TestCase):
    def test_partial_date_update_cannot_reuse_old_day(self):
        with self.assertRaises(ValueError):
            updates((('Year', '2021'), ('Month', '12')))

    def test_selected_namespace_cannot_contain_two_volume_ids(self):
        with self.assertRaises(ValueError):
            ComicInfoUpdates(AUTHORITY, (), (AUTHORITY, replace(AUTHORITY, provider_id='OTHER')))

    def test_preserve_unknown_namespaces_comments_attributes_and_credits(self):
        raw = b'<?xml version="1.0"?><ComicInfo xmlns:u="urn:user"><!--keep--><Title lang="en">Old</Title><Writer>A, B</Writer><Notes>Mine</Notes><u:Thing u:a="x"><u:Child/>text</u:Thing><?keep value?></ComicInfo>'
        output = merge_comicinfo(parse_comicinfo(raw), updates())
        document = parse_comicinfo(output)
        self.assertEqual(document.title, 'New')
        self.assertEqual(document.text('Writer'), 'A, B')
        self.assertEqual(document.text('Notes'), 'Mine')
        self.assertIn(b'<!--keep-->', output)
        self.assertIn(b'<?keep value?>', output)
        self.assertIn(b'xmlns:u="urn:user"', output)
        self.assertEqual(document.values('Title')[0].attributes, (('lang', 'en'),))
        self.assertTrue(document.values('{urn:user}Thing')[0].structured)

    def test_deterministic_and_idempotent_merge(self):
        existing = parse_comicinfo(xml('<Title>Old</Title>'))
        first = merge_comicinfo(existing, updates())
        self.assertEqual(first, merge_comicinfo(existing, updates()))
        self.assertEqual(first, merge_comicinfo(parse_comicinfo(first), updates()))

    def test_identity_extension_preserves_opaque_namespaces_not_local_ids(self):
        result = parse_comicinfo(merge_comicinfo(None, updates()))
        claims = comicinfo_claims(result, ORIGIN)
        self.assertEqual({c.reference for c in claims}, set(updates().identities))
        self.assertNotIn(b'local', result.raw_bytes)
        self.assertIn(b'selected="true"', result.raw_bytes)

    def test_conflicting_extension_blocks_merge(self):
        raw = xml('<Identity xmlns="' + IDENTITY_NS + '" provider="metron" kind="volume" id="OTHER"/>')
        with self.assertRaises(ComicInfoError):
            merge_comicinfo(parse_comicinfo(raw), updates())

    def test_duplicate_owned_field_blocks_merge(self):
        with self.assertRaises(ComicInfoError):
            merge_comicinfo(parse_comicinfo(xml('<Title>A</Title><Title>B</Title>')), updates())

    def test_fill_policy_preserves_present_empty(self):
        desired = updates((('Web', 'https://metron.cloud/issue/1/'),))
        self.assertEqual(parse_comicinfo(merge_comicinfo(parse_comicinfo(xml('<Web/>')), desired)).text('Web'), '')
        self.assertEqual(parse_comicinfo(merge_comicinfo(None, desired)).text('Web'), 'https://metron.cloud/issue/1/')

    def test_unowned_updates_rejected(self):
        with self.assertRaises(ValueError):
            updates((('Notes', 'erase user note'),))

    def test_selected_provider_updates_and_absence_not_deletion(self):
        volume = VolumeMetadata('metron', 'ABC', 'Selected Series', 2021, 1, None, None, None,
                                None, [], 'Publisher', 1, False, None)
        issue = IssueMetadata('metron', 'I', 'ABC', '01', 1.0, None, '2021-12-25', None)
        desired = selected_metadata_updates(volume, issue)
        result = parse_comicinfo(merge_comicinfo(parse_comicinfo(xml('<Title>User Title</Title><Writer>User Credit</Writer>')), desired))
        self.assertEqual(result.title, 'User Title')
        self.assertEqual(result.series, 'Selected Series')
        self.assertEqual(result.number, '01')
        self.assertEqual(result.date.complete_date, '2021-12-25')
        with self.assertRaises(ValueError):
            selected_metadata_updates(volume, replace(issue, provider='comicvine'))
        with self.assertRaises(ValueError):
            selected_metadata_updates(volume, replace(issue, date='2021-12-00'))


class ComicInfoArchives(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'Hero 001 (2021).cbz'

    def archive(self, raw=None, member='ComicInfo.xml', extra=()):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with ZipFile(self.path, 'w', ZIP_DEFLATED) as archive:
                archive.comment = b'user comment'
                archive.writestr('001.jpg', b'page bytes')
                if raw is not None:
                    archive.writestr(member, raw)
                for name, content in extra:
                    archive.writestr(name, content)
        return inspect_comicinfo(str(self.path))

    def candidate(self):
        return observe_import_candidate(str(self.path), DiscoveryScope('test', self.temp.name))

    def test_absent_is_not_failed(self):
        self.assertEqual(self.archive().state, InspectionState.ABSENT)
        self.path.write_bytes(b'not zip')
        result = inspect_comicinfo(str(self.path))
        self.assertEqual(result.state, InspectionState.FAILED)
        self.assertEqual(result.diagnostics[0].code, ComicInfoCode.ARCHIVE_UNREADABLE)

    def test_case_and_nested_member_found(self):
        result = self.archive(xml('<Number>01</Number>'), 'nested/cOmIcInFo.XML')
        self.assertEqual(result.document.number, '01')
        self.assertEqual(result.member, 'nested/cOmIcInFo.XML')

    def test_multiple_metadata_not_arbitrarily_selected(self):
        result = self.archive(xml(), extra=(('nested/ComicInfo.xml', xml()),))
        self.assertEqual(result.diagnostics[0].code, ComicInfoCode.MULTIPLE_DOCUMENTS)

    def test_duplicate_entries_rejected(self):
        result = self.archive(xml(), extra=(('ComicInfo.xml', xml()),))
        self.assertEqual(result.diagnostics[0].code, ComicInfoCode.DUPLICATE_MEMBER)

    def test_unsafe_paths_not_extracted(self):
        for name in ('../evil', '/evil', 'C:/evil', 'a/../../evil', 'a\\..\\evil'):
            result = self.archive(xml(), extra=((name, b'bytes'),))
            self.assertEqual(result.diagnostics[0].code, ComicInfoCode.UNSAFE_MEMBER)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_symlink_entry_rejected(self):
        self.archive()
        with ZipFile(self.path, 'a') as archive:
            info = ZipInfo('link')
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b'target')
        self.assertEqual(inspect_comicinfo(str(self.path)).diagnostics[0].code, ComicInfoCode.UNSAFE_MEMBER)

    def test_oversized_xml_rejected(self):
        result = self.archive(b' ' * (MAX_XML + 1))
        self.assertEqual(result.diagnostics[0].code, ComicInfoCode.LIMIT_EXCEEDED)

    def test_encrypted_flag_rejected(self):
        info = ZipInfo('ComicInfo.xml')
        info.flag_bits = 1
        with patch.object(ZipFile, 'infolist', return_value=[info]):
            self.archive()
            result = inspect_comicinfo(str(self.path))
        self.assertEqual(result.diagnostics[0].code, ComicInfoCode.ENCRYPTED)

    def test_central_directory_bound_precedes_zipfile_open(self):
        self.archive(xml())
        with patch('backend.implementations.comicinfo_archive.MAX_DIRECTORY', 1), \
                patch('backend.implementations.comicinfo_archive.ZipFile', side_effect=AssertionError):
            result = inspect_comicinfo(str(self.path))
        self.assertEqual(result.diagnostics[0].code, ComicInfoCode.LIMIT_EXCEEDED)

    def test_zip64_locator_cannot_override_small_classic_directory_guard(self):
        self.archive(xml())
        raw = self.path.read_bytes()
        offset = raw.rfind(b'PK\x05\x06')
        self.path.write_bytes(raw[:offset] + b'PK\x06\x07' + bytes(16) + raw[offset:])
        with patch('backend.implementations.comicinfo_archive.ZipFile', side_effect=AssertionError):
            result = inspect_comicinfo(str(self.path))
        self.assertEqual(result.diagnostics[0].code, ComicInfoCode.UNSUPPORTED_FORMAT)

    def test_expansion_ratio_bound(self):
        self.archive(xml())
        with patch('backend.implementations.comicinfo_archive.MAX_RATIO', 0):
            result = inspect_comicinfo(str(self.path))
        self.assertEqual(result.diagnostics[0].code, ComicInfoCode.LIMIT_EXCEEDED)

    def test_bulk_reuse_opens_once_per_candidate_not_per_field(self):
        self.archive(xml('<Series>Hero</Series><Number>1</Number><Publisher>Test</Publisher>'))
        candidate = self.candidate()
        with patch('backend.implementations.comicinfo_archive.ZipFile', wraps=ZipFile) as archive:
            for _ in range(25):
                result = inspect_comicinfo(str(self.path), candidate.file)
                enriched = enrich_comicinfo(candidate, result)
                self.assertEqual(enriched.comicinfo.document.series, 'Hero')
                self.assertEqual(enriched.comicinfo.document.publisher, 'Test')
                self.assertEqual(enriched.comicinfo.document.number, '1')
        self.assertEqual(archive.call_count, 25)

    def test_unsupported_compression_rejected(self):
        info = ZipInfo('ComicInfo.xml')
        info.compress_type = 999
        self.archive()
        with patch.object(ZipFile, 'infolist', return_value=[info]):
            result = inspect_comicinfo(str(self.path))
        self.assertEqual(result.diagnostics[0].code, ComicInfoCode.UNSUPPORTED_FORMAT)

    def test_crc_corruption_reported(self):
        with ZipFile(self.path, 'w', ZIP_STORED) as archive:
            archive.writestr('ComicInfo.xml', xml('<Title>CRC_SENTINEL</Title>'))
        raw = self.path.read_bytes().replace(b'CRC_SENTINEL', b'BAD_SENTINEL')
        self.path.write_bytes(raw)
        self.assertEqual(inspect_comicinfo(str(self.path)).diagnostics[0].code, ComicInfoCode.ARCHIVE_UNREADABLE)

    def test_failed_raw_xml_retained_without_parsed_document(self):
        self.archive(b'<broken')
        result = enrich_comicinfo(self.candidate())
        self.assertEqual(result.comicinfo.raw_bytes, b'<broken')
        self.assertIsNone(result.comicinfo.document)

    def test_stale_discovery_becomes_typed_failed_enrichment(self):
        self.archive(xml())
        candidate = self.candidate()
        self.archive(xml('<Title>changed</Title>'))
        enriched = enrich_comicinfo(candidate)
        self.assertEqual(enriched.comicinfo.diagnostics[0].code, ComicInfoCode.STALE)
        self.assertEqual(enriched.review, ReviewState.BLOCKED)

    def test_issue_url_disagreement_with_local_identity_requires_review(self):
        self.archive(xml('<Web>https://metron.cloud/issue/999/</Web>'))
        known = LocalAssociation(7, 8, False, AUTHORITY, Provenance(EvidenceSource.DATABASE, 'issues_files'),
                                 selected_issue=ProviderReference('metron', ResourceKind.ISSUE, '100'))
        candidate = replace(self.candidate(), existing=ExistingFileIdentity(1, (known,)))
        result = enrich_comicinfo(candidate)
        self.assertEqual(result.review, ReviewState.REQUIRED)
        self.assertEqual(result.existing, candidate.existing)

    def test_reenrichment_replaces_only_comicinfo_layer(self):
        self.archive(xml('<Series>Other</Series>'))
        candidate = enrich_comicinfo(self.candidate())
        again = enrich_comicinfo(candidate)
        self.assertEqual(len(candidate.claims), len(again.claims))
        self.assertEqual(len(candidate.diagnostics), len(again.diagnostics))

    def test_xml_failure_retains_safe_category(self):
        result = self.archive(b'<ComicInfo>')
        self.assertEqual(result.state, InspectionState.FAILED)
        self.assertEqual(result.diagnostics[0].code, ComicInfoCode.XML_MALFORMED)

    def test_unsupported_formats_explicit(self):
        for extension in ('.cbr', '.rar', '.pdf', '.jpg', ''):
            result = inspect_comicinfo(str(self.path.with_suffix(extension)))
            self.assertEqual(result.state, InspectionState.FAILED)
            self.assertEqual(result.diagnostics[0].code, ComicInfoCode.UNSUPPORTED_FORMAT)

    def test_candidate_enrichment_immutable_read_only_network_free(self):
        self.archive(xml('<Series>Hero</Series><Number>01</Number><Year>2021</Year><Month>12</Month>'))
        candidate = self.candidate()
        before = self.path.read_bytes()
        with patch('socket.create_connection', side_effect=AssertionError), \
                patch('os.replace', side_effect=AssertionError), \
                patch('backend.internals.db.get_db', side_effect=AssertionError):
            enriched = enrich_comicinfo(candidate)
        self.assertEqual(candidate.comicinfo.state, InspectionState.NOT_INSPECTED)
        self.assertEqual(enriched.comicinfo.document.number, '01')
        self.assertEqual(enriched.comicinfo.document.date.precision, DatePrecision.MONTH)
        self.assertIsNone(enriched.comicinfo.document.date.complete_date)
        self.assertEqual(self.path.read_bytes(), before)

    def test_inspection_can_be_reused_without_archive_reopen(self):
        result = self.archive(xml())
        candidate = self.candidate()
        with patch('backend.implementations.comicinfo_candidate.inspect_comicinfo', side_effect=AssertionError):
            enriched = enrich_comicinfo(candidate, result)
        self.assertIs(enriched.comicinfo.document, result.document)

    def test_filename_disagreement_requires_review(self):
        self.archive(xml('<Series>Other</Series><Number>99</Number>'))
        enriched = enrich_comicinfo(self.candidate())
        self.assertEqual(enriched.review, ReviewState.REQUIRED)
        self.assertEqual(enriched.filename.series, 'Hero')

    def test_existing_identity_not_overridden(self):
        self.archive(xml('<Series>Other</Series><Number>9</Number><Web>https://comicvine.gamespot.com/4050-123/</Web>'))
        existing = ExistingFileIdentity(1, (LocalAssociation(7, 8, True, AUTHORITY,
                                       Provenance(EvidenceSource.DATABASE, 'issues_files'), 'Hero', '1'),))
        candidate = replace(self.candidate(), existing=existing)
        result = enrich_comicinfo(candidate)
        self.assertEqual(result.existing, existing)
        self.assertEqual(result.review, ReviewState.REQUIRED)
        self.assertEqual(result.claims[0].reference.provider, 'comicvine')

    def test_matching_local_bibliography_preserved(self):
        self.archive(xml('<Series>Hero</Series><Number>1</Number>'))
        existing = ExistingFileIdentity(1, (LocalAssociation(7, 8, False, AUTHORITY,
                                       Provenance(EvidenceSource.DATABASE, 'issues_files'), 'Hero', '1'),))
        result = enrich_comicinfo(replace(self.candidate(), existing=existing))
        self.assertEqual(result.review, ReviewState.CONTINUE)

    def test_failed_inspection_blocks_not_absent(self):
        self.archive(b'<broken')
        result = enrich_comicinfo(self.candidate())
        self.assertEqual(result.review, ReviewState.BLOCKED)
        self.assertEqual(result.comicinfo.state, InspectionState.FAILED)

    def test_write_add_and_replace_preserves_pages_and_comment(self):
        for existing in (None, xml('<Writer>User</Writer>')):
            inspection = self.archive(existing)
            merged = merge_comicinfo(inspection.document, updates())
            written = write_comicinfo(inspection, merged)
            self.assertEqual(written.state, InspectionState.PRESENT)
            with ZipFile(self.path) as archive:
                self.assertEqual(archive.read('001.jpg'), b'page bytes')
                self.assertEqual(archive.comment, b'user comment')
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.namelist().count('ComicInfo.xml'), 1)
            self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_nested_metadata_replaced_in_original_location(self):
        inspection = self.archive(xml(), 'nested/COMICINFO.XML')
        written = write_comicinfo(inspection, merge_comicinfo(inspection.document, updates()))
        self.assertEqual(written.member, 'nested/COMICINFO.XML')

    def test_stale_write_keeps_newer_source(self):
        inspection = self.archive(xml())
        self.path.write_bytes(b'newer file')
        with self.assertRaises(ComicInfoError) as caught:
            write_comicinfo(inspection, xml())
        self.assertEqual(caught.exception.code, ComicInfoCode.STALE)
        self.assertEqual(self.path.read_bytes(), b'newer file')

    def test_replacement_failure_keeps_original_and_cleans_temp(self):
        inspection = self.archive(xml())
        original = self.path.read_bytes()
        with patch('backend.implementations.comicinfo_archive.os.replace', side_effect=PermissionError), self.assertRaises(ComicInfoError):
            write_comicinfo(inspection, xml('<Title>New</Title>'))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_source_changes_during_rewrite_blocks_replacement(self):
        inspection = self.archive(xml())
        original_replace = os.replace
        from backend.implementations import comicinfo_archive
        real_stamp = comicinfo_archive._stamp
        calls = 0

        def stamp(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.path.write_bytes(b'concurrent change')
            return real_stamp(path)

        with patch.object(comicinfo_archive, '_stamp', side_effect=stamp), \
                patch.object(comicinfo_archive.os, 'replace', wraps=original_replace) as replacement, \
                self.assertRaises(ComicInfoError):
            write_comicinfo(inspection, xml())
        replacement.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'concurrent change')

    def test_invalid_write_xml_never_touches_archive(self):
        inspection = self.archive(xml())
        before = self.path.read_bytes()
        with self.assertRaises(ComicInfoError):
            write_comicinfo(inspection, b'<broken')
        self.assertEqual(self.path.read_bytes(), before)
