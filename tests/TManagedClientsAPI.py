"""Authentication before parsing, revision safety, no client proxy or secrets."""
from dataclasses import asdict
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from flask import Blueprint, Flask
from Tbackend.features import organization_execution as execution_fixture

from backend.base.managed_client import ManagedClientConfig
from frontend.api import auth, error_handler, return_api
from frontend.managed_clients_api import register


class ManagedClientAPITests(TestCase):
    def setUp(self):
        self.fixture = execution_fixture.ExecutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.db.execute("UPDATE config SET value=72 WHERE key='database_version'")
        self.app = Flask(__name__)
        api = Blueprint('expanded_test',__name__)
        register(api,auth,error_handler,return_api)
        self.app.register_blueprint(api,url_prefix='/api')
        self.client = self.app.test_client()
        for target, options in (
            ('frontend.api.Settings',dict(return_value=SimpleNamespace(sv=SimpleNamespace(api_key='synthetic-app-key')))),
            ('frontend.api.StartTypeHandlers.diffuse_timer',dict(return_value=None)),
            ('backend.internals.managed_clients.get_db',dict(side_effect=self.fixture.db.cursor)),
            ('frontend.managed_clients_api.DBConnection.default_file',dict(new=self.fixture.dbpath))):
            p = patch(target,**options); p.start(); self.addCleanup(p.stop)
        self.config = asdict(ManagedClientConfig('fixture','Fixture','http://client:8080','fixture-user','fixture-password'))
        self.config.pop('key')

    def request(self, method, path, body=None, status=200, auth=True, **kwargs):
        response = self.client.open('/api' + path + ('&' if '?' in path else '?') + ('api_key=synthetic-app-key' if auth else ''),
            method=method, **({'json':body} if body is not None else {}), **kwargs)
        self.assertEqual(response.status_code,status,response.get_data(as_text=True)[:1000])
        self.assertNotIn('fixture-password',response.get_data(as_text=True))
        return response.get_json()['result'] if response.is_json else None

    def test_all_routes_auth_first(self):
        for rule in self.app.url_map.iter_rules():
            if not rule.rule.startswith('/api/'):
                continue
            path = rule.rule[4:].replace('<identifier>','a' * 32)
            for method in rule.methods - {'HEAD','OPTIONS'}:
                self.request(method,path,status=401,auth=False,data='{bad')

    def test_create_stale_update_and_delete(self):
        value = self.request('POST','/managed-clients',{'configuration':self.config})
        path = '/managed-clients/' + value['id']
        changed = self.request('PUT',path,{'configuration':dict(self.config,name='Changed'),'revision':value['revision']})
        self.request('PUT',path,{'configuration':self.config,'revision':value['revision']},status=409)
        self.request('DELETE',path,{'revision':value['revision']},status=409)
        self.request('DELETE',path,{'revision':changed['revision']})
        self.assertEqual(self.request('GET','/managed-clients'),[])

    def test_strict_bodies_and_queries(self):
        for raw in ('{bad','{"configuration":{},"configuration":{}}','{"configuration":{"name":"a","name":"b"}}'):
            self.request('POST','/managed-clients',status=400,data=raw,content_type='application/json')
        for forged in ('verified_quality','torrent_hash','path','source_html','download_url'):
            self.request('POST','/managed-clients',{'configuration':dict(self.config,**{forged:'forged'})},status=400)
        for query in ('limit=0','limit=101','offset=-1','limit=1&limit=2','url=http://other'):
            self.request('GET','/managed-downloads?' + query,status=400)
        self.request('POST','/managed-clients',data='x' * 65537,content_type='application/json',status=400)
        self.request('POST','/managed-clients',{'configuration':dict(self.config,url='file:///private')},status=400)

    def test_test_uses_only_saved_origin(self):
        value = self.request('POST','/managed-clients',{'configuration':self.config})
        path = '/managed-clients/' + value['id'] + '/test'
        self.request('POST',path,{'revision':value['revision'],'url':'http://other'},status=400)
        with patch('frontend.managed_clients_api.client_for') as factory:
            factory.return_value.check.return_value = {'product':'NZBGet','version':'25.3','protocol':'usenet'}
            self.request('POST',path,{'revision':value['revision']})
            self.assertEqual(factory.call_args.args[0].url,self.config['url'])

    def test_every_mutation_rejects_duplicate_unknown_and_forged_facts(self):
        for rule in self.app.url_map.iter_rules():
            if not rule.rule.startswith('/api/'):
                continue
            path = rule.rule[4:].replace('<identifier>','a' * 32)
            for method in rule.methods & {'POST','PUT','DELETE'}:
                with self.subTest(path=path,method=method):
                    self.request(method,path,status=400,data='{"revision":"one","revision":"two"}',content_type='application/json')
                    self.request(method,path,{'url':'http://arbitrary','verified_quality':3000,'torrent_hash':'a' * 40},status=400)
