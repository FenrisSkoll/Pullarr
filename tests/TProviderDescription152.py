"""Shared bounded plain text for metadata-qualified library/volume/issue DTOs."""
from unittest import TestCase
from unittest.mock import patch

from backend.implementations.metadata.search_presentation import \
    description_text
from frontend.metadata import volume_identity_results


class ProviderDescriptionTests(TestCase):
    def test_readable_paragraph_link_break_and_emphasis(self):
        self.assertEqual(description_text('<p>Trade paperback <a href="https://example.org/">Black Road</a>.</p><p><strong>Second</strong><br><em>line</em></p>'),
                         'Trade paperback Black Road.\nSecond\nline')

    def test_active_content_and_uri_are_never_rendered(self):
        value=description_text('<p>Safe</p><script>bad()</script><a href="javascript:bad()">Link</a>')
        self.assertNotIn('bad()',value)
        self.assertNotIn('<',value)
        self.assertIn('Link',value)

    def test_plain_and_malformed(self):
        self.assertEqual(description_text('Plain text'),'Plain text')
        self.assertEqual(description_text('<p>First<p>Second<br>Third'),'First\nSecond\nThird')

    def test_long_description_bounded(self):
        self.assertLessEqual(len(description_text('<p>Paragraph</p>'*10000)),16000)

    def test_library_and_volume_field_preserves_stored_description(self):
        raw='<p>Safe <a href="data:text/html,bad">link</a></p>'
        row=dict(id=1,description=raw)
        with patch('frontend.metadata.get_db') as db:
            db.return_value.execute.return_value=[(1,'comicvine','comicvine','101')]
            result=volume_identity_results([row],True)[0]
        self.assertEqual(result['description_text'],'Safe link')
        self.assertEqual(result['description'],raw)
        self.assertEqual(row,dict(id=1,description=raw))
