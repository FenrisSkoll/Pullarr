"""Authenticated transport on real disposable services; no provider IO."""

import json
import sqlite3
import tracemalloc
from time import perf_counter
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import TLibraryHealth as fixtures
from flask import Blueprint, Flask

from backend.base.maintenance_review import ReviewError
from backend.features.maintenance_runtime import MaintenanceRuntime
from frontend.api import auth, error_handler, return_api
from frontend.maintenance_api import MAX_BODY, register


class MaintenanceAPITests(TestCase):
    def setUp(self):
        self.fixture = fixtures.LibraryHealthTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.comic('wrong.cbz')
        self.tasks = []
        self.clock = [10.0]
        self.runtime = MaintenanceRuntime(str(self.fixture.database),
            enqueue=lambda task: self.tasks.append(task) or len(self.tasks), clock=lambda: self.clock[0])
        self.app = Flask(__name__)
        self.app.extensions['maintenance'] = self.runtime
        blueprint = Blueprint('maintenance_tests', __name__)
        register(blueprint, auth, error_handler, return_api)
        self.app.register_blueprint(blueprint, url_prefix='/api')
        self.client = self.app.test_client()
        for target, options in (
            ('frontend.api.Settings', dict(return_value=SimpleNamespace(sv=SimpleNamespace(api_key='fixture-auth-key')))),
            ('frontend.api.StartTypeHandlers.diffuse_timer', dict(return_value=None)),
        ):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, method, path, body=None, status=200, authorized=True, **kwargs):
        separator = '&' if '?' in path else '?'
        response = self.client.open('/api/maintenance' + path
            + (separator + 'api_key=fixture-auth-key' if authorized else ''),
            method=method, **({'json': body} if body is not None else {}), **kwargs)
        self.assertEqual(response.status_code, status, response.get_json(silent=True))
        return response.get_json()['result']

    def scan(self):
        scan = self.request('POST', '/scans', dict(scope=dict(kind='volumes', ids=[1]), level='inventory'))
        self.assertEqual(scan['state'], 'queued')
        self.tasks[-1].run()
        return self.request('GET', '/scans/' + scan['id'])

    def worklist(self):
        scan = self.scan()
        return self.request('POST', '/worklists', dict(scan_id=scan['id']))

    def test_every_route_requires_auth_before_payload_validation(self):
        sid, fid = 'a' * 32, 'b' * 64
        routes = [('POST', '/scans'), ('GET', f'/scans/{sid}'),
            ('POST', f'/scans/{sid}/cancel'), ('GET', f'/scans/{sid}/findings'),
            ('POST', '/worklists'), ('GET', f'/worklists/{sid}'), ('DELETE', f'/worklists/{sid}'),
            ('GET', f'/worklists/{sid}/items'), ('POST', f'/worklists/{sid}/revise'),
            ('POST', f'/worklists/{sid}/select-filtered'), ('POST', f'/worklists/{sid}/revalidate'),
            ('GET', f'/review-tasks/{sid}'), ('GET', '/history'),
            ('GET', f'/history/organization/{fid}'), ('GET', '/batches/test-batch')]
        self.assertEqual(len(routes), 15)
        for method, path in routes:
            with self.subTest(method=method, path=path):
                self.request(method, path, authorized=False, status=401)
        self.assertFalse(self.tasks)

    def test_real_scan_worklist_task_revision_preserves_database_and_bytes(self):
        before = tuple(self.fixture.db.iterdump()), self.fixture.filesystem()
        with patch('socket.socket', side_effect=AssertionError('network')):
            worklist = self.worklist()
            path = '/worklists/' + worklist['id']
            page = self.request('GET', path + '/items?limit=1')
            self.assertEqual(len(page['items']), 1)
            items = self.request('GET', path + '/items')['items']
            item = next(i for i in items if i['finding']['code'] == 'filename_deviation')
            self.assertNotIn('evidence', item['finding'])
            self.assertNotIn('preview', item)
            task = self.request('POST', path + '/revise', dict(revision=0, edits=[dict(
                finding_id=item['finding']['id'], selected=True, excluded=False, action='rename')]))
            self.assertEqual(self.request('GET', path)['revision'], 0)
            self.tasks[-1].run()
            outcome = self.request('GET', '/review-tasks/' + task['id'])
            self.assertEqual(outcome['state'], 'complete')
            self.assertTrue(outcome['operational_only'])
            self.assertFalse(outcome['library_mutation'])
            revised = self.request('GET', path)
            self.assertEqual(revised['revision'], 1)
            self.assertEqual(revised['selected'], 1)
            self.assertFalse(revised['apply_available'])
            stale = self.request('POST', path + '/revalidate', dict(revision=0))
            self.tasks[-1].run()
            self.assertEqual(self.request('GET', '/review-tasks/' + stale['id'])['reason'], 'revision_conflict')
        self.assertEqual(before, (tuple(self.fixture.db.iterdump()), self.fixture.filesystem()))
        self.assertEqual(self.fixture.db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual(self.fixture.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_select_filtered_exact_snapshot_and_cancellation(self):
        worklist = self.worklist()
        path = '/worklists/' + worklist['id']
        # Scan handle and report ID are different backend identities.
        task = self.request('POST', path + '/select-filtered', dict(revision=0,
            report_id=worklist['scan_id'], snapshot_digest=worklist['snapshot_digest'], filters={}, selected=True))
        self.tasks[-1].run()
        self.assertEqual(self.request('GET', '/review-tasks/' + task['id'])['state'], 'complete')
        updated = self.request('GET', path)
        self.assertEqual(updated['selected'], updated['total'])
        self.request('DELETE', path, dict(revision=0), status=409)
        self.request('DELETE', path, dict(revision=1))
        self.request('GET', path, status=410)

    def test_malformed_bounds_and_arbitrary_intent_rejected(self):
        self.request('POST', '/scans', status=400, data='{bad', content_type='application/json')
        self.request('POST', '/scans', status=400, data='{}', content_type='text/plain')
        self.request('POST', '/scans', status=400, data='{"scope":{},"scope":{},"level":"inventory"}', content_type='application/json')
        self.request('POST', '/scans', status=413, data=' ' * (MAX_BODY + 1), content_type='application/json')
        for body in (dict(scope=dict(kind='volumes', ids=[True]), level='inventory'),
                     dict(scope=dict(kind='library', ids=[]), level='invalid'),
                     dict(scope=dict(kind='library', ids=[]), level='inventory', target='C:\\arbitrary'),
                     dict(scope=dict(kind='volumes', ids=list(range(1, 1002))), level='inventory')):
            self.request('POST', '/scans', body, status=400)
        for query in ('limit=0', 'limit=101', 'limit=true', 'limit=1&limit=2', 'path=x', 'inverse=available'):
            self.request('GET', '/history?' + query, status=400)
        self.request('GET', '/history?batch_id=' + 'a' * 8200, status=413)
        self.request('GET', '/scans/not-an-id', status=400)
        self.assertFalse(self.tasks)

    def test_scan_cancel_cooperative_and_no_state_changing_get(self):
        scan = self.request('POST', '/scans', dict(scope=dict(kind='library', ids=[]), level='deep'))
        result = self.request('POST', '/scans/' + scan['id'] + '/cancel', {})
        self.assertFalse(result['immediate_io_interrupt'])
        self.tasks[-1].run()
        self.assertEqual(self.request('GET', '/scans/' + scan['id'])['state'], 'cancelled')
        response = self.client.get('/api/maintenance/scans/' + scan['id'] + '/cancel?api_key=fixture-auth-key')
        self.assertEqual(response.status_code, 405)

    def test_expiry_restart_capacity_and_task_errors_are_safe(self):
        worklist = self.worklist()
        for _ in range(self.runtime.MAX_DELIVERIES):
            self.runtime.submit_worklist('revalidate', worklist['id'], 0)
        self.request('POST', '/worklists/' + worklist['id'] + '/revalidate', dict(revision=0), status=429)
        self.clock[0] += 901
        task = self.runtime.submit_worklist('revalidate', worklist['id'], 0)
        with patch.object(self.runtime.reviews, 'revalidate', side_effect=RuntimeError('SECRET https://private/token')):
            self.tasks[-1].run()
        response = self.request('GET', '/review-tasks/' + task['id'])
        self.assertEqual(response['reason'], 'internal_error')
        self.assertNotIn('SECRET', json.dumps(response))
        self.app.extensions['maintenance'] = MaintenanceRuntime(str(self.fixture.database))
        self.request('GET', '/worklists/' + worklist['id'], status=410)
        self.request('GET', '/review-tasks/' + task['id'], status=410)

    def test_history_overview_no_artifact_or_inverse_work(self):
        from TMaintenanceHistory import MaintenanceHistoryTests
        fixture = MaintenanceHistoryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for index in range(6):
            fixture.job('job-' + str(index), rename_origin=dict(worklist=['w', 1, 'd']))
        self.runtime.history = fixture.service
        with patch('backend.features.organization_execution.OrganizationExecutor.preview_undo', side_effect=AssertionError('inverse')), \
                patch('socket.socket', side_effect=AssertionError('network')), \
                patch('zipfile.ZipFile', side_effect=AssertionError('archive')):
            first = self.request('GET', '/history?limit=2&operation=rename')
            from urllib.parse import urlencode
            second = self.request('GET', '/history?' + urlencode(dict(limit=2, operation='rename', before=json.dumps(first['next_cursor']))))
            self.assertFalse({i['id'] for i in first['items']} & {i['id'] for i in second['items']})
            self.request('GET', '/history?' + urlencode(dict(limit=2, operation='folder_organization', before=json.dumps(first['next_cursor']))), status=400)
            detail = self.request('GET', '/history/organization/job-0?limit=1')
            self.assertFalse(detail['entry']['eligibility_checked'])
        with patch.object(self.runtime.history, 'page', side_effect=RuntimeError('SECRET https://private/token')):
            response = self.request('GET', '/history', status=500)
            self.assertEqual(response, dict(reason='internal_error'))
        self.request('GET', '/history/organization/nonexistent', status=404)

    def test_services_share_the_application_owner(self):
        for service in (self.runtime.metadata, self.runtime.comicinfo, self.runtime.rename,
                        self.runtime.folder, self.runtime.duplicates):
            self.assertIs(service.maintenance, self.runtime.reviews)
        with self.assertRaises(ReviewError):
            self.runtime.submit_worklist('apply', 'a' * 32, 0)

    def test_api_page_diagnostics_are_bounded_and_read_only(self):
        from TMaintenanceHistory import MaintenanceHistoryTests
        fixture = MaintenanceHistoryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for index in range(100):
            fixture.job('job-' + str(index), batch='diagnostic',
                        rename_origin=dict(worklist=['w', 1, 'd'], selection=[], no_changes=[]))
        self.runtime.history = fixture.service
        for index in range(60):
            self.fixture.comic(f'unregistered-{index}.cbz', registered=False)
        scan = self.scan()
        worklist = self.request('POST', '/worklists', dict(scan_id=scan['id']))
        connect = sqlite3.connect
        selects = []
        def observed(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(lambda sql: selects.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
            return db
        for endpoint in ('/scans/' + scan['id'], '/scans/' + scan['id'] + '/findings?limit=50',
                         '/worklists/' + worklist['id'] + '/items?limit=50',
                         '/history?limit=50', '/batches/diagnostic?limit=50'):
            selects.clear()
            tracemalloc.start()
            started = perf_counter()
            with patch('sqlite3.connect', side_effect=observed):
                result = self.request('GET', endpoint)
            elapsed = perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            size = len(json.dumps(result).encode())
            self.assertLessEqual(len(result.get('items', result.get('findings', []))), 50)
            self.assertLess(len(selects), 15)
            print('8I API diagnostic', dict(endpoint=endpoint.split('?')[0].split('/')[1],
                selects=len(selects), seconds=round(elapsed, 4), peak_bytes=peak, response_bytes=size))
