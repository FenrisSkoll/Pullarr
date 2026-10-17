"""Disposable Chromium check for local ownership progress and untitled issues."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from zipfile import ZipFile

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]

from api_key_browser_acceptance import QuietHandler

from backend.features.custom_publications import create_publication
from backend.internals.db import commit, get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


def add_local_publication(library, title, total, owned):
    source = library / title
    source.mkdir()
    paths = []
    for number in range(1, total + 1):
        path = source / f'{title} #{number:02}.cbz'
        with ZipFile(path, 'w') as archive:
            archive.writestr('page.jpg', b'disposable fixture')
        paths.append(path)
    publication = create_publication({
        'title': title, 'year': 2025, 'sources': [str(paths[0])],
        'issues': [{'number': str(number), 'title': ''}
                   for number in range(1, total + 1)],
    })
    db = get_db()
    for issue, path in zip(publication['issues'][:owned], paths):
        file_id = db.execute('INSERT INTO files(filepath,size) VALUES(?,?)',
                             (str(path), path.stat().st_size)).lastrowid
        db.execute('INSERT INTO issues_files(file_id,issue_id,forced) VALUES(?,?,1)',
                   (file_id, issue['id']))
    commit()
    return publication


def check_view(page, origin, full_id, partial_id, width, theme, shots):
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(origin + '/')
    expect(page.locator('#library-status')).to_have_text('Showing 2 publications')
    for view, selector in [('posters', '#list-library'), ('table', '#table-library')]:
        page.locator('#view-button').select_option('Posters' if view == 'posters' else 'Table')
        row = page.locator(f'{selector} .vol-{full_id}')
        partial = page.locator(f'{selector} .vol-{partial_id}')
        expect(row.locator('.list-prog-num' if view == 'posters' else '.table-prog-num'))\
            .to_have_text('31/31')
        expect(partial.locator('.list-prog-num' if view == 'posters' else '.table-prog-num'))\
            .to_have_text('5/13')
        if view == 'table':
            expect(row.locator('.table-monitored')).to_have_attribute('title', 'Unmonitored')
            expect(partial.locator('.table-monitored')).to_have_attribute('title', 'Unmonitored')
        else:
            expect(row.locator('.list-unmonitored')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), (width, theme, view)
        page.screenshot(path=str(shots / f'{theme}-{width}-{view}.png'))


def main():
    with TemporaryDirectory(prefix='pullarr-library-progress-') as directory:
        root = Path(directory)
        library = root / 'library'
        library.mkdir()
        set_db_location(str(root / 'db'))
        server = Server()
        with server.app.app_context():
            setup_db()
            Settings().update({'api_key': 'disposable-library-progress'})
            get_db().execute('INSERT INTO root_folders VALUES(1,?)', (str(library),))
            commit()
            full = add_local_publication(library, 'Chronological Civil War II', 31, 31)
            partial = add_local_publication(library, 'Partial Local Edition', 13, 5)
        http = make_server('127.0.0.1', 0, server.app, threaded=True,
                           request_handler=QuietHandler)
        Thread(target=http.serve_forever, daemon=True).start()
        origin = f'http://127.0.0.1:{http.server_port}'
        shots = ROOT / '.devdata' / 'library-progress-browser'
        shots.mkdir(parents=True, exist_ok=True)
        errors = []
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch()
                for theme in ('dark', 'light'):
                    page = browser.new_page(viewport={'width': 1440, 'height': 900})
                    page.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({"
                                         "api_key:'disposable-library-progress',"
                                         "last_login:Date.now()/1000,theme:'%s'}))" % theme)
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin + '/')
                    for width in (1440, 390):
                        check_view(page, origin, full['volume_id'], partial['volume_id'],
                                   width, theme, shots)
                    page.goto(origin + f"/volumes/{full['volume_id']}")
                    expect(page.locator('#issue-availability-counts')).to_contain_text('Owned 31')
                    issue = page.locator(f"#issues-list tr[data-id='{full['issues'][0]['id']}']")
                    expect(issue.locator('.issue-title')).to_have_text('Untitled')
                    issue.locator('.issue-title').click()
                    expect(page.locator('#issue-info-title')).to_have_text('Issue #1')
                    page.close()
                browser.close()
            assert not errors, errors
            print('library progress Chromium: 31/31 and 5/13, posters/table, dark/light, 1440/390, Issue #1 PASS')
        finally:
            http.shutdown()


if __name__ == '__main__':
    main()
