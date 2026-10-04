"""Synthetic hostile descriptions, explicit links and bounded image transport."""

from asyncio import run
from dataclasses import replace
from io import BytesIO
from unittest import TestCase
from unittest.mock import Mock, patch

from fixtures.comicvine_search import ComicVineSearchHarness, volume_response
from fixtures.reading_orders import FixtureResponse
from PIL import Image

from backend.implementations.metadata.artwork import (ArtworkHTTP,
                                                      artwork_url, thumbnail)
from backend.implementations.metadata.comicvine import \
    ComicVineMetadataProvider
from backend.implementations.metadata.models import PublicationRelation
from backend.implementations.metadata.search_presentation import (
    comicvine_relations, description_text)
from frontend.metadata import qualified_volume_search_result


def picture():
    buffer = BytesIO()
    Image.new('RGB', (12, 20), 'blue').save(buffer, 'PNG')
    return buffer.getvalue()


class SearchTextTests(TestCase):
    def test_paragraph_list_spacing_and_inline_text(self):
        self.assertEqual(description_text('<p>First <b>bold</b> &amp; text.</p><p>Next</p><ul><li>One</li><li>Two</li></ul>'),
                         'First bold & text.\nNext\nOne\nTwo')

    def test_active_containers_are_removed(self):
        for tag in ('script', 'style', 'iframe', 'object', 'embed', 'svg', 'form', 'template'):
            with self.subTest(tag=tag):
                payload = '<embed src="hostile">' if tag == 'embed' else '<' + tag + '>hostile</' + tag + '>'
                self.assertEqual(description_text('<p>Safe</p>' + payload), 'Safe')

    def test_attributes_urls_and_comments_cannot_survive_as_markup(self):
        for payload in ('<img src=x onerror=alert(1)>', '<a onclick="alert(1)" href="javascript:alert(1)">text</a>',
                        '<a href="data:text/html,x">text</a>', '<!--secret-->'):
            value = description_text('<p>Safe</p>' + payload)
            self.assertNotIn('<', value)
            self.assertNotIn('alert', value)
            self.assertNotIn('data:', value)
            self.assertNotIn('secret', value)

    def test_bounded_text(self):
        self.assertEqual(len(description_text('x' * 200000)), 16000)

    def test_explicit_directions_and_titles(self):
        for phrase, kind in (('preceded by', 'continues_from'), ('continued from', 'continues_from'),
                             ('continued in', 'continues_as'), ('continues as', 'continues_as')):
            evidence = comicvine_relations('12', f'<p>{phrase} <a href="/batman/4050-128991/">Batman: Deluxe Edition</a></p>')
            self.assertEqual(len(evidence), 1)
            self.assertEqual((evidence[0].relation_type, evidence[0].target_id, evidence[0].target_title),
                             (kind, '128991', 'Batman: Deluxe Edition'))

    def test_relationship_url_admission(self):
        for url in ('https://evil.invalid/a/4050-2/', 'https://comicvine.gamespot.com.evil.invalid/4050-2/',
                    'https://user:password@comicvine.gamespot.com/4050-2/', '/4000-2/', '/4050-0/',
                    '/4050-12/', '/4050-2/?token=secret', '/4050-%32/', 'javascript:alert(1)',
                    '//evil.invalid/4050-2/', '/4050-2/#x', '/a/../4050-2/'):
            with self.subTest(url=url):
                self.assertEqual(comicvine_relations('12', f'continued in <a href="{url}">Other</a>'), [])

    def test_unintroduced_links_and_self_relations_rejected(self):
        self.assertEqual(comicvine_relations('1', '<p>See <a href="/4050-2/">Other</a></p>'), [])
        with self.assertRaises(ValueError):
            PublicationRelation('gcd', '1', 'continues_as', 'gcd', '1', '', 'fixture')

    def test_only_two_relationships(self):
        source = ''.join(f'<p>continued in <a href="/4050-{i}/">Other</a></p>' for i in range(2, 100))
        self.assertEqual(len(comicvine_relations('1', source)), 2)


