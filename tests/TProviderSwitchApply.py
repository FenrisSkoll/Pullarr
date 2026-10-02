"""Applied authority transitions using admitted fixture snapshots, never live HTTP."""

import asyncio
import hashlib
import sqlite3
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from flask import Flask

from backend.base.bibliography import (EditionFacts, IssueBibliography,
                                       PublicationFacts)
from backend.base.content_claims import ClaimKind, PublicationRef
from backend.base.definitions import SpecialVersion
from backend.base.issue_facts import (BibliographicDate, DateKind, IssueFacts,
                                      IssueNumberFacts, VariantOf)
from backend.base.provider_switch import ProviderReference
from backend.base.switch_review import SwitchReviewError
from backend.features.provider_switch_review import ProviderSwitchReviews
from backend.features.wanted_status import wanted_rows
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.format_evidence import (
    PhysicalFormat, ProviderFormatEvidence)
from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata)
from backend.implementations.metadata.snapshot import (ProviderVolumeSnapshot,
                                                       SnapshotIssue,
                                                       SnapshotReceipt)
from backend.implementations.metadata.switch_target import admit
from backend.internals.classification_provenance import (apply as classify,
                                                         control, details)
from backend.internals.content_claims import (apply_coverage, claim_preview,
                                              confirm_claim, coverage_preview)
from backend.internals.db import (DB_SCHEMA, DBConnection,
                                  setup_db_adapters_and_converters)
from backend.internals.issue_facts import mapped_facts
from backend.internals.provider_authority import capture, guarded_stage
from backend.internals.provider_identity import ProviderIdentityDB


def remote(provider, parent='700', first=701, count=2, physical=False):
    volume = VolumeMetadata(provider, parent, 'Target ' + provider, 2020, 7, None, None, 'Target description',
        'https://example.invalid/series', ['Alias'], 'Publisher', count, False,
        [IssueMetadata(provider, str(first + i), parent, str(i + 1), float(i + 1),
                       'Target issue', '2020-01-01', 'Issue description') for i in range(count)])
    if provider == 'gcd':
        volume.issues = None
        issues = tuple(SnapshotIssue(provider, str(first + i), parent, 'Rich issue',
            IssueFacts(IssueNumberFacts.interpret('1A' if i == 0 else '[nn]', 'gcd', 'number'),
                (BibliographicDate.interpret('2020-12-00', DateKind.PUBLICATION, 'gcd', 'date', zero_placeholders=True),), 'date'),
            VariantOf(provider, str(first), 'gcd') if i == 1 else None,
            IssueBibliography('gcd', EditionFacts(barcode='fixture', supplied=('barcode',)), ())) for i in range(count))
        snapshot = ProviderVolumeSnapshot(volume, issues, SnapshotReceipt('fixture', 'membership', 1000, count),
                                          PublicationFacts(binding='Hardcover', supplied=('binding',)))
        return VolumeFetchResult(volume, (), snapshot=snapshot)
    evidence = ProviderFormatEvidence(provider, parent, 'format', 'Hardcover', PhysicalFormat.HARDCOVER) if physical else None
    return VolumeFetchResult(volume, (), format_evidence=evidence)


