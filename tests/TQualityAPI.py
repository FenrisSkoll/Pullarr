"""Every quality route authenticates before parsing and rejects forged facts."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import TQualityStore as fixtures
from flask import Blueprint, Flask

from backend.base.quality import default_policy
from backend.features.quality import QualityAnalysis
from frontend.api import auth, error_handler, return_api
from frontend.quality_api import register


class QualityAPITests(TestCase):
    def setUp(self):
        self.fixture=fixtures.QualityStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tasks=[]
        self.app=Flask(__name__)
        self.app.extensions['quality_analysis']=QualityAnalysis(enqueue=self.tasks.append)
        api=Blueprint('quality_test',__name__)
        register(api,auth,error_handler,return_api)
        self.app.register_blueprint(api,url_prefix='/api')
        self.client=self.app.test_client()
        for target,options in (
            ('frontend.api.Settings',dict(return_value=SimpleNamespace(sv=SimpleNamespace(api_key='quality-fixture-key')))),
            ('frontend.api.StartTypeHandlers.diffuse_timer',dict(return_value=None)),
            ('frontend.quality_api.get_db',dict(side_effect=self.fixture.db.cursor)),
            ('backend.features.quality.get_db',dict(side_effect=self.fixture.db.cursor))):
            p=patch(target,**options);p.start();self.addCleanup(p.stop)

    def request(self,method,path,body=None,status=200,authenticated=True,**kwargs):
        response=self.client.open('/api'+path+('&' if '?' in path else '?')+('api_key=quality-fixture-key' if authenticated else ''),
            method=method,**({'json':body} if body is not None else {}),**kwargs)
        self.assertEqual(response.status_code,status,response.get_data(as_text=True)[:1000])
        return response.get_json()['result'] if response.is_json else response.data

    def test_auth_all_methods_and_mutation_get_absent(self):
        for rule in self.app.url_map.iter_rules():
            if not rule.rule.startswith('/api/'):
                continue
            path=rule.rule[4:].replace('<int:identifier>','1').replace('<string:identifier>','a'*32)
            for method in rule.methods-{'HEAD','OPTIONS'}:
                self.request(method,path,status=401,authenticated=False,data='malformed')
            if 'POST' in rule.methods and 'GET' not in rule.methods:
                self.request('GET',path,status=405)

    def test_strict_json_queries_forged_facts_safe_errors(self):
        for raw in ('{bad','{}','{"name":"A","name":"B","policy":{}}'):
            self.request('POST','/quality-profiles',data=raw,content_type='application/json',status=400)
        self.request('POST','/quality-profiles',dict(name='x',policy=default_policy(),verified_resolution=3000),status=400)
        self.request('POST','/quality-analysis',dict(file_ids=[1],path='C:/private'),status=400)
        self.request('POST','/quality-analysis',dict(file_ids=list(range(1,52))),status=400)
        for suffix in ('limit=101','limit=0','offset=-1','limit=1&limit=2','force=true'):
            self.request('GET','/volumes/1/quality?'+suffix,status=400)
        self.request('POST','/quality-profiles',data='x'*65537,content_type='application/json',status=400)
        with patch('frontend.quality_api.QualityStore.profiles',side_effect=RuntimeError('credential/private/path')):
            self.assertEqual(self.request('GET','/quality-profiles',status=500),{'reason':'internal_error'})

    def test_profile_assignment_and_stale_edit(self):
        created=self.request('POST','/quality-profiles',dict(name='<script>literal</script>',policy=default_policy()))
        self.request('POST',f'/quality-profiles/{created["id"]}',dict(name='changed',policy=default_policy(),revision=1))
        self.request('POST',f'/quality-profiles/{created["id"]}',dict(name='stale',policy=default_policy(),revision=1),status=409)
        self.request('POST','/volumes/1/quality',dict(profile_id=created['id'],expected_profile_id=None))
        state=self.request('GET','/volumes/1/quality')
        self.assertEqual(state['assignment']['source'],'volume')
        self.assertEqual(state['items'][0]['reason'],'missing')
        self.request('POST','/volumes/1/quality',dict(profile_id=None,expected_profile_id=None),status=409)
