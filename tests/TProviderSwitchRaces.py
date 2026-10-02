"""Deterministic production refresh/scan continuation races, no timing sleeps."""

import asyncio
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import TProviderSwitchApply as apply_fixtures
from fixtures.metadata_refresh import RefreshHarness

from backend.base.switch_review import SwitchReviewError
from backend.features.provider_switch_review import ProviderSwitchReviews
from backend.implementations.file_matching import scan_files
from backend.implementations.metadata.refresh import refresh_single_group
from backend.implementations.metadata.switch_target import admit
from backend.implementations.volumes import _refresh_provider_group
from backend.internals.provider_authority import capture


class RefreshSwitchRaceTests(RefreshHarness, TestCase):
    async def switch(self, provider, parent='700', first=701):
        result = apply_fixtures.remote(provider, parent, first)
        async def acquire(reference):
            return admit(result, reference)
        service = ProviderSwitchReviews(acquire=acquire, task_observer=lambda _: ())
        cursor = self.db.cursor()
        session = await service.create(cursor, self.volume_id, provider, parent)
        session = service.revise(cursor, session.id, 1, {1: str(first), 2: str(first + 1)})
        service.apply(cursor, session.id, session.revision, session.preview.view()['mapping_digest'],
                      confirmed=True, expected_authority=capture(cursor, (self.volume_id,))[self.volume_id])

    def test_cv_stale_first_and_second_write_stages_including_aba(self):
        for stage, reverse in (('volume', False), ('issues', False), ('volume', True), ('issues', True), ('bulk', False)):
            with self.subTest(stage=stage, reverse=reverse):
                if capture(self.db.cursor(), (self.volume_id,))[self.volume_id].provider != 'comicvine':
                    asyncio.run(self.switch('comicvine', '2127', 301))
                stale = apply_fixtures.remote('comicvine', '2127', 301).metadata
                async def switch_during_fetch():
                    await self.switch('metron')
                    if reverse:
                        await self.switch('comicvine', '2127', 301)
                async def volumes(ids):
                    if stage in ('volume', 'bulk'):
                        await switch_during_fetch()
                    return [stale]
                async def issues(ids):
                    if stage == 'issues':
                        await switch_during_fetch()
                    return stale.issues
                fake = SimpleNamespace(fetch_volumes=volumes, fetch_issues=issues)
                self.scan.reset_mock()
                with patch('backend.implementations.volumes.get_bulk_volume_provider', return_value=fake):
                    with self.assertRaisesRegex(SwitchReviewError, 'stale_metadata_authority'):
                        _refresh_provider_group('comicvine', {'2127': (self.volume_id, 0)}, datetime.now(),
                                                None if stage == 'bulk' else self.volume_id, False, stage == 'bulk')
                self.scan.assert_not_called()
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM issues').fetchone()[0], 2)
                self.assertFalse(self.db.in_transaction)

    def test_single_metron_and_gcd_pre_fetch_generation_reaches_writer(self):
        for source, target in (('metron', 'gcd'), ('gcd', 'metron')):
            for reverse in (False, True):
                with self.subTest(source=source, reverse=reverse):
                    current = capture(self.db.cursor(), (self.volume_id,))[self.volume_id]
                    if current.provider != source:
                        asyncio.run(self.switch(source))
                    current = capture(self.db.cursor(), (self.volume_id,))[self.volume_id]
                    source_first = int(self.db.execute('SELECT provider_id FROM issue_external_ids WHERE issue_id=1 AND provider=?', (source,)).fetchone()[0])
                    fetched = apply_fixtures.remote(source, current.provider_id, source_first)
                    async def fetch(*args):
                        await self.switch(target)
                        if reverse:
                            await self.switch(source, current.provider_id, source_first)
                        return fetched
                    with patch('backend.implementations.metadata.refresh.get_db', side_effect=self.db.cursor), \
                         patch('backend.implementations.metadata.snapshot_persistence.get_db', side_effect=self.db.cursor), \
                         patch('backend.implementations.metadata.refresh.fetch_volume_result', side_effect=fetch), \
                         patch('backend.implementations.file_matching.scan_files') as scan:
                        with self.assertRaisesRegex(SwitchReviewError, 'stale_metadata_authority'):
                            refresh_single_group(SimpleNamespace(), source, {current.provider_id: (self.volume_id, 0)},
                                                 datetime.now(), self.volume_id, False)
                    scan.assert_not_called()
                    self.assertFalse(self.db.in_transaction)

    def test_cv_switch_between_commits_blocks_deletion_classification_and_scan(self):
        for boundary in (2, 3):
            with self.subTest(boundary=boundary):
                if capture(self.db.cursor(), (self.volume_id,))[self.volume_id].provider != 'comicvine':
                    asyncio.run(self.switch('comicvine', '2127', 301))
                self.prepare_refresh()
                commits = 0
                receipt = None
                def commit_then_switch():
                    nonlocal commits, receipt
                    self.db.commit()
                    commits += 1
                    if commits == boundary:
                        asyncio.run(self.switch('metron'))
                        receipt = self.db.execute('SELECT * FROM classification_provenance').fetchall()
                with patch('backend.implementations.volumes.commit', side_effect=commit_then_switch), \
                     patch('backend.implementations.volumes.scan_files', side_effect=scan_files), \
                     patch('backend.implementations.file_matching.get_db', side_effect=self.db.cursor), \
                     patch('backend.implementations.file_matching._scan_files') as scan_body:
                    with self.assertRaisesRegex(SwitchReviewError, 'stale_metadata_authority'):
                        self.refresh(allow_skipping=False)
                    scan_body.assert_not_called()
                self.assertEqual(self.db.execute('SELECT * FROM classification_provenance').fetchall(), receipt)
                self.assertEqual(self.db.execute('SELECT id FROM issues ORDER BY id').fetchall(), [(1,), (2,)])


