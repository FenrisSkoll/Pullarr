"""Disposable real Chromium/HTTP/TaskHandler acceptance; synthetic provider fixtures only.

Modes: --rename [--loss|--restart|--recovery|--manual], --folder,
--metadata, --comicinfo [--malformed], --duplicate, --smoke.
The browser fetches the existing SRI client asset, never live provider data.
"""
import base64
import hashlib
import json
import logging
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from zipfile import ZipFile

import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from backend.base.logging import LOGGER
from backend.internals.db import get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings

records = []
class Capture(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            records.append(record.getMessage())

LOGGER.addHandler(Capture())
with TemporaryDirectory(prefix='kapowarr-8i-browser-') as directory:
    base = Path(directory)
    set_db_location(str(base / 'db'))
    server = Server()
    library = base / 'library'
    volume = library / 'Example'
    volume.mkdir(parents=True)
    comic = volume / 'wrong.cbz'
    with ZipFile(comic, 'w') as archive:
        archive.writestr('001.jpg', b'disposable-fixture')
        archive.writestr('ComicInfo.xml', '<ComicInfo><Series>Example</Series></ComicInfo>')
    original = hashlib.sha256(comic.read_bytes()).hexdigest()
    folder_mode = '--folder' in sys.argv
    metadata_mode = '--metadata' in sys.argv
    comicinfo_mode = '--comicinfo' in sys.argv
    duplicate_mode = '--duplicate' in sys.argv
    if comicinfo_mode:
        with ZipFile(comic, 'w') as archive:
            archive.writestr('001.jpg', b'disposable-fixture')
            archive.writestr('ComicInfo.xml', '<ComicInfo><Title>broken' if '--malformed' in sys.argv else '<ComicInfo><Year>bad</Year><Notes>&lt;script&gt;window.hostile=true&lt;/script&gt;</Notes></ComicInfo>')
    if duplicate_mode:
        duplicate = volume / 'copy.cbz'
        duplicate.write_bytes(comic.read_bytes())
    if '--scale' in sys.argv:
        for index in range(60):
            (volume / f'untracked-{index}.cbz').write_bytes(comic.read_bytes())
    if folder_mode:
        (volume / 'empty').mkdir()
        (volume / 'notes.txt').write_text('ancillary retained', encoding='utf-8')
    with server.app.app_context():
        setup_db()
        Settings().update({'api_key': 'disposable-maintenance-browser-key', 'file_naming': 'Issue {issue_number}', 'file_naming_empty': 'Issue {issue_number}'})
        if folder_mode:
            Settings().update({'volume_folder_naming': '{series_name} ({year})'})
        db = get_db()
        db.execute('INSERT INTO root_folders(id,folder) VALUES(1,?)', (str(library),))
        db.execute('''INSERT INTO volumes(id,title,root_folder,folder,metadata_provider,comicvine_id,volume_number,year)
            VALUES(1,?,1,?,'comicvine',100,1,2020)''', ('Example <img onerror="window.hostile=true">', str(volume)))
        db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(1,1,101,'1',1,1)")
        db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)', (str(comic), comic.stat().st_size))
        db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
        if metadata_mode:
            # Deliberate stored-provenance mismatch creates a genuine 8B
            # metadata finding; the 8D review still owns all proposed changes.
            db.execute("""INSERT INTO classification_provenance
                (volume_id,schema_version,applied_value,application_kind,recorded_at,input_scope,replay_status,lock_input)
                VALUES(1,'classification-provenance/v1','tpb','explicit_selection',
                '2026-09-30T00:00:00+00:00','explicit_selection','not_applicable',0)""")
        # Normal volume creation also records a cover. Keep the fixture valid
        # for existing Library/Volume UI instead of bypassing the cover route.
        db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,?)',
            (base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII='),))
        if duplicate_mode:
            db.execute('INSERT INTO files(id,filepath,size) VALUES(2,?,?)', (str(duplicate), duplicate.stat().st_size))
            db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(2,1)')
        if folder_mode:
            db.execute('UPDATE volumes SET custom_folder=1 WHERE id=1')
        db.connection.commit()
        if '--scale' in sys.argv:
            # Synthetic durable history only, never used for mutation. Reuse
            # the source-domain summary fixture rather than fake HTTP replies.
            sys.path.insert(0, str(REPO / 'tests'))
            from TMaintenanceHistory import MaintenanceHistoryTests
            history_fixture = MaintenanceHistoryTests()
            history_fixture.db = db.connection
            for index in range(100):
                history_fixture.job('scale-' + str(index), batch='scale', rename_origin={})
    if metadata_mode:
        sys.path.insert(0, str(REPO / 'tests'))
        from TProviderSwitchApply import remote

        from backend.implementations.metadata.switch_target import admit
        async def acquire(reference):
            return admit(remote(reference.provider, parent=reference.provider_id, first=101, count=1), reference)
        server.app.extensions['maintenance'].metadata.acquire = acquire
    if '--recovery' in sys.argv or '--manual' in sys.argv:
        def checkpoint(stage, job, ordinal):
            if stage == 'after_effect' and ordinal == 0:
                if '--manual' in sys.argv:
                    comic.write_bytes((volume / 'Issue 001.cbz').read_bytes())
                raise PermissionError('controlled post-move interruption')
        server.app.extensions['maintenance'].rename.checkpoint = checkpoint
    http = make_server('127.0.0.1', 0, server.app, threaded=True)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{http.server_port}'
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            # Fetch the existing integrity-matched public client once. No
            # provider requests; browser CDN/font requests remain intercepted.
            socket_asset = requests.get('https://cdn.socket.io/4.7.5/socket.io.min.js', timeout=15)
            socket_asset.raise_for_status()
            lost_posts = []
            def route(request):
                if '--loss' in sys.argv and '/maintenance/rename/reviews/' in request.request.url and request.request.url.split('?')[0].endswith('/apply'):
                    lost_posts.append(request.request.url)
                    payload = request.request.post_data_json
                    identifier = request.request.url.split('/reviews/')[1].split('/')[0]
                    batch_id = f"maintenance-rename:{identifier}:{payload['revision']}:{payload['digest']}"
                    response = request.fetch()
                    assert response.status == 200
                    for _ in range(100):
                        lookup = requests.get(origin + '/api/maintenance/batches/' + batch_id,
                            params={'api_key': 'disposable-maintenance-browser-key'}, timeout=5)
                        if lookup.status_code == 200 and lookup.json()['result']['state'] == 'complete':
                            break
                        time.sleep(.1)
                    else:
                        raise AssertionError('durable batch did not complete before simulated response loss')
                    # Registration ran, but the browser cannot parse its response.
                    # No network-console noise or production error handling changes.
                    request.fulfill(status=200, content_type='application/json', body='{truncated')
                    return
                if request.request.url.startswith(origin):
                    request.continue_()
                elif 'socket.io.min.js' in request.request.url:
                    request.fulfill(content_type='text/javascript', body=socket_asset.content,
                                    headers={'Access-Control-Allow-Origin': '*'})
                else:
                    request.fulfill(status=200, body='')
            context.route('**/*', route)
            context.add_init_script("localStorage.setItem('kapowarr', JSON.stringify({api_key:'disposable-maintenance-browser-key',last_login:Date.now()/1000,theme:'dark'}));")
            page = context.new_page()
            errors = []
            console_errors = []
            expected_console = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def console(message):
                if message.type != 'error':
                    return
                if ('--restart' in sys.argv and 'status of 410 (GONE)' in message.text
                        and '/api/maintenance/' in message.location.get('url', '')):
                    expected_console.append('controlled transient-state loss: HTTP 410')
                else:
                    console_errors.append(message.text)
            page.on('console', console)
            metrics = {}
            started = time.perf_counter()
            page.goto(origin + '/maintenance')
            page.get_by_role('heading', name='Library Maintenance', exact=True).wait_for()
            metrics['initial_maintenance_seconds'] = round(time.perf_counter() - started, 4)
            if comicinfo_mode or duplicate_mode:
                page.locator('#maintenance-level').select_option('deep' if duplicate_mode else 'archive')
            if folder_mode:
                page.locator('#maintenance-scope').select_option('volumes')
                page.locator('#maintenance-volume-results').get_by_role('button', name='Select:', exact=False).first.click()
                page.get_by_text('1 selected volumes (maximum 1000).', exact=False).wait_for()
            page.get_by_role('button', name='Start read-only scan', exact=True).click()
            page.get_by_role('button', name='Create reviewed worklist', exact=True).wait_for()
            page.wait_for_function("!document.getElementById('maintenance-create-review').disabled")
            page.get_by_role('button', name='Create reviewed worklist', exact=True).click()
            page.get_by_role('heading', name='Revision 0', exact=False).wait_for()
            metrics['scan_and_worklist_ready_seconds'] = round(time.perf_counter() - started, 4)
            if '--scale' in sys.argv:
                for name in ('findings', 'worklist', 'history-rows'):
                    page.wait_for_function(f"document.querySelectorAll('#maintenance-{name} tbody tr').length === 50")
                metrics['fifty_row_findings_worklist_history_ready_seconds'] = round(time.perf_counter() - started, 4)
            page.locator('#maintenance-category').select_option('metadata' if metadata_mode else 'comicinfo' if comicinfo_mode else 'duplicate' if duplicate_mode else 'policy')
            page.get_by_role('button', name='Filter findings', exact=True).click()
            page.wait_for_timeout(500)
            finding_row = page.locator('#maintenance-worklist tbody tr').filter(has_text='Stored application receipt' if metadata_mode else 'malformed' if '--malformed' in sys.argv else 'Invalid field' if comicinfo_mode else 'Identical streamed' if duplicate_mode else 'folder' if folder_mode else 'filename').first
            finding_row.locator('select').select_option('comicinfo_repair' if comicinfo_mode else 'duplicate_review' if duplicate_mode else 'metadata_repair' if metadata_mode else 'folder_organization' if folder_mode else 'rename')
            finding_row.get_by_role('button', name='Select intent', exact=True).click()
            page.get_by_role('heading', name='Revision 1', exact=False).wait_for()
            assert not page.evaluate('Boolean(window.hostile)')
            if not (folder_mode or metadata_mode or comicinfo_mode or duplicate_mode):
                assert 'supported preview' in page.locator('#maintenance-worklist').inner_text()
            page.reload()
            page.get_by_role('heading', name='Revision 1', exact=False).wait_for()
            if '--malformed' in sys.argv:
                assert page.get_by_role('button', name='Review ComicInfo', exact=False).count() == 0
                assert 'blocked' in page.locator('#maintenance-worklist').inner_text().lower()
            if metadata_mode or (comicinfo_mode and '--malformed' not in sys.argv):
                page.get_by_role('button', name='Review metadata' if metadata_mode else 'Review ComicInfo', exact=False).click()
                page.get_by_role('heading', name='Reviewed provider metadata repair' if metadata_mode else 'Reviewed ComicInfo repair').wait_for()
                row = page.locator('#maintenance-specialized tbody tr').filter(has_text='Title · volume 1' if metadata_mode else 'Series').first
                row.get_by_role('button', name='Select field' if metadata_mode else 'Use reviewed field', exact=True).click()
                page.wait_for_function("!Array.from(document.querySelectorAll('#maintenance-specialized button')).find(n => n.textContent === 'Review repair confirmation').disabled")
                page.get_by_role('button', name='Review repair confirmation').click()
                page.locator('#maintenance-confirm-submit').click()
                page.get_by_role('heading', name='Audit history' if metadata_mode else 'comicinfo repair', exact=False).wait_for()
                if comicinfo_mode:
                    with ZipFile(comic) as archive:
                        assert b'window.hostile=true' in archive.read('ComicInfo.xml')
                        assert archive.read('001.jpg') == b'disposable-fixture'
                        assert archive.testzip() is None
                    assert page.get_by_role('button', name='Check Revert', exact=True).count() == 0
                else:
                    with server.app.app_context():
                        assert tuple(get_db().execute('SELECT title,year FROM volumes WHERE id=1').fetchone()) == ('Target comicvine', 2020)
            if duplicate_mode:
                page.get_by_role('button', name='Review selected duplicates', exact=True).click()
                page.get_by_role('heading', name='Reviewed duplicate evidence').wait_for()
                page.get_by_role('button', name='Inspect group and choices').click()
                page.locator('#maintenance-duplicate-members tbody tr').filter(has_text='copy.cbz').get_by_role('button', name='Select this copy for quarantine').click()
                page.get_by_role('button', name='Prepare reviewed quarantine').click()
                page.wait_for_function("!Array.from(document.querySelectorAll('#maintenance-specialized button')).find(n => n.textContent === 'Review quarantine confirmation').disabled")
                page.get_by_role('button', name='Review quarantine confirmation').click()
                page.locator('#maintenance-confirm-submit').click()
                page.get_by_role('heading', name='Batch: complete', exact=True).wait_for()
                assert not duplicate.exists() and comic.exists()
                assert '.kapowarr-quarantine' not in page.locator('body').inner_text()
                page.locator('#maintenance-history-rows').get_by_role('button', name='duplicate quarantine', exact=True).click()
                page.get_by_role('button', name='Check Revert', exact=True).click()
                page.locator('#maintenance-specialized').get_by_role('button', name='Restore File', exact=True).click()
                page.locator('#maintenance-confirm-submit').click()
                page.locator('#maintenance-confirm').wait_for(state='hidden')
                page.wait_for_function("document.getElementById('maintenance-detail').textContent.includes('domain state: completed')")
                assert duplicate.read_bytes() == comic.read_bytes()
                assert '.kapowarr-quarantine' not in page.locator('body').inner_text()
            if folder_mode:
                page.get_by_role('button', name='Review selected folders', exact=True).click()
                page.get_by_role('heading', name='Reviewed whole-folder organization').wait_for()
                assert 'Preserve custom folder' in page.locator('#maintenance-specialized').inner_text()
                page.get_by_role('button', name='Review use of canonical folder', exact=True).click()
                page.get_by_text('Explicit custom → managed transition', exact=True).wait_for()
                target_text = page.locator('#maintenance-specialized tbody tr td').nth(1).inner_text()
                target = Path(target_text)
                page.get_by_role('button', name='Review folder confirmation', exact=True).click()
                assert page.locator('#maintenance-confirm-cancel').evaluate('(node) => node === document.activeElement')
                page.keyboard.press('Escape')
                page.get_by_role('button', name='Review folder confirmation', exact=True).click()
                page.get_by_role('button', name='Move reviewed folders', exact=True).click()
                page.get_by_role('heading', name='Batch: complete', exact=True).wait_for()
                assert not volume.exists()
                assert hashlib.sha256((target / comic.name).read_bytes()).hexdigest() == original
                assert (target / 'empty').is_dir()
                assert (target / 'notes.txt').read_text() == 'ancillary retained'
                page.locator('#maintenance-history-rows').get_by_role('button', name='folder organization', exact=True).click()
                page.get_by_role('button', name='Check Revert', exact=True).click()
                page.locator('#maintenance-specialized').get_by_role('button', name='Restore Previous Folder', exact=True).click()
                page.locator('#maintenance-confirm').get_by_role('button', name='Restore Previous Folder', exact=True).click()
                page.locator('#maintenance-confirm').wait_for(state='hidden')
                page.wait_for_function("document.getElementById('maintenance-detail').textContent.includes('domain state: completed')")
                assert volume.exists() and not target.exists()
                page.set_viewport_size({'width': 700, 'height': 850})
                assert page.locator('#maintenance-start').is_visible()
                assert not page.evaluate('Boolean(window.hostile)')
            if '--rename' in sys.argv:
                page.get_by_role('button', name='Review selected renames', exact=True).click()
                page.get_by_role('heading', name='Reviewed filename-only rename').wait_for()
                if '--restart' in sys.argv:
                    from backend.features.maintenance_runtime import \
                        MaintenanceRuntime
                    runtime = server.app.extensions['maintenance']
                    server.app.extensions['maintenance'] = MaintenanceRuntime(runtime.database)
                    page.reload()
                    page.get_by_text('This review has expired or the application restarted.', exact=False).wait_for()
                    page.evaluate('sessionStorage.clear()')
                    page.reload()
                    page.get_by_role('button', name='Start read-only scan', exact=True).click()
                    page.wait_for_function("!document.getElementById('maintenance-create-review').disabled")
                    page.get_by_role('button', name='Create reviewed worklist', exact=True).click()
                    page.get_by_role('heading', name='Revision 0', exact=False).wait_for()
                    row = page.locator('#maintenance-worklist tbody tr').filter(has_text='filename').first
                    row.locator('select').select_option('rename')
                    row.get_by_role('button', name='Select intent').click()
                    page.get_by_role('heading', name='Revision 1', exact=False).wait_for()
                    page.get_by_role('button', name='Review selected renames').click()
                    page.get_by_role('heading', name='Reviewed filename-only rename').wait_for()
                if '--narrow' in sys.argv:
                    page.set_viewport_size({'width': 700, 'height': 850})
                opener = page.get_by_role('button', name='Review rename confirmation', exact=True)
                opener.focus()
                page.keyboard.press('Enter')
                assert page.locator('#maintenance-confirm-cancel').evaluate('(node) => node === document.activeElement')
                page.keyboard.press('Tab')
                assert page.locator('#maintenance-confirm-submit').evaluate('(node) => node === document.activeElement')
                bounds = page.locator('#maintenance-confirm').bounding_box()
                assert bounds['x'] >= 0 and bounds['y'] >= 0
                assert bounds['width'] <= page.viewport_size['width']
                page.keyboard.press('Escape')
                assert not page.locator('#maintenance-confirm').is_visible()
                assert opener.evaluate('(node) => node === document.activeElement')
                page.get_by_role('button', name='Review rename confirmation', exact=True).click()
                page.get_by_role('button', name='Rename reviewed files', exact=True).click()
                if '--recovery' in sys.argv or '--manual' in sys.argv:
                    page.get_by_role('heading', name='Batch: pending or stopped', exact=True).wait_for()
                    page.locator('#maintenance-history-rows').get_by_role('button', name='rename', exact=True).click()
                    page.get_by_role('button', name='Review Recovery').click()
                    if '--manual' in sys.argv:
                        page.get_by_role('heading', name='Manual inspection required').wait_for()
                        for title in ('Continue Recovery', 'Force Complete', 'Delete', 'Revert Rename'):
                            assert page.get_by_role('button', name=title, exact=True).count() == 0
                    else:
                        page.locator('#maintenance-specialized').get_by_role('button', name='Continue Recovery').click()
                        page.locator('#maintenance-confirm-submit').click()
                        page.locator('#maintenance-confirm').wait_for(state='hidden')
                        page.wait_for_function("document.getElementById('maintenance-detail').textContent.includes('domain state: completed')")
                else:
                    page.get_by_role('heading', name='Batch: complete', exact=True).wait_for()
                if '--loss' in sys.argv:
                    page.get_by_text('Existing durable batch or receipt found.', exact=False).wait_for()
                    assert len(lost_posts) == 1
                    page.reload()
                    page.locator('#maintenance-history-rows').get_by_role('button', name='rename', exact=True).wait_for()
                if '--manual' not in sys.argv:
                    assert not comic.exists()
                target = volume / 'Issue 001.cbz'
                assert hashlib.sha256(target.read_bytes()).hexdigest() == original
                if '--manual' not in sys.argv:
                    if '--restart' in sys.argv:
                        runtime = server.app.extensions['maintenance']
                        server.app.extensions['maintenance'] = MaintenanceRuntime(runtime.database)
                        page.reload()
                    page.locator('#maintenance-history-rows').get_by_role('button', name='rename', exact=True).click()
                    page.get_by_role('button', name='Check Revert', exact=True).click()
                    page.locator('#maintenance-specialized').get_by_role('button', name='Revert Rename', exact=True).click()
                    page.locator('#maintenance-confirm').get_by_role('button', name='Revert Rename', exact=True).click()
                    page.locator('#maintenance-confirm').wait_for(state='hidden')
                    page.wait_for_function("document.getElementById('maintenance-detail').textContent.includes('domain state: completed')")
                    assert comic.exists()
                    assert not target.exists()
            assert page.get_by_role('button', name='Undo', exact=True).count() == 0
            assert page.get_by_role('button', name='Purge', exact=True).count() == 0
            if '--smoke' in sys.argv:
                for route_path in ('/', '/volumes/1', '/add', '/activity/history', '/settings/metadata', '/library-import', '/volumes/1/provider-switch', '/wanted'):
                    page.goto(origin + route_path)
                    page.wait_for_timeout(750)
                    assert page.locator('body').is_visible()
                page.goto(origin + '/maintenance')
                page.set_viewport_size({'width': 700, 'height': 850})
                page.get_by_role('heading', name='Library Maintenance').wait_for()
                assert page.locator('#maintenance-start').is_visible()
            assert not page.evaluate('Boolean(window.hostile)')
            assert not errors, errors
            assert not console_errors, console_errors
            browser.close()
        with server.app.app_context():
            db = get_db()
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            assert db.execute('SELECT filepath FROM files WHERE id=1').fetchone()[0] == str(comic)
            assert db.execute('SELECT COUNT(*) FROM issues_files WHERE file_id=1 AND issue_id=1').fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone()[0] == (100 if '--scale' in sys.argv else 1 if '--manual' in sys.argv else 2 if '--rename' in sys.argv or folder_mode or duplicate_mode else 1 if comicinfo_mode and '--malformed' not in sys.argv else 0)
            if folder_mode:
                assert db.execute('SELECT custom_folder FROM volumes WHERE id=1').fetchone()[0] == 1
        if not comicinfo_mode:
            assert hashlib.sha256(comic.read_bytes()).hexdigest() == original
        assert not records, records
        print(json.dumps(dict(result='pass', schema=66, chromium_errors=errors, console_errors=console_errors,
            server_errors=records, task_handler='real', socket='existing integrity-matched client',
            modes=sys.argv[1:], controlled_console=expected_console, diagnostics=metrics,
            byte_policy='ComicInfo rewrite; page and unknown XML preserved' if comicinfo_mode else 'unchanged',
            integrity='ok', foreign_keys=[])))
    finally:
        http.shutdown()
        thread.join()
