"""Synthetic archive payloads; no user comics or client data."""
import os
import shutil
import stat
import subprocess
import sys
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

from PIL import Image
from Tbackend.features import organization_execution as fixture

from backend.base.definitions import RAR_EXECUTABLES
from backend.base.files import folder_path
from backend.base.helpers import get_os_type
from backend.base.organization_job import JobState
from backend.features.organization_archive import register_archive, review
from backend.implementations.archive_normalization import (ArchiveFailure,
                                                           extract_legacy,
                                                           inspect, normalize)


def payloads():
    stream = BytesIO()
    Image.new('RGB', (32, 64), 'white').save(stream, format='PNG')
    return [('1.png', stream.getvalue()), ('2.png', stream.getvalue()), ('10.png', stream.getvalue()),
            ('ComicInfo.xml', '<ComicInfo><Title>Unicode café</Title><Unknown>preserved</Unknown></ComicInfo>'.encode())]


def comic(path, rar=False, version=5):
    data = payloads()
    if not rar:
        with ZipFile(path, 'w') as archive:
            for name, content in data:
                archive.writestr(name, content)
        return
    shutil.copyfile(Path(__file__).parent / 'fixtures' / 'archives' /
                    f'synthetic-rar{version}.cbr', path)


class ArchiveEngineTests(TestCase):
    def test_container_conversion_does_not_require_raster_decoding(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'actually-zip.cbr'
            with ZipFile(path,'w') as archive:archive.writestr('page.png',b'not an image')
            self.assertEqual(inspect(str(path))['status'], 'convertible')
            from backend.implementations.file_quality import analyze
            target = Path(directory)/'converted.cbz'
            normalize(str(path), str(target))
            with self.assertRaises(Exception): analyze(str(target))

    def test_container_conversion_preserves_pages_without_quality_codec_support(self):
        with TemporaryDirectory() as directory:
            source, target = Path(directory)/'source.cbr', Path(directory)/'result.cbz'
            with ZipFile(source, 'w') as archive:
                archive.writestr('page.avif', b'synthetic opaque page payload')
            with patch('backend.implementations.file_quality.Image.open', side_effect=AssertionError('raster decode')):
                result = normalize(str(source), str(target))
            self.assertEqual(result['pages'], 1)
            with ZipFile(target) as archive:
                self.assertEqual(archive.read('page.avif'), b'synthetic opaque page payload')

    def test_rar_header_buffer_is_bounded_without_cumulative_capacity_limit(self):
        from backend.base.acquisition_intake import IntakeFailure
        from backend.implementations.acquisition_preparation import (
            CHUNK, _BoundedHeaders)
        stream = _BoundedHeaders(BytesIO(b'x' * (CHUNK + 1)))
        self.assertEqual(len(stream.read(CHUNK)), CHUNK)
        self.assertEqual(stream.read(1), b'x')
        with self.assertRaises(IntakeFailure): stream.read(CHUNK + 1)

    def test_complete_child_extraction_and_unsafe_workspace(self):
        with TemporaryDirectory() as directory:
            root=Path(directory); child=root/'issue.cbz'; comic(child)
            outer=root/'package.zip'
            with ZipFile(outer,'w') as archive:
                archive.writestr('nested/issue.cbz',child.read_bytes())
            extract_legacy(str(outer),str(root/'extracted'))
            self.assertEqual((root/'extracted/nested/issue.cbz').read_bytes(),child.read_bytes())
            with self.assertRaises(FileExistsError): extract_legacy(str(outer),str(root/'extracted'))
            with ZipFile(outer,'a') as archive: archive.writestr('../escape.cbz',b'bad')
            with self.assertRaises(Exception): extract_legacy(str(outer),str(root/'unsafe'))
            self.assertFalse((root/'unsafe').exists())

    def test_bounds_collisions_symlink_pdf_and_cancel(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for bad in ('../x.png','a/../x.png','C:/x.png','/x.png','NUL.png','a\\x.png','x\x01.png','1.PNG','file.pdf'):
                source, target = root/'input.cbz', root/'output.cbz'
                comic(source)
                with ZipFile(source,'a') as archive:
                    info = ZipInfo()
                    info.filename = info.orig_filename = bad
                    archive.writestr(info,b'fixture')
                with self.assertRaises(ArchiveFailure):
                    normalize(str(source),str(target))
                if target.exists(): target.unlink()
            comic(source)
            with self.assertRaises(ArchiveFailure):
                normalize(str(source),str(target),cancelled=lambda:True)
            if target.exists(): target.unlink()
            info=ZipInfo('link.png');info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
            with ZipFile(source,'a') as archive: archive.writestr(info,b'1.png')
            with self.assertRaises(ArchiveFailure): normalize(str(source),str(target))

    def test_rar4_rar5_exact_payloads_metadata_and_determinism(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for version in (4, 5):
                source = root / ('fixture'+str(version)+'.cbr')
                comic(source, True, version)
                old = source.read_bytes()
                a, b = root / 'a.cbz', root / 'b.cbz'
                result = normalize(str(source), str(a))
                normalize(str(source), str(b))
                self.assertEqual(a.read_bytes(), b.read_bytes())
                self.assertEqual(source.read_bytes(), old)
                with ZipFile(a) as archive:
                    self.assertEqual(archive.namelist(), [n for n, _ in payloads()])
                    for name in ('1.png', '2.png', '10.png'):
                        self.assertEqual(sha256(archive.read(name)).hexdigest(),
                                         '4cc487c54dc29c7f6724beedb0712304091a6e872f2197ff2f7f55c30157c1df')
                    self.assertEqual(archive.read('ComicInfo.xml'), payloads()[-1][1])
                self.assertTrue(result['pages_preserved'])
                self.assertEqual(inspect(str(a))['status'], 'healthy')
                a.unlink(); b.unlink()

    def test_unsafe_and_broken_inputs_preserved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name, data in [('../escape.png', b'x'), ('/absolute.png', b'x'),
                               ('ComicInfo.xml', b'<!DOCTYPE x><ComicInfo/>')]:
                source, target = root/'source.cbz', root/'target.cbz'
                comic(source)
                with ZipFile(source, 'a') as archive:
                    archive.writestr(name, data)
                before = source.read_bytes()
                with self.assertRaises(ArchiveFailure):
                    normalize(str(source), str(target))
                self.assertEqual(source.read_bytes(), before)
                if target.exists():
                    target.unlink()


class ArchiveJournalTests(TestCase):
    def test_io_failure_matrix_preserves_authority_and_recovers_registered_jobs(self):
        import errno
        import sqlite3

        from backend.features.archive_maintenance import \
            recover_interrupted_archives
        failures = (
            ('backend.implementations.archive_normalization._PayloadWriter.write', OSError(errno.ENOSPC, 'fixture full'), False),
            ('backend.internals.organization_jobs.JobStore.create', OSError(errno.EIO, 'fixture journal failure'), False),
            ('backend.features.organization_archive.rename_no_replace', OSError(errno.EIO, 'fixture publish failure'), True),
            ('backend.features.organization_archive.QualityStore.assessment', sqlite3.OperationalError('fixture database failure'), True),
            ('backend.features.organization_archive.os.unlink', PermissionError('fixture cleanup failure'), True),
        )
        for target, failure, registered in failures:
            with self.subTest(target=target):
                other = ArchiveJournalTests(); other.setUp()
                try:
                    original = other.path.read_bytes()
                    if registered:
                        identifier = other.register()
                        with patch(target, side_effect=failure):
                            result = other.h.executor.apply_job(identifier)
                        self.assertEqual(result.state, JobState.RECOVERY)
                        result = recover_interrupted_archives(other.h.dbpath)
                        self.assertEqual(result[0]['state'], 'completed', result)
                        self.assertTrue(other.path.with_suffix('.cbz').exists())
                        self.assertFalse(other.path.exists())
                    else:
                        with patch(target, side_effect=failure), self.assertRaises((ArchiveFailure, OSError)):
                            other.register()
                        self.assertEqual(other.path.read_bytes(), original)
                        self.assertEqual(other.h.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
                    self.assertEqual(list(other.h.folder.glob('.pullarr-archive-*')), [])
                finally:
                    other.doCleanups()

    def setUp(self):
        self.h = fixture.ExecutionTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.path = self.h.folder/'Batman 001.cbr'
        comic(self.path, True)
        self.h.db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)', (str(self.path), self.path.stat().st_size))
        self.h.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')

    def register(self):
        _, confirmation = review(self.h.executor, 1)
        return register_archive(self.h.executor, 1, confirmation, 'archive-fixture')

    def test_conversion_keeps_file_identity_and_seed_bytes(self):
        seed = self.h.root/'seed.cbr'
        os.link(self.path, seed)
        before = seed.read_bytes()
        job = self.h.executor.apply_job(self.register())
        self.assertEqual(job.state, JobState.COMPLETED, job.error)
        self.assertEqual(seed.read_bytes(), before)
        self.assertFalse(self.path.exists())
        target = self.path.with_suffix('.cbz')
        self.assertFalse(os.path.samefile(seed, target))
        self.assertEqual(self.h.db.execute('SELECT filepath FROM files WHERE id=1').fetchone()[0], str(target))
        self.assertEqual(self.h.db.execute('SELECT issue_id FROM issues_files WHERE file_id=1').fetchone()[0], 1)
        self.assertFalse(self.h.db.execute('PRAGMA foreign_key_check').fetchall())
        self.assertFalse(self.h.executor.preview_undo(job.id).eligible)

    def test_all_effect_recovery(self):
        for ordinal in range(4):
            with self.subTest(ordinal=ordinal):
                other = ArchiveJournalTests(); other.setUp()
                try:
                    identifier = other.register()
                    def interrupt(stage, job, step):
                        if stage == 'after_effect' and step == ordinal:
                            raise fixture.Interrupted()
                    other.h.executor.hook = interrupt
                    with self.assertRaises(fixture.Interrupted):
                        other.h.executor.apply_job(identifier)
                    other.h.executor.hook = lambda *_: None
                    preview = other.h.executor.preview_recovery(identifier)
                    self.assertTrue(preview['eligible'], preview)
                    result = other.h.executor.apply_job(identifier, approved_recovery=preview['digest'])
                    self.assertEqual(result.state, JobState.COMPLETED, result.error)
                finally:
                    other.doCleanups()

    def test_automatic_restart_matrix_and_exact_retry(self):
        from backend.features.archive_maintenance import \
            recover_interrupted_archives
        for stage in ('after_started', 'after_effect'):
            for ordinal in range(4):
                with self.subTest(stage=stage, ordinal=ordinal):
                    other = ArchiveJournalTests(); other.setUp()
                    try:
                        identifier = other.register()
                        def interrupt(where, job, step):
                            if where == stage and step == ordinal:
                                raise fixture.Interrupted()
                        other.h.executor.hook = interrupt
                        with self.assertRaises(fixture.Interrupted):
                            other.h.executor.apply_job(identifier)
                        result = recover_interrupted_archives(other.h.dbpath)
                        self.assertEqual(result[0]['state'], 'completed', result)
                        self.assertEqual(recover_interrupted_archives(other.h.dbpath), [])
                        self.assertEqual(other.h.db.execute('SELECT count(*) FROM organization_jobs').fetchone()[0], 1)
                        self.assertFalse(list(other.h.folder.glob('.pullarr-archive-*')))
                        self.assertEqual(other.h.db.execute('SELECT filepath FROM files WHERE id=1').fetchone()[0],
                                         str(other.path.with_suffix('.cbz')))
                    finally: other.doCleanups()

    def test_interrupted_book_does_not_block_sibling_or_mutate_on_preview(self):
        from backend.features.archive_maintenance import (
            ArchiveMaintenance, recover_interrupted_archives)
        identifier = self.register()
        def interrupt(stage, job, step):
            if stage == 'after_started' and step == 1: raise fixture.Interrupted()
        self.h.executor.hook = interrupt
        with self.assertRaises(fixture.Interrupted): self.h.executor.apply_job(identifier)
        sibling = self.h.folder/'Batman - Deluxe Edition (2019) - 005 - Book 5.cbr'
        comic(sibling, True)
        self.h.db.execute('INSERT INTO files(id,filepath,size) VALUES(2,?,?)', (str(sibling), sibling.stat().st_size))
        self.h.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(2,1)')
        before = tuple(self.h.folder.iterdir())
        result = ArchiveMaintenance(self.h.dbpath)._preview(self.h.executor, 2, lambda: False)
        self.assertEqual(result['status'], 'convertible', result)
        self.assertEqual(tuple(self.h.folder.iterdir()), before)
        self.assertEqual(self.h.executor.store.get(identifier).state, JobState.RUNNING)
        self.assertEqual(recover_interrupted_archives(self.h.dbpath)[0]['state'], 'completed')
        self.h.executor.hook = lambda *_: None
        _, confirmation = review(self.h.executor, 2)
        second = register_archive(self.h.executor, 2, confirmation, 'book-five')
        self.assertEqual(self.h.executor.apply_job(second).state, JobState.COMPLETED)
        self.assertFalse(list(self.h.folder.glob('.pullarr-archive-*')))

    def test_automatic_recovery_refuses_changed_original_before_publication(self):
        from backend.features.archive_maintenance import \
            recover_interrupted_archives
        identifier = self.register()
        def interrupt(stage, job, step):
            if stage == 'after_started' and step == 1: raise fixture.Interrupted()
        self.h.executor.hook = interrupt
        with self.assertRaises(fixture.Interrupted): self.h.executor.apply_job(identifier)
        # Replace the directory entry, preserving the backup's old bytes.
        self.path.unlink(); self.path.write_bytes(b'changed source')
        result = recover_interrupted_archives(self.h.dbpath)
        self.assertEqual(result[0]['state'], 'recovery_required')
        self.assertFalse(self.path.with_suffix('.cbz').exists())
        self.assertEqual(self.path.read_bytes(), b'changed source')

    def test_concurrent_recovery_has_one_claimant(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        from backend.features.archive_maintenance import \
            recover_interrupted_archives
        from backend.features.organization_execution import \
            OrganizationExecutor
        identifier = self.register()
        entered, release = Event(), Event()
        def worker():
            def checkpoint(stage, job, step):
                if stage == 'after_started' and step == 0:
                    entered.set()
                    if not release.wait(20): raise AssertionError('worker timeout')
            executor = OrganizationExecutor(self.h.dbpath, (str(self.h.root),), checkpoint=checkpoint)
            try: return executor.apply_job(identifier, automatic_archive_recovery=True)
            finally: executor.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker)
            try:
                self.assertTrue(entered.wait(20))
                second = pool.submit(recover_interrupted_archives, self.h.dbpath).result(timeout=20)
                self.assertEqual(second[0]['reason'], 'executor_or_path_claimed')
            finally: release.set()
            self.assertEqual(first.result(timeout=20).state, JobState.COMPLETED)
        self.assertEqual(recover_interrupted_archives(self.h.dbpath), [])
        self.assertFalse(list(self.h.folder.glob('.pullarr-archive-*')))
        self.assertEqual(self.h.db.execute('SELECT count(*) FROM organization_reservations').fetchone()[0], 0)

    def test_stale_preview(self):
        _, confirmation = review(self.h.executor, 1)
        with self.path.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ArchiveFailure, 'stale_preview'):
            register_archive(self.h.executor, 1, confirmation, 'stale')
        self.assertTrue(self.path.exists())

    def test_legacy_cbz_preference_uses_journal_and_stable_id(self):
        from backend.implementations.conversion import mass_convert
        from backend.implementations.converters import (ProposedConversion,
                                                        cbr_to_cbz)
        from backend.internals.db import DBConnection
        proposal=ProposedConversion(str(self.path),cbr_to_cbz,'cbz')
        with patch('backend.implementations.conversion._get_convertable_files',return_value=[proposal]), \
                patch('backend.implementations.conversion.commit'), \
                patch('backend.implementations.conversion.mass_process_files') as processed, \
                patch('backend.internals.db.get_db',return_value=self.h.db), \
                patch.object(DBConnection,'default_file',self.h.dbpath):
            result=mass_convert(1)
        processed.assert_called_once_with(1)
        self.assertEqual(result,[str(self.path.with_suffix('.cbz'))])
        self.assertEqual(self.h.db.execute('SELECT file_id FROM issues_files').fetchone()[0],1)
        self.assertEqual(self.h.db.execute('SELECT state FROM organization_jobs').fetchone()[0],'completed')

    def test_c2_and_canonical_context_preserved(self):
        from backend.base.content_claims import ClaimKind, PublicationRef
        from backend.internals.content_claims import (apply_coverage,
                                                      claim_preview,
                                                      confirm_claim,
                                                      coverage_preview)
        db=self.h.db
        db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(2,1,202,'2',2)")
        cursor=db.cursor();ref=PublicationRef('comicvine','202')
        preview=claim_preview(cursor,1,ref,ClaimKind.COMPLETE,manual=True)
        claim=confirm_claim(cursor,1,ref,ClaimKind.COMPLETE,preview['preview_token'],manual=True)
        preview=coverage_preview(cursor,1,1,[claim])
        apply_coverage(cursor,1,1,[claim],preview['preview_token'])
        before=db.execute('SELECT * FROM valid_file_content_coverage').fetchall()
        self.assertTrue(before)
        self.assertEqual(self.h.executor.apply_job(self.register()).state,JobState.COMPLETED)
        self.assertEqual(db.execute('SELECT * FROM valid_file_content_coverage').fetchall(),before)

    def test_registered_target_blocks_but_unowned_workspace_is_preserved(self):
        _,confirmation=review(self.h.executor,1)
        target=str(self.path.with_suffix('.cbz'))
        self.h.db.execute('INSERT INTO files(id,filepath,size) VALUES(2,?,1)',(target,))
        with self.assertRaisesRegex(ArchiveFailure,'target_occupied'):
            register_archive(self.h.executor,1,confirmation,'registered-target')
        self.h.db.execute('DELETE FROM files WHERE id=2')
        evidence=self.h.folder/'.pullarr-archive-abandoned.cbz'; evidence.write_bytes(b'evidence')
        identifier = register_archive(self.h.executor,1,confirmation,'abandoned-workspace')
        self.assertEqual(self.h.executor.apply_job(identifier).state, JobState.COMPLETED)
        self.assertEqual(evidence.read_bytes(),b'evidence')

    def test_unicode_registered_target_collision(self):
        from backend.features.organization_archive import target_registered
        self.h.db.execute('INSERT INTO files(id,filepath,size) VALUES(2,?,1)',(str(self.h.folder/'CAFÉ.CBZ'),))
        self.assertTrue(target_registered(self.h.db,1,str(self.h.folder/'café.cbz')))

    def test_real_process_exit_and_restart(self):
        for ordinal in range(4):
            with self.subTest(ordinal=ordinal):
                other=ArchiveJournalTests();other.setUp()
                try:
                    identifier=other.register()
                    command = """import os,sys
from backend.features.organization_execution import OrganizationExecutor
def checkpoint(stage, job, ordinal):
    if stage == 'after_effect' and ordinal == int(sys.argv[4]): os._exit(77)
executor=OrganizationExecutor(sys.argv[1],(sys.argv[2],),checkpoint=checkpoint)
executor.apply_job(sys.argv[3])
"""
                    result=subprocess.run([sys.executable,'-c',command,other.h.dbpath,str(other.h.root),identifier,str(ordinal)],
                                          timeout=30,capture_output=True)
                    self.assertEqual(result.returncode,77,result.stderr.decode(errors='replace'))
                    executor=other.h.open_executor()
                    preview=executor.preview_recovery(identifier)
                    self.assertTrue(preview['eligible'],preview)
                    self.assertEqual(executor.apply_job(identifier,approved_recovery=preview['digest']).state,JobState.COMPLETED)
                finally: other.doCleanups()

    def test_target_collision_and_sharing_change(self):
        _,confirmation=review(self.h.executor,1)
        target=self.path.with_suffix('.cbz'); target.write_bytes(b'unrelated')
        with self.assertRaisesRegex(ArchiveFailure,'target_occupied'):
            register_archive(self.h.executor,1,confirmation,'collision')
        self.assertEqual(target.read_bytes(),b'unrelated')
        target.unlink()
        os.link(self.path,self.h.root/'new-hardlink.cbr')
        with self.assertRaisesRegex(ArchiveFailure,'stale_preview'):
            register_archive(self.h.executor,1,confirmation,'sharing-changed')
