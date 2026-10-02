"""Real refresh SQL with synthetic HTTP, fixed time and disposable files."""

from datetime import datetime

from fixtures.comicvine_fetch import (LibraryAddHarness,
                                      envelope, issue_response)
from fixtures.comicvine_search import volume_response

from backend.implementations.volumes import Library, refresh_and_scan

NOW = datetime(2026, 1, 1)


class RefreshHarness(LibraryAddHarness):
    def setUp(self):
        super().setUp()
        self.prepare_fetch(volume_response(count_of_issues=2), [
            issue_response(), issue_response(id=302, issue_number='2')
        ])
        self.volume_id = self.add_volume()
        self.scan.reset_mock()
        self.process.reset_mock()
        self.session.get.reset_mock()
        self.status.reset_mock()
        self.commit = self.start_patch('backend.implementations.volumes.commit',
                                       side_effect=self.db.commit)
        self.socket = self.start_patch(
            'backend.implementations.volumes.WebSocket').return_value
        self.pool_factory = self.start_patch(
            'backend.implementations.volumes.PortablePool')
        self.pool = self.pool_factory.return_value.__enter__.return_value
        self.pool.istarmap_unordered.side_effect = lambda fn, args: iter(
            [None for _ in args])
        self.prepare_refresh()

    def prepare_refresh(self, volumes=None, issues=None):
        if volumes is None:
            volumes = [volume_response(count_of_issues=2)]
        if issues is None:
            issues = [
                issue_response(),
                issue_response(
                    id=302,
                    issue_number='2')]
        self.response.json.side_effect = [
            envelope(volumes), envelope(
                issues, number_of_total_results=len(issues))]

    def refresh(self, **options):
        refresh_and_scan(self.volume_id, **options)

    def add_second_volume(self, cv_id=9001):
        self.prepare_fetch(
            volume_response(
                id=cv_id, name='Second', count_of_issues=1), [
                issue_response(
                    id=901, volume={
                        'id': cv_id})])
        result = Library.add(cv_id, 1, True)
        self.scan.reset_mock()
        self.session.get.reset_mock()
        self.prepare_refresh()
        return result

    def set_timestamp(self, identity, value):
        self.db.execute(
            'UPDATE volumes SET last_cv_fetch=? WHERE id=?', (value, identity))
        self.db.commit()

    def link_file(self, issue_ids=(1,), forced=True):
        path = self.root / 'synthetic.cbz'
        path.write_bytes(b'not-a-real-comic')
        file_id = self.db.execute(
            'INSERT INTO files(filepath,size) VALUES (?,?)',
            (str(path), path.stat().st_size)
        ).lastrowid
        self.db.executemany(
            'INSERT INTO issues_files VALUES (?,?,?)', [
                (file_id, identity, forced) for identity in issue_ids])
        self.db.commit()
        return path

    def snapshot(self):
        tables = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {name: self.db.execute(
            'SELECT * FROM "' + name + '" ORDER BY rowid'
        ).fetchall() for (name,) in tables}

    def schema(self):
        return self.db.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'
        ).fetchall()
