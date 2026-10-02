"""Disposable journaled ComicInfo repair; no metadata provider access."""

from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

import TMaintenanceReview as fixture

from backend.base.library_health import HealthLevel
from backend.base.maintenance_review import Action
from backend.base.metadata_repair import RepairError
from backend.features.comicinfo_repair import ComicInfoRepairReviews
from backend.features.organization_execution import OrganizationExecutor


class ComicInfoRepairTests(TestCase):
    def setUp(self):
        self.fixture = fixture.MaintenanceReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db, self.cursor = self.fixture.db, self.fixture.db.cursor()
        self.db.execute("INSERT OR REPLACE INTO config VALUES('database_version',65)")
        self.path = self.fixture.fixture.comic(xml=None)
        self.worklist = self.fixture.assign(self.fixture.review(HealthLevel.ARCHIVE), Action.COMICINFO, 'comicinfo_absent')
        self.service = ComicInfoRepairReviews(self.fixture.service)

    def review(self):
        item = next(i for i in self.worklist.items if i.selected)
        session = self.service.create(self.cursor, self.worklist.id, self.worklist.revision,
                                      self.worklist.manifest_digest, item.finding.id)
        return self.service.revise(self.cursor, session.id, 0, ('Series', 'Number', 'Provider identities'))

    def apply(self, session):
        return self.service.apply(self.cursor, session.id, session.revision, session.digest,
                                  confirmed=True, expected_authority=session.authority)

    def test_add_missing_journal_preserves_domain(self):
        session = self.review()
        self.assertIsInstance(session.stamp, str)  # Owned immutable freshness digest.
        before = {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in
                  ('volumes', 'issues', 'issues_files', 'volume_external_ids', 'issue_external_ids',
                   'bibliographic_content_claims', 'file_content_coverage')}
        with patch('socket.socket', side_effect=AssertionError('provider IO')):
            result = self.apply(session)
        self.assertEqual(result['state'], 'completed')
        with ZipFile(self.path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn(b'<Series>Example</Series>', archive.read('ComicInfo.xml'))
        self.assertEqual(before, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in before})
        self.assertEqual(self.db.execute('SELECT size FROM files').fetchone()[0], self.path.stat().st_size)
        retry = self.apply(session)
        self.assertEqual(retry['job_id'], result['job_id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_unsupported_selection_and_aba(self):
        session = self.review()
        with self.assertRaisesRegex(RepairError, 'unsupported'):
            self.service.revise(self.cursor, session.id, session.revision, ('Summary',))
        with self.assertRaisesRegex(RepairError, 'unavailable'):
            self.service.revise(self.cursor, session.id, session.revision, ('Date', 'Provider identities'))
        self.db.execute('UPDATE volumes SET authority_generation=2 WHERE id=1')
        self.db.commit()
        with self.assertRaisesRegex(RepairError, 'stale'):
            self.apply(session)
        with ZipFile(self.path) as archive:
            self.assertNotIn('ComicInfo.xml', archive.namelist())

    def test_replacement_guard_blocks_metadata_check_use_race(self):
        session = self.review()
        from backend.features import organization_execution
        real = organization_execution.write_comicinfo
        def writer(*args, **kwargs):
            # Deterministic race after executor preflight, before guarded replace.
            self.db.execute("UPDATE volumes SET title='new title' WHERE id=1")
            self.db.commit()
            return real(*args, **kwargs)
        with patch.object(organization_execution, 'write_comicinfo', side_effect=writer):
            result = self.apply(session)
        self.assertEqual(result['state'], 'recovery_required')
        with ZipFile(self.path) as archive:
            self.assertNotIn('ComicInfo.xml', archive.namelist())

    def test_post_replace_failure_recovers_not_undo(self):
        session = self.review()
        def checkpoint(stage, job, ordinal):
            if stage == 'after_effect' and ordinal == 0:
                raise PermissionError('fixture')
        self.service.checkpoint = checkpoint
        result = self.apply(session)
        self.assertEqual(result['state'], 'recovery_required')
        with ZipFile(self.path) as archive:
            self.assertIn('ComicInfo.xml', archive.namelist())
        executor = OrganizationExecutor(str(self.fixture.fixture.database), (str(self.fixture.fixture.root),))
        try:
            repaired = executor.apply_job(result['job_id'])
            self.assertEqual(repaired.state.value, 'completed')
            self.assertFalse(executor.preview_undo(result['job_id']).eligible)
        finally:
            executor.close()

    def test_selected_scalars_preserve_unknown_and_excluded_xml(self):
        with ZipFile(self.path, 'w') as archive:
            archive.writestr('001.jpg', b'page')
            archive.writestr('ComicInfo.xml', '<ComicInfo><Year>bad</Year><Title>Keep</Title><Notes>Personal</Notes><Custom x="1">Unknown</Custom></ComicInfo>')
        self.db.execute('UPDATE files SET size=?', (self.path.stat().st_size,))
        self.db.commit()
        self.worklist = self.fixture.assign(self.fixture.review(HealthLevel.ARCHIVE), Action.COMICINFO, 'invalid_field')
        session = self.review()
        self.assertEqual(self.apply(session)['state'], 'completed')
        with ZipFile(self.path) as archive:
            xml = archive.read('ComicInfo.xml')
            self.assertIn(b'<Notes>Personal</Notes>', xml)
            self.assertIn(b'<Custom x="1">Unknown</Custom>', xml)
            self.assertIn(b'<Title>Keep</Title>', xml)
            self.assertIn(b'<Year>bad</Year>', xml)  # Excluded, not secretly salvaged.

    def test_multiple_independent_archives(self):
        import time

        from backend.base.maintenance_review import Edit
        for n in range(1, 10):
            self.fixture.fixture.comic(f'archive-{n}.cbz', xml=None)
        worklist = self.fixture.review(HealthLevel.ARCHIVE)
        items = tuple(i for i in worklist.items if i.finding.code == 'comicinfo_absent')
        worklist = self.fixture.service.revise(worklist.id, 0,
            tuple(Edit(i.finding.id, True, False, Action.COMICINFO) for i in items))
        sessions = []
        for item in items:
            s = self.service.create(self.cursor, worklist.id, worklist.revision, worklist.manifest_digest, item.finding.id)
            sessions.append(self.service.revise(self.cursor, s.id, 0, ('Series', 'Provider identities')))
        started = time.perf_counter()
        for session in sessions:
            self.assertEqual(self.apply(session)['state'], 'completed')
        print(f'ComicInfo independent batch: {len(sessions)} archives in {time.perf_counter() - started:.3f}s')
