"""Authenticated server-owned archive plans and bounded tasks."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import TArchiveMaintenance as fixtures
from flask import Blueprint, Flask

from backend.features.archive_maintenance import ArchiveMaintenance
from frontend.api import auth, error_handler, return_api
from frontend.archive_maintenance_api import register


class ArchiveAPITests(TestCase):
    def setUp(self):
        self.fixture = fixtures.ArchiveJournalTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tasks = []
        self.service = ArchiveMaintenance(self.fixture.h.dbpath, enqueue=lambda t: self.tasks.append(t))
        app = Flask(__name__)
        app.extensions['maintenance'] = SimpleNamespace(archives=self.service)
        blueprint = Blueprint('archives_test', __name__)
        register(blueprint, auth, error_handler, return_api)
        app.register_blueprint(blueprint, url_prefix='/api')
        self.client = app.test_client()
        for target, value in [('frontend.api.Settings', SimpleNamespace(sv=SimpleNamespace(api_key='synthetic-archive-key'))),
                              ('frontend.api.StartTypeHandlers.diffuse_timer', None)]:
            patcher = patch(target, return_value=value); patcher.start(); self.addCleanup(patcher.stop)

    def request(self, method, path='', body=None, status=200, authorized=True, **kwargs):
        path = '/api/maintenance/archives'+path
        if authorized:
            path += ('&' if '?' in path else '?')+'api_key=synthetic-archive-key'
        response = self.client.open(path, method=method, **({'json':body} if body is not None else {}), **kwargs)
        self.assertEqual(response.status_code,status,response.get_json())
        return response.get_json()['result']

    def test_all_routes_auth_before_parse(self):
        for method, path in [('GET',''),('POST','/scan'),('POST','/batch-preview'),('POST','/batch-apply'),
                             ('GET','/tasks/invalid'),('POST','/tasks/invalid/cancel')]:
            self.request(method,path,status=401,authorized=False,data='{invalid',content_type='application/json')
        self.assertFalse(self.tasks)

    def test_strict_payload_and_query(self):
        for body in ({'selected':[True]}, {'selected':[1,1]}, {'selected':[1], 'path':'C:/forged'},
                     {'selected':list(range(1,102))}, {'selected':[1], 'hash':'forged'},
                     {'selected':[1], 'verified':True}):
            self.request('POST','/batch-preview',body,status=400)
        self.request('POST','/scan',status=400,data='{"selected":[1],"selected":[2]}',content_type='application/json')
        for query in ('?limit=0','?limit=1&limit=2','?url=anything','?path=anything','?issue_id=-1'):
            self.request('GET',query,status=400)
        self.request('POST','/scan?limit=1',{'selected':[1]},status=400)

    def test_scan_preview_subset_apply_and_history(self):
        self.assertEqual(self.request('GET')['items'][0]['file_id'],1)
        before = self.fixture.path.read_bytes()
        scan = self.request('POST','/scan',{'selected':[1]})
        self.tasks[-1].run()
        result = self.request('GET','/tasks/'+scan['id'])
        self.assertEqual(result['items'][0]['status'],'convertible',result)
        self.assertEqual(self.fixture.path.read_bytes(),before)
        preview = self.request('POST','/batch-preview',{'selected':[1]})
        self.tasks[-1].run()
        self.assertEqual(self.fixture.h.db.execute('SELECT count(*) FROM organization_jobs').fetchone()[0],0)
        apply = self.request('POST','/batch-apply',{'review_id':preview['id'],'selected':[1],'confirmed':True})
        self.tasks[-1].run()
        result = self.request('GET','/tasks/'+apply['id'])
        self.assertEqual(result['items'][0]['status'],'completed',result)
        self.assertTrue(self.fixture.path.with_suffix('.cbz').exists())

    def test_cancellation_and_duplicate_task(self):
        task = self.request('POST','/scan',{'selected':[1]})
        self.request('POST','/scan',{'selected':[1]},status=409)
        self.request('POST','/tasks/'+task['id']+'/cancel',{})
        self.tasks[-1].run()
        self.assertEqual(self.request('GET','/tasks/'+task['id'])['state'],'cancelled')
        self.assertTrue(self.fixture.path.exists())

    def test_intake_archive_rejection_is_controlled_finding(self):
        from backend.base.acquisition_intake import (IntakeErrorCode,
                                                     IntakeFailure)
        task=self.request('POST','/scan',{'selected':[1]})
        with patch('backend.features.archive_maintenance.inspect',side_effect=IntakeFailure(IntakeErrorCode.PREPARATION)):
            self.tasks[-1].run()
        result=self.request('GET','/tasks/'+task['id'])
        self.assertEqual(result['state'],'complete')
        self.assertEqual(result['items'][0]['status'],'review_required')
        self.assertFalse(result['items'][0]['apply_available'])
