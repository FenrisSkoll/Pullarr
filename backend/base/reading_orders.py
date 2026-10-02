"""Bounded semantic Reading Order contracts, independent of acquisition."""

import json
from hashlib import sha256
from xml.etree.ElementTree import Element, SubElement, indent, tostring
from xml.parsers import expat

from backend.base.collections import CollectionError, integer, reference, text
from backend.base.issue_facts import IssueNumberFacts

MAX_ENTRIES = 2000
MAX_ORDERS = 200
MAX_BYTES = 2 * 1024 * 1024
MAX_WARNINGS = 100
PROVIDERS = {'cv': 'comicvine', 'comicvine': 'comicvine', 'metron': 'metron', 'gcd': 'gcd'}


class ReadingOrderError(CollectionError):
    """Controlled reason, never source XML, URLs or exception representations."""


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def subject_key(value):
    """Identity ignores display changes; occurrence queues preserve repeats."""
    return digest(sorted((r['provider'], r['issue_id']) for r in value['refs'])) if value['refs'] else digest(value)


def entry(series='', number='', year='', volume='', publisher='', refs=None, note=''):
    values = dict(series=text(series, 500, True), number=text(number, 100, True),
        year=text(year, 20, True), volume=text(volume, 40, True), publisher=text(publisher, 200, True),
        note=text(note, 1000, True))
    values['number_kind'] = IssueNumberFacts.interpret(values['number'], 'reading_order_source', 'Number').interpretation.value
    safe = []
    for ref in refs or []:
        provider, identity = reference(ref['provider'], ref['issue_id'])
        volume_id = ref.get('volume_id')
        if volume_id is not None:
            reference(provider, volume_id)
        value = dict(provider=provider, issue_id=identity, volume_id=volume_id)
        if value not in safe:
            safe.append(value)
    if len(safe) > 8:
        raise ReadingOrderError('bounded')
    values['refs'] = safe
    return values


def parse_cbl(raw):
    """Expat with rejecting DTD/entity handlers and limits applied during parsing."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ReadingOrderError('bounded')
    stack, books, warnings = [], [], set()
    model = dict(title='', description='', entries=books, warnings=[])
    parser = expat.ParserCreate()
    count = 0
    current = None

    def forbidden(*args):
        raise ReadingOrderError('unsafe_xml')

    def start(name, attrs):
        nonlocal count, current
        count += 1
        if count > MAX_ENTRIES * 12 + 100 or len(stack) >= 8 or len(attrs) > 20:
            raise ReadingOrderError('bounded')
        if any(len(k) > 100 or len(v) > 2000 for k, v in attrs.items()):
            raise ReadingOrderError('bounded')
        path = [v[0] for v in stack]
        if not stack and name != 'ReadingList':
            raise ReadingOrderError('unsupported_cbl')
        if ':' in name or name in ('include', 'fallback'):
            raise ReadingOrderError('unsupported_cbl')
        if name == 'Book':
            if path != ['ReadingList', 'Books'] or current is not None:
                raise ReadingOrderError('unsupported_cbl')
            if len(books) >= MAX_ENTRIES:
                raise ReadingOrderError('bounded')
            current = entry(*(attrs.get(k, '') for k in ('Series', 'Number', 'Year', 'Volume', 'Publisher')))
            if set(attrs) - {'Series', 'Number', 'Year', 'Volume', 'Publisher'}:
                warnings.add('unsupported_book_attributes')
        elif name == 'Database' and path == ['ReadingList', 'Books', 'Book']:
            provider = PROVIDERS.get(attrs.get('Name', '').casefold())
            if provider and attrs.get('Issue'):
                try:
                    ref = dict(provider=provider, issue_id=attrs['Issue'], volume_id=attrs.get('Series') or None)
                    assert current is not None
                    current = entry(**{k: current[k] for k in ('series', 'number', 'year', 'volume', 'publisher', 'note')}, refs=current['refs'] + [ref])
                except CollectionError as error:
                    if str(error) == 'bounded':
                        raise
                    warnings.add('invalid_database_reference')
            else:
                warnings.add('unsupported_database_reference')
        elif name not in ('ReadingList', 'Name', 'Description', 'Books', 'NumIssues'):
            warnings.add('unsupported_source_fields')
        stack.append([name, ''])

    def chars(value):
        if stack:
            if stack[-1][0] in ('ReadingList', 'Books', 'Book', 'Database') and value.isspace():
                return
            stack[-1][1] += value
            if len(stack[-1][1]) > 8000:
                raise ReadingOrderError('bounded')

    def end(name):
        nonlocal current
        path = [v[0] for v in stack]
        value = stack.pop()[1]
        if path == ['ReadingList', 'Name']:
            model['title'] = text(value, 500, True)
        elif path == ['ReadingList', 'Description']:
            model['description'] = text(value, 8000, True)
        elif path == ['ReadingList', 'Books', 'Book']:
            books.append(current)
            current = None

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(raw, True)
    except (expat.ExpatError, UnicodeError, ValueError) as error:
        if isinstance(error, CollectionError):
            raise
        raise ReadingOrderError('invalid_xml') from None
    if not books:
        raise ReadingOrderError('unsupported_cbl')
    model['title'] = model['title'] or 'Imported Reading Order'
    model['warnings'] = sorted(warnings)[:MAX_WARNINGS]
    return model


def export_cbl(model):
    root = Element('ReadingList')
    SubElement(root, 'Name').text = model['title']
    SubElement(root, 'Description').text = model.get('description', '')
    SubElement(root, 'NumIssues').text = str(len(model['entries']))
    books = SubElement(root, 'Books')
    for item in model['entries']:
        book = SubElement(books, 'Book', {k.title(): str(item.get(k, '')) for k in ('series', 'number', 'volume', 'year', 'publisher') if item.get(k)})
        for ref in sorted(item['refs'], key=lambda r: (r['provider'], r['issue_id'])):
            attrs = dict(Name='cv' if ref['provider'] == 'comicvine' else ref['provider'], Issue=ref['issue_id'])
            if ref.get('volume_id'):
                attrs['Series'] = ref['volume_id']
            SubElement(book, 'Database', attrs)
    indent(root, space='  ')
    return tostring(root, encoding='utf-8', xml_declaration=True) + b'\n'


def page_bounds(offset=0, limit=50):
    return integer(offset, 0, MAX_ENTRIES), integer(limit, 1, 100)
