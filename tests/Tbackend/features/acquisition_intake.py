"""Disposable Phase 6 observation/preview acceptance. No downloader or provider."""

import os
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from Tbackend.features import organization_execution as execution_fixture

from backend.base.acquisition_intake import (AcquisitionCompletion,
                                             AcquisitionKind,
                                             DownloaderPathMapping,
                                             IntakeErrorCode, IntakeFailure)
from backend.base.import_candidate import DiscoveryScope
from backend.base.organization_job import JobState
from backend.base.organization_plan import PlanningPolicy, PlanStatus
from backend.features.acquisition_intake import IntakeCoordinator
from backend.features.local_artifact_planning import preview_local_artifacts
from backend.implementations.acquisition_paths import (contained,
                                                       map_download_path,
                                                       observe_artifacts)
from backend.internals.acquisition_intakes import ensure_intake
from backend.internals.intake_schema import SCHEMA as INTAKE_SCHEMA


class PathTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='kapowarr-intake-path-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.mapping = DownloaderPathMapping('map', 'sab', 'instance', '/complete', str(self.root))

    def test_explicit_posix_mapping_and_component_boundary(self):
        path, root, fingerprint = map_download_path('/complete/job/a.cbz', 'sab', 'instance', (self.mapping,))
        self.assertEqual(path, str(self.root / 'job' / 'a.cbz'))
        self.assertEqual(root, str(self.root))
        self.assertEqual(len(fingerprint), 64)
        with self.assertRaises(IntakeFailure):
            map_download_path('/completed/a.cbz', 'sab', 'instance', (self.mapping,))

    def test_windows_remote_paths_on_either_host(self):
        mapping = replace(self.mapping, remote_prefix='D:\\Completed', remote_style='windows')
        path, _, _ = map_download_path('d:\\completed\\job\\a.cbz', 'sab', 'instance', (mapping,))
        self.assertEqual(path, str(self.root / 'job' / 'a.cbz'))

    def test_longest_prefix_not_configuration_order(self):
        nested = replace(self.mapping, key='nested', remote_prefix='/complete/books', local_root=str(self.root / 'books'))
        for mappings in ((nested, self.mapping), (self.mapping, nested)):
            self.assertEqual(map_download_path('/complete/books/a.cbz', 'sab', 'instance', mappings)[0],
                             str(self.root / 'books' / 'a.cbz'))
        with self.assertRaises(IntakeFailure):
            map_download_path('/complete/a.cbz', 'sab', 'instance', (self.mapping, self.mapping))

    def test_mapping_never_falls_back_or_crosses_instance(self):
        for mappings, instance in (((), 'instance'), ((self.mapping,), 'different'),
                                   ((replace(self.mapping, enabled=False),), 'instance')):
            with self.assertRaises(IntakeFailure):
                map_download_path(str(self.root / 'a.cbz'), 'sab', instance, mappings)

    def test_traversal_device_stream_and_control_rejected(self):
        for path in ('/complete/../outside/a.cbz', '/complete/a\n.cbz', '/complete/a:b.cbz',
                     '//complete/a.cbz', '/complete/.. /outside.cbz', '/complete/NUL.cbz'):
            with self.subTest(path=path), self.assertRaises(IntakeFailure):
                map_download_path(path, 'sab', 'instance', (self.mapping,))
        windows = replace(self.mapping, remote_prefix='C:\\complete', remote_style='windows')
        for path in ('C:\\complete\\a.cbz:stream', '\\\\?\\C:\\complete\\a.cbz', 'C:\\complete\\..\\a.cbz'):
            with self.subTest(path=path), self.assertRaises(IntakeFailure):
                map_download_path(path, 'sab', 'instance', (windows,))

    def test_exact_scope_deterministic_and_no_ancillary_comic(self):
        job = self.root / 'job'
        job.mkdir()
        for name in ('b.cbz', 'a.pdf', 'readme.txt', 'cover.jpg'):
            (job / name).write_bytes(b'disposable')
        (self.root / 'unrelated.cbz').write_bytes(b'unrelated')
        artifacts = observe_artifacts((str(job),), str(self.root))
        self.assertEqual([Path(a.path).name for a in artifacts], ['a.pdf', 'b.cbz'])
        self.assertTrue(all(a.stamp[0] == len(b'disposable') for a in artifacts))

    def test_missing_and_zero_artifacts_are_distinct(self):
        with self.assertRaises(IntakeFailure) as missing:
            observe_artifacts((str(self.root / 'missing'),), str(self.root))
        self.assertEqual(missing.exception.code, IntakeErrorCode.PATH_UNAVAILABLE)
        with self.assertRaises(IntakeFailure) as empty:
            observe_artifacts((str(self.root),), str(self.root))
        self.assertEqual(empty.exception.code, IntakeErrorCode.UNSUPPORTED_ARTIFACT)

    def test_enumeration_bound_fails_without_partial_output(self):
        for n in range(4):
            (self.root / f'{n}.cbz').write_bytes(b'file')
        with patch('backend.implementations.acquisition_paths.MAX_ARTIFACTS', 2), self.assertRaises(IntakeFailure):
            observe_artifacts((str(self.root),), str(self.root))

    def test_synthetic_reparse_rejected_without_traversal(self):
        original = os.lstat

        def stat_with_reparse(path):
            result = original(path)
            if str(path) == str(self.root):
                from types import SimpleNamespace
                return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
            return result

        with patch('os.lstat', side_effect=stat_with_reparse), self.assertRaises(IntakeFailure):
            contained(str(self.root / 'a.cbz'), str(self.root))

    def test_completion_is_not_local_identity(self):
        completion = AcquisitionCompletion(AcquisitionKind.DIRECT_DOWNLOAD, 'queue-identity', 'candidate',
            1, (1,), ('/reported/file.cbz',), '2026-09-28T12:00:00+00:00')
        self.assertEqual(completion.issue_ids, (1,))
        self.assertFalse(hasattr(completion, 'existing'))
        with self.assertRaises(IntakeFailure):
            replace(completion, kind=AcquisitionKind.SABNZBD)


