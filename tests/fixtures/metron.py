"""Synthetic public shapes, checked against Metron serializers 2026-09-25.

No account data, API dumps or credentials. IDs intentionally describe fictional
records. See docs/development/metron-api-audit.md for official source URLs.
"""

from copy import deepcopy

SERIES = {
    'id': 700, 'name': 'Example Collection', 'sort_name': 'Example Collection',
    'alt_names': ['Example Alias'], 'language': 'en', 'volume': 2,
    'year_began': 2020, 'year_end': None, 'desc': 'A fictional collection.',
    'issue_count': 2, 'publisher': {'id': 3, 'name': 'Example Publisher'},
    'imprint': None, 'series_type': {'id': 2, 'name': 'Trade Paperback'},
    'cv_id': None, 'gcd_id': 800, 'modified': '2026-01-01T00:00:00Z',
    'resource_url': 'https://metron.cloud/series/example-collection-2020/',
}


def issue(identity=701, number='1', **changes):
    value = {
        'id': identity,
        'series': {'id': 700, 'name': 'Example Collection', 'volume': 2,
                   'year_began': 2020, 'language': 'en'},
        'number': number, 'alt_number': '', 'title': 'A meaningful collection',
        'name': ['First story', 'Second story'], 'desc': 'Issue description.',
        'cover_date': '2020-02-01', 'store_date': '2020-01-15',
        'image': '', 'cv_id': None, 'gcd_id': None,
        'isbn': '', 'upc': '', 'page': None,
        'modified': '2026-01-01T00:00:00Z',
        'resource_url': 'https://metron.cloud/issue/example/',
    }
    value.update(changes)
    return value


def issue_summary(value):
    return {key: deepcopy(value[key]) for key in (
        'id', 'series', 'number', 'cover_date', 'store_date', 'image', 'modified')}


def series_summary(value=None):
    value = deepcopy(SERIES if value is None else value)
    result = {key: value[key] for key in (
        'id', 'year_began', 'year_end', 'volume', 'issue_count',
        'publisher', 'series_type', 'cv_id', 'gcd_id', 'modified')}
    result['series'] = 'Example Collection (2020)'
    return result


def page(results, next_url=None, count=None):
    return {'count': len(results) if count is None else count,
            'next': next_url, 'previous': None, 'results': results}
