"""Pinned REST bibliography interpretation. No HTTP or identity inference."""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from backend.base.bibliography import (MAX_STORIES, MAX_TEXT, ROLES,
                                       CreditObservation, EditionFacts,
                                       IssueBibliography, PublicationFacts,
                                       StoryObservation)
from backend.implementations.metadata.gcd_client import GcdError


def optional_text(data, name, diagnostics, supplied):
    if name not in data:
        diagnostics.append(name + ':unavailable')
        return None
    value = data[name]
    if value is not None and (not isinstance(value, str) or len(value) > MAX_TEXT):
        diagnostics.append(name + ':invalid_or_oversized')
        return None
    supplied.append(name)
    return value


def numeric(raw):
    if raw is None or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', raw) or len(raw) > 32:
        return None
    try:
        value = Decimal(raw)
        if value == 0:
            return '0'
        return format(value, 'f').rstrip('0').rstrip('.') if '.' in raw else raw.lstrip('0') or '0'
    except InvalidOperation:
        return None


def isbn(raw):
    if not raw:
        return None, 'unknown'
    clean = re.sub(r'[- ]', '', raw).upper()
    if re.fullmatch(r'[0-9]{9}[0-9X]', clean):
        valid = sum((10 - i) * (10 if c == 'X' else int(c)) for i, c in enumerate(clean)) % 11 == 0
    elif re.fullmatch(r'97[89][0-9]{10}', clean):
        valid = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(clean)) % 10 == 0
    else:
        valid = False
    return (clean if valid else None), ('valid' if valid else 'invalid')


def safe_cover(raw):
    if raw in (None, ''):
        return True
    try:
        value = urlsplit(raw)
        return (value.scheme in ('http', 'https') and value.hostname == 'images.comics.org'
                and value.port is None and not value.username and not value.password
                and not value.query and not value.fragment
                and not any(ord(c) < 33 for c in raw)
                and value.path.startswith('/img/gcd/covers_by_id/'))
    except ValueError:
        return False


def publication(data):
    diagnostics, supplied = [], []
    values = {name: optional_text(data, name, diagnostics, supplied) for name in
              ('binding', 'publishing_format', 'color', 'dimensions', 'paper_stock')}
    return PublicationFacts(**values, supplied=tuple(supplied), diagnostics=tuple(diagnostics))


def bibliography(data):
    diagnostics, supplied = [], []
    names = ('isbn', 'barcode', 'page_count', 'variant_name', 'indicia_publisher',
             'indicia_printer', 'brand_emblem', 'rating', 'indicia_frequency', 'cover')
    values = {name: optional_text(data, name, diagnostics, supplied) for name in names}
    normalized, validity = isbn(values['isbn'])
    pages = numeric(values['page_count'])
    if values['page_count'] not in (None, '') and pages is None:
        diagnostics.append('page_count:unsupported_numeric')
    if not safe_cover(values['cover']):
        diagnostics.append('cover:unsafe_reference')
        supplied.remove('cover')
        values['cover'] = None
    mapping = {'brand_emblem': 'brand', 'cover': 'cover_reference'}
    edition = EditionFacts(**{mapping.get(k, k): v for k, v in values.items()},
        isbn_normalized=normalized, isbn_validity=validity, page_count_numeric=pages,
        supplied=tuple(mapping.get(k, k) for k in supplied))
    container = data.get('story_set')
    if not isinstance(container, list) or len(container) > MAX_STORIES:
        raise GcdError('bibliography_story_container')
    stories = []
    sequences = set()
    for position, row in enumerate(container):
        if not isinstance(row, dict):
            raise GcdError('bibliography_story_container')
        errors, present = [], []
        fields = {name: optional_text(row, name, errors, present) for name in
                  ('type', 'title', 'feature', 'page_count', 'characters', 'genre', *ROLES)}
        sequence = row.get('sequence_number')
        if type(sequence) is int and abs(sequence) <= 1000000:
            sequence = str(sequence)
        elif isinstance(sequence, str) and re.fullmatch(r'-?[0-9]{1,7}(?:\.[0-9]{1,3})?', sequence):
            pass
        else:
            sequence = None
            errors.append('sequence_number:unavailable_or_invalid')
        if sequence is not None:
            key = Decimal(sequence)
            if key in sequences:
                errors.append('sequence_number:duplicate')
            sequences.add(key)
        pages = numeric(fields['page_count'])
        if fields['page_count'] not in (None, '') and pages is None:
            errors.append('page_count:unsupported_numeric')
        stories.append(StoryObservation(position, sequence, fields['type'], fields['title'],
            fields['feature'], fields['page_count'], pages, fields['characters'], fields['genre'],
            tuple(CreditObservation(role, fields[role]) for role in ROLES if role in present)))
        diagnostics.extend(f'story[{position}].' + error for error in errors)
    stories.sort(key=lambda s: (s.sequence is None, Decimal(s.sequence or '0'), s.source_position))
    return IssueBibliography('gcd', edition, tuple(stories), tuple(diagnostics))