class SwitchApplyTests(TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory(prefix='kapowarr-switch-apply-')
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'collected.cbz'
        self.path.write_bytes(b'unchanged disposable comic fixture')
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.addCleanup(self.db.close)
        self.db.executescript(DB_SCHEMA)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('INSERT INTO root_folders(id,folder) VALUES(1,?)', (self.folder.name,))
        self.cursor = self.db.cursor()
        self.clock = 0
        self.service = ProviderSwitchReviews(task_observer=lambda _: (), clock=lambda: self.clock)

    def source(self, provider='comicvine', count=2):
        self.db.execute('''INSERT INTO volumes(id,title,root_folder,folder,metadata_provider,comicvine_id,volume_number)
            VALUES(1,'Source',1,?,?,?,3)''', (self.folder.name, provider, 100 if provider == 'comicvine' else None))
        if provider != 'comicvine':
            self.db.execute('INSERT INTO volume_external_ids VALUES(1,?,?,?,NULL)', (provider, '100', 'fixture'))
        for i in range(count):
            self.db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,monitored) VALUES(?,1,?,?,?)',
                            (i + 1, 101 + i if provider == 'comicvine' else None, str(i + 1), i % 2))
            if provider != 'comicvine':
                self.db.execute('INSERT INTO issue_external_ids VALUES(?,?,?,?)', (i + 1, provider, str(101 + i), 'fixture'))
        self.db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)', (str(self.path), self.path.stat().st_size))
        self.db.execute('INSERT INTO issues_files VALUES(1,1,1)')
        self.db.commit()

    def review(self, target, manual=True):
        async def acquire(reference):
            return admit(target, reference)
        self.service.acquire = acquire
        session = asyncio.run(self.service.create(self.cursor, 1, target.metadata.provider, target.metadata.provider_id))
        if manual:
            issues = target.snapshot.issues if target.snapshot else target.metadata.issues
            ids = [r[0] for r in self.db.execute('SELECT id FROM issues WHERE volume_id=1 ORDER BY id')]
            session = self.service.revise(self.cursor, session.id, session.revision,
                                         {iid: issue.provider_id for iid, issue in zip(ids, issues)})
        return session

    def apply(self, session, token=None):
        token = token or capture(self.cursor, (1,))[1]
        with patch('backend.implementations.metadata.switch_target.acquire_target', side_effect=AssertionError('No HTTP in apply')):
            return self.service.apply(self.cursor, session.id, session.revision, session.preview.view()['mapping_digest'],
                                      confirmed=True, expected_authority=token)

    def unchanged_files(self):
        return (hashlib.sha256(self.path.read_bytes()).hexdigest(), self.path.stat().st_mtime_ns,
                sorted(p.name for p in Path(self.folder.name).iterdir()),
                self.db.execute('SELECT * FROM files').fetchall(), self.db.execute('SELECT * FROM issues_files').fetchall(),
                self.db.execute('SELECT folder,root_folder FROM volumes WHERE id=1').fetchone())

    def assert_integrity(self):
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        with patch('backend.internals.provider_identity.get_db', side_effect=self.db.cursor):
            self.assertEqual(ProviderIdentityDB.audit(('comicvine', 'metron', 'gcd')), [])

    def test_all_six_applied_pairs(self):
        for source in ('comicvine', 'metron', 'gcd'):
            for target in ('comicvine', 'metron', 'gcd'):
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    # Separate complete fixture/database for each pair.
                    fixture = SwitchApplyTests()
                    fixture.setUp()
                    try:
                        fixture.source(source)
                        before = fixture.unchanged_files()
                        session = fixture.review(remote(target, count=3))
                        self.assertTrue(session.preview.view()['apply_available'])
                        result = fixture.apply(session)
                        self.assertEqual(fixture.db.execute('SELECT metadata_provider,authority_generation FROM volumes').fetchone(), (target, 1))
                        self.assertEqual(fixture.db.execute('SELECT id,monitored FROM issues ORDER BY id').fetchall(), [(1, 0), (2, 1), (3, 1)])
                        self.assertEqual(fixture.db.execute('SELECT COUNT(*) FROM issue_external_ids WHERE provider=?', (source,)).fetchone()[0], 2)
                        self.assertEqual(result['mapped_count'], 2)
                        self.assertEqual(result['added_count'], 1)
                        self.assertEqual(before, fixture.unchanged_files())
                        self.assertEqual(details(fixture.cursor, 1)['provenance']['status'], 'recorded')
                        if target == 'gcd':
                            self.assertEqual(fixture.db.execute('SELECT calculated_issue_number,date FROM issues WHERE id=1').fetchone(), (None, None))
                            self.assertEqual(fixture.db.execute('SELECT precision FROM issue_date_facts WHERE issue_id=1').fetchone()[0], 'month')
                            self.assertEqual(fixture.db.execute('SELECT base_provider_id FROM issue_variant_of WHERE issue_id=2').fetchone()[0], '701')
                        fixture.assert_integrity()
                    finally:
                        fixture.doCleanups()

    def test_reverse_pairs_and_aba(self):
        for source, target in (('comicvine', 'metron'), ('comicvine', 'gcd'), ('metron', 'gcd')):
            with self.subTest(source=source, target=target):
                fixture = SwitchApplyTests()
                fixture.setUp()
                try:
                    fixture.source(source)
                    original = capture(fixture.cursor, (1,))[1]
                    fixture.apply(fixture.review(remote(target)))
                    fixture.apply(fixture.review(remote(source, '100', 101)))
                    self.assertEqual(capture(fixture.cursor, (1,))[1].generation, 2)
                    self.assertEqual(fixture.db.execute('SELECT id FROM issues ORDER BY id').fetchall(), [(1,), (2,)])
                    self.assertEqual([r['target_generation'] for r in fixture.service.history(fixture.cursor, 1)], [2, 1])
                    with self.assertRaisesRegex(SwitchReviewError, 'stale_metadata_authority'):
                        with guarded_stage(fixture.cursor, (original,)):
                            self.fail('ABA refresh accepted')
                    fixture.assert_integrity()
                finally:
                    fixture.doCleanups()

    def test_retry_after_consumption_expiry_and_restart(self):
        self.source()
        session = self.review(remote('metron', count=3))
        token = capture(self.cursor, (1,))[1]
        first = self.apply(session, token)
        before = tuple(self.db.iterdump())
        self.clock = 10000
        self.service = ProviderSwitchReviews(task_observer=lambda _: ())
        repeated = self.apply(session, token)
        self.assertEqual(first['id'], repeated['id'])
        self.assertTrue(repeated['already_applied'])
        self.assertEqual(before, tuple(self.db.iterdump()))
        detail = self.service.receipt(self.cursor, first['id'], detail=True, limit=2)
        self.assertEqual(len(detail['issues']), 2)
        with self.assertRaises(SwitchReviewError):
            self.service.history(self.cursor, 1, limit=1000)

    def test_stale_revision_digest_expiry_and_state(self):
        self.source()
        session = self.review(remote('metron'))
        token = capture(self.cursor, (1,))[1]
        for revision, digest, confirmed in ((session.revision - 1, session.preview.view()['mapping_digest'], True),
                                          (session.revision, 'wrong', True), (session.revision, 'wrong', False)):
            with self.assertRaises(SwitchReviewError):
                self.service.apply(self.cursor, session.id, revision, digest, confirmed=confirmed, expected_authority=token)
        self.db.execute("UPDATE issues SET title='Manual edit' WHERE id=1")
        self.db.commit()
        before = tuple(self.db.iterdump())
        with self.assertRaisesRegex(SwitchReviewError, 'stale_local_state'):
            self.apply(session, token)
        self.assertEqual(before, tuple(self.db.iterdump()))
        session = self.review(remote('metron'))
        self.clock = 901
        with self.assertRaisesRegex(SwitchReviewError, 'expired'):
            self.apply(session, token)

    def test_locked_historical_receipt_and_unlocked_provider_evidence(self):
        self.source(count=1)
        classify(self.cursor, 1, SpecialVersion.TPB)
        control(self.cursor, 1, True)
        self.db.commit()
        before = details(self.cursor, 1)
        self.apply(self.review(remote('metron', count=1, physical=True)))
        self.assertEqual(details(self.cursor, 1), before)
        control(self.cursor, 1, False)
        self.db.commit()
        self.apply(self.review(remote('comicvine', '100', 101, count=1, physical=True)))
        result = details(self.cursor, 1)['provenance']
        self.assertEqual(result['reason'], 'sole_issue_physical_evidence')
        self.assertEqual(result['applied_value'], SpecialVersion.HARD_COVER.value)

    def content(self, partial=False):
        self.db.execute("INSERT INTO volumes(id,title,root_folder,folder,metadata_provider) VALUES(2,'External source',1,'other','gcd')")
        self.db.execute("INSERT INTO volume_external_ids VALUES(2,'gcd','900','fixture',NULL)")
        self.db.execute("INSERT INTO issues(id,volume_id,issue_number,monitored) VALUES(99,2,'7',1)")
        self.db.execute("INSERT INTO issue_external_ids VALUES(99,'gcd','999','fixture')")
        source = capture(self.cursor, (1,))[1]
        self.db.execute('''INSERT INTO bibliographic_content_claims VALUES('claim',?,?,'gcd','999',?,
            'operator_confirmed','kapowarr-collected-content/v1',1,NULL,NULL)''',
            (source.provider, '101', 'partial_issue_content' if partial else 'complete_issue_containment'))
        self.db.execute("INSERT INTO bibliographic_content_claim_evidence VALUES('claim','gcd','edge','snapshot','999','101',NULL,NULL,1)")
        if not partial:
            self.db.execute("INSERT INTO file_content_coverage VALUES('coverage',1,1,99,'claim',1,1,99,'kapowarr-collected-content/v1',1,NULL)")
        self.db.commit()

    def test_claim_coverage_noncontiguous_cross_volume_and_reverse(self):
        self.source('gcd')
        self.content()
        before = self.unchanged_files()
        first = self.apply(self.review(remote('metron')))
        self.assertEqual((first['claim_count'], first['coverage_count']), (1, 1))
        self.assertEqual(self.db.execute('SELECT issue_id,file_id FROM canonical_issue_files ORDER BY issue_id').fetchall(), [(1, 1), (99, 1)])
        second = self.apply(self.review(remote('gcd', '100', 101)))
        self.assertEqual((second['claim_count'], second['coverage_count']), (1, 1))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bibliographic_content_claims').fetchone()[0], 3)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM file_content_coverage WHERE retired_at IS NULL').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT DISTINCT origin_issue,target_issue FROM bibliographic_content_claim_evidence').fetchall(), [('999', '101')])
        self.assertEqual(before, self.unchanged_files())
        self.assert_integrity()

    def test_partial_claim_does_not_create_coverage(self):
        self.source('gcd')
        self.content(partial=True)
        result = self.apply(self.review(remote('metron')))
        self.assertEqual(result['coverage_count'], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM canonical_issue_files WHERE issue_id=99').fetchone()[0], 0)

    def test_every_injected_apply_stage_rolls_back(self):
        self.source('gcd')
        self.content()
        session = self.review(remote('metron', count=3))
        before, files = tuple(self.db.iterdump()), self.unchanged_files()
        for stage in ('before_mutation', 'identities', 'target_only', 'volume_metadata', 'canonical_facts', 'bibliography',
                      'classification', 'claim_supersession', 'coverage_rebinding', 'authority_generation', 'receipt_details', 'before_commit'):
            with self.subTest(stage=stage):
                def inject(current):
                    if current == stage:
                        raise RuntimeError('injected ' + stage)
                self.service.fault_hook = inject
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    self.apply(session)
                self.assertEqual(before, tuple(self.db.iterdump()))
                self.assertEqual(files, self.unchanged_files())
                self.assert_integrity()

    def test_large_apply_diagnostic(self):
        self.source(count=1000)
        session = self.review(remote('metron', count=1000))
        statements = []
        self.db.set_trace_callback(statements.append)
        tracemalloc.start()
        started = time.monotonic()
        self.apply(session)
        elapsed, peak = time.monotonic() - started, tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.db.set_trace_callback(None)
        selects = sum(s.lstrip().upper().startswith('SELECT') for s in statements)
        self.assertLess(selects, 100)
        print(f'Switch apply 1000 issues: {selects} SELECTs, {len(statements)} traced statements, {elapsed:.3f}s, peak {peak} bytes')

    def test_competing_connections_durable_retry_and_different_targets(self):
        self.source()
        first = self.review(remote('metron'))
        second = self.review(remote('gcd'))
        token = capture(self.cursor, (1,))[1]
        for same in (True, False):
            with self.subTest(same_session=same):
                path = str(self.path.parent / ('race-' + str(same) + '.sqlite'))
                with closing(sqlite3.connect(path)) as disk:
                    self.db.backup(disk)
                barrier = Barrier(2)
                def worker(session):
                    service = ProviderSwitchReviews(clock=lambda: 0, task_observer=lambda _: ())
                    service._store(session)
                    with closing(sqlite3.connect(path, timeout=10)) as connection:
                        connection.execute('PRAGMA foreign_keys=ON')
                        barrier.wait()
                        try:
                            return service.apply(connection.cursor(), session.id, session.revision,
                                session.preview.view()['mapping_digest'], confirmed=True, expected_authority=token)
                        except SwitchReviewError as error:
                            return str(error)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(worker, (first, first if same else second)))
                successes = [r for r in results if isinstance(r, dict)]
                self.assertEqual(len(successes), 2 if same else 1)
                if same:
                    self.assertEqual(successes[0]['id'], successes[1]['id'])
                    self.assertEqual(sorted(r['already_applied'] for r in successes), [False, True])
                else:
                    self.assertIn('stale_local_state', results)
                with closing(sqlite3.connect(path)) as disk:
                    self.assertEqual(disk.execute('SELECT authority_generation FROM volumes').fetchone()[0], 1)
                    self.assertEqual(disk.execute('SELECT COUNT(*) FROM provider_switch_receipts').fetchone()[0], 1)
                    self.assertEqual(disk.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_dependency_blocks_and_new_dependency_stales(self):
        self.source()
        baseline = self.review(remote('metron'))
        cases = (
            ('''INSERT INTO organization_jobs(id,plan_digest,executor_version,intent,intent_digest,state,created_at,updated_at)
                VALUES('job','digest','v1','{"volume_id":1}','digest','running','now','now')''', 'active_organization'),
            ('''INSERT INTO acquisition_downloads(id,intent_digest,intent,client_id,client_instance,state,created_at,updated_at)
                VALUES('download','digest','{"volume_id":1,"issue_ids":[1]}','fixture','fixture','ambiguous','now','now')''', 'active_download'),
            ('''INSERT INTO acquisition_intakes(id,kind,download_id,completion,completion_digest,rename,auto_apply,state,created_at,updated_at)
                VALUES('intake','sab','download','{"volume_id":1}','digest',0,0,'review','now','now')''', 'active_intake'),
            ('''INSERT INTO wanted_searches(id,volume_id,issue_ids,trigger,state,selection_policy,started_at)
                VALUES('search',1,'[1]','manual','searching','fixture',1)''', 'active_wanted_search'),
        )
        for sql, blocker in cases:
            with self.subTest(blocker=blocker):
                self.db.execute(sql)
                self.db.commit()
                before = tuple(self.db.iterdump())
                with self.assertRaisesRegex(SwitchReviewError, 'stale_local_state'):
                    self.apply(baseline)
                session = self.review(remote('metron'))
                self.assertIn(blocker, session.preview.view()['blockers'])
                with self.assertRaisesRegex(SwitchReviewError, 'switch_review_blocked'):
                    self.apply(session)
                self.assertEqual(before, tuple(self.db.iterdump()))
        self.assert_integrity()

    def test_source_endpoint_noncontiguous_coverage_and_stale_row_not_revived(self):
        self.source('gcd')
        self.content()
        # Reverse endpoint roles: outside collected issue directly owns the file,
        # both non-contiguous local issues are covered sources.
        self.db.execute('DELETE FROM file_content_coverage')
        self.db.execute('DELETE FROM bibliographic_content_claim_evidence')
        self.db.execute('DELETE FROM bibliographic_content_claims')
        self.db.execute('DELETE FROM issues_files')
        self.db.execute('INSERT INTO issues_files VALUES(1,99,1)')
        self.db.execute("UPDATE issues SET issue_number='7A' WHERE id=2")
        for iid in (1, 2):
            self.db.execute("""INSERT INTO bibliographic_content_claims VALUES(?, 'gcd','999','gcd',?,
                'complete_issue_containment','operator_confirmed','kapowarr-collected-content/v1',1,NULL,NULL)""", (str(iid), str(100 + iid)))
            self.db.execute("INSERT INTO file_content_coverage VALUES(?,1,99,?,?,1,99,?,'kapowarr-collected-content/v1',1,NULL)",
                            (str(iid), iid, str(iid), iid))
        self.db.execute("INSERT INTO file_content_coverage VALUES('stale',NULL,NULL,NULL,'1',2,99,1,'kapowarr-collected-content/v1',1,NULL)")
        self.db.commit()
        before = self.db.execute('SELECT DISTINCT issue_id,file_id FROM canonical_issue_files ORDER BY issue_id').fetchall()
        result = self.apply(self.review(remote('metron')))
        self.assertEqual((result['claim_count'], result['coverage_count']), (2, 2))
        self.assertEqual(before, self.db.execute('SELECT DISTINCT issue_id,file_id FROM canonical_issue_files ORDER BY issue_id').fetchall())
        self.assertEqual(self.db.execute("SELECT claim_id FROM file_content_coverage WHERE id='stale'").fetchone()[0], '1')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM valid_file_content_coverage WHERE id='stale'").fetchone()[0], 0)
        self.assert_integrity()

    def test_graph_and_historical_bibliography_untouched(self):
        self.source('gcd')
        self.db.execute("INSERT INTO volume_bibliography(volume_id,provider,policy,binding) VALUES(1,'gcd','fixture','Hardcover')")
        self.db.execute("INSERT INTO bibliographic_graph_snapshots VALUES('snapshot','gcd','fixture','fixture','fingerprint',1,1,0,0,0,0,'digest')")
        self.db.execute("INSERT INTO bibliographic_issue_refs VALUES('gcd','101','100','Source','1A','',0,'snapshot')")
        self.db.commit()
        tables = ('volume_bibliography', 'bibliographic_graph_snapshots', 'bibliographic_issue_refs')
        before = {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables}
        self.apply(self.review(remote('metron')))
        self.assertEqual(before, {t: self.db.execute('SELECT * FROM ' + t).fetchall() for t in tables})

    def test_gcd_bibliography_write_failure_and_history_survives_issue_deletion(self):
        self.source()
        session = self.review(remote('gcd', count=3))
        before = tuple(self.db.iterdump())
        def inject(stage):
            if stage == 'bibliography':
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issue_bibliography').fetchone()[0], 3)
                raise RuntimeError('bibliography fixture failure')
        self.service.fault_hook = inject
        with self.assertRaisesRegex(RuntimeError, 'bibliography fixture'):
            self.apply(session)
        self.assertEqual(before, tuple(self.db.iterdump()))
        self.service.fault_hook = lambda _: None
        result = self.apply(session)
        self.db.execute('DELETE FROM issues WHERE id=3')
        self.db.commit()
        history = self.service.receipt(self.cursor, result['id'], detail=True)
        self.assertEqual([r['local_issue_id'] for r in history['issues']], [1, 2, 3])
        self.assert_integrity()

    def test_wanted_queue_and_cross_volume_recovery_block_apply(self):
        self.source()
        self.db.execute('''INSERT INTO wanted_searches(id,volume_id,issue_ids,trigger,state,selection_policy,started_at)
            VALUES('search',1,'[1]','manual','complete','fixture',1)''')
        self.db.execute('''INSERT INTO wanted_decisions VALUES('decision','search','operator','candidate','fixture','fixture',
            'eval','score','selection','fixture','[1]','Title','review','nzb',NULL,NULL,1,1,'missing')''')
        self.db.execute("INSERT INTO wanted_reservations(decision_id,issue_id) VALUES('decision',1)")
        self.db.execute('''INSERT INTO download_queue(id,volume_id,client_type,download_link,source_type,source_name)
            VALUES(1,1,'fixture','https://example.invalid/fixture','fixture','fixture')''')
        self.db.execute('''INSERT INTO organization_jobs(id,plan_digest,executor_version,intent,intent_digest,state,created_at,updated_at)
            VALUES('job','digest','v1','{"volume_id":999,"database_restore":{"file":{"links":[[1,1]]}}}',
            'digest','recovery_required','now','now')''')
        self.db.commit()
        session = self.review(remote('metron'))
        for blocker in ('active_wanted_reservation', 'active_wanted_decision', 'active_download_queue', 'active_organization'):
            self.assertIn(blocker, session.preview.view()['blockers'])
        before = tuple(self.db.iterdump())
        with self.assertRaisesRegex(SwitchReviewError, 'switch_review_blocked'):
            self.apply(session)
        self.assertEqual(before, tuple(self.db.iterdump()))
        self.db.execute("UPDATE organization_jobs SET state='completed'")
        self.db.execute("UPDATE wanted_decisions SET state='completed'")
        self.db.execute('UPDATE wanted_reservations SET active=0')
        self.db.execute('DELETE FROM download_queue')
        self.db.commit()
        self.apply(self.review(remote('metron')))
        self.assert_integrity()

    def test_hundred_issue_apply_diagnostic_and_irrelevant_config(self):
        self.source(count=100)
        session = self.review(remote('gcd', count=100))
        self.db.execute("INSERT INTO config VALUES('irrelevant-fixture','changed')")
        self.db.commit()
        statements = []
        self.db.set_trace_callback(statements.append)
        started = time.monotonic()
        self.apply(session)
        elapsed = time.monotonic() - started
        self.db.set_trace_callback(None)
        selects = sum(s.lstrip().upper().startswith('SELECT') for s in statements)
        self.assertLess(selects, 100)
        print(f'Switch apply 100 rich issues: {selects} SELECTs, {len(statements)} traced statements, {elapsed:.3f}s')

    def test_production_cursor_converters_with_direct_and_collected_files(self):
        self.source('gcd')
        self.content()
        with Flask(__name__).app_context(), patch.dict(sqlite3.adapters), patch.dict(sqlite3.converters):
            setup_db_adapters_and_converters()
            with closing(DBConnection(db_file=':memory:')) as production:
                self.db.backup(production)
                self.cursor = production.cursor()
                original = self.db
                self.db = production
                try:
                    result = self.apply(self.review(remote('metron')))
                    self.assertEqual(result['coverage_count'], 1)
                    self.assert_integrity()
                finally:
                    self.db = original
                    self.cursor = original.cursor()

    def test_wanted_derives_preserved_coverage_and_target_only_missing(self):
        self.source('gcd')
        self.content()
        self.db.execute('UPDATE volumes SET monitored=1')
        self.db.execute('UPDATE issues SET monitored=1')
        self.db.commit()
        def projection():
            self.db.row_factory = sqlite3.Row
            try:
                return {row['id']: (row['owned'], row['wanted']) for row in
                        wanted_rows(SimpleNamespace(db=self.db, clock=lambda: 0))}
            finally:
                self.db.row_factory = None
        before = projection()
        self.apply(self.review(remote('metron', count=3)))
        after = projection()
        self.assertEqual({iid: after[iid] for iid in before}, before)
        # New local ID follows the already present outside-volume issue 99.
        self.assertEqual(after[100], (0, True))
        self.assertEqual(after[99], (1, False))
        for table in ('wanted_searches', 'wanted_decisions', 'wanted_reservations'):
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    def test_receipt_constraints_reject_invalid_success_records(self):
        self.source()
        result = self.apply(self.review(remote('metron')))
        result.pop('already_applied')
        before = tuple(self.db.iterdump())
        for changes in ({}, {'target_generation': 3}, {'source_provider': 'metron'},
                        {'unresolved_count': 1}, {'mapped_count': 10001},
                        {'actor': 'invented-user'}, {'mapping_digest': 'invalid'}):
            with self.subTest(changes=changes):
                value = dict(result, id='other-receipt')
                if changes:
                    value.update(session_id='other-session', source_generation=1, target_generation=2)
                value.update(changes)
                self.db.execute('SAVEPOINT invalid_receipt')
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute('INSERT INTO provider_switch_receipts(' + ','.join(value) + ') VALUES(' +
                        ','.join('?' for _ in value) + ')', tuple(value.values()))
                self.db.execute('ROLLBACK TO invalid_receipt')
                self.db.execute('RELEASE invalid_receipt')
                self.assertEqual(before, tuple(self.db.iterdump()))
