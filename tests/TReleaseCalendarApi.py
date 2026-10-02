"""Auth before parsing, bounds, safe errors and actual Calendar task adapter."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import TReleaseCalendar as fixtures
from flask import Blueprint, Flask

from backend.features.release_calendar import ReleaseCalendar
from frontend.api import auth, error_handler, return_api
from frontend.calendar_api import register


class CalendarAPITests(TestCase):
    def setUp(self):
        self.fixture = fixtures.CalendarTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tasks = []
        self.owner = ReleaseCalendar(enqueue=lambda task: self.tasks.append(task))
        self.app = Flask(__name__)
        self.app.extensions['release_calendar'] = self.owner
        api = Blueprint('calendar_test', __name__)
        register(api, auth, error_handler, return_api)
        self.app.register_blueprint(api, url_prefix='/api')
        self.client = self.app.test_client()
        for target, opts in (
            ('frontend.api.Settings', dict(return_value=SimpleNamespace(sv=SimpleNamespace(api_key='fixture-calendar-key')))),
            ('frontend.api.StartTypeHandlers.diffuse_timer', dict(return_value=None)),
            ('frontend.calendar_api.get_db', dict(side_effect=self.fixture.db.cursor)),
            ('backend.features.release_calendar.get_db', dict(side_effect=self.fixture.db.cursor)),
        ):
            p = patch(target, **opts); p.start(); self.addCleanup(p.stop)

    def request(self, method, path='', body=None, status=200, authenticated=True, **kwargs):
        response = self.client.open('/api/calendar' + path + ('&' if '?' in path else '?') +
            ('api_key=fixture-calendar-key' if authenticated else ''), method=method,
            **({'json': body} if body is not None else {}), **kwargs)
        self.assertEqual(response.status_code, status, response.get_json())
        return response.get_json()['result']

    def test_every_route_auth_bounds_methods_and_safe_errors(self):
        for method, path in [('GET', ''), ('GET', '/events/issue:1'), ('POST', '/refresh'), ('GET', '/tasks/' + 'a' * 32)]:
            self.request(method, path, authenticated=False, status=401, data='malformed')
        self.request('POST', '/refresh', {'unexpected': 1}, status=400)
        self.request('POST', '/refresh', data='{}' * 40000, content_type='application/json', status=409)
        self.request('POST', '/refresh', data='{"provider":"all","provider":"gcd"}', content_type='application/json', status=400)
        self.assertEqual(self.client.get('/api/calendar/refresh?api_key=fixture-calendar-key').status_code, 405)
        for query in ('limit=0', 'limit=101', 'offset=-1', 'unknown=maybe', 'from=2027-03', 'provider=other', 'oops=x', 'limit=1&limit=2', 'volume=0'):
            self.request('GET', '?' + query, status=400)
        with patch('frontend.calendar_api.CalendarStore.page', side_effect=RuntimeError('SECRET raw URL')):
            value = self.request('GET', '', status=500)
        self.assertEqual(value, {'reason': 'internal_error'})

    def test_task_provider_fixture_durable_result_and_restart(self):
        from fixtures.release_calendar import (CalendarFixture,
                                               CalendarGCD, CalendarMetron)
        self.fixture.external()
        with patch.dict('backend.implementations.metadata.registry.PROVIDERS',
                        comicvine=CalendarFixture, metron=CalendarMetron, gcd=CalendarGCD):
            value = self.request('POST', '/refresh', {'provider': 'all'})
            self.assertEqual(self.request('GET', '/tasks/' + value['id'])['state'], 'queued')
            self.request('POST', '/refresh', {'provider': 'all'}, status=409)
            self.tasks[0].run()
            status = self.request('GET', '/tasks/' + value['id'])
            self.assertEqual(status['state'], 'complete', status)
            self.assertEqual(status['processed'], 1)
            self.app.extensions['release_calendar'] = ReleaseCalendar()
            self.assertEqual(self.request('GET', '/tasks/' + value['id'])['state'], 'complete')
            page = self.request('GET', '?from=2027-01-01&to=2027-12-31')
            self.assertEqual(page['items'][0]['effective']['date'], '2027-03-24')

    def test_partial_failure_keeps_evidence_and_cancel(self):
        from fixtures.release_calendar import CalendarFixture
        self.fixture.external()
        self.fixture.observed(1, '2027-03-17')
        with patch.dict('backend.implementations.metadata.registry.PROVIDERS', comicvine=CalendarFixture), patch.object(CalendarFixture, 'failed', True):
            value = self.request('POST', '/refresh', {'provider': 'all'})
            self.tasks[0].run()
            self.assertEqual(self.request('GET', '/tasks/' + value['id'])['state'], 'partial')
        self.assertEqual(self.fixture.page()['items'][0]['effective']['date'], '2027-03-17')
        value = self.request('POST', '/refresh', {'provider': 'all'})
        self.tasks[-1].stop = True
        self.tasks[-1].run()
        self.assertEqual(self.request('GET', '/tasks/' + value['id'])['state'], 'cancelled')

    def test_taskhandler_queued_removal_needs_no_run_to_release_guard(self):
        value = self.request('POST', '/refresh', {'provider': 'all'})
        # TaskHandler.remove sets stop and removes the queued object; no run().
        self.tasks[0].stop = True
        self.assertEqual(self.request('GET', '/tasks/' + value['id'])['state'], 'cancelled')
        second = self.request('POST', '/refresh', {'provider': 'all'})
        self.assertNotEqual(second['id'], value['id'])
        self.tasks[1].stop = True
        self.tasks[1].run()  # A cancellation/start race remains terminal as well.
        self.assertEqual(self.request('GET', '/tasks/' + second['id'])['state'], 'cancelled')

    def test_shutdown_signal_outside_application_context_is_safe(self):
        value = self.request('POST', '/refresh', {'provider': 'all'})
        with patch('backend.features.release_calendar.get_db', side_effect=RuntimeError('No app context')):
            self.tasks[0].stop = True
        self.app.extensions['release_calendar'] = ReleaseCalendar()
        self.assertEqual(self.request('GET', '/tasks/' + value['id'])['state'], 'interrupted')
