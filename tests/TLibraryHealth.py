"""Health discovery on disposable schema-64 libraries; never live providers."""

import hashlib
import json
import os
import sqlite3
import tracemalloc
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from backend.base.content_claims import ClaimKind, PublicationRef
from backend.base.issue_facts import (BibliographicDate, DateKind,
                                      IssueFacts, IssueNumberFacts)
from backend.base.library_health import (HealthLevel as Level, HealthLimits,
                                         HealthScope,
                                         InspectionStatus as Status)
from backend.base.naming_policy import NamingSettings
from backend.features.library_health import scan_health
from backend.internals.content_claims import (apply_coverage, claim_preview,
                                              confirm_claim, coverage_preview)
from backend.internals.db import DB_SCHEMA
from backend.internals.issue_facts import write_facts
from backend.internals.settings import PublicSettingsValues


class LibraryHealthTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='kapowarr-health-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'library'
        self.volume = self.root / 'Example'
        self.volume.mkdir(parents=True)
        self.database = self.base / 'health.db'
        self.db = sqlite3.connect(self.database)
        self.addCleanup(self.db.close)
        self.db.executescript(DB_SCHEMA)
        self.db.execute('PRAGMA foreign_keys=ON')
        for key, value in asdict(NamingSettings.capture(PublicSettingsValues())).items():
            self.db.execute('INSERT OR REPLACE INTO config(key,value) VALUES(?,?)', (key, int(value) if type(value) is bool else value))
        self.db.execute("UPDATE config SET value='Issue {issue_number}' WHERE key IN ('file_naming','file_naming_empty')")
        self.db.execute('INSERT INTO root_folders(id,folder) VALUES(1,?)', (str(self.root),))
        self.db.execute('''INSERT INTO volumes(id,title,root_folder,folder,metadata_provider,comicvine_id,volume_number,year)
            VALUES(1,'Example',1,?,'comicvine',100,1,2020)''', (str(self.volume),))
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(1,1,101,'1',1,1)")
        self.db.execute("INSERT OR REPLACE INTO config(key,value) VALUES('api_key','HEALTH_SECRET_SENTINEL')")
        self.db.commit()

    def comic(self, name='Issue 001.cbz', xml='<ComicInfo><Series>Example</Series></ComicInfo>', pages=True, registered=True):
        path = self.volume / name
        with ZipFile(path, 'w') as archive:
            if pages:
                archive.writestr('001.jpg', b'not decoded by metadata inspection')
            if xml is not None:
                archive.writestr('ComicInfo.xml', xml)
        if registered:
            fid = self.db.execute('INSERT INTO files(filepath,size) VALUES(?,?)', (str(path), path.stat().st_size)).lastrowid
            self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(?,1)', (fid,))
            self.db.commit()
        return path

    def scan(self, level=Level.INVENTORY, **kwargs):
        self.db.commit()
        return scan_health(str(self.database), HealthScope('volumes', (1,)), level, **kwargs)

    def codes(self, report):
        return {f.code for f in report.findings}

    def filesystem(self):
        return {str(p.relative_to(self.root)): ('directory' if p.is_dir() else
            (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())) for p in self.root.rglob('*')}

    def test_all_levels_do_not_mutate_domain_or_files(self):
        self.comic('noncanonical.cbz')
        before = tuple(self.db.iterdump()), self.filesystem()
        with patch('socket.socket', side_effect=AssertionError('network')), \
                patch('backend.implementations.file_matching.scan_files', side_effect=AssertionError('scan writer')), \
                patch('backend.features.organization_execution.OrganizationExecutor.apply_job', side_effect=AssertionError('apply')), \
                patch('backend.implementations.comicinfo_archive.write_comicinfo', side_effect=AssertionError('XML writer')):
            for level in Level:
                report = self.scan(level)
                self.assertNotIn('HEALTH_SECRET_SENTINEL', json.dumps(report.summary()) + json.dumps(report.page()))
        self.assertEqual(before, (tuple(self.db.iterdump()), self.filesystem()))
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_inventory_never_opens_archive_or_hashes(self):
        self.comic()
        with patch('backend.features.library_health.inspect_comicinfo', side_effect=AssertionError('archive')):
            report = self.scan()
        self.assertEqual(json.loads(report.counts_json).get('hash_bytes', 0), 0)

    def test_missing_and_untracked(self):
        path = self.comic()
        path.unlink()
        self.comic('unexpected.cbz', registered=False)
        self.assertTrue({'missing_file', 'untracked_file'} <= self.codes(self.scan()))

    def test_missing_root_never_created(self):
        self.volume.rmdir()
        self.root.rmdir()
        report = self.scan()
        self.assertIn('missing_folder', self.codes(report))
        self.assertFalse(self.root.exists())

    def test_file_outside_root_not_opened(self):
        path = self.base / 'outside.cbz'
        path.write_bytes(b'private')
        fid = self.db.execute('INSERT INTO files(filepath,size) VALUES(?,7)', (str(path),)).lastrowid
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(?,1)', (fid,))
        report = self.scan(Level.DEEP)
        self.assertIn('file_outside_root', self.codes(report))
        self.assertEqual(json.loads(report.counts_json).get('hash_bytes', 0), 0)

    def test_valid_absent_malformed_and_empty_archives(self):
        self.comic('good.cbz')
        self.comic('missing.cbz', xml=None)
        self.comic('malformed.cbz', xml='<ComicInfo>')
        self.comic('empty.cbz', pages=False)
        codes = self.codes(self.scan(Level.ARCHIVE))
        self.assertTrue({'comicinfo_absent', 'xml_malformed', 'no_comic_pages'} <= codes)

    def test_corrupt_unsafe_multiple_and_unsupported(self):
        self.comic().write_bytes(b'not zip')
        with ZipFile(self.volume / 'unsafe.cbz', 'w') as archive:
            archive.writestr('../escape.jpg', b'bad')
        with ZipFile(self.volume / 'multiple.cbz', 'w') as archive:
            archive.writestr('ComicInfo.xml', '<ComicInfo/>')
            archive.writestr('nested/ComicInfo.xml', '<ComicInfo/>')
        (self.volume / 'unsupported.cbr').write_bytes(b'Rar!')
        report = self.scan(Level.ARCHIVE)
        self.assertTrue({'archive_unreadable', 'unsafe_member', 'multiple_documents', 'unsupported_container'} <= self.codes(report))
        rar = next(f for f in report.findings if f.path and f.path.endswith('.cbr') and f.category == 'archive')
        self.assertEqual(rar.inspection, Status.UNSUPPORTED)

    def test_metadata_diagnostic_and_partial_date(self):
        self.comic(xml='<ComicInfo><Year>2020</Year><Month>99</Month></ComicInfo>')
        self.assertIn('invalid_field', self.codes(self.scan(Level.ARCHIVE)))
        (self.volume / 'Issue 001.cbz').unlink()
        self.db.execute('DELETE FROM files')
        self.comic(xml='<ComicInfo><Year>2020</Year></ComicInfo>')
        self.assertNotIn('invalid_field', self.codes(self.scan(Level.ARCHIVE)))

    def test_historical_identities_are_not_conflicts(self):
        self.comic()
        self.db.execute("INSERT INTO volume_external_ids VALUES(1,'metron','opaque','operator',NULL)")
        self.db.execute("INSERT INTO issue_external_ids VALUES(1,'metron','opaque-issue','operator')")
        self.assertFalse(any(f.category == 'identity' for f in self.scan().findings))

    def test_missing_selected_identity_and_generation_staleness(self):
        self.comic('wrong.cbz')
        first = self.scan()
        self.db.execute('UPDATE volumes SET authority_generation=2 WHERE id=1')
        second = self.scan()
        self.assertNotEqual(first.state_digest, second.state_digest)
        self.assertNotEqual(next(f.state_digest for f in first.findings if f.code == 'filename_deviation'),
                            next(f.state_digest for f in second.findings if f.code == 'filename_deviation'))
        self.db.execute("UPDATE volumes SET metadata_provider='metron' WHERE id=1")
        self.assertIn('selected_identity_missing', self.codes(self.scan()))

    def test_canonical_noncanonical_custom_and_collision(self):
        self.comic()
        report = self.scan()
        self.assertNotIn('filename_deviation', self.codes(report))
        self.comic('wrong.cbz')
        self.db.execute('UPDATE volumes SET custom_folder=1')
        report = self.scan()
        self.assertTrue({'filename_deviation', 'path_collision', 'same_publication_files'} <= self.codes(report))
        self.assertEqual(next(f for f in report.findings if f.code == 'folder_deviation').severity.value, 'informational')

    def test_deep_exact_duplicates_only(self):
        path = self.comic()
        (self.volume / 'copy.cbz').write_bytes(path.read_bytes())
        (self.volume / 'different.cbz').write_bytes(b'different')
        report = self.scan(Level.DEEP)
        duplicates = [f for f in report.findings if f.code == 'exact_byte_duplicate']
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(len(json.loads(duplicates[0].evidence_json)['evidence']['paths']), 2)
        self.assertNotIn('exact_byte_duplicate', self.codes(self.scan()))

    def test_limits_report_incompleteness(self):
        for n in range(4):
            self.comic(f'{n}.cbz', registered=False)
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number) VALUES(2,1,102,'2')")
        for limits, reason in ((HealthLimits(files=1), 'file_limit'), (HealthLimits(entries=1), 'entry_limit'),
                               (HealthLimits(findings=1), 'finding_limit'), (HealthLimits(archives=1), 'archive_limit'),
                               (HealthLimits(hashes=1), 'hash_limit'), (HealthLimits(result_bytes=1), 'result_bytes_limit'),
                               (HealthLimits(rows=1), 'database_snapshot_limit')):
            with self.subTest(reason=reason):
                result = self.scan(Level.DEEP, limits=limits)
                self.assertIn(reason, result.reasons)
                self.assertEqual(result.state, Status.BOUNDED)

    def test_cancel_progress_and_immutable_pagination(self):
        self.comic()
        self.assertEqual(self.scan(cancel=lambda: True).state, Status.CANCELLED)
        progress = []
        report = self.scan(progress=progress.append)
        self.assertTrue(progress)
        with self.assertRaises(FrozenInstanceError):
            report.state = Status.COMPLETE
        page = report.page(limit=1)
        page['findings'].clear()
        self.assertTrue(report.findings)
        with self.assertRaises(ValueError):
            report.page(limit=101)
        with self.assertRaises(ValueError):
            HealthScope('root', (0,))
        with self.assertRaises(ValueError):
            scan_health(str(self.database), HealthScope('volumes', (99,)))

    def test_scope_and_missing_database(self):
        self.comic()
        for scope in (HealthScope(), HealthScope('root', (1,)), HealthScope('volumes', (1,))):
            self.assertEqual(json.loads(scan_health(str(self.database), scope).counts_json)['files'], 1)
        missing = self.base / 'not-created.db'
        self.assertIn('database_unavailable', scan_health(str(missing)).reasons)
        self.assertFalse(missing.exists())

    def test_c2_coverage_is_not_direct_identity_and_remains_unchanged(self):
        self.comic()
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(2,1,102,'2',2)")
        cursor = self.db.cursor()
        reference = PublicationRef('comicvine', '102')
        preview = claim_preview(cursor, 1, reference, ClaimKind.COMPLETE, manual=True)
        claim = confirm_claim(cursor, 1, reference, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        preview = coverage_preview(cursor, 1, 1, [claim])
        apply_coverage(cursor, 1, 1, [claim], preview['preview_token'])
        self.db.commit()
        before = tuple(self.db.iterdump())
        result = self.scan()
        self.assertNotIn('multiple_direct_volumes', self.codes(result))
        self.assertNotIn('same_publication_files', self.codes(result))
        self.assertEqual(before, tuple(self.db.iterdump()))
        self.comic('second.cbz')
        preview = coverage_preview(cursor, 1, 2, [claim])
        apply_coverage(cursor, 1, 2, [claim], preview['preview_token'])
        result = self.scan()
        overlap = next(f for f in result.findings if f.code == 'overlapping_collected_coverage')
        self.assertEqual(overlap.severity.value, 'informational')

    def test_valid_rich_number_and_partial_date_not_errors(self):
        self.comic()
        facts = IssueFacts(IssueNumberFacts.interpret('[nn]', 'gcd', 'number'),
            (BibliographicDate.interpret('2020-12-00', DateKind.PUBLICATION, 'gcd', 'date', zero_placeholders=True),), 'date')
        write_facts(self.db.cursor(), 1, facts)
        result = self.scan()
        self.assertNotIn('invalid_canonical_fact', self.codes(result))

    def test_multi_issue_direct_file_not_automatically_invalid(self):
        self.comic()
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(2,1,102,'2',2)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,2)')
        self.assertFalse(any(f.category == 'association' for f in self.scan().findings))

    def test_inaccessible_directory_and_transient_disappearance(self):
        path = self.comic()
        with patch('backend.features.library_health.os.scandir', side_effect=PermissionError('private diagnostic')):
            report = self.scan()
        self.assertIn('enumeration_unavailable', report.reasons)
        self.assertNotIn('private diagnostic', json.dumps(report.page()))
        from backend.features.library_health import _Scan
        original = _Scan.archives

        def disappeared(scan, filename, fid, vid):
            Path(filename).unlink()
            return original(scan, filename, fid, vid)

        with patch.object(_Scan, 'archives', disappeared):
            report = self.scan(Level.ARCHIVE)
        self.assertIn('archive_unavailable', self.codes(report))
        self.assertFalse(path.exists())  # Fixture hook, not a scanner effect.

    def test_one_zip_open_and_signature_mismatch(self):
        self.comic()
        with patch('backend.implementations.comicinfo_archive.ZipFile', wraps=ZipFile) as opened:
            self.scan(Level.ARCHIVE)
        self.assertEqual(opened.call_count, 1)
        path = self.volume / 'wrong.cbr'
        path.write_bytes((self.volume / 'Issue 001.cbz').read_bytes())
        self.assertIn('extension_content_mismatch', self.codes(self.scan(Level.ARCHIVE)))

    def test_same_filename_is_not_byte_equivalence(self):
        self.comic('same.cbz')
        (self.volume / 'nested').mkdir()
        (self.volume / 'nested' / 'same.cbz').write_bytes(b'different bytes')
        self.assertNotIn('exact_byte_duplicate', self.codes(self.scan(Level.DEEP)))

    def test_unavailable_naming_does_not_invent_values(self):
        self.comic()
        self.db.execute("UPDATE config SET value='{issue_title}' WHERE key IN ('file_naming','file_naming_empty')")
        self.assertIn('naming_unavailable', self.codes(self.scan()))

    def test_hash_byte_budget_and_cancellation_between_chunks(self):
        self.comic()
        report = self.scan(Level.DEEP, limits=HealthLimits(hash_bytes=1))
        self.assertIn('hash_limit', report.reasons)
        self.assertEqual(json.loads(report.counts_json).get('hash_bytes', 0), 0)

    def test_registry_and_qualified_collision_checks(self):
        self.db.execute("INSERT INTO volumes(id,title,root_folder,folder,metadata_provider) VALUES(2,'Other',1,?,'metron')", (str(self.root / 'Other'),))
        self.db.execute("INSERT INTO volume_external_ids VALUES(2,'metron','same','fixture',NULL)")
        self.db.execute("INSERT INTO volume_external_ids VALUES(1,'metron','same','fixture',NULL)")
        self.assertIn('identity_ownership_conflict', self.codes(self.scan()))

    def test_read_snapshot_refuses_writes(self):
        from backend.internals.library_health import read_snapshot

        def attempt(*args):
            args[1].execute("UPDATE volumes SET title='bad'")

        with patch('backend.internals.organization_plan.load_planning_records', side_effect=attempt):
            with self.assertRaises(sqlite3.OperationalError):
                read_snapshot(str(self.database), HealthScope(), 20000)
        self.assertEqual(self.db.execute('SELECT title FROM volumes WHERE id=1').fetchone()[0], 'Example')

    def test_synthetic_inventory_diagnostics(self):
        real_connect = sqlite3.connect
        counts = []
        for count in (100, 1000):
            for path in self.volume.iterdir():
                path.unlink()
            for index in range(count):
                (self.volume / f'{index}.cbz').write_bytes(b'fixture')
            start = perf_counter()
            statements = []

            def connect(*args, **kwargs):
                db = real_connect(*args, **kwargs)
                db.set_trace_callback(statements.append)
                return db

            tracemalloc.start()
            with patch('backend.internals.library_health.sqlite3.connect', side_effect=connect):
                report = self.scan()
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            counts.append(sum(s.lstrip().upper().startswith('SELECT') for s in statements))
            self.assertEqual(json.loads(report.counts_json)['files'], count)
            self.assertEqual(json.loads(report.counts_json).get('archives', 0), 0)
            print(f'Health inventory {count} files: {counts[-1]} SELECTs; {perf_counter() - start:.3f}s; peak {peak}; zero archive/hash probes')
        self.assertEqual(counts[0], counts[1])
