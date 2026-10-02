"""Disposable duplicate evidence/intent fixtures; no deletion authorization."""

import json
import sqlite3
from dataclasses import FrozenInstanceError, replace
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

import TMaintenanceReview as maintenance_fixture

from backend.base.content_claims import ClaimKind, PublicationRef
from backend.base.duplicate_review import (DuplicateAction as Action,
                                           DuplicateChoice as Choice,
                                           DuplicateKind as Kind,
                                           DuplicateReviewError)
from backend.base.library_health import HealthLevel
from backend.base.maintenance_review import Action as Intent, Edit
from backend.features.duplicate_review import DuplicateReviews
from backend.implementations.duplicate_evidence import HashBudget
from backend.internals.content_claims import (apply_coverage, claim_preview,
                                              confirm_claim, coverage_preview)
from backend.internals.duplicate_review import read_duplicate_state
from backend.internals.organization_jobs import JobStore


class DuplicateReviewTests(TestCase):
    def setUp(self):
        self.fixture = maintenance_fixture.MaintenanceReviewTests('test_unknown_report_and_finding_rejected')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.health = self.fixture.fixture
        self.db = self.fixture.db
        self.paths = [self.health.comic('one.cbz'), self.health.comic('two.cbz')]
        self.paths[1].write_bytes(self.paths[0].read_bytes())
        self.service = DuplicateReviews(self.fixture.service, clock=lambda: self.fixture.clock[0])

    def worklist(self, code='exact_byte_duplicate', level=HealthLevel.DEEP):
        worklist = self.fixture.review(level)
        ids = tuple(i.finding.id for i in worklist.items if i.finding.code == code)
        self.assertTrue(ids)
        worklist = self.fixture.service.revise(worklist.id, worklist.revision,
            tuple(Edit(i, True, False, Intent.DUPLICATE) for i in ids))
        return worklist, ids

    def review(self, code='exact_byte_duplicate', level=HealthLevel.DEEP, **kwargs):
        worklist, ids = self.worklist(code, level)
        return self.service.create(worklist.id, worklist.revision, worklist.manifest_digest, ids, **kwargs)

    def test_exact_review_fresh_hash_immutable_and_read_only(self):
        before = tuple(self.db.iterdump()), self.health.filesystem()
        with patch('socket.socket', side_effect=AssertionError('network')), \
                patch('backend.implementations.file_matching.scan_files', side_effect=AssertionError('scan')), \
                patch('backend.features.organization_execution.OrganizationExecutor.create_job', side_effect=AssertionError('job')):
            session = self.review()
            self.assertEqual(session.groups[0].kind, Kind.EXACT)
            self.assertEqual(session.hash_bytes, sum(p.stat().st_size for p in self.paths))
            self.assertFalse(session.summary()['apply_available'])
            page = session.detail(session.groups[0].id)
            page['members'][0]['filepath'] = 'hostile changed view'
            self.assertNotIn('hostile changed view', session.files_json)
            with self.assertRaises(FrozenInstanceError):
                session.revision = 4
        self.assertEqual(before, (tuple(self.db.iterdump()), self.health.filesystem()))
        self.assertNotIn('HEALTH_SECRET_SENTINEL', session.files_json + session.ownership_json)
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_explicit_choice_no_default_removal_and_no_apply(self):
        session = self.review()
        group = session.groups[0]
        self.assertEqual(group.quarantine, ())
        revised = self.service.revise(session.id, 0, (Choice(group.id, Action.QUARANTINE, (1,)),))
        self.assertIn('quarantine_recovery_not_implemented', revised.groups[0].blockers)
        impact = json.loads(revised.groups[0].impact_json)
        self.assertEqual(impact[0]['after_files'], [2])
        self.assertFalse(impact[0]['becomes_wanted'])
        self.assertFalse(hasattr(self.service, 'apply'))
        self.assertNotEqual(session.digest, revised.digest)
        with self.assertRaises(DuplicateReviewError):
            self.service.revise(session.id, 0, (Choice(group.id, Action.KEEP),))
        with self.assertRaises(DuplicateReviewError):
            self.service.revise(session.id, 1, (Choice(group.id, Action.QUARANTINE, (1, 2)),))

    def test_same_publication_not_bytes_and_no_quality_winner(self):
        self.paths[1].write_bytes(b'different bytes, same direct publication')
        self.db.execute('UPDATE files SET size=? WHERE id=2', (self.paths[1].stat().st_size,))
        self.db.commit()
        with patch.object(HashBudget, 'inspect', side_effect=AssertionError('unrequested hash')):
            session = self.review('same_publication_files', HealthLevel.INVENTORY)
        self.assertEqual(session.groups[0].kind, Kind.PUBLICATION)
        self.assertEqual(session.hash_bytes, 0)
        with self.assertRaises(DuplicateReviewError):
            self.service.revise(session.id, 0, (Choice(session.groups[0].id, Action.QUARANTINE, (1,)),))

    def test_three_copies_one_equivalence_class(self):
        third = self.health.comic('three.cbz')
        third.write_bytes(self.paths[0].read_bytes())
        session = self.review()
        self.assertEqual(len(session.groups), 1)
        self.assertEqual(session.groups[0].file_ids, (1, 2, 3))

    def test_collision_is_not_content_equivalence(self):
        session = self.review('path_collision', HealthLevel.INVENTORY)
        self.assertTrue(all(g.kind == Kind.COLLISION for g in session.groups))
        self.assertEqual(session.hash_bytes, 0)
        with self.assertRaises(DuplicateReviewError):
            self.service.revise(session.id, 0, (Choice(session.groups[0].id, Action.QUARANTINE, (1,)),))

    def test_owned_handoff_rejects_unknown_revision_manifest_selection(self):
        worklist, ids = self.worklist()
        for revision, digest, selected in ((0, worklist.manifest_digest, ids),
                (worklist.revision, 'a' * 64, ids), (worklist.revision, worklist.manifest_digest, ('a' * 64,)),
                (worklist.revision, worklist.manifest_digest, ids + ids)):
            with self.assertRaises(DuplicateReviewError):
                self.service.create(worklist.id, revision, digest, selected)
        self.assertEqual(self.service._sessions, {})

    def test_hash_change_between_worklist_and_child_rejected(self):
        worklist, ids = self.worklist()
        self.paths[0].write_bytes(b'changed')
        self.paths[1].write_bytes(b'changed')
        with self.assertRaisesRegex(DuplicateReviewError, 'duplicate_hash_evidence_changed'):
            self.service.create(worklist.id, worklist.revision, worklist.manifest_digest, ids)

    def test_generation_aba_and_stale_latch(self):
        session = self.review()
        self.db.execute('UPDATE volumes SET authority_generation=2 WHERE id=1')
        self.db.commit()
        stale = self.service.revalidate(session.id, 0)
        self.assertTrue(stale.stale)
        self.db.execute('UPDATE volumes SET authority_generation=0 WHERE id=1')
        self.db.commit()
        self.assertTrue(self.service.revalidate(session.id, 1).stale)
        with self.assertRaisesRegex(DuplicateReviewError, 'stale_duplicate_review'):
            self.service.revise(session.id, 2, (Choice(session.groups[0].id, Action.KEEP),))

    def test_association_and_file_changes_stale(self):
        session = self.review()
        self.db.execute('UPDATE issues_files SET forced=1 WHERE file_id=1')
        self.db.commit()
        self.assertTrue(self.service.revalidate(session.id, 0).stale)
        new = self.review()
        self.paths[0].write_bytes(b'new content')
        self.assertTrue(self.service.revalidate(new.id, 0).stale)

    def test_hash_budget_cancel_deadline_and_changed_during_read(self):
        with self.assertRaisesRegex(DuplicateReviewError, 'byte_limit'):
            HashBudget(maximum=1).inspect(str(self.paths[0]))
        with self.assertRaisesRegex(DuplicateReviewError, 'cancelled'):
            HashBudget(cancel=lambda: True).inspect(str(self.paths[0]))
        clock = [0]
        budget = HashBudget(seconds=1, clock=lambda: clock[0])
        clock[0] = 2
        with self.assertRaisesRegex(DuplicateReviewError, 'deadline'):
            budget.inspect(str(self.paths[0]))
        def mutate(_):
            with self.paths[0].open('ab') as stream:
                stream.write(b'changed')
        # Cancel after the first chunk to avoid a deliberately growing fixture.
        changed = [False]
        def progress(count):
            if not changed[0]:
                mutate(count)
                changed[0] = True
        with self.assertRaisesRegex(DuplicateReviewError, 'source_changed'):
            HashBudget(progress=progress).inspect(str(self.paths[0]))

    def test_c2_ownership_impact_and_history_unchanged(self):
        self.db.execute('UPDATE volumes SET monitored=1 WHERE id=1')
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(2,1,102,'2',2,1)")
        ref = PublicationRef('comicvine', '102')
        preview = claim_preview(self.db.cursor(), 1, ref, ClaimKind.COMPLETE, manual=True)
        claim = confirm_claim(self.db.cursor(), 1, ref, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        preview = coverage_preview(self.db.cursor(), 1, 1, [claim])
        apply_coverage(self.db.cursor(), 1, 1, [claim], preview['preview_token'])
        self.db.commit()
        before = tuple(self.db.iterdump())
        session = self.review()
        revised = self.service.revise(session.id, 0, (Choice(session.groups[0].id, Action.QUARANTINE, (1,)),))
        impact = {r['issue_id']: r for r in json.loads(revised.groups[0].impact_json)}
        self.assertFalse(impact[1]['loses_ownership'])
        self.assertTrue(impact[2]['loses_ownership'])
        self.assertTrue(impact[2]['becomes_wanted'])
        self.assertIn('ownership_loss_requires_separate_contract', revised.groups[0].blockers)
        self.assertEqual(before, tuple(self.db.iterdump()))

    def test_coverage_overlap_remains_nontransitive_nonpublication(self):
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(2,1,102,'2',2,1)")
        ref = PublicationRef('comicvine', '102')
        preview = claim_preview(self.db.cursor(), 1, ref, ClaimKind.COMPLETE, manual=True)
        claim = confirm_claim(self.db.cursor(), 1, ref, ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        for fid in (1, 2):
            preview = coverage_preview(self.db.cursor(), 1, fid, [claim])
            apply_coverage(self.db.cursor(), 1, fid, [claim], preview['preview_token'])
        self.db.commit()
        session = self.review('overlapping_collected_coverage', HealthLevel.INVENTORY)
        self.assertEqual(session.groups[0].kind, Kind.COVERAGE)
        self.assertEqual(session.hash_bytes, 0)
        with self.assertRaises(DuplicateReviewError):
            self.service.revise(session.id, 0, (Choice(session.groups[0].id, Action.QUARANTINE, (1,)),))

    def test_bounds_expiry_revision_and_detached_pagination(self):
        session = self.review()
        self.assertEqual(len(session.detail(session.groups[0].id, limit=1)['members']), 1)
        with self.assertRaises(DuplicateReviewError):
            session.page(limit=101)
        self.service._sessions[session.id] = replace(session, revision=1000)
        with self.assertRaises(DuplicateReviewError):
            self.service.revise(session.id, 1000, (Choice(session.groups[0].id, Action.KEEP),))
        self.fixture.clock[0] += 901
        with self.assertRaises(DuplicateReviewError):
            self.service.get(session.id)
        self.assertEqual(self.service._sessions, {})

    def test_size_and_group_limits_fail_without_partial_review(self):
        worklist, ids = self.worklist()
        with patch.object(self.service, 'MAX_BYTES', 100):
            with self.assertRaisesRegex(DuplicateReviewError, 'size_limit'):
                self.service.create(worklist.id, worklist.revision, worklist.manifest_digest, ids)
        with patch.object(self.service, 'MAX_GROUP_FILES', 1):
            with self.assertRaisesRegex(DuplicateReviewError, 'group_file_limit'):
                self.service.create(worklist.id, worklist.revision, worklist.manifest_digest, ids)
        self.assertEqual(self.service._sessions, {})

    def test_snapshot_limit_and_read_only_sql(self):
        worklist, _ = self.worklist()
        with patch('backend.internals.duplicate_review.MAX_ROWS', 1):
            with self.assertRaises(Exception):
                read_duplicate_state(str(self.health.database), worklist.report.scope)
        original = sqlite3.connect
        def readonly(*args, **kwargs):
            connection = original(*args, **kwargs)
            self.assertIn('mode=ro', args[0])
            return connection
        with patch('backend.internals.duplicate_review.sqlite3.connect', side_effect=readonly):
            read_duplicate_state(str(self.health.database), worklist.report.scope)

    def test_small_review_diagnostic(self):
        start = perf_counter()
        session = self.review()
        print('8G two-file review', dict(seconds=perf_counter() - start, hash_bytes=session.hash_bytes,
                                       files_bytes=len(session.files_json), groups=len(session.groups)))

    def test_subtree_reservation_blocks_hash_and_choice_capability(self):
        self.db.execute("INSERT OR REPLACE INTO config VALUES('database_version',65)")
        self.db.commit()
        store = JobStore(str(self.health.database))
        self.addCleanup(store.close)
        store.create(dict(effects=[], volume_id=1, source=str(self.health.volume), target=str(self.health.root / 'target')),
                     'fixture-folder-reservation', (str(self.health.volume),))
        with patch.object(HashBudget, 'inspect', side_effect=AssertionError('blocked hash')):
            session = self.review()
        self.assertIn('duplicate_path_reserved', session.groups[0].blockers)
        self.assertIn('active_volume_dependency', session.groups[0].blockers)
        self.assertEqual(session.hash_bytes, 0)

    def test_historical_identities_are_evidence_not_false_duplicates(self):
        self.db.execute("INSERT INTO volume_external_ids VALUES(1,'metron','old-volume','operator',NULL)")
        self.db.execute("INSERT INTO issue_external_ids VALUES(1,'metron','old-issue','operator')")
        self.db.commit()
        session = self.review()
        self.assertEqual(len(session.groups), 1)
        self.assertEqual(session.groups[0].blockers, ())
        self.assertIn('old-volume', session.files_json)
        self.assertIn('old-issue', session.files_json)

    def test_unregistered_exact_copy_remains_blocked(self):
        third = self.health.comic('unregistered.cbz', registered=False)
        third.write_bytes(self.paths[0].read_bytes())
        session = self.review()
        self.assertIn('incomplete_registered_group', session.groups[0].blockers)
        self.assertEqual(session.groups[0].file_ids, (1, 2))

    def test_choice_change_never_rehashes_or_removes_bytes(self):
        session = self.review()
        group = session.groups[0]
        before = tuple(self.db.iterdump()), self.health.filesystem()
        with patch.object(HashBudget, 'inspect', side_effect=AssertionError('selection hash')):
            session = self.service.revise(session.id, 0, (Choice(group.id, Action.QUARANTINE, (1,)),))
            session = self.service.revise(session.id, 1, (Choice(group.id, Action.KEEP),))
        self.assertEqual(session.groups[0].quarantine, ())
        self.assertNotIn('quarantine_recovery_not_implemented', session.groups[0].blockers)
        self.assertEqual(before, (tuple(self.db.iterdump()), self.health.filesystem()))
        self.service.delete(session.id)
        with self.assertRaises(DuplicateReviewError):
            self.service.get(session.id)

    def test_review_scale_100_findings_and_capacity_failure(self):
        # 100 distinct direct publication sets, two files each. No byte ranking.
        for issue in range(2, 101):
            self.db.execute('''INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number)
                VALUES(?,1,?,?,?)''', (issue, 1000 + issue, str(issue), issue))
            for copy in range(2):
                path = self.health.volume / f'issue-{issue}-{copy}.cbz'
                path.write_bytes(b'bounded diagnostic')
                fid = self.db.execute('INSERT INTO files(filepath,size) VALUES(?,?)', (str(path), path.stat().st_size)).lastrowid
                self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(?,?)', (fid, issue))
        self.db.commit()
        worklist, ids = self.worklist('same_publication_files', HealthLevel.INVENTORY)
        self.assertEqual(len(ids), 100)
        counts = [0]
        original = sqlite3.connect
        def connect(*args, **kwargs):
            db = original(*args, **kwargs)
            db.set_trace_callback(lambda sql: counts.__setitem__(0, counts[0] + int(sql.lstrip().upper().startswith('SELECT'))))
            return db
        start = perf_counter()
        with patch('sqlite3.connect', side_effect=connect):
            session = self.service.create(worklist.id, worklist.revision, worklist.manifest_digest, ids)
        self.assertEqual(len(session.groups), 100)
        self.assertEqual(session.hash_bytes, 0)
        print('8G 100-publication child review', dict(seconds=perf_counter() - start, selects=counts[0],
              files_bytes=len(session.files_json), ownership_bytes=len(session.ownership_json)))
        with patch.object(self.service, 'MAX_GROUPS', 99):
            with self.assertRaisesRegex(DuplicateReviewError, 'duplicate_group_limit'):
                self.service.create(worklist.id, worklist.revision, worklist.manifest_digest, ids)
        self.assertEqual(len(self.service._sessions), 1)
