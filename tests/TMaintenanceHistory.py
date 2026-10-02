"""History projections on disposable schema66; reads never authorize effects."""

import json
import sqlite3
import tracemalloc
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest import TestCase
from unittest.mock import patch

from backend.base.maintenance_history import HistoryError, HistoryFilter
from backend.base.organization_job import EXECUTOR_POLICY
from backend.features.maintenance_history import MaintenanceHistory
from backend.internals import maintenance_history
from backend.internals.db import DB_SCHEMA
from backend.internals.organization_jobs import canonical, digest


class MaintenanceHistoryTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.db'
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript(DB_SCHEMA)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.service = MaintenanceHistory(str(self.path))

    def job(self, identifier, *, stamp='2026-09-29T12:00:00.000000+00:00', state='completed',
            batch=None, inverse_of=None, **values):
        intent = dict(version=EXECUTOR_POLICY, source='/root/old.cbz', target='/root/new.cbz',
                      volume_id=1, inverse=bool(inverse_of), effects=[], xml=None,
                      database_before=dict(file=dict(id=7)), **values)
        payload = canonical(intent)
        self.db.execute('''INSERT INTO organization_jobs
            (id,plan_digest,executor_version,intent,intent_digest,state,created_at,updated_at,batch_id,inverse_of)
            VALUES(?,?,?,?,?,?,?,?,?,?)''', (identifier, identifier, EXECUTOR_POLICY, payload,
            digest(payload), state, stamp, stamp, batch, inverse_of))
        self.db.commit()
        return identifier

    def repair(self, identifier='repair', stamp='2026-09-29T13:00:00+00:00'):
        self.db.execute('''INSERT INTO metadata_repair_receipts VALUES
            (?,?,1,'comicvine','100',0,1,?,?,?,'worklist',?,'repair/v1',?,'operator',1,'preserve','{}','preserve')''',
            (identifier, identifier, 'a' * 64, 'b' * 64, 'c' * 64, 'd' * 64, stamp))
        self.db.execute("INSERT INTO metadata_repair_fields VALUES(?,0,'volume',1,'title',?,?)",
                        (identifier, json.dumps('Before'), json.dumps('After')))
        self.db.commit()

    def test_kinds_raw_states_and_current_eligibility_not_assumed(self):
        self.job('rename', rename_origin=dict(worklist=['w', 1, 'd']))
        self.job('folder', directory_effect='volume-tree/v1')
        self.job('comicinfo', repair_origin=dict(worklist_id='w'))
        self.job('quarantine', quarantine_effect='retained-artifact/v1', original='/root/original.cbz')
        self.job('restore', inverse_of='quarantine', quarantine_effect='retained-artifact/v1', original='/root/original.cbz')
        self.job('future', directory_effect='future/v99')
        items = {v['id']: v for v in self.service.page()['items']}
        self.assertEqual(items['rename']['operation'], 'rename')
        self.assertEqual(items['folder']['operation'], 'folder_organization')
        self.assertEqual(items['comicinfo']['inverse_capability'], 'unsupported')
        self.assertEqual(items['restore']['operation'], 'duplicate_restore')
        self.assertEqual(items['future']['operation'], 'unsupported_history')
        self.assertEqual(items['quarantine']['relationship_state'], 'reverted')
        self.assertIsNone(items['quarantine']['target'])
        self.assertEqual(items['quarantine']['source'], '/root/original.cbz')
        self.assertTrue(all(not v['eligibility_checked'] for v in items.values()))
        self.assertEqual(items['rename']['inverse_capability'], 'unchecked')

    def test_inverse_exists_is_not_proof_of_revert(self):
        self.job('forward')
        self.job('inverse', state='recovery_required', inverse_of='forward')
        result = self.service.get('organization', 'forward')
        self.assertEqual(result['relationship_state'], 'inverse_incomplete')
        self.assertEqual(result['inverse_job']['state'], 'recovery_required')

    def test_mixed_paging_ties_filters_and_cursor_binding(self):
        for index in range(12):
            self.job(f'job-{index:02}', state='recovery_required' if index == 0 else 'completed')
        self.repair()
        whole = self.service.page()['items']
        found, before = [], None
        while True:
            page = self.service.page(before=before, limit=3)
            found.extend(page['items'])
            before = page['next_cursor']
            if before is None:
                break
        self.assertEqual(found, whole)
        self.assertEqual(whole[0]['domain'], 'metadata_repair')
        self.assertEqual(len({v['entry_id'] for v in found}), 13)
        self.assertEqual(len(self.service.page(HistoryFilter(state='recovery_required'))['items']), 1)
        self.assertEqual(len(self.service.page(HistoryFilter(file_id=7))['items']), 12)
        self.assertEqual(len(self.service.page(HistoryFilter(volume_id=1))['items']), 13)
        self.assertEqual(len(self.service.page(HistoryFilter(since='2026-09-29T12:30:00.000Z'))['items']), 1)
        cursor = self.service.page(limit=3)['next_cursor']
        with self.assertRaisesRegex(HistoryError, 'cursor_filter_mismatch'):
            self.service.page(HistoryFilter(domain='organization'), before=cursor)

    def test_deleted_live_objects_do_not_hide_organization_or_repair_history(self):
        # No live IDs exist. Both domains deliberately retain historical IDs.
        self.job('historical')
        self.repair()
        self.assertEqual(self.service.get('organization', 'historical')['volume_id'], 1)
        detail = self.service.detail('metadata_repair', 'repair')
        self.assertEqual(detail['detail']['fields'][0]['before_value'], 'Before')
        self.assertEqual(detail['entry']['inverse_capability'], 'unsupported')

    def test_summary_and_details_are_read_only_detached_and_no_expensive_overview(self):
        self.job('j')
        self.repair()
        before = tuple(self.db.iterdump())
        with ExitStack() as stack:
            for name in ('backend.features.organization_execution.OrganizationExecutor.history',
                         'backend.features.organization_execution.OrganizationExecutor.preview_undo',
                         'backend.features.organization_execution.OrganizationExecutor.inspect_job',
                         'backend.features.organization_execution.artifact',
                         'backend.features.organization_execution.sha256',
                         'backend.implementations.folder_inventory.inspect_folder',
                         'zipfile.ZipFile', 'socket.socket'):
                stack.enter_context(patch(name, side_effect=AssertionError(name)))
            result = self.service.page()
            self.service.detail('organization', 'j')
            self.service.detail('metadata_repair', 'repair')
        self.assertEqual(before, tuple(self.db.iterdump()))
        result['items'][0]['summary'].clear()
        self.assertTrue(self.service.page()['items'][0]['summary'])
        with maintenance_history.connection(str(self.path)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM organization_jobs')

    def test_batch_partial_and_no_fake_jobs(self):
        origin = dict(worklist=['w', 1, 'd'], selection=['a', 'b', 'c'], no_changes=['c'])
        self.job('a', batch='batch', rename_origin=origin)
        self.job('b', batch='batch', state='recovery_required', rename_origin=origin)
        page = self.service.batch('batch', limit=1)
        self.assertEqual(page['state'], 'partially_completed_batch')
        self.assertEqual(page['selected_count'], 3)
        self.assertEqual(page['mutation_job_count'], 2)
        self.assertEqual(page['nonmutating_count'], 1)
        self.assertEqual(self.service.batch('batch', offset=2)['items'][0]['job_id'], None)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0], 2)

    def test_quarantine_mixed_group_correlation_survives_service_loss(self):
        origin = dict(worklist=['w', 1, 'd'], selection=['keep', 'quarantine', 'ack'], groups=[
            dict(id='keep', action='keep_all', retained=[2]),
            dict(id='quarantine', action='quarantine_selected', quarantine=[7], retained=[8]),
            dict(id='ack', action='acknowledge')])
        self.job('q', batch='qbatch', quarantine_effect='retained-artifact/v1', duplicate_origin=origin)
        service = MaintenanceHistory(str(self.path))
        result = service.batch('qbatch')
        self.assertEqual(result['selected_unit'], 'group')
        self.assertEqual(result['nonmutating_count'], 2)
        self.assertEqual(result['group_count'], 3)
        self.assertEqual(result['state'], 'complete')

    def test_folder_file_filter_uses_recorded_file_map(self):
        self.job('tree', directory_effect='volume-tree/v1', tree_database_before=canonical(dict(files=[dict(id=55)])))
        self.assertEqual(self.service.page(HistoryFilter(file_id=55))['items'][0]['id'], 'tree')

    def test_unknown_errors_and_large_projection_fail_closed(self):
        self.job('j')
        self.db.execute('UPDATE organization_jobs SET error=?', ('https://credential.invalid/?token=secret',))
        self.db.commit()
        self.assertEqual(self.service.get('organization', 'j')['error'], 'domain_error')
        with patch.object(maintenance_history, 'MAX_DETAIL_BYTES', 10):
            with self.assertRaisesRegex(HistoryError, 'projection_too_large'):
                self.service.detail('organization', 'j')

    def test_bounds_unknown_ids_and_filter_validation(self):
        for limit in (0, 101, True):
            with self.assertRaises(HistoryError):
                self.service.page(limit=limit)
        for args in (dict(state='undo'), dict(inverse='available'), dict(volume_id=True),
                     dict(since='yesterday'), dict(batch_id='x' * 513)):
            with self.assertRaises(HistoryError):
                HistoryFilter(**args)
        with self.assertRaisesRegex(HistoryError, 'entry_unavailable'):
            self.service.get('organization', 'missing')

    def test_history_scale_has_constant_select_count(self):
        for count in (100, 1000):
            for index in range(count // 10 if count == 100 else 90):
                self.repair('repair-' + str(count) + '-' + str(index))
            for index in range(100 if count == 100 else 900):
                self.job(str(count) + '-' + str(index), batch='batch-' + str(index // 50),
                         stamp='2026-09-29T14:00:00.000000+00:00' if count == 100 and index < 25
                         else '2026-09-29T12:00:00.000000+00:00')
            with maintenance_history.connection(str(self.path)) as db:
                selects = []
                db.set_trace_callback(lambda sql: selects.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
                tracemalloc.start()
                start = perf_counter()
                result = maintenance_history.page(db)
                seconds = perf_counter() - start
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            self.assertEqual(len(result['items']), 50)
            self.assertEqual({item['domain'] for item in result['items']}, {'organization', 'metadata_repair'})
            self.assertLessEqual(len(selects), 9)
            print('8H history diagnostics', dict(jobs=count, selects=len(selects), seconds=seconds,
                peak_bytes=peak, page_bytes=len(json.dumps(result['items']))))

    def test_actual_switch_receipt_is_history_only_and_not_snapshot_undo(self):
        import TProviderSwitchApply as fixture

        from backend.features.maintenance_recovery import MaintenanceRecovery
        case = fixture.SwitchApplyTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.source()
        session = case.review(fixture.remote('metron'))
        case.apply(session)
        case.db.backup(self.db)
        entry = self.service.page(HistoryFilter(domain='provider_switch'))['items'][0]
        detail = self.service.detail('provider_switch', entry['id'])
        self.assertEqual(detail['detail']['target_provider'], 'metron')
        self.assertEqual(len(detail['detail']['issues']), 2)
        actions = MaintenanceRecovery(str(self.path), ())
        inverse = actions.preview_inverse('provider_switch', entry['id'])
        self.assertEqual(inverse['capability'], 'fresh_operation_required')
        self.assertFalse(inverse['eligible'])
        # Existing source schema cascades switch receipts with volume deletion;
        # 8H does not weaken that policy or fabricate deleted receipts.
        self.db.execute('DELETE FROM issues_files')
        self.db.execute('DELETE FROM issues')
        self.db.execute('DELETE FROM volumes')
        self.db.commit()
        self.assertEqual(self.service.page(HistoryFilter(domain='provider_switch'))['items'], [])

    def test_actual_repair_receipt_survives_deletion_and_classification_is_current_only(self):
        import TMetadataRepairApply as fixture

        from backend.base.metadata_repair import Field, FieldSelection
        case = fixture.MetadataRepairApplyTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.apply(case.review((FieldSelection('volume', 1, Field.TITLE),)))
        case.db.backup(self.db)
        entry = self.service.page(HistoryFilter(domain='metadata_repair'))['items'][0]
        self.assertEqual(self.service.detail('metadata_repair', entry['id'])['detail']['fields'][0]['after_value'], 'Target comicvine')
        provenance = self.service.classification(1)
        self.assertFalse(provenance['complete_history'])
        self.assertEqual(provenance['inverse_capability'], 'unsupported')
        self.db.execute('DELETE FROM issues_files')
        self.db.execute('DELETE FROM issues')
        self.db.execute('DELETE FROM volumes')
        self.db.commit()
        self.assertEqual(self.service.get('metadata_repair', entry['id'])['volume_id'], 1)

    def test_c2_history_is_separate_from_live_coverage_and_retains_historical_ids(self):
        self.db.execute('''INSERT INTO bibliographic_content_claims VALUES
            ('claim','comicvine','1','comicvine','2','complete_issue_containment',
             'operator_confirmed','content/v1',1000,NULL,NULL)''')
        self.db.execute('''INSERT INTO file_content_coverage VALUES
            ('coverage',NULL,NULL,NULL,'claim',7,1,2,'content/v1',1001,NULL)''')
        self.db.commit()
        before = tuple(self.db.iterdump())
        claim = self.service.detail('content_claim', 'claim')
        coverage = self.service.detail('content_coverage', 'coverage')
        self.assertEqual(claim['detail']['claim']['source_provider_id'], '2')
        self.assertEqual(coverage['detail']['coverage']['original_file_id'], 7)
        self.assertEqual(coverage['detail']['coverage']['currently_valid'], 0)
        self.assertEqual(self.service.page(HistoryFilter(file_id=7))['items'][0]['id'], 'coverage')
        self.assertEqual(before, tuple(self.db.iterdump()))

    def test_intake_is_operational_and_task_history_is_not_a_mutation_receipt(self):
        self.db.execute('''INSERT INTO acquisition_intakes
            (id,kind,download_id,completion,completion_digest,rename,auto_apply,state,created_at,updated_at)
            VALUES('intake','sabnzbd','download','{}',?,0,0,'completed',?,?)''',
            ('a' * 64, '2026-09-29T12:00:00+00:00', '2026-09-29T12:00:00+00:00'))
        self.db.execute("INSERT INTO task_history VALUES('update_all','Task complete',1000)")
        self.db.commit()
        page = self.service.page()
        self.assertEqual(len(page['items']), 1)
        self.assertTrue(page['items'][0]['summary']['operational_only'])
        self.assertEqual(self.service.detail('intake', 'intake')['detail']['artifacts'], [])

    def test_batch_pages_fifty_and_hundred_jobs_do_not_expand_events(self):
        origin = dict(worklist=['w', 1, 'd'], selection=[str(i) for i in range(100)], no_changes=[])
        for index in range(100):
            self.job(str(index), batch='large', state='recovery_required' if index % 10 == 0 else 'completed',
                     rename_origin=origin)
        before = tuple(self.db.iterdump())
        for limit in (50, 100):
            start = perf_counter()
            result = self.service.batch('large', limit=limit)
            print('8H batch diagnostic', dict(jobs=100, page=limit,
                seconds=perf_counter() - start, bytes=len(json.dumps(result))))
            self.assertEqual(len(result['items']), limit)
            self.assertEqual(result['counts']['recovery_required'], 10)
            self.assertEqual(result['file_count'], 1)
            self.assertEqual(result['state'], 'partially_completed_batch')
        self.assertEqual(before, tuple(self.db.iterdump()))

    def test_unfiltered_overview_does_not_parse_off_page_intent_payloads(self):
        for index in range(120):
            self.job(f'{index:04}', diagnostic_identity=index)
        observed = set()
        def valid_json(value):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict) and 'diagnostic_identity' in parsed:
                    observed.add(parsed['diagnostic_identity'])
                return 1
            except (ValueError, TypeError):
                return 0
        with maintenance_history.connection(str(self.path)) as db:
            db.create_function('json_valid', 1, valid_json, deterministic=True)
            page = maintenance_history.page(db, limit=10)
        self.assertEqual(len(page['items']), 10)
        self.assertLessEqual(len(observed), 11)
