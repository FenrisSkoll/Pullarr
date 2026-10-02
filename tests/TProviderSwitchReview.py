"""Offline reviewed-session fixtures: never an authority application."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from backend.base.content_claims import ClaimKind, PublicationRef
from backend.base.definitions import SpecialVersion
from backend.base.provider_switch import ProviderReference
from backend.base.switch_review import SwitchReviewError
from backend.features.provider_switch_review import ProviderSwitchReviews
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata)
from backend.implementations.metadata.switch_target import admit
from backend.internals.classification_provenance import apply, control
from backend.internals.content_claims import (apply_coverage, claim_preview,
                                              confirm_claim, coverage_preview)
from backend.internals.db import DB_SCHEMA


def target(provider='metron', count=1):
    return VolumeMetadata(provider, '700', 'Target', 2020, 1, None, None,
        None, None, [], None, count, False,
        [IssueMetadata(provider, str(701 + i), '700', str(i + 1), float(i + 1),
                       None, '2020-01-01', None) for i in range(count)])


class SwitchReviewTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.addCleanup(self.db.close)
        self.db.executescript(DB_SCHEMA)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute("INSERT INTO root_folders(id,folder) VALUES(1,'/fixture')")
        self.db.execute("""INSERT INTO volumes(id,title,root_folder,folder,metadata_provider)
            VALUES(1,'Source',1,'/fixture/source','gcd')""")
        self.db.execute("INSERT INTO volume_external_ids(volume_id,provider,provider_id,provenance) VALUES(1,'gcd','100','fixture')")
        self.db.execute("INSERT INTO issues(id,volume_id,issue_number,monitored) VALUES(1,1,'[nn]',1)")
        self.db.execute("INSERT INTO issue_external_ids VALUES(1,'gcd','101','fixture')")
        self.db.commit()
        self.cursor = self.db.cursor()
        self.clock = 0
        self.remote = target()
        async def acquire(reference):
            return admit(VolumeFetchResult(self.remote, ()), reference)
        self.service = ProviderSwitchReviews(acquire=acquire, task_observer=lambda _: (), clock=lambda: self.clock)

    def create(self):
        return asyncio.run(self.service.create(self.cursor, 1, self.remote.provider, self.remote.provider_id))

    def test_create_revise_no_domain_mutation(self):
        before = tuple(self.db.iterdump())
        session = self.create()
        self.assertFalse(session.preview.view()['correspondence_complete'])
        revised = self.service.revise(self.cursor, session.id, 1, {1: '701'})
        self.assertTrue(revised.preview.view()['correspondence_complete'])
        self.assertTrue(revised.preview.view()['apply_available'])
        self.assertNotEqual(session.preview.view()['mapping_digest'], revised.preview.view()['mapping_digest'])
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(self.service.get(self.cursor, session.id).revision, 2)
        with self.assertRaisesRegex(SwitchReviewError, 'stale_review_revision'):
            self.service.revise(self.cursor, session.id, 1, {})

    def test_snapshot_owned_immutable_and_opaque_session(self):
        session = self.create()
        self.remote.title = 'Changed after admission'
        self.assertEqual(session.target.data.view()['volume']['title'], 'Target')
        detached = session.target.data.view()
        detached['volume']['title'] = 'Changed view'
        self.assertEqual(session.target.data.view()['volume']['title'], 'Target')
        with self.assertRaises(FrozenInstanceError):
            session.revision = 10
        self.assertGreaterEqual(len(session.id), 40)

    def test_expiry(self):
        session = self.create()
        self.clock = 900
        with self.assertRaisesRegex(SwitchReviewError, 'expired'):
            self.service.get(self.cursor, session.id)

    def test_stale_metadata(self):
        session = self.create()
        self.db.execute("UPDATE issues SET title='Changed' WHERE id=1")
        self.db.commit()
        with self.assertRaisesRegex(SwitchReviewError, 'stale_local_state'):
            self.service.get(self.cursor, session.id)

    def test_authority_aba_after_review_is_stale(self):
        session = self.create()
        self.assertEqual(session.preview.view()['source_generation'], 0)
        # Simulated future successful transitions; not the switch apply service.
        self.db.execute("UPDATE volumes SET metadata_provider='metron',authority_generation=1 WHERE id=1")
        self.db.execute("UPDATE volumes SET metadata_provider='gcd',authority_generation=2 WHERE id=1")
        self.db.commit()
        with self.assertRaisesRegex(SwitchReviewError, 'stale_local_state'):
            self.service.get(self.cursor, session.id)

    def test_authority_aba_during_target_acquisition_is_rejected(self):
        async def acquire(reference):
            admitted = admit(VolumeFetchResult(self.remote, ()), reference)
            self.db.execute("UPDATE volumes SET metadata_provider='metron',authority_generation=1 WHERE id=1")
            self.db.execute("UPDATE volumes SET metadata_provider='gcd',authority_generation=2 WHERE id=1")
            self.db.commit()
            return admitted
        self.service.acquire = acquire
        with self.assertRaisesRegex(SwitchReviewError, 'source_changed_during_fetch'):
            self.create()
        self.assertEqual(len(self.service._sessions), 0)

    def test_target_count_and_duplicate_rejected(self):
        self.remote.issue_count = 2
        with self.assertRaisesRegex(SwitchReviewError, 'incomplete_target'):
            self.create()
        self.remote.issues.append(self.remote.issues[0])
        with self.assertRaisesRegex(SwitchReviewError, 'duplicate_or_incomplete'):
            self.create()

    def test_six_provider_pair_previews(self):
        for source in ('comicvine', 'metron', 'gcd'):
            for destination in ('comicvine', 'metron', 'gcd'):
                if source == destination:
                    continue
                with self.subTest(source=source, target=destination):
                    self.db.execute('UPDATE volumes SET metadata_provider=? WHERE id=1', (source,))
                    self.db.execute('DELETE FROM volume_external_ids')
                    self.db.execute('DELETE FROM issue_external_ids')
                    self.db.execute('INSERT INTO volume_external_ids(volume_id,provider,provider_id,provenance) VALUES(1,?,?,?)',
                                    (source, '100', 'fixture'))
                    self.db.execute('INSERT INTO issue_external_ids VALUES(1,?,?,?)', (source, '101', 'fixture'))
                    self.db.commit()
                    self.remote = target(destination, 2)
                    original = tuple(self.db.iterdump())
                    session = self.create()
                    preview = self.service.revise(self.cursor, session.id, 1, {1: '701'}).preview.view()
                    self.assertEqual(preview['blockers'], [])
                    self.assertEqual(len(preview['target_only']), 1)
                    self.assertEqual(preview['issues'][0]['local']['id'], 1)
                    self.assertEqual(tuple(self.db.iterdump()), original)

    def test_fifo_capacity_size_and_delete(self):
        self.service.max_sessions = 2
        first, second, third = self.create(), self.create(), self.create()
        with self.assertRaisesRegex(SwitchReviewError, 'unavailable'):
            self.service.get(self.cursor, first.id)
        self.assertEqual(self.service.get(self.cursor, second.id), second)
        self.service.delete(third.id)
        self.service.session_bytes = 10
        with self.assertRaisesRegex(SwitchReviewError, 'session_size_limit'):
            self.create()

    def test_locked_classification_is_separate_from_candidate(self):
        apply(self.cursor, 1, SpecialVersion.HARD_COVER)
        control(self.cursor, 1, True)
        self.db.commit()
        before = tuple(self.db.iterdump())
        preview = self.create().preview.view()['classification']
        self.assertEqual(preview['future_action'], 'preserve_locked_value_and_receipt')
        self.assertFalse(preview['evaluation_is_application'])
        self.assertEqual(preview['current']['provenance']['status'], 'recorded')
        self.assertEqual(preview['current']['provenance']['application_kind'], 'explicit_selection')
        self.assertEqual(preview['current']['last_control_action']['action'], 'lock')
        self.assertNotEqual(preview['current']['stored']['value'], preview['target_unlocked_evaluation']['value'])
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_irrelevant_config_does_not_stale(self):
        session = self.create()
        self.db.execute("INSERT INTO config(key,value) VALUES('unrelated-test','changed')")
        self.db.commit()
        self.assertEqual(self.service.get(self.cursor, session.id), session)

    def test_manual_wrong_id_and_types_rejected(self):
        session = self.create()
        for mapping in ({1: 'not admitted'}, {2: '701'}, {'1': '701'}, {True: '701'}):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                self.service.revise(self.cursor, session.id, 1, mapping)

    def test_exact_existing_target_and_direct_files(self):
        self.db.execute("INSERT INTO issue_external_ids VALUES(1,'metron','701','fixture')")
        self.db.execute("INSERT INTO files VALUES(1,'/fixture/book.cbz',20)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
        self.db.commit()
        preview = self.create().preview.view()
        self.assertTrue(preview['correspondence_complete'])
        self.assertEqual(preview['issues'][0]['direct_files'], [1])
        self.assertEqual(preview['effects'], dict(moves=0, renames=0, comicinfo_writes=0, library_writes=0))

    def test_active_jobs_and_terminal_history(self):
        self.db.execute("""INSERT INTO organization_jobs(id,plan_digest,executor_version,intent,intent_digest,state,created_at,updated_at)
            VALUES('job','digest','v1','{"volume_id":1}','digest','running','now','now')""")
        self.db.commit()
        self.assertIn('active_organization', self.create().preview.view()['blockers'])
        self.db.execute("UPDATE organization_jobs SET state='completed'")
        self.db.commit()
        self.assertNotIn('active_organization', self.create().preview.view()['blockers'])

    def test_bound_ten_thousand(self):
        self.remote.issue_count = 10001
        with self.assertRaisesRegex(SwitchReviewError, 'target_issue_limit'):
            self.create()

    def collected_fixture(self):
        self.db.execute("""INSERT INTO volumes(id,title,root_folder,folder,metadata_provider,monitored)
            VALUES(2,'Source series',1,'/fixture/series','gcd',1)""")
        self.db.execute("INSERT INTO issues(id,volume_id,issue_number,monitored) VALUES(2,2,'1A',1)")
        self.db.execute("INSERT INTO issue_external_ids VALUES(2,'gcd','102','fixture')")
        self.db.execute("INSERT INTO files VALUES(1,'/fixture/collection.cbz',20)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
        ref = PublicationRef('gcd', '102')
        preview = claim_preview(self.cursor, 1, ref, ClaimKind.COMPLETE, manual=True)
        claim = confirm_claim(self.cursor, 1, ref, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        preview = coverage_preview(self.cursor, 1, 1, [claim])
        apply_coverage(self.cursor, 1, 1, [claim], preview['preview_token'])
        self.db.commit()
        return claim

    def test_coverage_rebind_only_preview_and_wanted_consequence(self):
        claim = self.collected_fixture()
        before = tuple(self.db.iterdump())
        session = self.create()
        self.assertIn('content_claim_rebind_blocked', session.preview.view()['blockers'])
        result = self.service.revise(self.cursor, session.id, 1, {1: '701'}).preview.view()
        effect = result['content']['claims'][0]
        self.assertEqual(effect['claim_id'], claim)
        self.assertEqual(effect['future_action'], 'supersede_preserving_evidence')
        self.assertEqual(len(effect['valid_coverage']), 1)
        ownership = result['content']['ownership'][0]
        self.assertFalse(ownership['owned_without_rebinding'])
        self.assertTrue(ownership['owned_after_exact_rebinding'])
        self.assertTrue(ownership['wanted_without_rebinding'])
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_claim_change_stales_review(self):
        self.collected_fixture()
        session = self.create()
        self.db.execute('UPDATE bibliographic_content_claims SET retired_at=1')
        self.db.commit()
        with self.assertRaisesRegex(SwitchReviewError, 'stale_local_state'):
            self.service.get(self.cursor, session.id)

    def test_active_download_and_intake(self):
        self.db.execute("""INSERT INTO acquisition_downloads
            (id,intent_digest,intent,client_id,client_instance,state,created_at,updated_at)
            VALUES('download','digest','{"volume_id":1,"issue_ids":[1]}','fixture','fixture','ambiguous','now','now')""")
        self.db.execute("""INSERT INTO acquisition_intakes
            (id,kind,download_id,completion,completion_digest,rename,auto_apply,state,created_at,updated_at)
            VALUES('intake','sab','download','{"volume_id":1,"issue_ids":[1]}','digest',0,0,'review','now','now')""")
        self.db.commit()
        blockers = self.create().preview.view()['blockers']
        self.assertIn('active_download', blockers)
        self.assertIn('active_intake', blockers)

    def test_foreign_target_owner_conflict(self):
        self.db.execute("INSERT INTO volumes(id,title,root_folder,folder) VALUES(2,'Other',1,'/fixture/other')")
        self.db.execute("INSERT INTO issues(id,volume_id,issue_number) VALUES(2,2,'1')")
        self.db.execute("INSERT INTO issue_external_ids VALUES(2,'metron','701','fixture')")
        self.db.commit()
        self.assertIn('target_issue_identity_conflict', self.create().preview.view()['blockers'])

    def test_serialized_target_size_bound(self):
        self.remote.description = 'x' * (17 * 1024 * 1024)
        with self.assertRaisesRegex(SwitchReviewError, 'size_limit'):
            self.create()

    def test_concurrent_revision_one_winner(self):
        session = self.create()
        def revise(_):
            try:
                self.service.revise(self.db.cursor(), session.id, 1, {1: '701'})
                return 'revised'
            except SwitchReviewError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(revise, range(2))), ['revised', 'stale_review_revision'])

    def test_bibliography_and_graph_are_retained_historical(self):
        self.db.execute("INSERT INTO volume_bibliography(volume_id,provider,policy,binding) VALUES(1,'gcd','fixture','Hardcover')")
        self.db.execute("""INSERT INTO bibliographic_graph_snapshots VALUES
            ('snapshot','gcd','fixture','fixture','fingerprint',1,1,0,0,0,0,'digest')""")
        self.db.execute("INSERT INTO bibliographic_issue_refs VALUES('gcd','101','100','Source','[nn]','',0,'snapshot')")
        self.db.commit()
        before = tuple(self.db.iterdump())
        result = self.create().preview.view()
        self.assertTrue(result['bibliography']['old_evidence_becomes_historical'])
        self.assertEqual(result['bibliography']['retained']['volume'][0]['provider'], 'gcd')
        self.assertTrue(result['graph']['gcd_selected_mapping_removed'])
        self.assertFalse(result['graph']['cross_provider_remap'])
        self.assertEqual(tuple(self.db.iterdump()), before)

    def test_query_bound_thousand_issues(self):
        self.db.executemany('INSERT INTO issues(id,volume_id,issue_number) VALUES(?,1,?)',
                            ((i, str(i)) for i in range(2, 1001)))
        self.db.executemany("INSERT INTO issue_external_ids VALUES(?,'gcd',?,'fixture')",
                            ((i, str(100 + i)) for i in range(2, 1001)))
        self.db.commit()
        self.remote = target(count=1000)
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            result = self.create()
        finally:
            self.db.set_trace_callback(None)
        selects = sum(s.lstrip().upper().startswith('SELECT') for s in statements)
        self.assertLess(selects, 100)
        self.assertEqual(len(result.preview.view()['issues']), 1000)

    def test_wanted_active_reservation_and_decision(self):
        self.db.execute("""INSERT INTO wanted_searches
            (id,volume_id,issue_ids,trigger,state,selection_policy,started_at)
            VALUES('search',1,'[1]','manual','complete','fixture',1)""")
        self.db.execute("""INSERT INTO wanted_decisions VALUES
            ('decision','search','operator','candidate','fixture','fixture','eval','score','selection','fixture',
             '[1]','Title','review','nzb',NULL,NULL,1,1,'missing')""")
        self.db.execute("INSERT INTO wanted_reservations(decision_id,issue_id) VALUES('decision',1)")
        self.db.commit()
        result = self.create().preview.view()
        self.assertIn('active_wanted_reservation', result['blockers'])
        self.assertIn('active_wanted_decision', result['blockers'])

    def test_volume_target_conflict(self):
        self.db.execute("INSERT INTO volume_external_ids(volume_id,provider,provider_id,provenance) VALUES(1,'metron','999','fixture')")
        self.db.commit()
        self.assertIn('established_target_volume_conflict', self.create().preview.view()['blockers'])

    def test_pending_transaction_not_committed(self):
        self.db.execute("UPDATE volumes SET title='Pending' WHERE id=1")
        with self.assertRaisesRegex(SwitchReviewError, 'pending_transaction'):
            self.create()
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()

    def test_invalid_session_and_observed_task_staleness(self):
        session = self.create()
        with self.assertRaisesRegex(SwitchReviewError, 'unavailable'):
            self.service.get(self.cursor, 'not-a-session')
        self.service.task_observer = lambda _: ((1, 'refresh', 1),)
        with self.assertRaisesRegex(SwitchReviewError, 'stale_local_state'):
            self.service.get(self.cursor, session.id)

    def test_provider_identity_change_during_acquisition(self):
        async def acquire(reference):
            self.db.execute("UPDATE volume_external_ids SET provider_id='changed' WHERE volume_id=1")
            self.db.commit()
            return admit(VolumeFetchResult(self.remote, ()), reference)
        self.service.acquire = acquire
        with self.assertRaisesRegex(SwitchReviewError, 'source_changed_during_fetch'):
            self.create()

    def test_disposable_comic_unchanged(self):
        with TemporaryDirectory() as directory:
            comic = Path(directory) / 'synthetic.cbz'
            comic.write_bytes(b'synthetic comic fixture, not a real archive')
            self.db.execute('INSERT INTO files VALUES(1,?,?)', (str(comic), comic.stat().st_size))
            self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
            self.db.commit()
            original, stamp = comic.read_bytes(), comic.stat().st_mtime_ns
            session = self.create()
            self.service.revise(self.cursor, session.id, 1, {1: '701'})
            self.service.get(self.cursor, session.id)
            self.assertEqual(comic.read_bytes(), original)
            self.assertEqual(comic.stat().st_mtime_ns, stamp)
            self.assertEqual(list(Path(directory).iterdir()), [comic])

    def test_source_claim_endpoint_preview(self):
        self.collected_fixture()
        self.db.execute("INSERT INTO volume_external_ids(volume_id,provider,provider_id,provenance) VALUES(2,'gcd','200','fixture')")
        self.db.commit()
        session = asyncio.run(self.service.create(self.cursor, 2, 'metron', '700'))
        result = self.service.revise(self.cursor, session.id, 1, {2: '701'}).preview.view()
        claim = result['content']['claims'][0]
        self.assertEqual(set(claim['endpoints']), {'source'})
        self.assertEqual(claim['blockers'], [])
        self.assertTrue(result['content']['ownership'][0]['owned_after_exact_rebinding'])

    def test_cross_volume_organizer_restore_dependency(self):
        self.db.execute("""INSERT INTO organization_jobs(id,plan_digest,executor_version,intent,intent_digest,state,created_at,updated_at)
            VALUES('job','digest','v1','{"volume_id":2,"database_restore":{"file":{"links":[[1,1,0]]}}}',
                   'digest','recovery_required','now','now')""")
        self.db.commit()
        self.assertIn('active_organization', self.create().preview.view()['blockers'])
