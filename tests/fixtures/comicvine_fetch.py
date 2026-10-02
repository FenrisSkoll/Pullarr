"""Offline raw fetch fixtures and a disposable, real-schema add harness."""

import sqlite3
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fixtures.comicvine_search import (ComicVineSearchHarness,
                                       public_result, volume_response)
from flask import Flask

from backend.base.definitions import RootFolder
from backend.implementations.volumes import Library
from backend.internals.db import (DB_SCHEMA, DBConnection,
                                  setup_db_adapters_and_converters)
from backend.internals.settings import PublicSettingsValues

COVER = b'synthetic-cover-bytes'


def issue_response(**overrides):
    result = {
        'id': '301', 'volume': {'id': '2127'}, 'issue_number': '1',
        'name': ' The%20Beginning ', 'cover_date': '2021-01-01',
        'store_date': '2020-12-15', 'description': '<p>First issue.</p>'
    }
    result.update(overrides)
    return result


def issue_result(**overrides):
    result = {
        'comicvine_id': 301, 'volume_id': 2127, 'issue_number': '1',
        'calculated_issue_number': 1.0, 'title': 'The Beginning',
        'date': '2021-01-01', 'description': '<p>First issue.</p>'
    }
    result.update(overrides)
    return result


def fetched_result(**overrides):
    result = public_result(cover=COVER, issues=[issue_result()])
    result.update(overrides)
    return result


def envelope(results, status_code=1, **extra):
    return {'status_code': status_code, 'results': deepcopy(results), **extra}


class ComicVineFetchHarness(ComicVineSearchHarness):
    def setUp(self):
        super().setUp()
        self.session.get_content = AsyncMock(return_value=COVER)
        self.prepare_fetch()

    def prepare_fetch(self, volume=None, issues=None):
        if volume is None:
            volume = volume_response()
        if issues is None:
            issues = [issue_response()]
        self.response.json.side_effect = [
            envelope(volume),
            envelope(issues, number_of_total_results=len(issues))
        ]


class LibraryAddHarness(ComicVineFetchHarness):
    """Real add/SQL/naming, isolated from downloads, tasks and existing DBs."""

    def setUp(self):
        super().setUp()
        context = Flask(__name__).app_context()
        context.push()
        self.addCleanup(context.pop)
        for registry in (sqlite3.adapters, sqlite3.converters):
            saved = patch.dict(registry)
            saved.start()
            self.addCleanup(saved.stop)
        setup_db_adapters_and_converters()
        self.db = DBConnection(db_file=':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript(DB_SCHEMA)
        temporary = TemporaryDirectory(prefix='kapowarr-add-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db.execute('INSERT INTO root_folders VALUES (1, ?)',
                        (str(self.root),))
        self.db.commit()
        self.db_lookup.side_effect = self.db.cursor
        for target in (
            'backend.implementations.volumes.get_db',
            'backend.internals.provider_identity.get_db',
            'backend.implementations.metadata.identity_enrichment.get_db',
            'frontend.metadata.get_db',
            'backend.internals.db_models.get_db'
        ):
            self.start_patch(target, side_effect=self.db.cursor)
        root_folders = self.start_patch(
            'backend.implementations.volumes.RootFolders').return_value
        root_folders.get_one.return_value = RootFolder(1, str(self.root), None)
        defaults = vars(PublicSettingsValues())
        for name, value in defaults.items():
            if not hasattr(self.settings, name):
                setattr(self.settings, name, value)
        for target in (
            'backend.implementations.volumes.Settings',
            'backend.implementations.naming.Settings'
        ):
            settings = self.start_patch(target).return_value
            settings.sv = self.settings
            settings.get_settings.return_value = self.settings
        self.scan = self.start_patch(
            'backend.implementations.volumes.scan_files')
        self.process = self.start_patch(
            'backend.implementations.volumes.mass_process_files')
        self.start_patch(
            'backend.implementations.volumes.time',
            return_value=1234567890)
        clock = self.start_patch(
            'backend.implementations.volumes.datetime',
            wraps=datetime)
        clock.now.return_value = datetime(2026, 1, 1)

    def add_volume(self, **options):
        return Library.add(2127, 1, True, **options)

    def rows(self, table):
        return self.db.cursor().execute(
            'SELECT * FROM ' + table + ' ORDER BY id'
        ).fetchalldict()

    def assert_empty_library(self):
        for table in ('volumes', 'issues', 'volumes_covers',
                      'volume_external_ids', 'issue_external_ids'):
            self.assertEqual(self.db.execute(
                'SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        self.assertEqual(list(self.root.iterdir()), [])
