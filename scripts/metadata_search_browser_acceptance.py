"""Chromium Add Comics acceptance with disposable data and synthetic providers."""

import sys
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / 'scripts')]

from api_key_browser_acceptance import QuietHandler
from PIL import Image
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from backend.features.metadata_artwork import SearchArtwork
from backend.implementations.metadata.artwork import thumbnail
from backend.implementations.metadata.models import VolumeSearchResult
from backend.implementations.metadata.provider import (MetadataArtworkProvider,
                                                       MetadataSearchProvider)
from backend.implementations.metadata.search_presentation import \
    comicvine_relations
from backend.internals.db import get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


def main():
    calls, errors, external = [], [], []
    buffer = BytesIO()
    Image.new('RGB', (24, 36), 'blue').save(buffer, 'PNG')
    cover = thumbnail(buffer.getvalue())

    class Fixture(MetadataSearchProvider, MetadataArtworkProvider):
        provider = 'comicvine'
        search_label = 'ComicVine'

        async def search_volumes(self, query):
            identity = '128991' if query == 'cv:128991' else '100'
            title = 'Batman: Deluxe Edition' if identity == '128991' else 'Batman: Rebirth Deluxe Edition'
            text = '<p>Books 4–6.</p>' if identity == '128991' else '<p>Books 1–3.</p><p>Continued in <a href="/batman/4050-128991/">Batman: Deluxe Edition</a></p>'
            text += '<script>window.metadataHostile=true</script><img src=x onerror="window.metadataHostile=true">'
            row = VolumeSearchResult(self.provider, identity, title, 2017, 1, None, text, None, [], 'Fixture', 3, False, None)
            if self.provider == 'comicvine':
                row.relations = comicvine_relations(identity, text)
            else:
                row.artwork_hint = identity
            return [row]

        def search_artwork_url(self, identity, hint):
            calls.append((self.provider, identity))
            return 'fixture-image'

    class MetronFixture(Fixture):
        provider, search_label = 'metron', 'Metron'

    class GcdFixture(Fixture):
        provider, search_label = 'gcd', 'GCD'

    class Images:
        def fetch_image(self, provider, url):
            return cover

    with TemporaryDirectory(prefix='pullarr-metadata-browser-') as directory:
        set_db_location(str(Path(directory) / 'db'))
        server = Server()
        with server.app.app_context():
            setup_db()
            Settings().update({'api_key': 'synthetic-metadata-browser'})
            library = Path(directory) / 'library'
            library.mkdir()
            get_db().execute('INSERT INTO root_folders VALUES(1,?)', (str(library),))
            get_db().connection.commit()
        http = make_server('127.0.0.1', 0, server.app, threaded=True, request_handler=QuietHandler)
        Thread(target=http.serve_forever, daemon=True).start()
        origin = 'http://127.0.0.1:' + str(http.server_port)
        try:
            with patch.dict('backend.implementations.metadata.registry.PROVIDERS',
                            comicvine=Fixture, metron=MetronFixture, gcd=GcdFixture), \
                    patch('backend.features.metadata_artwork.ARTWORK', SearchArtwork(fetcher=Images())), sync_playwright() as driver:
                browser = driver.chromium.launch()
                context = browser.new_context()
                context.add_init_script("localStorage.setItem('kapowarr',JSON.stringify({api_key:'synthetic-metadata-browser',last_login:Date.now()/1000}));")
                page = context.new_page()
                page.on('pageerror', lambda _: errors.append('page error'))
                page.on('request', lambda r: external.append('external') if not r.url.startswith(origin) else None)
                for width in (1440, 390):
                    page.set_viewport_size(dict(width=width, height=900))
                    page.goto(origin + '/add')
                    page.locator('#metadata-provider').select_option('all')
                    page.locator('#search-input').fill('Batman Rebirth Deluxe Edition')
                    page.locator('#search-input').press('Enter')
                    cards = page.locator('#search-results .search-entry')
                    cards.nth(3).wait_for()
                    assert cards.count() == 4
                    assert cards.nth(0).get_attribute('data-provider-id') == '100'
                    assert cards.nth(1).get_attribute('data-provider-id') == '128991'
                    assert 'Related continuation' in cards.nth(1).inner_text()
                    assert 'Continues as → Batman: Deluxe Edition' in cards.nth(0).inner_text()
                    assert '<p>' not in cards.nth(0).inner_text()
                    selector = '.entry-description' if width > 600 else '.entry-spare-description'
                    assert cards.nth(0).locator(selector).evaluate("e => getComputedStyle(e).whiteSpace") == 'pre-line'
                    for provider in ('metron', 'gcd'):
                        card = page.locator('#search-results .search-entry[data-provider="' + provider + '"]')
                        card.scroll_into_view_if_needed()
                        page.wait_for_function("provider => document.querySelector('#search-results .search-entry[data-provider=\"'+provider+'\"] img').src.startsWith('data:image/jpeg;base64,')", arg=provider)
                    assert not page.evaluate('Boolean(window.metadataHostile)')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                browser.close()
            assert not errors and not external, (errors, external)
            assert sorted(calls) == [('gcd', '100'), ('metron', '100')], calls
            print('Metadata search browser PASS: 1440/390px, readable Batman descriptions, exact distinct continuation, GCD/Metron thumbnails, cache, no executable HTML or external browser requests')
        finally:
            http.shutdown()


if __name__ == '__main__':
    main()
