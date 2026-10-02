"""Synthetic ComicVine-shaped data; no captured credentials or live requests."""

import sqlite3
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.base.definitions import DateType

FAKE_APP_KEY = 'test-only-application-key'
FAKE_CV_KEY = 'test-only-comicvine-key'


def volume_response(**overrides):
    result = {
        'id': '2127',
        'name': ' Example%20Hero ',
        'start_year': '2021',
        'deck': 'Volume 2',
        'description': '<p>An example series.</p>',
        'image': {'small_url': 'https://example.invalid/cover.jpg'},
        'site_detail_url': 'https://example.invalid/volume/4050-2127/',
        'aliases': 'Alternate Hero\r\n Another Hero \r\n',
        'publisher': {'name': 'Example Publisher'},
        'count_of_issues': '12'
    }
    result.update(overrides)
    return result


def public_result(**overrides):
    """Explicit legacy public schema, independent of production transforms."""
    result = {
        'comicvine_id': 2127,
        'title': 'Example Hero',
        'year': 2021,
        'volume_number': 2,
        'cover_link': 'https://example.invalid/cover.jpg',
        'description': '<p>An example series.</p>',
        'site_url': 'https://example.invalid/volume/4050-2127/',
        'aliases': ['Alternate Hero', 'Another Hero'],
        'publisher': 'Example Publisher',
        'issue_count': 12,
        'translated': False,
        'already_added': None,
        'issues': None
    }
    result.update(overrides)
    return result


class ComicVineSearchHarness:
    """Mixin: real transformation/SQL, mocked settings, transport and timers.

    The tiny in-memory table models only the already-added lookup; setup_db
    and migrations never run. All patches are restored after each test.
    """

    def setUp(self):
        super().setUp()
        self.settings = SimpleNamespace(
            comicvine_api_key=FAKE_CV_KEY,
            api_key=FAKE_APP_KEY,
            date_type=DateType.COVER_DATE
        )
        settings = self.start_patch(
            'backend.implementations.comicvine.Settings'
        ).return_value
        settings.get_settings.return_value = self.settings
        self.start_patch('backend.implementations.comicvine.Session')
        self.status = self.start_patch(
            'backend.implementations.comicvine.StatusHandlers'
        ).return_value

        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute(
            'CREATE TABLE volumes (id INTEGER PRIMARY KEY, '
            'comicvine_id INTEGER NOT NULL);'
        )
        self.db_lookup = self.start_patch(
            'backend.implementations.comicvine.get_db',
            side_effect=self.db.cursor
        )

        self.response = MagicMock()
        self.response.json = AsyncMock()
        self.session = MagicMock()
        self.session.get = AsyncMock(return_value=self.response)
        context = self.start_patch(
            'backend.implementations.comicvine.AsyncSession'
        ).return_value
        context.__aenter__.return_value = self.session
        self.respond([volume_response()])

        # Block real HTTP, not sockets: Windows asyncio uses a loopback
        # socket pair internally even when the coroutine performs no IO.
        for target in (
            'requests.sessions.Session.request',
            'aiohttp.ClientSession._request'
        ):
            self.start_patch(
                target,
                side_effect=AssertionError('HTTP access forbidden in tests')
            )

    def start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def respond(self, results, status_code=1):
        self.response.json.side_effect = None
        self.response.json.return_value = {
            'status_code': status_code,
            'results': deepcopy(results)
        }
