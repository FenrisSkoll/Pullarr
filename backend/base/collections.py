"""Local publication organization; never metadata authority or content ownership."""

from re import fullmatch

POLICY = 'collection-membership/v1'
KINDS = ('unknown', 'series', 'limited_series', 'trade_paperback', 'hardcover',
         'deluxe', 'omnibus', 'absolute', 'one_shot', 'anthology', 'compendium', 'other')
MONITORING = ('inherit', 'monitored', 'unmonitored')
MAX_COLLECTIONS = 100
MAX_NODES = 256
MAX_DEPTH = 8
MAX_PUBLICATIONS = 2000
MAX_CATALOG = 20000
MAX_SUGGESTIONS = 2000
MAX_REFS = 8


class CollectionError(ValueError):
    """Controlled reason codes only, never provider/SQL exception text."""


def integer(value, minimum=1, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CollectionError('invalid_request')
    return value


def text(value, maximum=200, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or '\x00' in value
            or not empty and not value.strip()):
        raise CollectionError('invalid_request')
    return value.strip()


def choice(value, values):
    if not isinstance(value, str) or value not in values:
        raise CollectionError('invalid_request')
    return value


def reference(provider, identity):
    # Current three registered providers use positive decimal publication IDs.
    choice(provider, ('comicvine', 'metron', 'gcd'))
    if not isinstance(identity, str) or not fullmatch(r'[1-9][0-9]{0,18}', identity) or int(identity) >= 2**63:
        raise CollectionError('invalid_reference')
    return provider, identity
