"""Every Discover route authenticates before strict bounded input processing."""

from types import SimpleNamespace
from unittest.mock import patch

from flask import Blueprint, Flask
from TDiscover import DiscoverFixture

from frontend.api import auth, error_handler, return_api
from frontend.discovery_api import register


class DiscoveryAPITests(DiscoverFixture):
    def setUp(self):
        super().setUp()
        self.app=Flask(__name__)
        self.app.extensions['discover']=self.owner
        api=Blueprint('discovery_test',__name__)
        register(api,auth,error_handler,return_api)
        self.app.register_blueprint(api,url_prefix='/api')
        self.client=self.app.test_client()
        for target,kwargs in (
            ('frontend.api.Settings',dict(return_value=SimpleNamespace(sv=SimpleNamespace(api_key='discovery-fixture-key')))),
            ('frontend.api.StartTypeHandlers.diffuse_timer',dict(return_value=None)),
            ('frontend.discovery_api.get_db',dict(side_effect=self.db.cursor))):
            p=patch(target,**kwargs);p.start();self.addCleanup(p.stop)

    def request(self,method,path,value=None,status=200,authenticated=True,**kwargs):
        response=self.client.open('/api'+path+('&' if '?' in path else '?')+('api_key=discovery-fixture-key' if authenticated else ''),
            method=method,**({'json':value} if value is not None else {}),**kwargs)
        self.assertEqual(response.status_code,status,response.get_data(as_text=True)[:1000])
        return response.get_json()['result'] if response.is_json else response.data

    def test_auth_all_routes_no_mutating_get(self):
        for rule in self.app.url_map.iter_rules():
            if not rule.rule.startswith('/api/'):continue
            path=rule.rule[4:].replace('<int:identifier>','1').replace('<string:identifier>','a'*32)
            for method in rule.methods-{'HEAD','OPTIONS'}:
                self.request(method,path,status=401,authenticated=False,data='{bad')
            if 'POST' in rule.methods:
                self.request('GET',path,status=405)

    def test_strict_keys_bounds_and_forgeries(self):
        for raw in ('{bad','{"url":"https://private/"}','{"x":1,"x":2}'):
            self.request('POST','/discover/refresh',status=400,data=raw,content_type='application/json')
        for data in (dict(path='C:/private'),dict(html='<p>remote</p>'),dict(verified_resolution=3000),dict(offering_url='https://remote/')):
            self.request('POST','/discover/refresh',data,status=400)
        for suffix in ('limit=101','limit=0','limit=1&limit=2','offset=-1','url=file:///private','state=fake','quality=fake'):
            self.request('GET','/discover?'+suffix,status=400)
        self.request('POST','/discover/refresh',status=400,data='x'*16385,content_type='application/json')
        self.request('GET','/discover/tasks/notvalid',status=400)
        self.request('GET','/discover/posts/0',status=400)
        self.request('GET','/discover/posts/99',status=404)
        self.request('POST','/discover/posts/1/acquire',dict(preview_id='a'*32,offering_id='b'*64,confirmed=False),status=400)

    def test_task_settings_stale_and_offline_read(self):
        task=self.request('POST','/discover/refresh',{})
        self.tasks.pop().run()
        self.assertEqual(self.request('GET','/discover/tasks/'+task['id'])['state'],'complete')
        self.assertEqual(len(self.request('GET','/discover')['items']),1)
        self.assertFalse(self.request('GET','/discover/status')['automatic'])
        data=dict(revision=1,enabled=True,automatic=True,interval_minutes=30)
        self.request('POST','/discover/settings',data)
        self.request('POST','/discover/settings',data,status=409)
        data['revision']=2;data['interval_minutes']=1
        self.request('POST','/discover/settings',data,status=400)
        self.assertEqual(self.request('GET','/discover/posts/1')['match'],'unmatched')
        with patch.object(self.owner,'page',side_effect=RuntimeError('secret path')):
            self.assertEqual(self.request('GET','/discover',status=500),dict(reason='internal_error'))
