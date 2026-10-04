"""Bounded search text and explicit publication evidence; never identity equality."""

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment

from backend.base.file_extraction import volume_regex

# Shared with ComicVine's established volume-number inference.
PREDECESSOR = r'(?:preceded by|continued from|continues from)'
SUCCESSOR = r'(?:continued in|continued as|continues as)'
PRECEDING = re.compile(PREDECESSOR + r' (?:<a[^>]*>)?(.*?)' + volume_regex.pattern, re.I)
CONTINUING = re.compile(SUCCESSOR + r' (?:<a[^>]*>)?(.*?)' + volume_regex.pattern, re.I)
MAX_DESCRIPTION = 100000


def description_text(value):
    if not value:
        return value
    soup = BeautifulSoup(value[:MAX_DESCRIPTION], 'html.parser')
    for tag in soup.find_all(['script', 'style', 'iframe', 'object', 'embed', 'svg', 'form', 'template']):
        tag.decompose()
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for tag in soup.find_all(['p', 'br', 'li', 'div', 'ul', 'ol']):
        tag.insert_before('\n')
        tag.insert_after('\n')
    return '\n'.join(' '.join(line.split()) for line in soup.get_text().splitlines() if line.strip())[:16000]


def comicvine_relations(identity, description):
    """Only immediately introduced links to exact ComicVine volume resources."""
    from backend.implementations.metadata.models import PublicationRelation
    soup = BeautifulSoup((description or '')[:MAX_DESCRIPTION], 'html.parser')
    for tag in soup.find_all(['script', 'style', 'iframe', 'object', 'embed', 'svg', 'template']):
        tag.decompose()
    result = []
    for link in soup.find_all('a', href=True):
        # A nearby title is insufficient: the phrase must introduce this link.
        prefix = ''
        for sibling in link.previous_siblings:
            prefix = (sibling.get_text(' ') if hasattr(sibling, 'get_text') else str(sibling)) + prefix
            if len(prefix) >= 120:
                break
        match = re.search(r'\b(' + PREDECESSOR + '|' + SUCCESSOR + r')\s*[:–-]?\s*$', prefix, re.I)
        if not match:
            continue
        href = link['href']
        if not isinstance(href, str) or len(href) > 2048 or any(ord(c) <= 32 for c in href) or '\\' in href or '..' in href:
            continue
        try:
            parsed = urlsplit(urljoin('https://comicvine.gamespot.com/', href))
            if (parsed.scheme not in ('http', 'https') or parsed.netloc not in ('comicvine.gamespot.com', 'www.comicvine.com', 'comicvine.com')
                    or parsed.query or parsed.fragment):
                continue
            target = re.fullmatch(r'/(?:[a-zA-Z0-9_-]+/)?4050-([1-9][0-9]{0,18})/?', parsed.path)
            if not target or target[1] == identity:
                continue
            kind = 'continues_from' if re.fullmatch(PREDECESSOR, match[1], re.I) else 'continues_as'
            relation = PublicationRelation('comicvine', identity, kind, 'comicvine', target[1],
                description_text(link.get_text(' '))[:500], 'comicvine:description:explicit-link')
            if relation not in result:
                result.append(relation)
            if len(result) == 2:
                break
        except ValueError:
            continue
    return result
