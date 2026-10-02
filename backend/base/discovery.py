"""Bounded discovery observations, never canonical identity or acquisition authority."""

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from hashlib import sha256
from urllib.parse import urlsplit, urlunsplit
from xml.etree.ElementTree import Element, SubElement
from xml.parsers import expat

from bs4 import BeautifulSoup

MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 500
MAX_POSTS = 25000
MAX_PAGES = 4
ORIGIN = 'https://getcomics.org'
FEED = ORIGIN + '/feed/'


class DiscoveryError(ValueError):
    def __init__(self, code, retry_after=0):
        super().__init__(code)
        self.retry_after = min(86400, max(0, retry_after))


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def integer(value, low=1, high=2147483647):
    if type(value) is not int or not low <= value <= high:
        raise DiscoveryError('invalid_request')
    return value


def text(value, maximum, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or '\x00' in value:
        raise DiscoveryError('bounded')
    return value.strip()


def source_url(value):
    """Only the audited source, no credentials, query, custom port or proxy origin."""
    value = text(value, 2048)
    try:
        p = urlsplit(value)
        if (p.scheme.lower() != 'https' or p.hostname not in ('getcomics.org', 'www.getcomics.org')
                or p.port not in (None, 443) or p.username is not None or p.password is not None
                or p.query or '\\' in value or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
            raise ValueError()
        return urlunsplit(('https', 'getcomics.org', p.path or '/', '', ''))
    except ValueError:
        raise DiscoveryError('blocked_destination') from None


def plain(value, maximum=2000):
    if len(value) > 32768:
        raise DiscoveryError('bounded')
    soup = BeautifulSoup(value, 'html.parser')
    for node in soup(['script', 'style', 'iframe', 'object', 'svg']):
        node.decompose()
    return ' '.join(soup.stripped_strings)[:maximum]


def hints(value):
    year = re.search(r'\bYear\s*:\s*([^|\n]{1,40})', value, re.I)
    size = re.search(r'\bSize\s*:\s*([^|\n]{1,100})', value, re.I)
    year_text = year.group(1).strip() if year else ''
    # Size is the labelled segment, not an arbitrary first number in the summary.
    size_text = size.group(1).strip() if size else ''
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)', size_text, re.I)
    size_bytes = int(Decimal(match.group(1)) * (1024 ** ('KB', 'MB', 'GB', 'TB').index(match.group(2).upper()) * 1024)) if match else None
    if size_bytes is not None and size_bytes > 10**15:
        size_bytes = None
    return year_text, size_text, size_bytes


def timestamp(value):
    if not value:
        return None, 'unavailable'
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return datetime.strptime(value, '%Y-%m-%d').date().isoformat(), 'day'
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if 'T' in value else parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None, 'unavailable'
        return parsed.astimezone(timezone.utc).isoformat(), 'instant'
    except (ValueError, TypeError, OverflowError):
        return None, 'unavailable'


def observation(title, url, *, guid=None, categories=(), published=None, updated=None, summary='', kind='rss'):
    title, url = text(title, 1000), source_url(url)
    cats = sorted(set(text(c, 100) for c in categories))
    if len(cats) > 20:
        raise DiscoveryError('bounded')
    # GUIDs are opaque identifiers, never fetched or exposed as external links.
    guid = text(guid, 1000) if guid else None
    published_at, precision = timestamp(published)
    updated_at, _ = timestamp(updated)
    cleaned = plain(summary)
    # Preserve the size label from its own paragraph, avoiding following prose.
    paragraphs = BeautifulSoup(summary, 'html.parser').find_all('p')
    label = next((p.get_text(' ', strip=True) for p in paragraphs if re.search(r'\bSize\s*:', p.get_text(' '), re.I)), cleaned)
    year, size, size_bytes = hints(label)
    nonrelease = any(c.casefold() in ('news', 'sponsored', 'announcements') for c in cats)
    trusted_category = any(c.casefold() in ('marvel comics', 'dc comics', 'other comics') for c in cats)
    return dict(title=title, url=url, guid=guid, categories=cats, published_at=published_at,
        published_precision=precision, source_updated_at=updated_at, year_text=year,
        size_text=size, size_bytes=size_bytes, summary=cleaned, source_kind=kind,
        release_kind='non_release' if nonrelease else 'release' if trusted_category else 'uncertain')


def safe_xml(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise DiscoveryError('oversized_response')
    parser = expat.ParserCreate(namespace_separator='}')
    stack, root = [], []
    count = 0
    def forbidden(*args):
        raise DiscoveryError('unsafe_xml')
    def start(name, attrs):
        nonlocal count
        count += 1
        if count > 15000 or len(stack) >= 24 or len(attrs) > 30 or len(name) > 300:
            raise DiscoveryError('bounded')
        if 'http://www.w3.org/2001/XInclude}' in name:
            forbidden()
        if '}' in name:
            name = '{' + name
        if any(len(k) > 300 or len(v) > 4096 for k, v in attrs.items()):
            raise DiscoveryError('bounded')
        node = SubElement(stack[-1], name, attrs) if stack else Element(name, attrs)
        if not stack:
            root.append(node)
        stack.append(node)
    def chars(value):
        if stack:
            node = stack[-1]
            node.text = (node.text or '') + value
            if len(node.text) > 32768:
                raise DiscoveryError('bounded')
    parser.StartElementHandler, parser.EndElementHandler = start, lambda name: stack.pop()
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(raw, True)
    except expat.ExpatError:
        raise DiscoveryError('invalid_feed') from None
    if len(root) != 1:
        raise DiscoveryError('invalid_feed')
    return root[0]


def parse_feed(raw):
    root = safe_xml(raw)
    atom = root.tag == '{http://www.w3.org/2005/Atom}feed'
    if not atom and (root.tag != 'rss' or root.find('channel') is None):
        raise DiscoveryError('invalid_feed')
    namespace = '{http://www.w3.org/2005/Atom}' if atom else ''
    items = root.findall(namespace + 'entry') if atom else root.findall('./channel/item')
    if len(items) > MAX_ITEMS:
        raise DiscoveryError('bounded')
    result = []
    for item in items:
        def field(name):
            return item.findtext(namespace + name) or ''
        links = [n.attrib.get('href', '') for n in item.findall(namespace + 'link') if n.attrib.get('rel', 'alternate') == 'alternate'] if atom else [field('link')]
        if len(links) != 1:
            raise DiscoveryError('invalid_feed')
        categories = [n.attrib.get('term', '') if atom else (n.text or '') for n in item.findall(namespace + 'category')]
        result.append(observation(field('title'), links[0], guid=field('id' if atom else 'guid'),
            categories=categories, published=field('published' if atom else 'pubDate'), updated=field('updated') if atom else None,
            summary=field('summary' if atom else 'description'), kind='atom' if atom else 'rss'))
    return result


def parse_listing(raw):
    if len(raw) > MAX_BYTES:
        raise DiscoveryError('oversized_response')
    soup = BeautifulSoup(raw, 'html.parser')
    cards = soup.select('article.post')
    if not cards:
        raise DiscoveryError('parser_contract_changed')
    if len(cards) > MAX_ITEMS:
        raise DiscoveryError('bounded')
    result = []
    for card in cards:
        title = card.select_one('h1.post-title a')
        cats = card.select('.post-category')
        if title is None or not cats:
            raise DiscoveryError('parser_contract_changed')
        date = card.find('time')
        excerpt = card.select_one('.post-excerpt')
        result.append(observation(title.get_text(), title.get('href'), categories=[c.get_text() for c in cats],
            published=date.get('datetime') if date else None, summary=str(excerpt) if excerpt else '', kind='html'))
    return result
