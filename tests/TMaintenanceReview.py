"""Read-only maintenance intents; no live services or production libraries."""

import json
import sqlite3
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Barrier
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

import TLibraryHealth as health_fixture

from backend.base.library_health import HealthLevel, HealthScope
from backend.base.maintenance_review import (Action, Capability, Edit,
                                             FindingFilter, ReviewError)
from backend.features.maintenance_review import MaintenanceReviews
from backend.implementations.maintenance_review import batch_collisions
from backend.internals.library_health import read_snapshot


class MaintenanceReviewTests(TestCase):
    def setUp(self):
        self.fixture = health_fixture.LibraryHealthTests('test_inventory_never_opens_archive_or_hashes')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.tasks = []
        self.clock = [10.0]
        self.service = MaintenanceReviews(str(self.fixture.database), clock=lambda: self.clock[0],
                                          enqueue=lambda t: self.tasks.append(t) or len(self.tasks))

    def review(self, level=HealthLevel.INVENTORY):
        self.db.commit()
        scan = self.service.request_scan(HealthScope('volumes', (1,)), level)
        self.assertEqual(self.service.scan_status(scan)['state'], 'queued')
        self.tasks[-1].run()
        self.assertNotEqual(self.service.scan_status(scan)['state'], 'failed')
        return self.service.create(scan)

    def assign(self, worklist, action, code='filename_deviation'):
        item = next(i for i in worklist.items if i.finding.code == code)
        return self.service.revise(worklist.id, worklist.revision,
            (Edit(item.finding.id, True, False, action),))

    def test_rename_detached_plan_and_preservation(self):
        self.fixture.comic('wrong.cbz')
        before = tuple(self.db.iterdump()), self.fixture.filesystem()
        with patch('backend.implementations.comicinfo_archive.write_comicinfo', side_effect=AssertionError('writer')), \
                patch('backend.features.organization_execution.OrganizationExecutor.create_job', side_effect=AssertionError('job')), \
                patch('socket.socket', side_effect=AssertionError('network')):
            worklist = self.review()
            revised = self.assign(worklist, Action.RENAME)
        item = next(i for i in revised.items if i.selected)
        self.assertEqual(item.capability, Capability.PREVIEW, item.view())
        self.assertTrue(json.loads(item.preview_json)['target'].endswith('Issue 001.cbz'))
        self.assertFalse(revised.summary()['apply_available'])
        self.assertEqual(before, (tuple(self.db.iterdump()), self.fixture.filesystem()))
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_unknown_report_and_finding_rejected(self):
        with self.assertRaises(ReviewError):
            self.service.create('client report')
        self.fixture.comic('wrong.cbz')
        worklist = self.review()
        with self.assertRaises(ReviewError):
            self.service.revise(worklist.id, 0, (Edit('a' * 64, True, False, Action.RENAME),))
        self.assertEqual(self.service.get(worklist.id).revision, 0)

    def test_select_exclude_revision_and_detached_pages(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.review()
        revised = self.assign(worklist, Action.ACKNOWLEDGE)
        selected = next(i for i in revised.items if i.selected)
        with self.assertRaises(ReviewError):
            self.service.revise(worklist.id, 0, ())
        excluded = self.service.revise(worklist.id, 1, (Edit(selected.finding.id, False, True, Action.NONE),))
        self.assertEqual(excluded.summary()['selected'], 0)
        self.assertEqual(excluded.page(filters=FindingFilter(excluded=True))['total'], 1)
        detached = excluded.page()
        detached['items'][0]['finding']['path'] = 'fake'
        self.assertNotEqual(excluded.items[0].finding.path, 'fake')
        with self.assertRaises(FrozenInstanceError):
            excluded.revision = 50

    def test_filtered_select_all_binds_exact_snapshot_not_page(self):
        for number in range(110):
            self.fixture.comic(f'other-{number}.cbz', registered=False)
        worklist = self.review()
        revised = self.service.select_filtered(worklist.id, 0, worklist.report.id,
            worklist.report.state_digest, FindingFilter(category='filesystem'))
        self.assertEqual(revised.summary()['selected'], 110)
        self.assertEqual(len(revised.page(limit=10)['items']), 10)
        with self.assertRaises(ReviewError):
            self.service.select_filtered(worklist.id, 1, 'new report', worklist.report.state_digest, FindingFilter())

    def test_authority_generation_and_aba_stale_without_retargeting(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.assign(self.review(), Action.RENAME)
        old = next(i.preview_json for i in worklist.items if i.selected)
        self.db.execute('UPDATE volumes SET authority_generation=2 WHERE id=1')
        self.db.commit()
        revised = self.service.revalidate(worklist.id, worklist.revision)
        item = next(i for i in revised.items if i.selected)
        self.assertEqual(item.capability, Capability.STALE)
        self.assertEqual(item.preview_json, old)

    def test_filesystem_and_settings_staleness(self):
        path = self.fixture.comic('wrong.cbz')
        worklist = self.assign(self.review(), Action.RENAME)
        path.write_bytes(b'changed fixture')
        revised = self.service.revalidate(worklist.id, 1)
        self.assertEqual(next(i.capability for i in revised.items if i.selected), Capability.STALE)
        fresh = self.review()
        self.db.execute("UPDATE config SET value='Changed {issue_number}' WHERE key='file_naming'")
        self.db.commit()
        self.assertEqual(next(i.capability for i in self.assign(fresh, Action.RENAME).items if i.selected), Capability.STALE)

    def test_comicinfo_absent_merge_preview_no_write(self):
        path = self.fixture.comic('Issue 001.cbz', xml=None)
        before = path.read_bytes(), path.stat().st_mtime_ns
        worklist = self.assign(self.review(HealthLevel.ARCHIVE), Action.COMICINFO, 'comicinfo_absent')
        item = next(i for i in worklist.items if i.selected)
        self.assertEqual(item.capability, Capability.PREVIEW, item.view())
        view = json.loads(item.preview_json)
        self.assertEqual(view['metadata']['state'], 'add')
        self.assertNotIn('xml', view['metadata'])
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_batch_collision_index(self):
        effects = [('a', '/lib/a', '/lib/b'), ('b', '/lib/b', '/lib/a'), ('c', '/lib/c', '/lib/a')]
        codes = {r['code'] for r in batch_collisions(effects, windows=False)}
        self.assertTrue({'shared_target', 'source_target_dependency_or_cycle'} <= codes)
        self.assertEqual(batch_collisions([('a', 'C:\\lib\\A.cbz', 'C:\\lib\\a.cbz')], windows=True)[0]['code'], 'case_only_requires_staging')
        self.assertIn('parent_child_collision', {r['code'] for r in batch_collisions([
            ('a', '/x/a', '/y/folder'), ('b', '/x/b', '/y/folder/file')], windows=False)})

    def test_same_destination_and_occupied_targets(self):
        self.fixture.comic('wrong-a.cbz')
        self.fixture.comic('wrong-b.cbz')
        worklist = self.review()
        edits = tuple(Edit(i.finding.id, True, False, Action.RENAME) for i in worklist.items if i.finding.code == 'filename_deviation')
        revised = self.service.revise(worklist.id, 0, edits)
        self.assertTrue(all(i.capability == Capability.BLOCKED for i in revised.items if i.selected))
        self.assertIn('shared_target', {r['code'] for r in revised.collisions()['items']})

    def test_lifetime_capacity_cancel_and_restart(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.review()
        with self.assertRaises(ReviewError):
            MaintenanceReviews(str(self.fixture.database)).get(worklist.id)
        self.service.delete(worklist.id, 0)
        with self.assertRaises(ReviewError):
            self.service.get(worklist.id)
        identifier = self.service.request_scan()
        self.service.cancel_scan(identifier)
        self.tasks[-1].run()
        self.assertEqual(self.service.scan_status(identifier)['state'], 'cancelled')
        self.clock[0] += 3601
        with self.assertRaises(ReviewError):
            self.service.scan_status(identifier)

    def test_no_arbitrary_action_and_invalid_filters(self):
        with self.assertRaises(ValueError):
            Action('delete')
        with self.assertRaises(ReviewError):
            FindingFilter(category='invented')
        with self.assertRaises(ReviewError):
            Edit('a'*64, True, True, Action.NONE)

    def test_bounds_and_digest(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.review()
        self.assertEqual(worklist.manifest_digest, worklist.manifest_digest)
        self.assertNotEqual(worklist.manifest_digest, self.assign(worklist, Action.LATER).manifest_digest)
        with self.assertRaises(ReviewError):
            worklist.page(limit=2001)
        self.service.MAX_BYTES = 10
        with self.assertRaises(ReviewError):
            self.service.create(next(iter(self.service._scans)))

    def test_unchanged_snapshot_and_review_stay_fresh(self):
        self.fixture.comic('wrong.cbz')
        scope = HealthScope('volumes', (1,))
        a = read_snapshot(str(self.fixture.database), scope, 20000)
        b = read_snapshot(str(self.fixture.database), scope, 20000)
        self.assertEqual(a['digest'], b['digest'])
        worklist = self.assign(self.review(), Action.RENAME)
        current = self.service.revalidate(worklist.id, 1)
        self.assertEqual(next(i.capability for i in current.items if i.selected), Capability.PREVIEW)

    def test_competing_revisions_exactly_one_wins(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.review()
        barrier = Barrier(2)
        from backend.features.maintenance_review import build_previews

        def synchronized(*args):
            barrier.wait(timeout=10)
            return build_previews(*args)

        def revise():
            try:
                return self.assign(worklist, Action.LATER).revision
            except ReviewError as error:
                return str(error)

        with patch('backend.features.maintenance_review.build_previews', side_effect=synchronized):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: revise(), range(2)))
        self.assertCountEqual(results, [1, 'stale_worklist_revision'])

    def test_target_appearing_after_review_keeps_original_target_stale(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.assign(self.review(), Action.RENAME)
        old = next(i for i in worklist.items if i.selected)
        target = json.loads(old.preview_json)['target']
        from pathlib import Path
        Path(target).write_bytes(b'new unrelated file')
        revised = self.service.revalidate(worklist.id, 1)
        item = next(i for i in revised.items if i.selected)
        self.assertEqual(item.capability, Capability.STALE)
        self.assertEqual(item.preview_json, old.preview_json)

    def test_taskhandler_adapter_and_cancel_does_not_stop_queue_cleanup(self):
        service = MaintenanceReviews(str(self.fixture.database))
        with patch('backend.features.tasks.TaskHandler.add', side_effect=lambda task: self.tasks.append(task) or 42):
            scan = service.request_scan()
        self.assertEqual(service.scan_status(scan)['task_id'], 42)
        service.cancel_scan(scan)
        self.assertFalse(self.tasks[-1].stop)
        self.tasks[-1].run()
        self.assertEqual(service.scan_status(scan)['state'], 'cancelled')

    def test_incomplete_scan_and_unsupported_findings_remain_reviewable(self):
        (self.fixture.volume / 'unsupported.cbr').write_bytes(b'Rar!\x1a\x07fixture')
        worklist = self.review(HealthLevel.ARCHIVE)
        self.assertEqual(worklist.summary()['source_completeness'], 'partial')
        item = next(i for i in worklist.items if i.finding.code == 'unsupported_container')
        revised = self.service.revise(worklist.id, 0, (Edit(item.finding.id, True, False, Action.ACKNOWLEDGE),))
        self.assertFalse(revised.summary()['apply_available'])
        self.assertEqual(revised.summary()['source_completeness'], 'partial')

    def test_malformed_comicinfo_not_salvaged(self):
        self.fixture.comic(xml='<ComicInfo><broken>')
        worklist = self.review(HealthLevel.ARCHIVE)
        item = next(i for i in worklist.items if i.finding.category == 'comicinfo')
        revised = self.service.revise(worklist.id, 0, (Edit(item.finding.id, True, False, Action.COMICINFO),))
        self.assertEqual(next(i.capability for i in revised.items if i.selected), Capability.BLOCKED)

    def test_unknown_xml_preserved_in_semantic_preview(self):
        self.fixture.comic(xml='<ComicInfo><Year>bad</Year><Notes>Keep me</Notes><Custom data="retain">unknown</Custom></ComicInfo>')
        worklist = self.assign(self.review(HealthLevel.ARCHIVE), Action.COMICINFO, 'invalid_field')
        item = next(i for i in worklist.items if i.selected)
        fields = json.loads(item.preview_json).get('metadata', {}).get('fields', [])
        self.assertIn(('Notes', 'preserve'), {(v['field'], v['action']) for v in fields})
        self.assertIn(('Custom', 'preserve'), {(v['field'], v['action']) for v in fields})

    def test_folder_metadata_association_intents_are_not_execution(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.assign(self.review(), Action.FOLDER, 'folder_deviation')
        self.assertIn('whole_volume_or_root_transition_unsupported', next(i.blockers for i in worklist.items if i.selected))
        self.db.execute("UPDATE volumes SET metadata_provider='metron'")
        self.db.commit()
        worklist = self.assign(self.review(), Action.METADATA, 'selected_identity_missing')
        self.assertIn('requires_provider_review_8d', next(i.blockers for i in worklist.items if i.selected))
        self.assertFalse(worklist.summary()['apply_available'])

    def test_duplicate_review_keeps_associations_separate(self):
        self.fixture.comic('a.cbz')
        self.fixture.comic('b.cbz')
        worklist = self.assign(self.review(), Action.DUPLICATE, 'same_publication_files')
        item = next(i for i in worklist.items if i.selected)
        self.assertEqual(item.capability, Capability.LATER)
        members = json.loads(item.preview_json)['members']
        self.assertEqual(len(members), 2)
        self.assertTrue(all(m['direct'] and not m['coverage'] for m in members))
        self.assertFalse(worklist.summary()['apply_available'])

    def test_creation_capacity_and_revision_limit_fail_closed(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.review()
        self.service.MAX_WORKLISTS = 1
        with self.assertRaises(ReviewError):
            self.service.create(next(iter(self.service._scans)))
        self.service.MAX_REVISIONS = 1
        revised = self.assign(worklist, Action.LATER)
        with self.assertRaises(ReviewError):
            self.service.revalidate(revised.id, 1)

    def test_rename_does_not_open_archive_and_paging_never_replans(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.review()
        with patch('backend.implementations.maintenance_review.enrich_comicinfo', side_effect=AssertionError('archive')):
            revised = self.assign(worklist, Action.RENAME)
        with patch('backend.features.maintenance_review.read_snapshot', side_effect=AssertionError('database')):
            self.service.get(revised.id).page()
            self.service.get(revised.id).collisions()

    def test_worklist_scale_diagnostics(self):
        real_connect = sqlite3.connect
        for count in (100, 1000, 2000):
            for path in self.fixture.volume.iterdir():
                path.unlink()
            for index in range(count):
                (self.fixture.volume / f'{index}.cbz').write_bytes(b'fixture')
            worklist = self.review()
            statements = []
            def connect(*args, **kwargs):
                db = real_connect(*args, **kwargs)
                db.set_trace_callback(statements.append)
                return db
            tracemalloc.start()
            start = perf_counter()
            with patch('backend.internals.library_health.sqlite3.connect', side_effect=connect):
                revised = self.service.select_filtered(worklist.id, 0, worklist.report.id,
                    worklist.report.state_digest, FindingFilter(category='filesystem'))
            elapsed = perf_counter() - start
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            selects = sum(s.lstrip().upper().startswith('SELECT') for s in statements)
            self.assertEqual(selects, 48)
            self.assertEqual(revised.summary()['selected'], count)
            size = sum(len(json.dumps(i.view())) for i in revised.items)
            print(f'Worklist {count}: {selects} SELECTs, {elapsed:.3f}s, peak {peak}, serialized items {size}')

    def test_readonly_review_with_collected_coverage(self):
        self.fixture.test_c2_coverage_is_not_direct_identity_and_remains_unchanged()
        before = tuple(self.db.iterdump()), self.fixture.filesystem()
        worklist = self.review()
        revised = self.service.select_filtered(worklist.id, 0, worklist.report.id,
            worklist.report.state_digest, FindingFilter())
        self.assertFalse(revised.summary()['apply_available'])
        self.assertEqual(before, (tuple(self.db.iterdump()), self.fixture.filesystem()))

    def test_large_collision_batch_and_preview_limit(self):
        start = perf_counter()
        effects = [(str(i), f'/lib/source{i}', '/lib/target') for i in range(250)]
        collisions = batch_collisions(effects, windows=False)
        self.assertEqual(len(collisions), 1)
        self.assertEqual(len(collisions[0]['items']), 250)
        with self.assertRaises(ReviewError):
            batch_collisions(effects + [('extra', '/lib/extra', '/lib/target')], windows=False)
        print(f'Collision index 250 selected targets: {len(collisions)} grouped diagnostic; {perf_counter()-start:.4f}s')

    def test_plan_limit_preserves_previous_revision(self):
        for index in range(251):
            self.fixture.comic(f'wrong{index}.cbz')
        worklist = self.review()
        edits = tuple(Edit(i.finding.id, True, False, Action.RENAME) for i in worklist.items if i.finding.code == 'filename_deviation')
        with self.assertRaisesRegex(ReviewError, 'preview_limit'):
            self.service.revise(worklist.id, 0, edits)
        self.assertEqual(self.service.get(worklist.id).revision, 0)

    def test_foreign_edit_values_and_size_failure_do_not_change_selection(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.review()
        with self.assertRaises(ReviewError):
            self.service.revise(worklist.id, 0, ({'path': 'client target'},))
        self.service.MAX_BYTES = 10
        with self.assertRaisesRegex(ReviewError, 'size_limit'):
            self.assign(worklist, Action.RENAME)
        self.assertEqual(self.service.get(worklist.id).summary()['selected'], 0)

    def test_changed_during_planning_is_not_published_as_current(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.review()
        from backend.features.maintenance_review import build_previews
        def changed(*args):
            result = build_previews(*args)
            self.db.execute('UPDATE volumes SET authority_generation=1')
            self.db.commit()
            return result
        with patch('backend.features.maintenance_review.build_previews', side_effect=changed):
            revised = self.assign(worklist, Action.RENAME)
        self.assertEqual(next(i.capability for i in revised.items if i.selected), Capability.STALE)

    def test_association_without_exact_proposal_blocks(self):
        self.fixture.comic()
        self.db.execute('DELETE FROM issues_files')
        self.db.commit()
        worklist = self.assign(self.review(), Action.ASSOCIATION, 'unassociated_file_row')
        self.assertIn('exact_additive_association_proposal_required', next(i.blockers for i in worklist.items if i.selected))

    def test_hostile_text_is_data_not_markup(self):
        self.fixture.comic('wrong.cbz')
        self.db.execute("UPDATE volumes SET title=?", ('<img src=x onerror=evil()>',))
        self.db.commit()
        worklist = self.assign(self.review(), Action.RENAME)
        text = json.dumps(worklist.page())
        self.assertNotIn('HEALTH_SECRET_SENTINEL', text)
        self.assertIsInstance(json.loads(text), dict)

    def test_exclusion_cannot_clear_stale_latch_and_delete_survives_limit(self):
        self.fixture.comic('wrong.cbz')
        worklist = self.assign(self.review(), Action.RENAME)
        old_setting = self.db.execute("SELECT value FROM config WHERE key='file_naming'").fetchone()[0]
        self.db.execute("UPDATE config SET value='Changed {issue_number}' WHERE key='file_naming'")
        self.db.commit()
        stale = self.service.revalidate(worklist.id, 1)
        item = next(i for i in stale.items if i.selected)
        self.assertEqual(item.capability, Capability.STALE)
        excluded = self.service.revise(worklist.id, 2, (Edit(item.finding.id, False, True, Action.NONE),))
        self.db.execute("UPDATE config SET value=? WHERE key='file_naming'", (old_setting,))
        self.db.commit()
        selected = self.service.revise(worklist.id, excluded.revision, (Edit(item.finding.id, True, False, Action.RENAME),))
        self.assertEqual(next(i.capability for i in selected.items if i.selected), Capability.STALE)
        self.service.MAX_REVISIONS = selected.revision
        self.service.delete(selected.id, selected.revision)
        with self.assertRaises(ReviewError):
            self.service.get(selected.id)
