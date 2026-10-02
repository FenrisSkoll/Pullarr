"""Read-only, bounded local catalog capability. No HTTP, settings or app writes.

Pinned model/migration contract: gcd-django 3d9d4b5516ce2a439ed8ede26787b8db28685caf.
An operator supplies an authorized, complete catalog; schema validation cannot
certify its origin or establish that an intentionally truncated dump is complete.
"""

import json
import sqlite3
import stat
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from time import monotonic, time

from backend.base.reprint_graph import (CreatorEntity, CreatorName,
                                        GraphSnapshot, IssueReference,
                                        ReprintEdge, StoryCredit, StoryEntity)
from backend.implementations.metadata.errors import MetadataProviderError

SCHEMA_POLICY = 'kapowarr-gcd-catalog-schema/v1'
MAX_SEEDS = 10000
MAX_ROWS = 100000
MAX_TEXT = 8192
MAX_TEXT_BYTES = 64 * 1024 * 1024
BATCH = 400
# Fixed identifiers only. Extra columns are neither read nor copied.
COLUMNS = {
    'gcd_series': ('id', 'name', 'deleted'),
    'gcd_issue': ('id', 'series_id', 'number', 'title', 'deleted'),
    'gcd_story': ('id', 'issue_id', 'title', 'sequence_number', 'deleted'),
    'gcd_creator': ('id', 'gcd_official_name', 'deleted'),
    'gcd_creator_name_detail': ('id', 'creator_id', 'name', 'is_official_name', 'type_id', 'deleted'),
    'gcd_credit_type': ('id', 'name'),
    'gcd_story_credit': ('id', 'story_id', 'creator_id', 'credit_type_id', 'credited_as',
                         'signed_as', 'is_credited', 'is_signed', 'uncertain', 'deleted'),
    'gcd_reprint': ('id', 'origin_issue_id', 'target_issue_id', 'origin_id', 'target_id', 'notes', 'modified'),
}
RELATIONS = {
    ('gcd_issue', 'series_id'): 'gcd_series',
    ('gcd_story', 'issue_id'): 'gcd_issue',
    ('gcd_creator_name_detail', 'creator_id'): 'gcd_creator',
    ('gcd_story_credit', 'story_id'): 'gcd_story',
    ('gcd_story_credit', 'creator_id'): 'gcd_creator_name_detail',
    ('gcd_story_credit', 'credit_type_id'): 'gcd_credit_type',
    ('gcd_reprint', 'origin_issue_id'): 'gcd_issue',
    ('gcd_reprint', 'target_issue_id'): 'gcd_issue',
    ('gcd_reprint', 'origin_id'): 'gcd_story',
    ('gcd_reprint', 'target_id'): 'gcd_story',
}
INDEXED = (('gcd_reprint', 'origin_issue_id'), ('gcd_reprint', 'target_issue_id'),
           ('gcd_story', 'issue_id'), ('gcd_story_credit', 'story_id'))


class CatalogError(MetadataProviderError):
    def __init__(self, reason):
        # Codes, never SQLite messages, paths or untrusted row values.
        super().__init__('gcd', 'catalog_' + reason)