class LocalPreviewTests(TestCase):
    def setUp(self):
        self.fixture = execution_fixture.ExecutionTests('test_pending_survives_reopen_without_mutation')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.policy = PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt')

    def preview(self, paths=None, **kwargs):
        fixture = self.fixture
        return preview_local_artifacts(fixture.db, tuple(paths or (str(fixture.source),)),
            DiscoveryScope('intake-disposable', str(fixture.incoming)), self.policy,
            volume_id=1, issue_ids=(1,), **kwargs)

    def test_preview_has_no_effect_then_existing_executor_applies(self):
        fixture = self.fixture
        before = fixture.source.read_bytes()
        batch = self.preview()
        plan = batch.plans[0]
        self.assertEqual(plan.status, PlanStatus.READY)
        self.assertIsNone(plan.identification.candidate.existing)
        self.assertTrue(fixture.source.exists())
        self.assertEqual(fixture.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)
        self.assertEqual(fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
        job = fixture.executor.create_job(plan)
        self.assertEqual(fixture.executor.apply_job(job).state, JobState.COMPLETED)
        self.assertEqual(Path(plan.target_path).read_bytes(), before)
        self.assertEqual(fixture.db.execute('SELECT issue_id FROM issues_files').fetchall(), [(1,)])

    def test_wrong_actual_issue_cannot_be_relabelled_by_target(self):
        fixture = self.fixture
        fixture.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(2,1,202,'2',2)")
        wrong = fixture.incoming / 'Batman 002 (2020).cbz'
        fixture.source.rename(wrong)
        plan = self.preview((str(wrong),)).plans[0]
        self.assertEqual(plan.status, PlanStatus.REVIEW)
        self.assertIsNone(plan.identification.selected)
        self.assertTrue(wrong.exists())

    def test_batch_collision_blocks_both_without_mutation(self):
        second = self.fixture.incoming / 'Batman 1 (2020).cbz'
        second.write_bytes(self.fixture.source.read_bytes())
        batch = self.preview((str(second), str(self.fixture.source)))
        self.assertEqual([p.status for p in batch.plans], [PlanStatus.BLOCKED] * 2)
        self.assertTrue(second.exists())
        self.assertTrue(self.fixture.source.exists())

    def test_embedded_wrong_identity_survives_target_hypothesis(self):
        with ZipFile(self.fixture.source, 'a') as archive:
            archive.writestr('ComicInfo.xml', '<ComicInfo><Series>Other</Series><Number>1</Number><Year>2020</Year></ComicInfo>')
        plan = self.preview().plans[0]
        self.assertNotEqual(plan.status, PlanStatus.READY)
        self.assertTrue(self.fixture.source.exists())

    def test_explicit_rename_policy_changes_only_planning(self):
        self.policy = replace(self.policy, rename=False)
        plan = self.preview().plans[0]
        self.assertEqual(Path(plan.target_path).name, self.fixture.source.name)
        self.assertIsNone(plan.metadata.xml)

    def test_planning_context_not_acquired_per_file(self):
        paths = []
        for n in range(100):
            path = self.fixture.incoming / f'Batman 001 (2020) ({n}).cbz'
            path.write_bytes(self.fixture.source.read_bytes())
            paths.append(str(path))
        from backend.features import local_artifact_planning as module
        with patch.object(module, 'load_planning_records', wraps=module.load_planning_records) as load:
            batch = self.preview(paths)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(len(batch.plans), 100)

    def test_bulk_query_bounds_one_hundred_thousand_artifacts(self):
        paths = []
        payload = self.fixture.source.read_bytes()
        for number in range(1000):
            path = self.fixture.incoming / f'Batman 001 (2020) copy {number}.cbz'
            path.write_bytes(payload)
            paths.append(str(path))
        receipts = []
        for count in (1, 100, 1000):
            queries = []
            self.fixture.db.set_trace_callback(queries.append)
            started = time.perf_counter()
            batch = self.preview(paths[:count])
            elapsed = time.perf_counter() - started
            self.fixture.db.set_trace_callback(None)
            reads = sum(q.lstrip().upper().startswith(('SELECT', 'WITH')) for q in queries)
            self.assertEqual(len(batch.plans), count)
            self.assertLessEqual(reads, 10 + (count + 399) // 400)
            receipts.append((count, reads, round(elapsed, 4)))
        print('\nPhase 6 local preview artifacts/SELECTs/seconds:', receipts)


class DurableIntakeTests(TestCase):
    def setUp(self):
        self.fixture = execution_fixture.ExecutionTests('test_pending_survives_reopen_without_mutation')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.db.executescript(INTAKE_SCHEMA)
        self.fixture.db.execute("UPDATE config SET value=57 WHERE key='database_version'")
        self.clock = 100.
        self.completion = AcquisitionCompletion(AcquisitionKind.DIRECT_DOWNLOAD, 'durable-ddl', 'candidate',
            1, (1,), (str(self.fixture.source),), '2026-09-28T12:00:00+00:00')
        self.identifier = ensure_intake(self.fixture.db, self.completion, rename=True, auto_apply=True,
                                        local_root=str(self.fixture.incoming))
        self.coordinators = []
        self.addCleanup(self.close_coordinators)
        self.coordinator = self.open()

    def close_coordinators(self):
        for coordinator in self.coordinators:
            coordinator.close()
        self.coordinators.clear()

    def open(self, **kwargs):
        coordinator = IntakeCoordinator(self.fixture.dbpath, clock=lambda: self.clock, **kwargs)
        self.coordinators.append(coordinator)
        return coordinator

    def stable(self):
        self.coordinator.process(self.identifier)
        self.clock += 11

    def test_stability_then_organization_and_repeated_completion(self):
        self.stable()
        result = self.coordinator.process(self.identifier)
        self.assertEqual(result['state'], 'completed', result)
        self.assertFalse(self.fixture.source.exists())
        self.assertEqual(result['artifacts'][0]['state'], 'organized')
        replay = ensure_intake(self.fixture.db, self.completion, rename=False, auto_apply=False)
        self.assertEqual(replay, self.identifier)
        self.assertEqual(self.coordinator.process(replay), result)
        self.assertEqual(self.fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)

    def test_missing_local_path_preserves_remote_completion(self):
        self.fixture.source.unlink()
        result = self.coordinator.process(self.identifier)
        self.assertEqual(result['state'], 'waiting')
        self.assertEqual(result['error'], IntakeErrorCode.PATH_UNAVAILABLE.value)
        self.assertEqual(self.coordinator.store.completion(self.identifier), self.completion)

    def test_artifact_change_resets_stability(self):
        self.stable()
        with ZipFile(self.fixture.source, 'a') as archive:
            archive.writestr('002.jpg', b'changed')
        result = self.coordinator.process(self.identifier)
        self.assertEqual(result['state'], 'observing')
        self.assertEqual(self.fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
        self.clock += 11
        self.assertEqual(self.coordinator.process(self.identifier)['state'], 'completed')

    def test_wrong_file_review_survives_restart(self):
        with ZipFile(self.fixture.source, 'a') as archive:
            archive.writestr('ComicInfo.xml', '<ComicInfo><Series>Other</Series><Number>6</Number></ComicInfo>')
        self.stable()
        result = self.coordinator.process(self.identifier)
        self.assertEqual(result['state'], 'review')
        self.close_coordinators()
        self.coordinator = self.open()
        self.assertEqual(self.coordinator.process(self.identifier), result)
        self.assertTrue(self.fixture.source.exists())

    def test_after_apply_crash_reconciles_same_job_without_moving_again(self):
        self.stable()

        def checkpoint(stage, artifact):
            if stage == 'after_apply':
                raise execution_fixture.Interrupted()

        self.coordinator.checkpoint = checkpoint
        with self.assertRaises(execution_fixture.Interrupted):
            self.coordinator.process(self.identifier)
        self.assertFalse(self.fixture.source.exists())
        self.close_coordinators()
        self.coordinator = self.open()
        with patch('backend.features.organization_execution.rename_no_replace', side_effect=AssertionError('second move')):
            result = self.coordinator.process(self.identifier)
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(self.fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)

    def test_job_creation_crash_recovers_link_not_replacement_job(self):
        self.stable()

        def checkpoint(stage, artifact):
            if stage == 'after_job':
                raise execution_fixture.Interrupted()

        self.coordinator.checkpoint = checkpoint
        with self.assertRaises(execution_fixture.Interrupted):
            self.coordinator.process(self.identifier)
        self.close_coordinators()
        self.coordinator = self.open()
        result = self.coordinator.process(self.identifier)
        self.assertEqual(result['state'], 'review', result)
        self.assertIsNotNone(result['artifacts'][0]['organization_job_id'])
        self.assertEqual(result['artifacts'][0]['error'], IntakeErrorCode.RECOVERY.value)
        self.assertEqual(self.fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)
        self.assertTrue(self.fixture.source.exists())

    def test_preview_only_policy_cannot_apply(self):
        self.fixture.db.execute('UPDATE acquisition_intakes SET auto_apply=0')
        self.stable()
        result = self.coordinator.process(self.identifier)
        self.assertEqual(result['state'], 'review')
        self.assertEqual(result['artifacts'][0]['state'], 'ready')
        self.assertEqual(self.fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)

    def test_sab_mapping_required_then_explicit_mapping(self):
        sab = replace(self.completion, kind=AcquisitionKind.SABNZBD, download_id='sab-local-job',
                      client_id='sab', client_instance='instance', remote_job_id='SABnzbd_nzo_1',
                      reported_paths=('/remote/' + self.fixture.source.name,))
        identifier = ensure_intake(self.fixture.db, sab, rename=True, auto_apply=True)
        result = self.coordinator.process(identifier)
        self.assertEqual(result['state'], 'review')
        self.assertEqual(result['error'], IntakeErrorCode.PATH_MAPPING.value)
        self.fixture.db.execute('''INSERT INTO acquisition_path_mappings
            VALUES('map','sab','instance','/remote','posix',?,1,NULL)''', (str(self.fixture.incoming),))
        self.coordinator.store.state(identifier, 'pending')
        self.coordinator.process(identifier)
        self.clock += 11
        result = self.coordinator.process(identifier)
        self.assertEqual(result['state'], 'completed', result)

    def test_mapping_drift_cannot_retarget_observed_intake(self):
        sab = replace(self.completion, kind=AcquisitionKind.SABNZBD, download_id='sab-drift',
                      client_id='sab', client_instance='instance', remote_job_id='SABnzbd_nzo_2',
                      reported_paths=('/remote/' + self.fixture.source.name,))
        identifier = ensure_intake(self.fixture.db, sab, rename=True, auto_apply=True)
        self.fixture.db.execute('''INSERT INTO acquisition_path_mappings
            VALUES('map','sab','instance','/remote','posix',?,1,NULL)''', (str(self.fixture.incoming),))
        self.coordinator.process(identifier)
        self.fixture.db.execute("UPDATE acquisition_path_mappings SET local_root=?", (str(self.fixture.folder),))
        self.clock += 11
        result = self.coordinator.process(identifier)
        self.assertEqual(result['state'], 'review')
        self.assertEqual(result['error'], IntakeErrorCode.PATH_MAPPING.value)
        self.assertTrue(self.fixture.source.exists())

    def test_receipt_tamper_is_not_accepted(self):
        with self.assertRaises(IntakeFailure):
            ensure_intake(self.fixture.db, replace(self.completion, candidate_id='different'),
                          rename=True, auto_apply=True)

    def test_cross_intake_known_batch_collision_has_no_first_winner(self):
        from backend.features.intake_runtime import IntakeRuntime
        second = self.fixture.incoming / 'Batman 1 (2020).cbz'
        second.write_bytes(self.fixture.source.read_bytes())
        other = replace(self.completion, download_id='independent-request', reported_paths=(str(second),))
        ensure_intake(self.fixture.db, other, rename=True, auto_apply=True, local_root=str(self.fixture.incoming))
        runtime = IntakeRuntime(self.fixture.dbpath, clock=lambda: self.clock)
        runtime.tick()
        self.clock += 11
        runtime.tick()
        self.assertEqual(self.fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 0)
        self.assertEqual(self.fixture.db.execute('SELECT DISTINCT state FROM acquisition_intakes').fetchall(), [('review',)])
        self.assertTrue(self.fixture.source.exists())
        self.assertTrue(second.exists())

    def test_retry_requires_two_fresh_observations(self):
        self.stable()
        self.coordinator.store.state(self.identifier, 'review')
        self.coordinator.store.retry(self.identifier)
        self.assertEqual(self.coordinator.process(self.identifier)['state'], 'observing')
        self.assertTrue(self.fixture.source.exists())
        self.clock += 11
        self.assertEqual(self.coordinator.process(self.identifier)['state'], 'completed')

    def test_review_api_is_authenticated_read_only_and_restart_safe(self):
        from flask import Flask

        from frontend.api import api
        app = Flask(__name__)
        app.register_blueprint(api, url_prefix='/api')
        self.coordinator.store.state(self.identifier, 'review', IntakeErrorCode.IDENTIFICATION)
        before = tuple(self.fixture.db.iterdump())
        with patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                patch('backend.internals.db.DBConnection.default_file', self.fixture.dbpath), \
                patch('socket.socket', side_effect=AssertionError('No provider or release requests')):
            settings.return_value.sv.api_key = 'test-intake-api-key'
            client = app.test_client()
            self.assertEqual(client.get('/api/acquisition-intakes').status_code, 401)
            suffix = '?api_key=test-intake-api-key'
            result = client.get('/api/acquisition-intakes' + suffix)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['result'][0]['state'], 'review')
            self.assertEqual(tuple(self.fixture.db.iterdump()), before)
            self.assertEqual(client.get('/api/acquisition-intakes/' + self.identifier + '/retry' + suffix).status_code, 405)
            self.assertEqual(client.post('/api/acquisition-intakes/' + self.identifier + '/retry' + suffix).status_code, 200)
            self.assertEqual(self.coordinator.store.get(self.identifier)['state'], 'pending')

    def test_idle_worker_does_not_enumerate_or_prepare(self):
        from backend.features.intake_runtime import IntakeRuntime
        self.coordinator.store.state(self.identifier, 'review')
        with patch('backend.features.acquisition_intake.observe_artifacts', side_effect=AssertionError('idle enumeration')), \
                patch('backend.features.acquisition_intake.prepare_artifacts', side_effect=AssertionError('idle preparation')):
            IntakeRuntime(self.fixture.dbpath).tick()

    def test_partial_filesystem_effect_retains_exact_job_for_explicit_recovery(self):
        from backend.base.organization_plan import EffectKind
        from backend.features.acquisition_intake import act_on_linked_job
        from backend.features.organization_execution import \
            OrganizationExecutor
        self.stable()
        def create_executor(database, roots):
            executor = OrganizationExecutor(database, roots)
            def interrupt(stage, job, ordinal):
                if stage == 'after_effect' and executor.store.get(job).steps[ordinal].kind == EffectKind.RELOCATE.value:
                    raise execution_fixture.Interrupted()
            executor.hook = interrupt
            return executor
        with patch('backend.features.acquisition_intake.OrganizationExecutor', side_effect=create_executor):
            with self.assertRaises(execution_fixture.Interrupted):
                self.coordinator.process(self.identifier)
        self.assertFalse(self.fixture.source.exists())
        result = self.coordinator.process(self.identifier)
        self.assertEqual(result['state'], 'review')
        artifact = result['artifacts'][0]
        job = artifact['organization_job_id']
        inspected = act_on_linked_job(self.fixture.dbpath, self.identifier, artifact['id'], 'reconcile')
        self.assertEqual(inspected['id'], job)
        resumed = act_on_linked_job(self.fixture.dbpath, self.identifier, artifact['id'], 'resume')
        self.assertEqual(resumed['state'], 'completed')
        self.assertEqual(self.coordinator.store.preview(self.identifier)['state'], 'completed')
        self.assertEqual(self.fixture.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 1)

    def test_multi_artifact_pack_keeps_review_sibling_and_reports_partial(self):
        ambiguous = self.fixture.incoming / 'Unrelated 009 (2020).cbz'
        ambiguous.write_bytes(self.fixture.source.read_bytes())
        completion = replace(self.completion, download_id='pack',
                             reported_paths=(str(self.fixture.source), str(ambiguous)))
        identifier = ensure_intake(self.fixture.db, completion, rename=True, auto_apply=True,
                                  local_root=str(self.fixture.incoming))
        self.coordinator.process(identifier)
        self.clock += 11
        result = self.coordinator.process(identifier)
        self.assertEqual(result['state'], 'partial', result)
        self.assertEqual({a['state'] for a in result['artifacts']}, {'organized', 'review'})
        self.assertTrue(ambiguous.exists())
        self.assertFalse(self.fixture.source.exists())
