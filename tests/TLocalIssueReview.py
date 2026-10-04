"""Exact managed-volume review receipts, atomic association and stale rejection."""
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from Tbackend.features import organization_execution as fixtures

from backend.features.local_issue_review import LocalReviewError, review_issue
from backend.features.local_organization import (_sessions, apply_preview,
                                                 scan_preview)


class LocalIssueReviewTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.ExecutionTests('test_pending_survives_reopen_without_mutation')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(_sessions.clear)
        self.db, self.database = self.fixture.db, self.fixture.dbpath
        self.db.execute("UPDATE config SET value=57 WHERE key='database_version'")
        self.db.execute("UPDATE volumes SET title='Batman: Rebirth Deluxe Edition',year=2017")
        self.db.execute("UPDATE issues SET title='Book 1',date='2017-01-01'")
        for n in (2, 3):
            self.db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,title,date) VALUES(?,1,?,?,?,?,?)',
                            (n, 200+n, str(n), n, f'Book {n}', '2017-01-01'))
        self.paths = [self.fixture.folder/f'Batman - Rebirth Deluxe Edition (2017) - {n:03} - Book {n}.cbz' for n in (1,2,3)]
        for n in (1,2,3): self.write(n)

    def write(self, n, *, number=None, series='Batman: Rebirth Deluxe Edition', extra=''):
        with ZipFile(self.paths[n-1], 'w') as archive:
            archive.writestr('page.png', b'synthetic unchanged page payload')
            archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>{series}</Series><Number>{number or n}</Number><Year>2017</Year>{extra}</ComicInfo>')

    def preview(self):
        self.value = scan_preview(self.database, 1)
        return self.value

    def review(self, index=0, **kwargs):
        return review_issue(self.database, 1, self.value['id'], index, **kwargs)

    def test_punctuation_and_numeric_books_are_ready_without_mutation(self):
        for series in ('Batman - Rebirth Deluxe Edition', 'Batman: Rebirth Deluxe Edition',
                       'Batman – Rebirth Deluxe Edition', 'Batman —  Rebirth Deluxe Edition'):
            with self.subTest(series=series):
                self.write(1, series=series)
                preview = self.preview()
                self.assertEqual([p['status'] for p in preview['plans']], ['ready']*3)
                self.assertEqual([p['issue_ids'] for p in preview['plans']], [[1],[2],[3]])
                self.assertEqual([p['volume_id'] for p in preview['plans']], [1]*3)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0],0)

    def test_conflict_details_read_only_then_exact_save_partial_apply_and_retry(self):
        self.write(1, number='2')
        before = [p.read_bytes() for p in self.paths]
        preview = self.preview()
        self.assertEqual([p['status'] for p in preview['plans']], ['review_required','ready','ready'])
        for _ in range(3):
            detail = self.review()
            self.assertEqual(detail['volume_id'],1)
            self.assertIn('ComicInfo issue number differs from the filename issue.',detail['reasons'])
            self.assertEqual([i['id'] for i in detail['issues']], [1,2,3])
            self.assertIsNone(detail['blocked'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0],0)
        saved = self.review(issue_ids=[1])
        self.assertEqual([p['status'] for p in saved['plans']], ['associated','ready','ready'])
        self.assertEqual(self.review(issue_ids=[1]),saved)
        self.assertEqual(self.db.execute('SELECT issue_id,forced FROM issues_files').fetchall(),[(1,1)])
        self.assertEqual(len(apply_preview(self.database,preview['id'])['jobs']),2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0],3)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0],3)
        self.assertEqual([p.read_bytes() for p in self.paths],before)

    def test_invalid_issue_ids_and_cross_volume_never_mutate(self):
        self.write(1,number='2');self.preview()
        for ids in ([],[999],[True],[1,1],['1']):
            with self.subTest(ids=ids), self.assertRaisesRegex(LocalReviewError,'invalid_issue_selection'):
                self.review(issue_ids=ids)
        with self.assertRaisesRegex(LocalReviewError,'stale_preview'):
            review_issue(self.database,2,self.value['id'],0,issue_ids=[1])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0],0)

    def test_exact_embedded_volume_conflict_is_not_issue_override(self):
        self.write(1,extra='<Web>https://comicvine.gamespot.com/volume/4050-999/</Web>')
        self.preview()
        self.assertEqual(self.review()['blocked'],'publication_or_file_conflict')
        with self.assertRaisesRegex(LocalReviewError,'publication_or_file_conflict'):
            self.review(issue_ids=[1])

    def test_embedded_issue_disagreement_can_be_reviewed_within_same_volume(self):
        self.write(1,extra='<Web>https://comicvine.gamespot.com/issue/4000-202/</Web>')
        self.preview()
        self.assertEqual(self.value['plans'][0]['status'],'review_required')
        self.assertTrue(any(e['value']=='comicvine:202' for e in self.review()['evidence']))
        self.review(issue_ids=[1])
        self.assertEqual(self.db.execute('SELECT issue_id FROM issues_files').fetchall(),[(1,)])

    def test_semantic_title_conflict_retains_managed_volume(self):
        self.write(1,series='Superman: Rebirth Deluxe Edition');self.preview()
        self.assertEqual(self.value['plans'][0]['volume_id'],1)
        self.assertEqual(self.value['plans'][0]['status'],'review_required')
        self.assertTrue(any('series differs' in r or 'title differs' in r for r in self.review()['reasons']))

    def test_source_change_missing_or_expired_preview_is_explicit(self):
        self.preview()
        with self.paths[0].open('ab') as stream: stream.write(b'changed')
        with self.assertRaisesRegex(LocalReviewError,'stale_preview'): self.review(issue_ids=[1])
        self.preview();_sessions[self.value['id']].expires=0
        with self.assertRaisesRegex(LocalReviewError,'stale_preview'): self.review()

    def test_same_size_replacement_and_folder_change_are_stale(self):
        self.preview(); path=self.paths[0]; data=path.read_bytes(); path.unlink();path.write_bytes(data)
        with self.assertRaisesRegex(LocalReviewError,'stale_preview'): self.review()
        self.preview();self.db.execute('UPDATE volumes SET folder=?',(str(self.fixture.incoming),))
        with self.assertRaises(Exception): self.review(issue_ids=[1])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0],0)

    def test_existing_manual_link_is_shown_and_reassociation_is_exact(self):
        self.write(1,number='2')
        self.db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)',(str(self.paths[0]),self.paths[0].stat().st_size))
        self.db.execute('INSERT INTO issues_files VALUES(1,2,1)')
        self.preview()
        self.assertEqual(self.review()['existing'],[dict(issue_id=2,label='2',forced=True)])
        self.review(issue_ids=[1]);self.review(issue_ids=[1])
        self.assertEqual(self.db.execute('SELECT * FROM issues_files').fetchall(),[(1,1,1)])

    def test_catalog_change_and_external_association_change_are_stale(self):
        self.preview();self.db.execute("UPDATE issues SET issue_number='changed' WHERE id=1")
        with self.assertRaisesRegex(LocalReviewError,'stale_preview'):self.review()
        self.db.execute("UPDATE issues SET issue_number='1' WHERE id=1");self.preview();self.review(issue_ids=[1])
        self.db.execute('UPDATE issues_files SET issue_id=2')
        with self.assertRaisesRegex(LocalReviewError,'stale_preview'):self.review(issue_ids=[1])

    def test_duplicate_numeric_labels_need_review_and_operator_can_choose_exact_id(self):
        self.db.execute("UPDATE issues SET issue_number='1',calculated_issue_number=1 WHERE id=2")
        self.preview();self.assertEqual(self.value['plans'][0]['status'],'review_required')
        self.review(issue_ids=[1]);self.assertEqual(self.db.execute('SELECT issue_id FROM issues_files').fetchall(),[(1,)])

    def test_failure_before_commit_rolls_back_and_same_preview_can_retry(self):
        self.write(1,number='2');self.preview()
        from backend.features import local_issue_review as review
        real=review._stamp;calls=0
        def changed(path):
            nonlocal calls
            calls+=1
            return real(path) if calls<3 else ()
        with patch.object(review,'_stamp',side_effect=changed), self.assertRaisesRegex(LocalReviewError,'stale_preview'):
            self.review(issue_ids=[1])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0],0)
        self.review(issue_ids=[1])

    def test_ready_rows_can_apply_before_remaining_review_is_saved(self):
        self.write(1,number='2');self.preview()
        self.assertEqual(len(apply_preview(self.database,self.value['id'])['jobs']),2)
        self.review(issue_ids=[1])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0],3)

    def test_other_volume_association_is_blocked(self):
        self.db.execute('INSERT INTO volumes(id,comicvine_id,title,root_folder,folder) VALUES(2,102,\'Other\',1,?)',
                        (str(self.fixture.incoming),))
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(4,2,204,'1',1)")
        self.db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)',(str(self.paths[0]),self.paths[0].stat().st_size))
        self.db.execute('INSERT INTO issues_files VALUES(1,4,1)');self.preview()
        self.assertEqual(self.review()['blocked'],'publication_or_file_conflict')
        with self.assertRaisesRegex(LocalReviewError,'publication_or_file_conflict'):self.review(issue_ids=[1])
        self.assertEqual(self.db.execute('SELECT issue_id FROM issues_files').fetchall(),[(4,)])

    def test_ambiguous_numeric_range_keeps_publication_and_can_choose_exact_issue(self):
        self.paths[0].unlink()
        path=self.fixture.folder/'Batman - Rebirth Deluxe Edition (2017) - 001-003.cbz'
        with ZipFile(path,'w') as archive:archive.writestr('page.png',b'synthetic')
        self.db.execute('UPDATE issues SET calculated_issue_number=1 WHERE id=2')
        value=self.preview();index=next(i for i,p in enumerate(value['plans']) if p['source']==str(path))
        self.assertEqual(value['plans'][index]['volume_id'],1)
        self.assertEqual(value['plans'][index]['status'],'review_required')
        self.review(index,issue_ids=[1])

    def test_reservation_and_invalid_row_cannot_be_bypassed(self):
        from backend.base.organization_job import (ExecutionCode,
                                                   OrganizationError)
        self.preview()
        with self.assertRaisesRegex(LocalReviewError,'stale_preview'):self.review(999,issue_ids=[1])
        with patch('backend.features.local_issue_review.require_unreserved',side_effect=OrganizationError(ExecutionCode.BUSY)):
            with self.assertRaises(OrganizationError):self.review(issue_ids=[1])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0],0)

    def test_symlink_replacement_is_rejected_without_mutation(self):
        self.preview();path=self.paths[0];path.unlink()
        try:path.symlink_to(self.fixture.source)
        except OSError:self.skipTest('Host does not permit test symlinks')
        from backend.base.acquisition_intake import IntakeFailure
        with self.assertRaises(IntakeFailure):self.review(issue_ids=[1])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0],0)

    def test_diagnostics_never_return_authenticated_links_and_are_bounded(self):
        from backend.features.local_issue_review import _text
        self.assertNotIn('password',_text('https://user:password@example.invalid/private'))
        self.assertEqual(len(_text('x'*1000)),300)
        self.assertEqual(_text(0),'0')

    def test_equivalent_numeric_metadata_keeps_exact_existing_issue(self):
        self.db.execute("UPDATE issues SET issue_number='001' WHERE id=1")
        self.db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)',(str(self.paths[0]),self.paths[0].stat().st_size))
        self.db.execute('INSERT INTO issues_files VALUES(1,1,1)')
        plan=self.preview()['plans'][0]
        self.assertEqual(plan['status'],'no_changes')
        self.assertEqual(plan['issue_ids'],[1])

    def test_review_never_reruns_publication_identification(self):
        self.write(1,number='2');self.preview()
        with patch('backend.implementations.identification.identify',side_effect=AssertionError('Publication is fixed')):
            self.assertEqual(self.review()['volume_id'],1)
            self.review(issue_ids=[1])

    def test_changed_source_cannot_be_retained_with_older_parsed_evidence(self):
        from backend.base.acquisition_intake import IntakeFailure
        from backend.features.local_organization import retain_preview
        self.preview();session=_sessions[self.value['id']]
        with self.paths[0].open('ab') as stream:stream.write(b'changed')
        with self.assertRaises(IntakeFailure):
            retain_preview(self.database,session.roots,session.batch,volume_id=1)
