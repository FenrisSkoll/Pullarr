"""Disposable Chromium checks for Library Import controls and metadata artwork."""
import base64
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]

from api_key_browser_acceptance import QuietHandler
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from backend.internals.db import set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


def jpeg(width, height):
    output = BytesIO()
    Image.new('RGB', (width, height), '#5a376e').save(output, 'JPEG')
    return output.getvalue()


def main():
    files = [dict(row_id=str(index), filepath=f'/fixture/30 Days of Night/Book {index:03}.cbz',
        file_title=f'Book {index:03}', folder='/fixture/30 Days of Night', size=100,
        embedded=None, metadata_source=None, parsed=dict(title='30 Days of Night', year=2002))
        for index in range(1, 216)]
    files[0]['file_title'] = 'Abe Sapien v03 - Dark and Terrible and the New Race of Man - Book One'
    embedded_files = [dict(row_id='embedded', filepath='/fixture/Embedded/Book 001.cbz',
        file_title='Book 001', folder='/fixture/Embedded', size=100,
        embedded=dict(series='Embedded Edition', number='1', title='Book One'),
        metadata_source=dict(provider='metron', id='202'),
        parsed=dict(title='Embedded Edition', year=2023))]
    search_files = [dict(row_id=f'search-{index}', filepath=f'/fixture/Search/{name}',
        file_title=name.removesuffix('.cbz'), folder='/fixture/Search', size=100,
        embedded=None, metadata_source=None, parsed=dict(title=parsed, year=year))
        for index, (name, parsed, year) in enumerate([
            ('Astro City - Honor Guard (2016) - v001.cbz', 'Astro City Honor Guard', 2016),
            ('Astro City - Honor Guard (2016) - v002.cbz', 'Astro City Honor Guard', 2016),
            ('Astro City - Family Album (1998) - v001.cbz', 'Astro City Family Album', 1998),
            ('30 Days of Night Deluxe Edition (2023) - 002 - Book Two.cbz',
                '30 Days of Night Deluxe Edition', 2023),
            ('100 Bullets - Brother Lono - The Deluxe Edition (2024) - Hard-Cover.cbz',
                '100 Bullets Brother Lono The Deluxe Edition', 2024),
            ('2000 AD - 001.cbz', '2000 AD', None),
            ('52 - 001.cbz', 'Search', None),
        ])]
    large = jpeg(600, 900)
    small = 'data:image/jpeg;base64,' + base64.b64encode(jpeg(100, 150)).decode()
    calls = dict(scans=[], suggestions=[], catalogues=[], searches=[], artwork=[], imports=0,
                 embedded_only=False, search_only=False,
                 fail_suggestions=False)
    errors = []
    with TemporaryDirectory(prefix='pullarr-import-controls-') as temporary:
        set_db_location(str(Path(temporary) / 'db'))
        server = Server()
        with server.app.app_context():
            setup_db()
            Settings().update({'api_key':'disposable-import-controls'})
        http = make_server('127.0.0.1', 0, server.app, threaded=True,
                           request_handler=QuietHandler)
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{http.server_port}'
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch()
                page = browser.new_page(viewport={'width':1440, 'height':900})
                page.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'disposable-import-controls',last_login:Date.now()/1000}));")
                page.on('pageerror', lambda error: errors.append(str(error)))

                def scan(route):
                    params = parse_qs(urlsplit(route.request.url).query)
                    limit = int(params['max_unregistered'][0])
                    offset = int(params.get('offset', ['0'])[0])
                    calls['scans'].append(dict(limit=limit, offset=offset,
                        filter=params.get('folder_filter', [''])[0]))
                    available = search_files if calls['search_only'] else \
                        embedded_files if calls['embedded_only'] else files
                    selected = available[:limit]
                    chunk = selected[offset:offset+100]
                    counts = dict(scanned_folders=905, eligible_folders=905,
                        not_yet_scanned_folders=0, discovered=len(selected),
                        total_unregistered=len(available), more_unregistered=len(available)>limit,
                        already_managed=0, limited=False)
                    route.fulfill(json=dict(result=dict(items=chunk, counts=counts,
                        next_offset=offset+len(chunk) if offset+len(chunk)<len(selected) else None,
                        next_after=chunk[-1]['filepath'] if offset+len(chunk)<len(selected) else None,
                        enumeration_complete=limit>=len(available))))

                def suggestions(route):
                    paths = route.request.post_data_json['files']
                    calls['suggestions'].append(paths)
                    if calls['fail_suggestions']:
                        route.fulfill(status=429, json=dict(error='RateLimited',result={}))
                        return
                    route.fulfill(json=dict(result=[dict(filepath=path, cv=dict(id=101,
                        title='30 Days of Night (2002)', link='https://comicvine.gamespot.com/volume/4050-101/'))
                        for path in paths]))

                def catalogue(route):
                    paths = route.request.post_data_json['files']
                    calls['catalogues'].append(paths)
                    issues = [dict(id=str(2000+index), number=str(index), title=f'Book {index}',
                        date=None, owned=False) for index in range(1,216)]
                    proposals = {path:dict(issue_ids=[str(2000+int(Path(path).stem.split()[-1]))],
                        confidence='strong') for path in paths}
                    route.fulfill(json=dict(result=dict(title='30 Days of Night', year=2002,
                        issues=issues, proposals=proposals, loaded_issue_count=215,
                        reported_issue_count=215, completeness='complete')))

                def search(route):
                    calls['searches'].append(route.request.url)
                    def result(provider, identifier, cover, state, site, title='30 Days of Night'):
                        return dict(metadata_source=dict(provider=provider,id=str(identifier)),
                            result_key=f'{provider}:{identifier}', title=title,
                            year=2002, publisher='Fixture', issue_count=3,
                            cover_link=cover, artwork_state=state, site_url=site)
                    groups = [dict(label='ComicVine', status='complete', result_count=1,
                        results=[result('comicvine',101,'https://art.example/large.jpg',
                            'available','https://comicvine.gamespot.com/volume/4050-101/')]),
                        dict(label='Metron',status='complete',result_count=1,
                        results=[result('metron',202,None,'pending','https://metron.cloud/series/202/')]),
                        dict(label='GCD',status='complete',result_count=1,
                        results=[result('gcd',303,None,'unavailable','https://www.comics.org/series/303/',
                            'Abe Sapien v03 - Dark and Terrible and the New Race of Man')]),
                        dict(label='Unsafe or missing',status='complete',result_count=2,
                        results=[result('comicvine',404,None,'unavailable','javascript:alert(1)'),
                            result('comicvine',405,None,'unavailable',None)])]
                    route.fulfill(json=dict(result=dict(schema='metadata-search/v2',
                        providers=groups, artwork_ticket='fixture-ticket')))

                def artwork(route):
                    identities = route.request.post_data_json['identities']
                    calls['artwork'].append(identities)
                    route.fulfill(json=dict(result=[dict(result_key=identity,
                        artwork_state='available',image=small) for identity in identities]))

                def unexpected_import(route):
                    calls['imports'] += 1
                    route.fulfill(status=500, json=dict(error='UnexpectedImport'))

                page.route('**/api/libraryimport/files?*', scan)
                page.route('**/api/libraryimport/suggestions?*', suggestions)
                page.route('**/api/libraryimport/catalog?*', catalogue)
                page.route('**/api/libraryimport/submit?*', unexpected_import)
                page.route('**/api/volumes/search/artwork?*', artwork)
                page.route('**/api/volumes/search?*', search)
                page.route('https://art.example/large.jpg', lambda route:
                    route.fulfill(body=large, content_type='image/jpeg'))
                page.route('https://comicvine.gamespot.com/**', lambda route:
                    route.fulfill(body='provider', content_type='text/plain'))
                page.goto(origin + '/library-import')
                page.locator('#auto-match-input').uncheck()
                page.locator('#scan-file-limit').select_option('1000')
                page.locator('#run-import-button').click()
                rows = page.locator('.proposal-list .li-result')
                expect(rows).to_have_count(100)
                assert calls['scans'][0]['limit'] == 1000
                expect(page.locator('#scan-summary')).to_contain_text('More selected result pages available')
                expect(page.locator('#loaded-count')).to_have_text('100 loaded of 215 discovered')
                expect(page.locator('#import-selection-count')).to_have_text('100 selected for import')
                assert page.locator('.table-container thead th').all_inner_texts() == \
                    ['Import', 'File', 'Publication', 'Issue association', 'Status']
                assert page.locator('.change-match').count() == 0
                page.locator('#import-next-page').click()
                expect(rows).to_have_count(200)
                page.locator('#import-next-page').click()
                expect(rows).to_have_count(215)
                expect(page.locator('#scan-summary')).to_contain_text('All selected result pages shown')
                expect(page.locator('#loaded-count')).to_have_text('215 loaded of 215 discovered')
                assert len(set(row['filepath'] for row in files)) == 215

                choice = rows.first.locator('.publication-choice')
                choice.focus()
                page.keyboard.press('Enter')
                expect(page.locator('#cv-window')).to_be_visible()
                expect(page.locator('#search-input')).to_be_focused()
                expect(page.locator('#match-context')).to_contain_text('Only this file will change')
                expect(page.locator('#search-input')).to_have_value(
                    'Abe Sapien v03 - Dark and Terrible and the New Race of Man')
                assert page.locator('#search-input').evaluate(
                    'node => node.selectionStart === 0 && node.selectionEnd === node.value.length')
                assert len(calls['searches']) == 0
                page.locator('#search-input').fill('30 Days of Night')
                page.locator('#search-input').press('Enter')
                results = page.locator('.search-results tr.search-result')
                expect(results).to_have_count(5)
                assert len(calls['searches']) == 1
                cv = results.first
                link = cv.get_by_role('link', name='View on provider')
                assert cv.get_by_role('link', name='30 Days of Night (2002)').get_attribute('href') == \
                    'https://comicvine.gamespot.com/volume/4050-101/'
                assert link.get_attribute('href') == 'https://comicvine.gamespot.com/volume/4050-101/'
                assert link.get_attribute('target') == '_blank'
                assert link.get_attribute('rel') == 'noopener noreferrer'
                assert results.nth(1).get_by_role('link', name='View on provider').get_attribute('href') == 'https://metron.cloud/series/202/'
                assert results.nth(2).get_by_role('link', name='View on provider').get_attribute('href') == 'https://www.comics.org/series/303/'
                assert results.nth(3).get_by_role('link').count() == 0
                assert results.nth(4).get_by_role('link').count() == 0
                assert results.nth(2).locator('.import-cover-button').is_disabled()
                assert results.nth(2).locator('.import-cover-button img').get_attribute('src').endswith('cover-placeholder.svg')
                pending_cover = results.nth(1).locator('.import-cover-button')
                expect(pending_cover).to_be_enabled()
                pending_cover.click()
                viewer = page.locator('#import-cover-lightbox')
                expect(viewer).to_be_visible()
                expect(page.locator('#import-cover-resolution')).to_be_visible()
                assert page.locator('#import-cover-image').evaluate(
                    '(image) => image.naturalWidth === 100 && image.naturalHeight === 150')
                page.locator('#import-cover-close').click()
                assert pending_cover.evaluate('(node) => node === document.activeElement')
                with page.expect_popup() as popup:
                    link.click()
                popup.value.close()
                assert choice.inner_text() == 'Choose publication'
                cover = cv.locator('.import-cover-button')
                expect(cover).to_be_enabled()
                cover.click()
                expect(viewer).to_be_visible()
                assert page.locator('#import-cover-image').evaluate(
                    '(image) => image.naturalWidth === 600 && image.naturalHeight === 900')
                for width in (1440,390):
                    page.set_viewport_size({'width':width,'height':900})
                    bounds = viewer.bounding_box()
                    assert bounds['width'] <= width * (.96 if width == 390 else .76), bounds
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), \
                        page.evaluate("""() => ({scroll:document.documentElement.scrollWidth,width:innerWidth,
                            outside:[...document.querySelectorAll('body *')].filter(node => node.getBoundingClientRect().right > innerWidth+1).slice(0,12).map(node => [node.tagName,node.id,node.className,node.getBoundingClientRect().right,node.outerHTML.slice(0,160)])})""")
                page.keyboard.press('Escape')
                expect(viewer).not_to_be_visible()
                assert cover.evaluate('(node) => node === document.activeElement')
                expect(page.locator('#cv-window')).to_be_visible()
                assert results.count() == 5
                assert results.first.locator('.import-search-actions button').count() == 1
                expect(results.first.locator('.import-search-actions button')).to_have_text('Select for this file')
                assert all(size['width'] > 90 and size['height'] < 55 for size in
                    results.first.locator('.import-search-actions button').evaluate_all(
                        'nodes => nodes.map(node => ({width:node.getBoundingClientRect().width,height:node.getBoundingClientRect().height}))'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                cover.focus()
                page.keyboard.press('Space')
                expect(viewer).to_be_visible()
                page.locator('#import-cover-close').click()
                cover.click()
                viewer.click(position={'x':3, 'y':3})
                expect(viewer).not_to_be_visible()
                page.set_viewport_size({'width':1440,'height':900})
                cv.get_by_role('button', name='Select for this file').click()
                expect(rows.first.locator('.issue-column')).to_contain_text('#1 — Book 1')
                expect(rows.first.locator('.publication-meta')).to_contain_text('Selected manually · comicvine · ID 101')
                assert len(calls['catalogues']) == 1
                rows.first.locator('.publication-choice').focus()
                page.keyboard.press('Space')
                expect(page.locator('#cv-window')).to_be_visible()
                expect(page.locator('#search-input')).to_be_focused()
                expect(page.locator('#search-input')).to_have_value(
                    'Abe Sapien v03 - Dark and Terrible and the New Race of Man')
                assert len(calls['searches']) == 1
                expect(rows.first.locator('.issue-column')).to_contain_text('#1 — Book 1')
                expect(results).to_have_count(0)
                page.locator('#cv-window .cancel-window').click()
                expect(rows.first.locator('.publication-choice')).to_be_focused()
                page.keyboard.press('Enter')
                expect(page.locator('#search-input')).to_be_focused()
                page.keyboard.press('Escape')
                expect(rows.first.locator('.publication-choice')).to_be_focused()

                # Import checkboxes start checked; bulk checkboxes require explicit choices.
                assert page.locator('.proposal-list .import-file-select:checked').count() == 215, \
                    (page.locator('.proposal-list .import-file-select:checked').count(), rows.count())
                assert page.locator('.proposal-list .bulk-match-select:checked').count() == 0
                expect(page.locator('#open-bulk-match')).to_be_disabled()
                three = rows.nth(1), rows.nth(2), rows.nth(3)
                for target in three:
                    target.locator('.import-file-select').uncheck()
                    target.locator('.bulk-match-select').check()
                expect(page.locator('#open-bulk-match')).to_have_text('Bulk match (3)')
                page.locator('#clear-bulk-match').click()
                expect(page.locator('#open-bulk-match')).to_be_disabled()
                assert all(not target.locator('.import-file-select').is_checked() for target in three)
                for target in three:
                    target.locator('.bulk-match-select').check()
                rows.nth(4).locator('.publication-choice').click()
                expect(page.locator('#match-context')).to_contain_text('Only this file will change')
                page.locator('#search-input').press('Enter')
                assert results.first.locator('.bulk-match').count() == 0
                results.first.get_by_role('button', name='Select for this file').click()
                expect(rows.nth(4).locator('.publication-meta')).to_contain_text('Selected manually')
                assert page.locator('.proposal-list .bulk-match-select:checked').count() == 3
                assert all(target.locator('.publication-choice').inner_text() == 'Choose publication'
                    for target in three)
                page.locator('#import-filter').fill('Book 001')
                expect(page.locator('#bulk-selection-count')).to_contain_text('3 outside current filter')
                page.locator('#open-bulk-match').click()
                expect(page.locator('#match-context')).to_contain_text('including 3 outside the current filter')
                page.locator('#search-input').press('Enter')
                results.first.locator('.bulk-match').click()
                expect(page.locator('#bulk-match-review')).not_to_be_visible()
                for number, target in enumerate(three, start=2):
                    expect(target.locator('.issue-column')).to_contain_text(f'#{number} — Book {number}')
                    assert not target.locator('.import-file-select').is_checked()
                expect(page.locator('#bulk-result-status')).to_contain_text('3 issue suggestions found; 0 need review')
                page.locator('#import-filter').fill('')
                page.locator('.proposal-list .bulk-match-select').evaluate_all("nodes => nodes.slice(0,205).forEach(node => {node.checked=true;node.dispatchEvent(new Event('change',{bubbles:true}));})")
                expect(page.locator('#open-bulk-match')).to_have_text('Bulk match (205)')
                page.locator('#selectall-input').check()
                page.locator('#selectall-input').uncheck()
                assert page.locator('.proposal-list .import-file-select:checked').count() == 0
                assert page.locator('.proposal-list .bulk-match-select:checked').count() == 205
                page.locator('#open-bulk-match').click()
                expect(page.locator('#match-context')).to_contain_text('Matching 205 explicitly chosen files')
                expect(page.locator('#search-input')).to_have_value('')
                page.locator('#search-input').fill('30 Days of Night')
                page.locator('#search-input').press('Enter')
                expect(results.first.locator('.bulk-match')).to_have_text('Apply to 205 selected files')
                assert results.first.get_by_role('button', name='Select for this file').count() == 0
                results.first.locator('.bulk-match').click()
                expect(page.locator('#bulk-match-review')).to_be_visible()
                expect(page.locator('#bulk-review-cancel')).to_be_focused()
                page.locator('#bulk-review-cancel').click()
                assert rows.nth(204).locator('.publication-choice').inner_text() == 'Choose publication'
                expect(page.locator('#cv-window')).to_be_visible()
                results.first.locator('.bulk-match').click()
                page.locator('#bulk-review-apply').click()
                expect(rows.nth(204).locator('.issue-column')).to_contain_text('#205 — Book 205')
                expect(page.locator('#bulk-result-status')).to_contain_text('Publication assigned to 205 files')
                assert rows.nth(214).locator('.publication-choice').inner_text() == 'Choose publication'
                assert all(len(batch) <= 100 for batch in calls['catalogues'])
                assert len({path for batch in calls['catalogues'] for path in batch}) == 205
                assert page.locator('.proposal-list .bulk-match-select:checked').count() == 0
                assert page.locator('.proposal-list .import-file-select:checked').count() == 0
                rows.nth(214).locator('.publication-choice').click()
                page.locator('#search-input').press('Enter')
                results.nth(2).get_by_role('button', name='Select for this file').click()
                expect(rows.nth(214).locator('.publication-meta')).to_contain_text('gcd · ID 303')
                assert rows.nth(214).locator('.publication-title').evaluate(
                    'node => node.scrollWidth <= node.clientWidth + 1')
                rows.first.locator('.bulk-match-select').check()
                page.locator('#open-bulk-match').click()
                page.locator('#search-input').press('Enter')
                results.nth(2).locator('.bulk-match').click()
                expect(page.locator('#bulk-match-review')).to_be_visible()
                expect(page.locator('#bulk-review-detail')).to_contain_text('1 existing manual matches will change')
                page.keyboard.press('Escape')
                expect(page.locator('#bulk-match-review')).not_to_be_visible()
                expect(rows.first.locator('.publication-meta')).to_contain_text('comicvine · ID 101')
                results.nth(2).locator('.bulk-match').click()
                page.locator('#bulk-review-apply').click()
                expect(rows.first.locator('.publication-meta')).to_contain_text('gcd · ID 303')
                for width in (1440, 1024, 768, 390):
                    page.set_viewport_size({'width':width,'height':900})
                    screenshot_root = Path(os.environ.get('PULLARR_SCREENSHOT_DIR', temporary))
                    screenshot_root.mkdir(parents=True, exist_ok=True)
                    page.evaluate('window.scrollTo(0,0)')
                    page.screenshot(path=str(screenshot_root / f'library-import-toolbar-{width}.png'))
                    rows.nth(214).scroll_into_view_if_needed()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
                    assert rows.nth(214).locator('.publication-title').evaluate(
                        'node => node.scrollWidth <= node.clientWidth + 1'), width
                    page.screenshot(path=str(screenshot_root / f'library-import-{width}.png'))
                page.evaluate("document.documentElement.classList.add('dark-mode')")
                page.evaluate('window.scrollTo(0,0)')
                page.screenshot(path=str(screenshot_root / 'library-import-dark-390.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.set_viewport_size({'width':1440,'height':900})

                # The optional matching budget uses the same scan scope and retains later-page proposals.
                page.locator('#list-window .cancel-button').click()
                page.locator('#auto-match-input').check()
                page.locator('#advanced-matching summary').click()
                page.locator('#match-folder-limit').select_option('20')
                page.locator('#run-import-button').click()
                expect(page.locator('#matching-status')).to_contain_text('Matching complete')
                assert all(call['limit'] == 1000 for call in calls['scans'])
                assert all(len(batch) <= 10 for batch in calls['suggestions'])
                page.locator('#import-next-page').click()
                expect(page.locator('.proposal-list .li-result')).to_have_count(200)
                expect(page.locator('.proposal-list .li-result').nth(150).locator('.publication-meta')).to_contain_text('Suggested by ComicVine · comicvine · ID 101')
                page.locator('#list-window .cancel-button').click()
                calls['fail_suggestions'] = True
                page.locator('#run-import-button').click()
                expect(page.locator('#matching-status')).to_contain_text('provider error or rate limit')
                expect(page.locator('.proposal-list .li-result')).to_have_count(100)
                assert not errors, errors
                assert all(len(batch) <= 4 for batch in calls['artwork'])
                assert sum(map(len,calls['artwork'])) <= 12 * len(calls['searches'])
                assert calls['imports'] == 0
                calls['embedded_only'] = True
                page.goto(origin + '/library-import')
                page.locator('#auto-match-input').uncheck()
                page.locator('#run-import-button').click()
                embedded_row = page.locator('.proposal-list .li-result')
                expect(embedded_row).to_have_count(1)
                expect(embedded_row.locator('.publication-title')).to_have_text('Embedded Edition')
                expect(embedded_row.locator('.publication-meta')).to_have_text(
                    'From file metadata · metron · ID 202')
                expect(embedded_row.locator('.publication-choice')).to_have_text('Change publication')
                calls['search_only'] = True
                page.goto(origin + '/library-import')
                page.locator('#auto-match-input').uncheck()
                page.locator('#run-import-button').click()
                search_rows = page.locator('.proposal-list .li-result')
                expect(search_rows).to_have_count(len(search_files))
                expected = ['Astro City - Honor Guard', 'Astro City - Honor Guard',
                    'Astro City - Family Album', '30 Days of Night Deluxe Edition',
                    '100 Bullets - Brother Lono - The Deluxe Edition', '2000 AD', '52']
                searches_before = len(calls['searches'])
                for index, title in enumerate(expected):
                    search_rows.nth(index).locator('.publication-choice').click()
                    expect(page.locator('#search-input')).to_have_value(title)
                    expect(page.locator('#search-input')).to_be_focused()
                    assert page.locator('#search-input').evaluate(
                        'node => node.selectionStart === 0 && node.selectionEnd === node.value.length')
                    if index == 0:
                        expect(page.locator('#match-context')).to_contain_text('local year 2016')
                        page.locator('#search-input').press('Enter')
                        expect(page.locator('.search-results tr.search-result')).to_have_count(5)
                        searches_before += 1
                    else:
                        expect(page.locator('.search-results tr.search-result')).to_have_count(0)
                    assert len(calls['searches']) == searches_before
                    page.locator('#cv-window .cancel-window').click()
                for index in (0, 1):
                    search_rows.nth(index).locator('.bulk-match-select').check()
                page.locator('#open-bulk-match').click()
                expect(page.locator('#search-input')).to_have_value('Astro City - Honor Guard')
                assert len(calls['searches']) == searches_before
                page.locator('#cv-window .cancel-window').click()
                search_rows.nth(2).locator('.bulk-match-select').check()
                page.locator('#open-bulk-match').click()
                expect(page.locator('#search-input')).to_have_value('')
                expect(page.locator('#match-context')).to_contain_text('Matching 3 explicitly chosen files')
                assert len(calls['searches']) == searches_before
                page.locator('#cv-window .cancel-window').click()
                assert all(row.locator('.publication-choice').inner_text() == 'Choose publication'
                    for row in [search_rows.nth(0), search_rows.nth(1), search_rows.nth(2)])
                for width in (1440, 1024, 768, 390):
                    page.set_viewport_size({'width':width, 'height':900})
                    search_rows.nth(5).locator('.publication-choice').click()
                    expect(page.locator('#search-input')).to_have_value('2000 AD')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
                    page.locator('#cv-window .cancel-window').click()
                search_rows.nth(5).locator('.publication-choice').click()
                page.get_by_role('button', name='Search publications').click()
                expect(page.locator('.search-results tr.search-result')).to_have_count(5)
                searches_before += 1
                assert len(calls['searches']) == searches_before
                page.locator('#cv-window .cancel-window').click()
                page.evaluate("""() => {
                    const original = window.fetch.bind(window);
                    window.fetch = (...args) => String(args[0]).includes('/api/volumes/search?')
                        ? new Promise(resolve => {
                            window.releaseDelayedSearch = () => {
                                window.releaseDelayedSearch = null;
                                original(...args).then(resolve);
                            };
                        }) : original(...args);
                }""")
                search_rows.nth(0).locator('.publication-choice').click()
                page.locator('#search-input').press('Enter')
                page.wait_for_function('!!window.releaseDelayedSearch')
                page.locator('#cv-window .cancel-window').click()
                search_rows.nth(2).locator('.publication-choice').click()
                expect(page.locator('#search-input')).to_have_value('Astro City - Family Album')
                page.evaluate('window.releaseDelayedSearch()')
                page.wait_for_timeout(100)
                expect(page.locator('.search-results tr.search-result')).to_have_count(0)
                expect(page.locator('#match-error')).to_have_text('')
                page.locator('#cv-window .cancel-window').click()
                assert calls['imports'] == 0
                browser.close()
            print(json.dumps(dict(result='pass', scan_requests=len(calls['scans']),
                suggestion_batches=len(calls['suggestions']),catalogue_batches=list(map(len,calls['catalogues'])),
                artwork_batches=list(map(len,calls['artwork'])),browser_errors=errors)))
        finally:
            http.shutdown()
            thread.join()


if __name__ == '__main__':
    main()