class ScanContinuationTests(TestCase):
    def test_real_scan_body_defers_commit_until_after_cleanup(self):
        fixture = apply_fixtures.SwitchApplyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.source()
        path = str(fixture.path.parent / 'real-scan.sqlite')
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        fixture.db.backup(db)
        other = sqlite3.connect(path, timeout=0)
        self.addCleanup(other.close)
        token = capture(db.cursor(), (1,))[1]
        checkpoints = []
        def check(label):
            self.assertTrue(db.in_transaction)
            with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                other.execute('UPDATE volumes SET authority_generation=1 WHERE id=1')
            other.rollback()
            checkpoints.append(label)
        def listing(*args, **kwargs):
            check('planning')
            return [str(fixture.path)]
        settings = SimpleNamespace(create_empty_volume_folders=True, delete_empty_folders=True,
                                   unmonitor_deleted_issues=True)
        with patch('backend.implementations.file_matching.get_db', side_effect=db.cursor), \
             patch('backend.implementations.file_matching.Settings') as configured, \
             patch('backend.implementations.volumes.Volume') as volume, \
             patch('backend.implementations.file_matching.list_files', side_effect=listing), \
             patch('backend.implementations.file_matching.FilesDB.delete_unmatched_files', side_effect=lambda: check('writes')), \
             patch('backend.implementations.file_matching.delete_empty_child_folders', side_effect=lambda *a, **k: check('cleanup')):
            configured.return_value.get_settings.return_value = settings
            volume.return_value.get_data.return_value = SimpleNamespace(folder=fixture.folder.name, root_folder=1)
            volume.return_value.get_issues.return_value = [SimpleNamespace(id=1, calculated_issue_number=1.0, date='2020-01-01')]
            volume.return_value.get_all_files.return_value = [{'filepath': str(fixture.path), 'id': 1}]
            scan_files(1, expected_authority=token)
            self.assertEqual(checkpoints, ['planning', 'writes', 'cleanup', 'planning'])
            self.assertFalse(db.in_transaction)
            other.execute('UPDATE volumes SET authority_generation=1 WHERE id=1')
            other.commit()
            before = len(checkpoints)
            with self.assertRaisesRegex(SwitchReviewError, 'stale_metadata_authority'):
                scan_files(1, expected_authority=token)
            self.assertEqual(len(checkpoints), before)

    def test_guard_covers_planning_writes_and_cleanup_without_internal_commit(self):
        fixture = apply_fixtures.SwitchApplyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.source()
        path = str(fixture.path.parent / 'scan.sqlite')
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        fixture.db.backup(db)
        other = sqlite3.connect(path, timeout=0)
        self.addCleanup(other.close)
        token = capture(db.cursor(), (1,))[1]
        checkpoints = []
        def attempt(label):
            checkpoints.append(label)
            with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                other.execute('UPDATE volumes SET authority_generation=1 WHERE id=1')
            other.rollback()
        def body(volume_id, paths, unmatched, websocket, defer=False):
            self.assertTrue(defer)
            attempt('planning')
            db.execute("UPDATE issues SET title='Scan fixture' WHERE id=1")
            attempt('writes')
            attempt('cleanup')
        with patch('backend.implementations.file_matching.get_db', side_effect=db.cursor), \
             patch('backend.implementations.file_matching._scan_files', side_effect=body):
            scan_files(1, expected_authority=token)
            self.assertEqual(checkpoints, ['planning', 'writes', 'cleanup'])
            other.execute('UPDATE volumes SET authority_generation=1 WHERE id=1')
            other.commit()
            with self.assertRaisesRegex(SwitchReviewError, 'stale_metadata_authority'):
                scan_files(1, expected_authority=token)
            self.assertEqual(len(checkpoints), 3)
        with patch('backend.implementations.file_matching.get_db', side_effect=db.cursor), \
             patch('backend.implementations.file_matching._scan_files') as manual:
            scan_files(1)
            manual.assert_called_once_with(1, [], True, False, True)
