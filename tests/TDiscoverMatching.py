"""Same canonical matching and quality projection, no global identity writes."""

from unittest.mock import patch

from TDiscover import DiscoverFixture, parse_feed, rss
from TQuality import policy

from backend.base.content_claims import ClaimKind, PublicationRef
from backend.features.discovery_matching import project
from backend.internals.content_claims import (apply_coverage, claim_preview,
                                              confirm_claim, coverage_preview)
from backend.internals.quality import QualityStore


class DiscoveryMatchingTests(DiscoverFixture):
    def test_c2_is_not_direct_owned_or_upgrade_and_does_not_mutate_claims(self):
        self.seed()
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder) VALUES(2,900,'Omnibus',1,'omnibus')")
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number) VALUES(2,2,901,'1')")
        self.db.execute("INSERT INTO files(id,filepath,size) VALUES(1,'omnibus.cbz',1)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,2)')
        self.db.commit()
        preview=claim_preview(self.db.cursor(),2,PublicationRef('comicvine','102'),ClaimKind.COMPLETE,manual=True)
        claim=confirm_claim(self.db.cursor(),2,PublicationRef('comicvine','102'),ClaimKind.COMPLETE,preview['preview_token'],manual=True)
        coverage=coverage_preview(self.db.cursor(),2,1,[claim])
        apply_coverage(self.db.cursor(),2,1,[claim],coverage['preview_token'])
        before=list(self.db.iterdump())
        value=project(self.db.cursor(),parse_feed(rss([(1,'One #1 (2026)')])))[0]
        self.assertFalse(value['local']['direct_owned'])
        self.assertTrue(value['local']['content_elsewhere'])
        self.assertNotIn(value['interest'],('upgrade','satisfied'))
        self.assertEqual(before,list(self.db.iterdump()))

    def seed(self):
        self.db.execute("INSERT INTO root_folders VALUES(1,'fixture-root')")
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,year,root_folder,folder,monitored) VALUES(1,101,'One',2026,1,'fixture-volume',1)")
        self.db.execute("INSERT INTO issues(id,comicvine_id,volume_id,issue_number,calculated_issue_number,monitored) VALUES(1,102,1,'1',1,1)")
        self.db.commit()

    def test_matched_missing_then_owned_monitoring_and_metadata_repair(self):
        self.seed()
        posts=parse_feed(rss([(1,'One #1 (2026)')]))
        value=project(self.db.cursor(),posts)[0]
        self.assertEqual(value['match'],'matched')
        self.assertEqual(value['interest'],'missing')
        self.assertEqual(value['quality']['result'],'provisional')
        self.db.execute('UPDATE issues SET monitored=0')
        self.assertEqual(project(self.db.cursor(),posts)[0]['interest'],'in_library')
        self.db.execute("INSERT INTO files(id,filepath,size) VALUES(1,'fixture.cbz',12)")
        self.db.execute('INSERT INTO issues_files(issue_id,file_id) VALUES(1,1)')
        self.assertEqual(project(self.db.cursor(),posts)[0]['interest'],'satisfied')
        self.db.execute("UPDATE volumes SET title='Changed'")
        self.assertEqual(project(self.db.cursor(),posts)[0]['match'],'unmatched')

    def test_ambiguous_bundle_nonrelease_and_no_side_effects(self):
        self.seed()
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,year,root_folder,folder,monitored) VALUES(2,201,'One',2026,1,'fixture-other',1)")
        self.db.execute("INSERT INTO issues(id,comicvine_id,volume_id,issue_number,calculated_issue_number,monitored) VALUES(2,202,2,'1',1,1)")
        self.db.commit()
        posts=parse_feed(rss([(1,'One #1 (2026)'),(2,'One #1-5 (2026)'),(3,'2026.09.30 Weekly Pack'),(4,'One Omnibus (2026)')]))
        before=list(self.db.iterdump())
        values=project(self.db.cursor(),posts)
        self.assertEqual([v['match'] for v in values],['ambiguous','bundle','bundle','bundle'])
        self.assertEqual(before,list(self.db.iterdump()))

    def test_quality_classification_exact_parity_and_batch_queries(self):
        self.seed()
        posts=parse_feed(rss([(i,'One #1 (2026) (HD-Digital)') for i in range(50)]))
        queries=[]
        self.db.set_trace_callback(queries.append)
        values=project(self.db.cursor(),posts)
        selects=[q for q in queries if q.lstrip().upper().startswith('SELECT')]
        self.assertLess(len(selects),20)
        self.assertTrue(all(v['claims']['quality_class']=='hd_digital' for v in values))
        self.assertTrue(all(v['match']=='matched' for v in values))

    def test_poll_never_acquires_and_profile_conflict_is_live(self):
        self.seed()
        with patch('backend.features.wanted_automation.WantedAutomation.grab',side_effect=AssertionError('poll must not acquire')):
            self.owner.poll()
        store=QualityStore(self.db.cursor())
        store.save('Opt in',policy())
        before=list(self.db.iterdump())
        self.owner.page()
        self.assertEqual(before,list(self.db.iterdump()))