def identity(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise CatalogError('identity')
    return str(value)


def optional_id(value):
    return None if value is None else identity(value)


def flag(value):
    if type(value) is not int or value not in (0, 1):
        raise CatalogError('schema')
    return bool(value)


def text(value):
    if not isinstance(value, str) or len(value) > MAX_TEXT or '\x00' in value:
        raise CatalogError('text')
    return value


def file_observation(path):
    """Reject reparse/symlink components, non-files and SQLite sidecars.

    This v1 accepts a quiescent standalone dump, not a live WAL catalog. We do
    not use immutable=1 (which could silently ignore a real WAL).
    """
    path = Path(path)
    if not path.is_absolute() or str(path).startswith(('\\\\', '//')):
        raise CatalogError('path')
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise CatalogError('path')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or any(Path(str(path) + suffix).exists()
            for suffix in ('-wal', '-shm', '-journal')):
        raise CatalogError('file')
    with path.open('rb') as stream:
        if stream.read(16) != b'SQLite format 3\x00':
            raise CatalogError('file')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class Catalog:
    def __init__(self, path):
        self.path = Path(path)
        self.select_count = 0
        self.text_bytes = 0

    @contextmanager
    def opened(self):
        db = None
        try:
            before = file_observation(self.path)
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=1)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA mmap_size=0')
            deadline = monotonic() + 60
            db.set_progress_handler(lambda: int(monotonic() > deadline), 10000)
            db.execute('BEGIN')
            signature = self.validate(db)
            if file_observation(self.path) != before:
                raise CatalogError('changed')
            yield db, sha256(json.dumps((SCHEMA_POLICY, before, signature)).encode()).hexdigest()
            if file_observation(self.path) != before:
                raise CatalogError('changed')
        except (OSError, sqlite3.Error, ValueError):
            raise CatalogError('unavailable_or_invalid') from None
        finally:
            if db is not None:
                db.close()

    def validate(self, db):
        signature = []
        for table, columns in COLUMNS.items():
            row = db.execute('SELECT type FROM sqlite_master WHERE name=?', (table,)).fetchone()
            if row is None or row[0] != 'table':
                raise CatalogError('schema')
            ddl = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
            if 'VIRTUAL' in ddl.upper():
                raise CatalogError('schema')
            info = {r[1]: r for r in db.execute(f'PRAGMA table_info({table})')}
            if not set(columns) <= info.keys() or info['id'][5] != 1 or 'INT' not in info['id'][2].upper():
                raise CatalogError('schema')
            if sum(bool(r[5]) for r in info.values()) != 1:
                raise CatalogError('schema')
            signature.append((table, [(c, info[c][2], info[c][5]) for c in columns]))
            # Converted dumps may omit FK declarations. Selected rows are still
            # validated exactly. A contradictory declared relationship is fatal.
            for rel in db.execute(f'PRAGMA foreign_key_list({table})'):
                expected = RELATIONS.get((table, rel[3]))
                if expected and (rel[2] != expected or rel[4] != 'id'):
                    raise CatalogError('schema')
            indexed = set()
            for idx in db.execute(f'PRAGMA index_list({table})'):
                if idx[4]:
                    continue  # Partial index does not prove full endpoint lookup.
                # PRAGMA table-valued function permits a bound index name.
                first = db.execute('SELECT name FROM pragma_index_info(?) WHERE seqno=0', (idx[1],)).fetchone()
                if first:
                    indexed.add(first[0])
            if any(t == table and col not in indexed for t, col in INDEXED):
                raise CatalogError('index')
        return signature

    def test(self):
        with self.opened() as (_, fingerprint):
            result = dict(schema_policy=SCHEMA_POLICY, fingerprint=fingerprint, readable=True,
                          schema_supported=True)
        return result

    def rows(self, db, table, field, ids):
        if table not in COLUMNS or field not in COLUMNS[table]:
            raise ValueError('Unknown catalog query')
        result = {}
        values = sorted({int(i) for i in ids})
        for offset in range(0, len(values), BATCH):
            batch = values[offset:offset + BATCH]
            self.select_count += 1
            # Reject excessive text before Python materialization. IDs/flags are
            # still type-checked; substr is not used to silently truncate facts.
            # A rejected optional endpoint must NOT become NULL (which means a
            # legitimate issue-only edge). The bounded blob sentinel fails every
            # scalar validator without allocating the oversized source string.
            projection = ','.join(f'CASE WHEN typeof({col}) IN (\'text\',\'blob\') AND length({col})>{MAX_TEXT} THEN X\'00\' ELSE {col} END AS {col}'
                                  for col in COLUMNS[table])
            query = f'SELECT {projection} FROM {table} WHERE {field} IN ('
            query += ','.join('?' for _ in batch) + ') LIMIT ?'
            for row in db.execute(query, (*batch, MAX_ROWS + 1)):
                key = identity(row['id'])
                if key in result:
                    raise CatalogError('duplicate_identity')
                result[key] = row
                self.text_bytes += sum(len(v.encode('utf-8')) for v in row if isinstance(v, str))
                if len(result) > MAX_ROWS or self.text_bytes > MAX_TEXT_BYTES:
                    raise CatalogError('limit')
        return result

    def exact(self, db, table, ids):
        ids = set(ids)
        rows = self.rows(db, table, 'id', ids)
        if set(rows) != ids:
            raise CatalogError('missing_endpoint')
        return rows

    def acquire(self, seeds):
        """Seeds map exact provider issue ID to expected provider series ID."""
        if len(seeds) > MAX_SEEDS:
            raise CatalogError('limit')
        self.select_count = self.text_bytes = 0
        for key, parent in seeds.items():
            if identity(int(key)) != key or identity(int(parent)) != parent:
                raise CatalogError('identity')
        with self.opened() as (db, fingerprint):
            edges = self.rows(db, 'gcd_reprint', 'origin_issue_id', seeds)
            for key, row in self.rows(db, 'gcd_reprint', 'target_issue_id', seeds).items():
                if key in edges and tuple(edges[key]) != tuple(row):
                    raise CatalogError('duplicate_identity')
                edges[key] = row
            if len(edges) > MAX_ROWS:
                raise CatalogError('limit')
            issue_ids = set(seeds)
            story_ids = set()
            for r in edges.values():
                issue_ids.update((identity(r['origin_issue_id']), identity(r['target_issue_id'])))
                story_ids.update(identity(r[k]) for k in ('origin_id', 'target_id') if r[k] is not None)
            issues = self.exact(db, 'gcd_issue', issue_ids)
            for key, parent in seeds.items():
                if identity(issues[key]['series_id']) != parent:
                    raise CatalogError('series_mismatch')
            series = self.exact(db, 'gcd_series', {identity(r['series_id']) for r in issues.values()})
            stories = self.rows(db, 'gcd_story', 'issue_id', seeds)
            stories.update(self.exact(db, 'gcd_story', story_ids - stories.keys()))
            # No neighbor story inventory or graph recursion.
            credits = self.rows(db, 'gcd_story_credit', 'story_id', stories)
            names = self.exact(db, 'gcd_creator_name_detail', {identity(r['creator_id']) for r in credits.values()})
            creators = self.exact(db, 'gcd_creator', {identity(r['creator_id']) for r in names.values()})
            roles = self.exact(db, 'gcd_credit_type', {identity(r['credit_type_id']) for r in credits.values()})
            for r in stories.values():
                if type(r['sequence_number']) is not int:
                    raise CatalogError('schema')
            try:
                result = GraphSnapshot('gcd', SCHEMA_POLICY, fingerprint, time(), tuple(sorted(seeds)),
                    tuple(IssueReference(k, identity(r['series_id']), text(series[str(r['series_id'])]['name']),
                        text(r['number']), text(r['title']), flag(r['deleted']) or flag(series[str(r['series_id'])]['deleted']))
                        for k, r in sorted(issues.items())),
                    tuple(StoryEntity(k, identity(r['issue_id']), text(r['title']), r['sequence_number'], flag(r['deleted']))
                        for k, r in sorted(stories.items())),
                    tuple(CreatorEntity(k, text(r['gcd_official_name']), flag(r['deleted'])) for k, r in sorted(creators.items())),
                    tuple(CreatorName(k, identity(r['creator_id']), text(r['name']), flag(r['is_official_name']),
                        optional_id(r['type_id']), flag(r['deleted'])) for k, r in sorted(names.items())),
                    tuple(StoryCredit(k, identity(r['story_id']), identity(r['creator_id']), identity(r['credit_type_id']),
                        text(roles[str(r['credit_type_id'])]['name']), text(r['credited_as']), text(r['signed_as']),
                        flag(r['is_credited']), flag(r['is_signed']), flag(r['uncertain']), flag(r['deleted']))
                        for k, r in sorted(credits.items())),
                    tuple(ReprintEdge(k, identity(r['origin_issue_id']), identity(r['target_issue_id']),
                        optional_id(r['origin_id']), optional_id(r['target_id']), text(r['notes']), text(r['modified']))
                        for k, r in sorted(edges.items())), self.select_count)
            except ValueError:
                raise CatalogError('inconsistent_graph') from None
        return result
