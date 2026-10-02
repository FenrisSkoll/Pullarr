"""Precision-aware release observations; canonical issue facts remain authoritative."""

from calendar import monthrange
from datetime import date

from backend.base.issue_facts import BibliographicDate, DatePrecision

POLICY = 'kapowarr-release-calendar/v1'
MAX_EVENTS = 20000
MAX_SYNC_SUBJECTS = 50
MAX_SCOPE = 2000
MAX_EVIDENCE = 24
KINDS = {'on_sale': 0, 'store': 0, 'release': 0, 'publication': 1,
         'cover': 2, 'legacy_selected_unknown': 3, 'unknown_provider_date': 3}


class CalendarError(ValueError):
    """Controlled Calendar failure, never provider exception text."""


def integer(value, minimum=1, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CalendarError('invalid_request')
    return value


def day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise CalendarError('invalid_date')
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise CalendarError('invalid_date') from None
    if result.isoformat() != value:
        raise CalendarError('invalid_date')
    return result


def observation(fact: BibliographicDate, provider, provider_id, fetched_at, *, origin='provider'):
    precision = fact.precision.value
    known = fact.precision in (DatePrecision.DAY, DatePrecision.MONTH, DatePrecision.YEAR) and not fact.uncertainty
    value = fact.display if known else None
    return dict(provider=provider, provider_id=provider_id, source_field=fact.source_field,
        date=value, precision=precision if known else 'unknown', kind=fact.kind.value,
        provenance=fact.provenance, fetched_at=fetched_at, origin=origin, current=True,
        previous_date=None, previous_precision=None)


def bounds(value):
    """Internal range intersection only; never exposed as a fabricated day."""
    if not value:
        return None
    parts = [int(p) for p in value.split('-')]
    year = parts[0]
    month = parts[1] if len(parts) > 1 else 1
    first = date(year, month, parts[2] if len(parts) == 3 else 1)
    last = first if len(parts) == 3 else date(year, month, monthrange(year, month)[1]) if len(parts) == 2 else date(year, 12, 31)
    return first, last


def effective(evidence, preferred_provider=None):
    dated = [e for e in evidence if e['date']]
    pool = dated or evidence
    if not pool:
        return dict(date=None, precision='unknown', kind='unknown_provider_date',
            provider=None, current=False, origin='unavailable', fetched_at=None)
    return min(pool, key=lambda e: (not e['current'], KINDS.get(e['kind'], 4),
        e['provider'] != preferred_provider if preferred_provider else False,
        {'day': 0, 'month': 1, 'year': 2}.get(e['precision'], 3),
        e['origin'] != 'canonical', e['provider'], e['source_field'], e['provider_id']))
