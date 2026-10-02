"""Current product surface checks; historical/compatibility identifiers stay stable."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from TDiscover import DiscoverFixture

from backend.implementations.volumes import Library
from backend.internals.db import KapowarrCursor
from frontend.api import api_volumes
from scripts.check_pullarr_assets import check


class PullarrAssets(unittest.TestCase):
    def test_current_brand_and_local_assets(self):
        self.assertEqual(check()['brand'], 'PASS')


class LibraryPaging(DiscoverFixture):
    def setUp(self):
        super().setUp()
        self.db.execute("INSERT INTO root_folders VALUES(1,'/synthetic')")
        self.db.executemany(
            'INSERT INTO volumes(id,comicvine_id,title,year,volume_number,root_folder,folder,monitored) '
            'VALUES(?,?,?,2020,1,1,?,1)',
            [(i, i, 'Same title' if i < 102 else 'Other', f'/synthetic/{i}') for i in range(1, 104)]
        )
        for target, options in (
            ('frontend.metadata.get_db', dict(side_effect=lambda:self.db.cursor(factory=KapowarrCursor))),
            ('backend.implementations.volumes.get_db', dict(side_effect=lambda:self.db.cursor(factory=KapowarrCursor))),
            ('frontend.api.Settings', dict(return_value=SimpleNamespace(sv=SimpleNamespace(api_key='pullarr-page-fixture')))),
            ('frontend.api.StartTypeHandlers.diffuse_timer', dict(return_value=None)),
        ):
            item = patch(target, **options)
            item.start()
            self.addCleanup(item.stop)
        app = Flask(__name__)
        app.add_url_rule('/volumes', view_func=api_volumes)
        self.client = app.test_client()

    def test_bounded_order_search_and_legacy_contract(self):
        self.assertEqual(len(Library.get_public_volumes()), 103)
        first = Library.get_public_volumes(limit=50, query='Same title')
        second = Library.get_public_volumes(limit=50, offset=50, query='Same title')
        last = Library.get_public_volumes(limit=50, offset=100, query='Same title')
        self.assertEqual([v['id'] for v in first + second + last], list(range(1, 102)))
        self.assertEqual(Library.get_public_volumes(limit=50, query='cv:2')[0]['id'], 2)
        self.assertEqual(Library.get_public_volumes(limit=50, query='<script>'), [])
        self.assertEqual(Library.get_public_volumes(limit=50, offset=1000), [])

    def test_authenticated_strict_page_bounds(self):
        self.assertEqual(self.client.get('/volumes?limit=invalid').status_code, 401)
        prefix = '/volumes?api_key=pullarr-page-fixture&'
        for query in ('limit=0', 'limit=101', 'offset=-1', 'offset=1000001',
                      'limit=1&limit=2', 'limit=1&path=/private', 'limit=1&query=' + 'x'*501):
            self.assertEqual(self.client.get(prefix + query).status_code, 400, query)
        response = self.client.get(prefix + 'limit=50&offset=50&query=Same%20title&metadata=true&issue_facts=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['result']), 50)
