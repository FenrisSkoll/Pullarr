"""Real loopback source/client through Wanted, intake and verified replacement."""
from unittest import TestCase
from unittest.mock import patch

import TQualityUpgrade as quality_fixture
from fixtures.expanded_clients import configure, services

from backend.features.direct_downloads import load_target
from backend.features.intake_runtime import IntakeRuntime
from backend.features.sab_downloads import poll_downloads
from backend.features.wanted_automation import WantedAutomation
from backend.features.wanted_search import UnifiedReleaseSearch
from backend.implementations.managed_clients import client_for
from backend.internals.download_jobs import DownloadStore
from backend.internals.release_sources import load_sources


class ExpandedPipelineTests(TestCase):
    def workflow(self, false_hd=False, after=None, kind='qbittorrent', failed=False, profile='legacy'):
        fixture = quality_fixture.UpgradeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        db = fixture.db
        db.execute("UPDATE config SET value=72 WHERE key='database_version'")
        if false_hd:
            quality_fixture.comic(fixture.h.source, 900)
        before, seed = fixture.old.read_bytes(), fixture.h.source.read_bytes()
        with services(profile) as remote, patch('backend.internals.release_sources.get_db',side_effect=db.cursor), \
                patch('backend.features.direct_downloads.get_db',side_effect=db.cursor), \
                patch('backend.internals.identification.get_db',side_effect=db.cursor):
            client = configure(db, remote, fixture.h.incoming, kind=kind)
            searches = UnifiedReleaseSearch(target_loader=load_target,nzb_loader=load_sources,ddl_loader=lambda:{})
            self.addCleanup(searches.close_all)
            service = WantedAutomation(fixture.h.dbpath,searches=searches)
            self.addCleanup(service.close)
            result = service.run_target(1,1,allow_grab=True)
            self.assertEqual(result['state'],'tracking', (result,db.execute('SELECT error FROM wanted_searches').fetchall()))
            remote['completed'] = True
            if failed:
                remote['nzb_status'] = 'FAILURE/UNPACK'
            downloads = DownloadStore(fixture.h.dbpath)
            try:
                poll_downloads(downloads,[client],client_factory=client_for)
            finally:
                downloads.close()
            runtime = IntakeRuntime(fixture.h.dbpath,clock=lambda:100.)
            for stamp in (100.,111.,122.,133.,144.):
                runtime.clock = lambda stamp=stamp: stamp
                runtime.tick()
            if kind == 'qbittorrent' or failed or false_hd:
                self.assertEqual(fixture.h.source.read_bytes(),seed)
            self.assertEqual(remote['submitted'],1)
            if failed:
                self.assertEqual(fixture.old.read_bytes(),before)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM acquisition_downloads WHERE state='failed'").fetchone()[0],1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM acquisition_intakes').fetchone()[0],0)
            elif false_hd:
                self.assertEqual(fixture.old.read_bytes(),before)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM acquisition_provenance WHERE state='rejected' AND error='dimension_floor_failed'").fetchone()[0],1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM quality_rejections').fetchone()[0],1)
                service.run_target(1,1,allow_grab=True)
                self.assertEqual(remote['submitted'],1)
            else:
                self.assertEqual(fixture.old.read_bytes(),seed)
                self.assertTrue(fixture.store.issue_states([1])[0]['cutoff_satisfied'])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM acquisition_provenance WHERE state='imported' AND client_kind=?",(kind,)).fetchone()[0],1)
            if after:
                after(fixture, client, remote)

    def test_true_upgrade_preserves_seeding_payload(self):
        self.workflow()

    def test_false_hd_preserves_current_and_suppresses_retry(self):
        self.workflow(True)

    def test_nzbget_shared_upgrade_pipeline(self):
        self.workflow(kind='nzbget')

    def test_nzbget_failed_history_never_imports(self):
        self.workflow(kind='nzbget', failed=True)

    def test_ratio_cleanup_and_library_survival(self):
        import json
        from dataclasses import asdict, replace

        from backend.base.managed_client import RetentionPolicy
        from backend.features.torrent_lifecycle import (cleanup,
                                                        cleanup_preview,
                                                        observe_torrents)
        def after(fixture, config, remote):
            config = replace(config, retention=RetentionPolicy('ratio', '.5', 60))
            fixture.db.execute('UPDATE acquisition_torrents SET policy=?', (json.dumps(asdict(config.retention)),))
            store = DownloadStore(fixture.h.dbpath)
            try:
                identifier = fixture.db.execute('SELECT download_id FROM acquisition_torrents').fetchone()[0]
                observe_torrents(store, [config], client_for, clock=lambda: 100.)
                preview = cleanup_preview(store, identifier, delete_data=True, clock=lambda: 100.)
                self.assertFalse(preview['eligible'])
                self.assertIn('tracker_requirements', preview['reasons'])
                remote['ratio'] = 1.
                observe_torrents(store, [config], client_for, clock=lambda: 100.)
                preview = cleanup_preview(store, identifier, delete_data=True, clock=lambda: 100.)
                self.assertTrue(preview['eligible'], preview)
                result = cleanup(store, identifier, config, client_for(config), preview['confirmation'], delete_data=True, clock=lambda: 100.)
                self.assertEqual(result['state'], 'removed_data')
                self.assertTrue(remote['delete_data'])
                expected = fixture.old.read_bytes()
                fixture.h.source.unlink()  # Simulate the fixture client's exact source removal.
                self.assertEqual(fixture.old.read_bytes(), expected)
                self.assertTrue(fixture.store.issue_states([1])[0]['cutoff_satisfied'])
            finally:
                store.close()
        self.workflow(after=after)

    def test_cleanup_revalidates_policy_library_and_client(self):
        from backend.base.download_job import DownloadFailure
        from backend.features.torrent_lifecycle import (cleanup,
                                                        cleanup_preview,
                                                        observe_torrents)

        def after(fixture, config, remote):
            store = DownloadStore(fixture.h.dbpath)
            try:
                identifier = fixture.db.execute('SELECT download_id FROM acquisition_torrents').fetchone()[0]
                remote['ratio'] = 1.
                observe_torrents(store, [config], client_for, clock=lambda: 100.)
                review = cleanup_preview(store, identifier, delete_data=True, manual=True, clock=lambda: 100.)
                self.assertTrue(review['eligible'], review)
                stale = cleanup_preview(store, identifier, delete_data=True, manual=True, clock=lambda: 161.)
                self.assertIn('fresh_client_observation_required', stale['reasons'])
                remote['ratio'] = .5
                with self.assertRaises(DownloadFailure):
                    cleanup(store, identifier, config, client_for(config), review['confirmation'],
                            delete_data=True, manual=True, clock=lambda: 100.)
                self.assertFalse(remote['removed'])
                remote['ratio'] = 1.
                observe_torrents(store, [config], client_for, clock=lambda: 100.)
                original = fixture.old.read_bytes()
                fixture.old.unlink()  # External replacement must not touch seeded bytes.
                fixture.old.write_bytes(b'changed library identity')
                changed = cleanup_preview(store, identifier, delete_data=True, manual=True, clock=lambda: 100.)
                self.assertIn('library_identity_changed', changed['reasons'])
                self.assertEqual(fixture.h.source.read_bytes(), original)
                fixture.old.write_bytes(original)
                remote['removed'] = True  # External removal retains library ownership.
                observe_torrents(store, [config], client_for, clock=lambda: 100.)
                missing = cleanup_preview(store, identifier, delete_data=True, manual=True, clock=lambda: 100.)
                self.assertIn('client_relationship_changed', missing['reasons'])
                self.assertTrue(fixture.store.issue_states([1])[0]['cutoff_satisfied'])
            finally:
                store.close()
        self.workflow(after=after)

    def test_restart_exact_hash_recovery_without_resubmission(self):
        from backend.features.torrent_lifecycle import recover_submissions
        def after(fixture, config, remote):
            identifier = fixture.db.execute('SELECT download_id FROM acquisition_torrents').fetchone()[0]
            fixture.db.execute("UPDATE acquisition_downloads SET nzo_id=NULL,state='ambiguous' WHERE id=?",(identifier,))
            store = DownloadStore(fixture.h.dbpath)
            try:
                recover_submissions(store,[config],client_for)
                recovered = store.get(identifier)
                self.assertEqual(recovered['state'],'submitted')
                self.assertEqual(recovered['nzo_id'],remote['hash'])
                recover_submissions(store,[config],client_for)
                self.assertEqual(remote['submitted'],1)
                poll_downloads(store,[config],client_factory=client_for)
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM acquisition_intakes').fetchone()[0],1)
            finally:
                store.close()
        self.workflow(after=after)


class ModernExpandedPipelineTests(ExpandedPipelineTests):
    """Repeat acquisition, recovery and reviewed cleanup with WebAPI 2.15.1."""
    def workflow(self, *args, **kwargs):
        return super().workflow(*args, **kwargs, profile='5.2.4')
