"""Exact assertions and explicit coverage; raw graph never becomes ownership."""

import sqlite3
from dataclasses import FrozenInstanceError
from unittest import TestCase

from backend.base.content_claims import (ClaimKind, ContentConflict,
                                         ContentEvidenceEvaluation,
                                         EvidenceOutcome, EvidenceReceipt,
                                         PublicationRef)
from backend.internals.content_claims import (apply_coverage, claim_preview,
                                              confirm_claim, coverage_preview,
                                              retire, retirement_preview)
from backend.internals.db import DB_SCHEMA
from backend.internals.issue_ownership import load_ownership


class ContentCoverageTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript(DB_SCHEMA)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute("INSERT INTO root_folders(id,folder) VALUES(1,'/fixture')")
        for vid in (1, 2, 3):
            self.db.execute('''INSERT INTO volumes(id,title,root_folder,folder,metadata_provider)
                VALUES(?,?,1,?,'gcd')''', (vid, 'Series ' + str(vid), '/fixture/' + str(vid)))
        for iid in range(1, 9):
            self.db.execute('INSERT INTO issues(id,volume_id,issue_number,monitored) VALUES(?,?,?,1)',
                            (iid, 1 if iid == 8 else 2 if iid < 5 else 3, str(iid)))
            self.db.execute("INSERT INTO issue_external_ids VALUES(?,'gcd',?,'fixture')", (iid, str(iid)))
        self.db.execute("INSERT INTO files(id,filepath,size) VALUES(1,'/fixture/collection.cbz',10)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,8)')
        self.db.commit()
        self.c = self.db.cursor()

    def claim(self, source=1, kind=ClaimKind.COMPLETE):
        ref = PublicationRef('gcd', str(source))
        preview = claim_preview(self.c, 8, ref, kind, manual=True)
        return confirm_claim(self.c, 8, ref, kind, preview['preview_token'], manual=True)

    def apply(self, claims):
        preview = coverage_preview(self.c, 8, 1, claims)
        self.assertFalse(any(preview['effects'][key] for key in
                             ('move', 'rename', 'comicinfo_write', 'direct_association_change')))
        return apply_coverage(self.c, 8, 1, claims, preview['preview_token'])

    def ownership(self, iid=1):
        return load_ownership(self.c, issue_ids=(iid,))[iid]

    def test_claim_then_separate_apply_then_revoke(self):
        claim = self.claim()
        self.assertFalse(self.ownership()['owned'])
        self.apply([claim])
        self.assertEqual(self.ownership()['state'], 'collected')
        preview = retirement_preview(self.c, claim)
        retire(self.c, claim, preview['preview_token'])
        self.assertFalse(self.ownership()['owned'])
        self.assertEqual(self.db.execute('SELECT file_id,issue_id FROM issues_files').fetchall(), [(1, 8)])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 1)

    def test_noncontiguous_cross_volume_exact_set(self):
        self.apply([self.claim(i) for i in (1, 3, 7)])
        result = load_ownership(self.c, issue_ids=range(1, 9))
        self.assertEqual([iid for iid, state in result.items() if state['owned']], [1, 3, 7, 8])
        self.assertEqual(self.db.execute('SELECT file_id,issue_id FROM issues_files').fetchall(), [(1, 8)])

    def test_partial_never_owned_or_eligible(self):
        claim = self.claim(kind=ClaimKind.PARTIAL)
        with self.assertRaises(ContentConflict):
            self.apply([claim])
        self.assertFalse(self.ownership()['owned'])

    def test_supersede_and_reconfirm_does_not_reactivate(self):
        old = self.claim()
        self.apply([old])
        partial = self.claim(kind=ClaimKind.PARTIAL)
        self.assertFalse(self.ownership()['owned'])
        complete = self.claim()
        self.assertFalse(self.ownership()['owned'])
        self.apply([complete])
        self.assertTrue(self.ownership()['owned'])
        self.assertEqual(self.db.execute('SELECT supersedes FROM bibliographic_content_claims WHERE id=?',
                                        (partial,)).fetchone()[0], old)

    def test_direct_and_collected_independent(self):
        self.apply([self.claim()])
        self.db.execute("INSERT INTO files(id,filepath,size) VALUES(2,'/fixture/source.cbz',1)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(2,1)')
        self.assertEqual(self.ownership()['state'], 'direct_and_collected')
        self.db.execute('DELETE FROM files WHERE id=2')
        self.assertEqual(self.ownership()['state'], 'collected')
        self.db.execute('DELETE FROM files WHERE id=1')
        self.assertEqual(self.ownership()['state'], 'none')
        self.assertEqual(self.db.execute('SELECT original_file_id FROM file_content_coverage').fetchone()[0], 1)

    def test_live_target_association_and_authority_predicates(self):
        self.apply([self.claim()])
        self.db.execute("UPDATE volumes SET metadata_provider='metron' WHERE id=2")
        self.assertFalse(self.ownership()['owned'])
        self.db.execute("UPDATE volumes SET metadata_provider='gcd' WHERE id=2")
        self.assertTrue(self.ownership()['owned'])
        self.db.execute('DELETE FROM issues_files WHERE issue_id=8')
        self.assertFalse(self.ownership()['owned'])

    def test_stale_claim_and_coverage_previews_reject(self):
        ref = PublicationRef('gcd', '1')
        preview = claim_preview(self.c, 8, ref, ClaimKind.COMPLETE, manual=True)
        claim = self.claim()
        with self.assertRaises(ContentConflict):
            confirm_claim(self.c, 8, ref, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        coverage = coverage_preview(self.c, 8, 1, [claim])
        self.claim(kind=ClaimKind.PARTIAL)
        with self.assertRaises(ContentConflict):
            apply_coverage(self.c, 8, 1, [claim], coverage['preview_token'])

    def test_repeat_apply_no_duplicate_and_rollback(self):
        claim = self.claim()
        first = self.apply([claim])
        self.assertEqual(self.apply([claim]), first)
        self.db.execute('''CREATE TEMP TRIGGER reject_claim BEFORE INSERT ON bibliographic_content_claims
            BEGIN SELECT RAISE(ABORT,'injected'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.claim(kind=ClaimKind.PARTIAL)
        self.assertTrue(self.ownership()['owned'])

    def test_evidence_all_shapes_never_complete(self):
        source, target = PublicationRef('gcd', '1'), PublicationRef('gcd', '8')
        edges = tuple(EvidenceReceipt('gcd', str(i), 'snapshot', '1', '8', origin, dest, True)
                      for i, (origin, dest) in enumerate(((None, None), ('s', None), (None, 't'), ('s', 't'))))
        for edge in edges:
            value = ContentEvidenceEvaluation(source, target, (edge,))
            self.assertIn(value.outcome, (EvidenceOutcome.MATERIAL, EvidenceOutcome.STORY))
        self.assertEqual(ContentEvidenceEvaluation(source, target, edges).outcome, EvidenceOutcome.MULTIPLE)
        with self.assertRaises(FrozenInstanceError):
            edges[0].active = False
        with self.assertRaises(ValueError):
            ContentEvidenceEvaluation(source, target, (edges[0], edges[0]))

    def test_multiple_files_explicit_selection_and_file_identity(self):
        self.db.execute("INSERT INTO files VALUES(2,'/fixture/second.cbz',1)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(2,8)')
        claim = self.claim()
        self.apply([claim])
        self.assertEqual([r['file_id'] for r in self.ownership()['collected_coverage']], [1])
        self.db.execute("UPDATE files SET filepath='/fixture/renamed.cbz' WHERE id=1")
        self.assertTrue(self.ownership()['owned'])
        self.db.execute('DELETE FROM files WHERE id=1')
        self.assertFalse(self.ownership()['owned'])
        self.db.execute("INSERT INTO files VALUES(3,'/fixture/renamed.cbz',1)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(3,8)')
        self.assertFalse(self.ownership()['owned'])

    def test_source_variant_opaque_and_unnumbered_are_exact_ids(self):
        self.db.execute("UPDATE issues SET issue_number='[nn]' WHERE id=1")
        self.db.execute("UPDATE issues SET issue_number='1A' WHERE id=3")
        self.db.execute("UPDATE issues SET issue_number='Annual' WHERE id=7")
        self.apply([self.claim(i) for i in (1, 3, 7)])
        self.assertTrue(self.ownership(1)['owned'])
        self.assertFalse(self.ownership(2)['owned'])
        self.assertTrue(self.ownership(3)['owned'])
        # Identical number/variant display never grants another identity coverage.
        self.db.execute("UPDATE issues SET issue_number='1A' WHERE id=4")
        self.db.execute("INSERT INTO issue_variant_of VALUES(4,'gcd','3','fixture')")
        self.assertFalse(self.ownership(4)['owned'])
        self.db.execute("INSERT INTO issue_variant_of VALUES(6,'gcd','8','fixture')")
        self.db.execute("INSERT INTO files VALUES(2,'/fixture/target-variant.cbz',1)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(2,6)')
        claim = self.db.execute("SELECT id FROM bibliographic_content_claims WHERE source_provider_id='3'").fetchone()[0]
        with self.assertRaises(ContentConflict):
            coverage_preview(self.c, 6, 2, [claim])

    def test_coverage_revocation_preserves_complete_claim_and_direct_identity(self):
        from backend.internals.content_claims import claim_history
        claim = self.claim()
        cid = self.apply([claim])[0]
        preview = retirement_preview(self.c, cid, coverage=True)
        retire(self.c, cid, preview['preview_token'], coverage=True)
        self.assertFalse(self.ownership()['owned'])
        self.assertIsNone(claim_history(self.c, claim)['claim']['retired_at'])
        self.assertTrue(self.ownership(8)['owned'])
        self.assertNotEqual(self.apply([claim])[0], cid)

    def test_source_deletion_retains_historical_coverage_without_ownership(self):
        self.apply([self.claim()])
        self.db.execute('DELETE FROM issues WHERE id=1')
        self.assertEqual(load_ownership(self.c, issue_ids=(1,)), {})
        row = self.db.execute('SELECT source_issue_id,original_source_id FROM file_content_coverage').fetchone()
        self.assertEqual(row, (None, 1))

    def test_apply_rollback_has_no_half_coverage(self):
        claims = [self.claim(i) for i in (1, 3)]
        self.db.execute('''CREATE TEMP TRIGGER reject_second BEFORE INSERT ON file_content_coverage
            WHEN NEW.source_issue_id=3 BEGIN SELECT RAISE(ABORT,'injected'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply(claims)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM file_content_coverage').fetchone()[0], 0)

    def test_ownership_performance_and_query_bounds(self):
        import tracemalloc
        from time import perf_counter
        self.db.executemany('INSERT INTO issues(id,volume_id,issue_number) VALUES(?,2,?)',
                            ((i, str(i)) for i in range(9, 10009)))
        self.db.executemany("INSERT INTO issue_external_ids VALUES(?,'gcd',?,'fixture')",
                            ((i, str(i)) for i in range(9, 10009)))
        self.apply([self.claim(i) for i in (1, 3, 7)])
        # Large already-confirmed fixture state, not an inference/import path.
        members = [i for i in range(1, 10009) if i not in (1, 3, 7, 8)]
        self.db.executemany('''INSERT INTO bibliographic_content_claims VALUES(
            ?,'gcd','8','gcd',?,'complete_issue_containment','operator_confirmed',
            'kapowarr-collected-content/v1',1,NULL,NULL)''', ((f'fixture-{i}', str(i)) for i in members))
        self.db.executemany('''INSERT INTO file_content_coverage VALUES(
            ?,1,8,?,?,1,8,?,'kapowarr-collected-content/v1',1,NULL)''',
            ((f'coverage-{i}', i, f'fixture-{i}', i) for i in members))
        reports = []
        for size in (100, 1000, 10000):
            queries = []
            self.db.set_trace_callback(queries.append)
            tracemalloc.start()
            started = perf_counter()
            data = load_ownership(self.c, issue_ids=range(1, size + 1))
            elapsed = perf_counter() - started
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            self.db.set_trace_callback(None)
            selects = sum(sql.startswith('SELECT') for sql in queries)
            self.assertEqual(selects, (size + 399) // 400)
            self.assertEqual(len(data), size)
            reports.append((size, selects, round(elapsed, 4), peak))
        queries = []
        self.db.set_trace_callback(queries.append)
        load_ownership(self.c, volume_id=2)
        self.db.set_trace_callback(None)
        self.assertEqual(sum(sql.startswith('SELECT') for sql in queries), 1)
        batch_claims = [row[0] for row in self.db.execute(
            'SELECT id FROM bibliographic_content_claims WHERE retired_at IS NULL ORDER BY id LIMIT 100')]
        queries = []
        self.db.set_trace_callback(queries.append)
        coverage_preview(self.c, 8, 1, batch_claims)
        self.db.set_trace_callback(None)
        self.assertEqual(sum(sql.startswith('SELECT') for sql in queries), 5)
        print('Ownership issues/SELECTs/seconds/Python peak bytes:', reports)

    def test_two_connections_cannot_duplicate_active_claim_or_coverage(self):
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from threading import Barrier
        self.db.commit()
        with TemporaryDirectory(prefix='kapowarr-content-concurrency-') as folder:
            path = Path(folder) / 'app.sqlite'
            disk = sqlite3.connect(path)
            self.db.backup(disk)
            ref = PublicationRef('gcd', '1')
            preview = claim_preview(disk.cursor(), 8, ref, ClaimKind.COMPLETE, manual=True)
            barrier = Barrier(2)
            def worker(operation):
                connection = sqlite3.connect(path, timeout=3)
                try:
                    barrier.wait(timeout=5)
                    try:
                        return operation(connection.cursor())
                    except ContentConflict:
                        return None
                finally:
                    connection.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                result = list(pool.map(worker, [lambda c: confirm_claim(c, 8, ref, ClaimKind.COMPLETE,
                                      preview['preview_token'], manual=True)] * 2))
            self.assertEqual(sum(value is not None for value in result), 1)
            claim = next(value for value in result if value is not None)
            self.assertEqual(disk.execute('SELECT COUNT(*) FROM bibliographic_content_claims WHERE retired_at IS NULL').fetchone()[0], 1)
            preview = coverage_preview(disk.cursor(), 8, 1, [claim])
            barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                result = list(pool.map(worker, [lambda c: apply_coverage(c, 8, 1, [claim], preview['preview_token'])] * 2))
            self.assertEqual(sum(value is not None for value in result), 1)
            self.assertEqual(disk.execute('SELECT COUNT(*) FROM file_content_coverage WHERE retired_at IS NULL').fetchone()[0], 1)
            disk.close()