class SearchAdapterTests(ComicVineSearchHarness, TestCase):
    def test_raw_relationship_survives_legacy_description_reduction(self):
        html = '<p>Books 1–3.</p><h2>Continuation</h2><p>continued in <a href="/batman/4050-128991/">Batman: Deluxe Edition</a></p>'
        self.respond([volume_response(name='Batman: Rebirth Deluxe Edition', description=html)])
        result = run(ComicVineMetadataProvider().search_volumes('Batman'))[0]
        self.assertEqual(result.relations[0].target_id, '128991')
        stored = result.description
        self.assertEqual(qualified_volume_search_result(result)['description'], 'Books 1–3.')
        self.assertEqual(result.description, stored)
        self.assertIn('<p>', stored)


class ArtworkSecurityTests(TestCase):
    def test_supported_image_reencoded_as_jpeg(self):
        data = thumbnail(picture())
        self.assertTrue(data.startswith(b'\xff\xd8'))
        self.assertLess(len(data), 65536)

    def test_invalid_active_and_oversize_payloads(self):
        for value in (b'<svg/>', b'<html/>', b'not an image', b'x' * (2 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                thumbnail(value)

    def test_unapproved_urls_never_fetch(self):
        for value in ('https://evil.invalid/a.jpg', 'https://user:pass@static.metron.cloud/media/issue/a.jpg',
                      'https://static.metron.cloud/media/issue/a.jpg?token=secret',
                      'http://static.metron.cloud/media/issue/a.jpg', 'https://127.0.0.1/media/issue/a.jpg',
                      'https://static.metron.cloud/media/issue/a.svg', 'https://static.metron.cloud/media/issue/../a.jpg'):
            with self.assertRaises(ValueError):
                artwork_url('metron', value)

    def test_official_gcd_double_slash_and_metron_path(self):
        for provider, url in (('gcd', 'https://files1.comics.org//img/gcd/covers_by_id/1054/w400/1054045.jpg'),
                              ('metron', 'https://static.metron.cloud/media/issue/2026/fixture.jpg')):
            self.assertEqual(artwork_url(provider, url), url)

    def transport(self, status=200, **headers):
        response = FixtureResponse(picture(), False)
        response.status = status
        response.headers = {'Content-Type': 'image/png', **headers}
        connection = Mock(sock=None)
        connection.getresponse.return_value = response
        factory = Mock(return_value=connection)
        transport = ArtworkHTTP(resolver=lambda _: ['93.184.216.34'], connection=factory)
        self.addCleanup(transport.dns.shutdown)
        return transport, factory, connection

    def test_pinned_transport_success_no_auth_proxy_cookie(self):
        transport, factory, connection = self.transport()
        self.assertTrue(transport.fetch_image('metron', 'https://static.metron.cloud/media/issue/a.jpg'))
        self.assertEqual(factory.call_args.args[:2], ('static.metron.cloud', '93.184.216.34'))
        headers = connection.request.call_args.kwargs['headers']
        self.assertFalse(set(headers) & {'Authorization', 'Cookie', 'Proxy-Authorization'})
        connection.close.assert_called_once()

    def test_redirect_encoding_content_type_length_rejected(self):
        for status, headers in ((302, {'Location': 'http://127.0.0.1/'}), (200, {'Content-Encoding': 'gzip'}),
                                (200, {'Content-Type': 'text/html'}), (200, {'Content-Length': '2097153'})):
            transport, factory, connection = self.transport(status, **headers)
            with self.assertRaises(ValueError):
                transport.fetch_image('metron', 'https://static.metron.cloud/media/issue/a.jpg')
            self.assertEqual(factory.call_count, 1)

    def test_private_resolution_rejected(self):
        factory = Mock()
        transport = ArtworkHTTP(resolver=lambda _: ['127.0.0.1'], connection=factory)
        self.addCleanup(transport.dns.shutdown)
        with self.assertRaises(ValueError):
            transport.fetch_image('metron', 'https://static.metron.cloud/media/issue/a.jpg')
        factory.assert_not_called()
