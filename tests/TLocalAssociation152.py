"""DB-only managed associations and independent preview capacity."""
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

import TLocalIssueReview as review_fixture

from backend.features.local_organization import (_sessions, apply_preview,
                                                 scan_preview)


class Association152Tests(TestCase):
    setUp=review_fixture.LocalIssueReviewTests.setUp
    write=review_fixture.LocalIssueReviewTests.write
    preview=review_fixture.LocalIssueReviewTests.preview
    def test_catalog_change_makes_receipt_stale(self):
        from backend.features.local_issue_review import LocalReviewError
        value=self.preview()
        self.db.execute("UPDATE issues SET title='Changed catalog' WHERE id=1")
        with self.assertRaisesRegex(LocalReviewError,'stale_preview'):
            apply_preview(self.database,value['id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0],0)

    def test_failed_apply_does_not_poison_later_preview(self):
        from backend.features.local_issue_review import LocalReviewError
        value=self.preview()
        self.write(1,number='2')
        with self.assertRaisesRegex(LocalReviewError,'stale_preview'):
            apply_preview(self.database,value['id'])
        self.assertEqual([p['status'] for p in self.preview()['plans']],['review_required','ready','ready'])

    def test_unrelated_invalid_identity_does_not_poison_managed_scan(self):
        self.db.execute("INSERT INTO volumes(id,title,metadata_provider,root_folder,folder) VALUES(99,'Unrelated','metron',1,'')")
        self.assertEqual([p['status'] for p in self.preview()['plans']],['ready']*3)

    def test_black_road_real_parser_and_repeat(self):
        for p in self.paths:p.unlink()
        self.db.execute("UPDATE volumes SET title='Black Road: The Holy North'")
        self.db.execute('DELETE FROM issues WHERE id<>1')
        self.db.execute("UPDATE issues SET title='Volume One'")
        p=self.fixture.folder/'Black Road - The Holy North (2016) - v001.cbz'
        with ZipFile(p,'w') as z:
            z.writestr('page.jpg',b'synthetic')
            z.writestr('ComicInfo.xml','<ComicInfo><Series>Black Road</Series><Title>The Holy North</Title><Volume>1</Volume></ComicInfo>')
        value=self.preview()
        self.assertEqual(value['plans'][0]['status'],'ready',value)
        apply_preview(self.database,value['id'])
        self.assertEqual(self.preview()['plans'][0]['status'],'no_changes')
    def test_abe_ready_apply_without_organizer_or_archive_reopen(self):
        for p in self.paths: p.unlink()
        self.db.execute("UPDATE volumes SET title='Abe Sapien: Dark and Terrible'")
        self.db.execute("DELETE FROM issues WHERE id=3")
        self.db.execute("UPDATE issues SET title='Volume ' || issue_number")
        paths=[]
        for n in (1,2):
            p=self.fixture.folder/f'Abe Sapien - Dark and Terrible (2017) - v{n:03}.cbz'
            with ZipFile(p,'w') as z:
                z.writestr('page.jpg',b'synthetic')
                z.writestr('ComicInfo.xml',f'<ComicInfo><Series>Abe Sapien: Dark and Terrible</Series><Volume>{n}</Volume><Title>Volume {n}</Title></ComicInfo>')
            paths.append(p)
        before=[p.read_bytes() for p in paths]
        value=scan_preview(self.database,1)
        self.assertEqual([p['status'] for p in value['plans']],['ready','ready'],value)
        with patch('backend.features.local_organization.OrganizationExecutor',side_effect=AssertionError('No jobs')), \
             patch('backend.implementations.comicinfo_archive.inspect_comicinfo',side_effect=AssertionError('No archive reopen')), \
             patch('backend.features.quality.analyze',side_effect=AssertionError('No quality work')):
            applied=apply_preview(self.database,value['id'])
            self.assertEqual(len(applied['jobs']),2)
            self.assertTrue(apply_preview(self.database,value['id'])['replay'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues_files').fetchone()[0],2)
        fresh=scan_preview(self.database,1)
        self.assertEqual([p['status'] for p in fresh['plans']],['no_changes','no_changes'],fresh)
        self.assertEqual([p.read_bytes() for p in paths],before)

    def test_capacity_does_not_poison_later_scan(self):
        for _ in range(25):
            value=self.preview()
            self.assertEqual(len(value['plans']),3)
        self.assertLessEqual(len(_sessions),16)

    def test_malformed_archive_is_one_row(self):
        self.paths[0].write_bytes(b'not an archive')
        value=self.preview()
        self.assertNotEqual(value['plans'][0]['status'],'ready')
        self.assertEqual([p['status'] for p in value['plans'][1:]],['ready','ready'])
