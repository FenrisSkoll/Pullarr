"""Transactional local sequences. Read projections never acquire metadata."""

import json
from collections import defaultdict, deque
from contextlib import contextmanager
from time import time

from backend.base.collections import integer, text
from backend.base.reading_orders import (MAX_ENTRIES, MAX_ORDERS,
                                         ReadingOrderError, digest, entry,
                                         export_cbl, page_bounds, subject_key)
from backend.internals.collections import rows
from backend.internals.wanted import EXTERNAL_HOLD


@contextmanager
def transaction(cursor):
    own = not cursor.connection.in_transaction
    cursor.execute('BEGIN IMMEDIATE' if own else 'SAVEPOINT reading_order_edit')
    try:
        yield
        if own:
            cursor.connection.commit()
        else:
            cursor.execute('RELEASE reading_order_edit')
    except BaseException:
        if own:
            cursor.connection.rollback()
        else:
            cursor.execute('ROLLBACK TO reading_order_edit')
            cursor.execute('RELEASE reading_order_edit')
        raise


# Exact historical references are consulted without persisting derived ownership.
PROJECTION = '''WITH matches AS (
 SELECT e.id,COUNT(DISTINCT x.issue_id) match_count,MIN(x.issue_id) exact_id
 FROM reading_order_entries e LEFT JOIN reading_order_entry_refs r ON r.entry_id=e.id
 LEFT JOIN issue_external_ids x ON x.provider=r.provider AND x.provider_id=r.issue_ref
 WHERE e.order_id IN (SELECT value FROM json_each(?)) GROUP BY e.id
), linked AS (
 SELECT e.*,COALESCE(e.issue_id,CASE WHEN m.match_count=1 THEN m.exact_id END) canonical_id,
 m.match_count FROM reading_order_entries e JOIN matches m ON m.id=e.id
), projection AS (
 SELECT e.*,i.volume_id,i.issue_number,v.title local_series,v.year local_year,v.publisher local_publisher,
 i.monitored issue_monitored,v.monitored volume_monitored,
 EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id AND f.role='direct') exact_owned,
 EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id AND f.role='collected') content_elsewhere,
 EXISTS(SELECT 1 FROM wanted_reservations r WHERE r.issue_id=i.id AND r.active=1) acquisition_active,
 EXISTS(SELECT 1 FROM wanted_searches s,json_each(s.issue_ids) member WHERE s.state='searching' AND member.value=i.id) searching,
 NOT(1 __EXTERNAL_HOLD__) existing_acquisition,
 CASE WHEN i.id IS NOT NULL THEN CASE WHEN EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id AND f.role='direct')
 THEN 'owned' ELSE 'missing' END
 WHEN e.match_count>1 OR e.match_kind='ambiguous' THEN 'ambiguous'
 WHEN EXISTS(SELECT 1 FROM reading_order_entry_refs r WHERE r.entry_id=e.id) THEN 'external' ELSE 'unresolved' END status
 FROM linked e LEFT JOIN issues i ON i.id=e.canonical_id LEFT JOIN volumes v ON v.id=i.volume_id
) '''.replace('__EXTERNAL_HOLD__', EXTERNAL_HOLD)


class ReadingOrderStore:
    def __init__(self, cursor, clock=time):
        self.db, self.clock = cursor, clock

    def get(self, identifier):
        integer(identifier)
        values = rows(self.db, 'SELECT * FROM reading_orders WHERE id=?', (identifier,))
        if not values:
            raise ReadingOrderError('not_found')
        value = values[0]
        source = rows(self.db, '''SELECT id,kind,locator,enabled,revision,checked_at,success_at,error,
            pending_digest,etag IS NOT NULL has_etag,last_modified IS NOT NULL has_modified
            FROM reading_order_sources WHERE order_id=?''', (identifier,))
        value['source'] = source[0] if source else None
        return value

    def guard(self, identifier, revision, structural=True):
        integer(revision, 0)
        value = self.get(identifier)
        if value['revision'] != revision:
            raise ReadingOrderError('revision_conflict')
        if structural and value['source'] and value['source']['enabled']:
            raise ReadingOrderError('detach_required')
        return value

    def bump(self, identifier):
        self.db.execute('UPDATE reading_orders SET revision=revision+1 WHERE id=?', (identifier,))

    def create(self, title, description=''):
        title, description = text(title, 500), text(description, 8000, True)
        with transaction(self.db):
            if self.db.execute('SELECT count(*) FROM reading_orders').fetchone()[0] >= MAX_ORDERS:
                raise ReadingOrderError('bounded')
            self.db.execute('INSERT INTO reading_orders(title,description,created_at) VALUES(?,?,?)', (title, description, self.clock()))
            identifier = self.db.lastrowid
        return self.get(identifier)

    def edit(self, identifier, revision, title, description):
        with transaction(self.db):
            self.guard(identifier, revision)
            self.db.execute('UPDATE reading_orders SET title=?,description=?,revision=revision+1 WHERE id=?',
                (text(title, 500), text(description, 8000, True), identifier))
        return self.get(identifier)

    def delete(self, identifier, revision, confirmed):
        if confirmed is not True:
            raise ReadingOrderError('confirmation_required')
        with transaction(self.db):
            self.guard(identifier, revision, False)
            self.db.execute('DELETE FROM reading_orders WHERE id=?', (identifier,))
        return dict(deleted=True)

    def page(self, offset=0, limit=50):
        offset, limit = page_bounds(offset, limit)
        result = rows(self.db, '''SELECT o.*,s.kind source_kind,s.success_at,s.error source_error
            FROM reading_orders o LEFT JOIN reading_order_sources s ON s.order_id=o.id
            ORDER BY o.id DESC LIMIT ? OFFSET ?''', (limit, offset))
        counts = rows(self.db, PROJECTION + '''SELECT order_id,status,count(*) count FROM projection GROUP BY order_id,status''',
            (json.dumps([r['id'] for r in result]),))
        for value in result:
            value['counts'] = {r['status']: r['count'] for r in counts if r['order_id'] == value['id']}
            value['entry_count'] = sum(value['counts'].values())
        total = self.db.execute('SELECT count(*) FROM reading_orders').fetchone()[0]
        return dict(items=result, total=total, offset=offset, has_next=offset+limit<total)

    def entries(self, identifier, offset=0, limit=50, status='all'):
        order = self.get(identifier)
        offset, limit = page_bounds(offset, limit)
        if status not in ('all', 'owned', 'missing', 'external', 'ambiguous', 'unresolved', 'content'):
            raise ReadingOrderError('invalid_request')
        predicate = '1' if status == 'all' else 'content_elsewhere=1' if status == 'content' else 'status=?'
        args = (json.dumps([identifier]),) + (() if status in ('all', 'content') else (status,))
        total = self.db.execute(PROJECTION + 'SELECT count(*) FROM projection WHERE ' + predicate, args).fetchone()[0]
        result = rows(self.db, PROJECTION + 'SELECT * FROM projection WHERE ' + predicate + ' ORDER BY position LIMIT ? OFFSET ?', (*args, limit, offset))
        refs = rows(self.db, '''SELECT * FROM reading_order_entry_refs WHERE entry_id IN (SELECT value FROM json_each(?))
            ORDER BY entry_id,provider,issue_ref''', (json.dumps([r['id'] for r in result]),))
        for value in result:
            original = json.loads(value.pop('source'))
            value['source_fields'] = original
            value['provenance'] = json.loads(value['provenance'])
            value['series'] = value.pop('local_series') or original['series']
            value['number'] = value['issue_number'] if value['canonical_id'] else original['number']
            value['year'] = value.pop('local_year') or original['year']
            value['refs'] = [dict(provider=r['provider'], issue_id=r['issue_ref'], volume_id=r['volume_ref']) for r in refs if r['entry_id'] == value['id']]
            value['wanted'] = bool(value['canonical_id'] and value['volume_monitored'] and value['issue_monitored'] and not value['exact_owned'] and not value['content_elsewhere'])
        return dict(order=order, items=result, total=total, offset=offset, has_next=offset+limit<total)

    def local_issues(self, query='', offset=0, limit=50):
        query = text(query, 200, True)
        offset, limit = page_bounds(offset, limit)
        result = rows(self.db, '''SELECT i.id,i.volume_id,i.issue_number,v.title,v.year FROM issues i
            JOIN volumes v ON v.id=i.volume_id WHERE instr(lower(v.title||' '||i.issue_number),lower(?))>0
            ORDER BY v.title,v.id,i.id LIMIT ? OFFSET ?''', (query, limit+1, offset))
        return dict(items=result[:limit], offset=offset, has_next=len(result)>limit)

    def match(self, model):
        books = model['entries']
        if not 1 <= len(books) <= MAX_ENTRIES:
            raise ReadingOrderError('bounded')
        encoded = json.dumps(books)
        exact = rows(self.db, '''SELECT DISTINCT b.key position,x.issue_id FROM json_each(?) b
            JOIN json_each(b.value,'$.refs') r JOIN issue_external_ids x
            ON x.provider=json_extract(r.value,'$.provider') AND x.provider_id=json_extract(r.value,'$.issue_id')''', (encoded,))
        candidates = rows(self.db, '''SELECT b.key position,i.id issue_id,v.title,i.issue_number,v.year
            FROM json_each(?) b JOIN volumes v ON lower(v.title)=lower(json_extract(b.value,'$.series'))
            JOIN issues i ON i.volume_id=v.id AND i.issue_number=json_extract(b.value,'$.number')
            WHERE (json_extract(b.value,'$.volume')='' OR CAST(v.year AS TEXT)=json_extract(b.value,'$.volume'))
            LIMIT ?''', (encoded, MAX_ENTRIES*20+1))
        if len(candidates) > MAX_ENTRIES*20:
            raise ReadingOrderError('bounded')
        found, fallback = defaultdict(list), defaultdict(list)
        for value in exact:
            found[value['position']].append(value['issue_id'])
        for value in candidates:
            fallback[value['position']].append(value)
        result = []
        occurrences = defaultdict(int)
        for position, book in enumerate(books):
            ids = sorted(set(found[position]))
            key = digest(book)
            occurrences[key] += 1
            # A textual single candidate still requires explicit resolution.
            state = 'exact' if len(ids)==1 else 'ambiguous' if len(ids)>1 or fallback[position] else 'external' if book['refs'] else 'unresolved'
            result.append(dict(position=position, source=book, issue_id=ids[0] if len(ids)==1 else None,
                match=state, candidates=([dict(issue_id=i) for i in ids] if len(ids)>1 else fallback[position])[:20],
                repeated=occurrences[key]>1))
        return result

    def snapshot_issue(self, issue_id):
        integer(issue_id)
        value = rows(self.db, '''SELECT i.issue_number,v.title,v.year,v.publisher FROM issues i
            JOIN volumes v ON v.id=i.volume_id WHERE i.id=?''', (issue_id,))
        if not value:
            raise ReadingOrderError('not_found')
        refs = rows(self.db, '''SELECT x.provider,x.provider_id issue_id,
            (SELECT CASE WHEN COUNT(DISTINCT v.provider_id)=1 THEN MIN(v.provider_id) END FROM volume_external_ids v JOIN issues i ON i.volume_id=v.volume_id
             WHERE i.id=x.issue_id AND v.provider=x.provider) volume_id
            FROM issue_external_ids x WHERE x.issue_id=?''', (issue_id,))
        v = value[0]
        return entry(v['title'], v['issue_number'] or '', str(v['year'] or ''), str(v['year'] or ''), v['publisher'] or '', refs)

    def insert_entry(self, identifier, position, source, issue_id, match_kind, provenance):
        self.db.execute('''INSERT INTO reading_order_entries(order_id,position,source,issue_id,match_kind,provenance)
            VALUES(?,?,?,?,?,?)''', (identifier, position, json.dumps(source), issue_id, match_kind, json.dumps(provenance)))
        eid = self.db.lastrowid
        self.db.executemany('INSERT INTO reading_order_entry_refs VALUES(?,?,?,?)',
            ((eid, r['provider'], r['issue_id'], r.get('volume_id')) for r in source['refs']))
        return eid

    def add_local(self, identifier, revision, issue_id):
        with transaction(self.db):
            self.guard(identifier, revision)
            count = self.db.execute('SELECT count(*) FROM reading_order_entries WHERE order_id=?', (identifier,)).fetchone()[0]
            if count >= MAX_ENTRIES:
                raise ReadingOrderError('bounded')
            source = self.snapshot_issue(issue_id)
            self.insert_entry(identifier, count, source, issue_id, 'manual', dict(kind='manual', version=1, accepted_at=self.clock()))
            self.bump(identifier)
        return self.get(identifier)

    def move(self, identifier, revision, entry_id, position):
        integer(entry_id)
        integer(position, 0, MAX_ENTRIES-1)
        with transaction(self.db):
            self.guard(identifier, revision)
            ids = [r[0] for r in self.db.execute('SELECT id FROM reading_order_entries WHERE order_id=? ORDER BY position', (identifier,))]
            if entry_id not in ids or position >= len(ids):
                raise ReadingOrderError('invalid_request')
            ids.remove(entry_id)
            ids.insert(position, entry_id)
            self.reposition(identifier, ids)
            self.bump(identifier)
        return self.get(identifier)

    def reposition(self, identifier, ids):
        self.db.execute('UPDATE reading_order_entries SET position=position+? WHERE order_id=?', (MAX_ENTRIES+1, identifier))
        self.db.executemany('UPDATE reading_order_entries SET position=? WHERE id=?', enumerate(ids))

    def remove(self, identifier, revision, entry_id):
        integer(entry_id)
        with transaction(self.db):
            self.guard(identifier, revision)
            self.db.execute('DELETE FROM reading_order_entries WHERE order_id=? AND id=?', (identifier, entry_id))
            if not self.db.rowcount:
                raise ReadingOrderError('not_found')
            ids = [r[0] for r in self.db.execute('SELECT id FROM reading_order_entries WHERE order_id=? ORDER BY position', (identifier,))]
            self.reposition(identifier, ids)
            self.bump(identifier)
        return self.get(identifier)

    def resolve(self, identifier, revision, entry_id, issue_id):
        with transaction(self.db):
            self.guard(identifier, revision)
            evidence = self.snapshot_issue(issue_id)
            self.db.execute("UPDATE reading_order_entries SET issue_id=?,match_kind='manual' WHERE order_id=? AND id=?", (issue_id, identifier, integer(entry_id)))
            if not self.db.rowcount:
                raise ReadingOrderError('not_found')
            self.db.executemany('INSERT OR IGNORE INTO reading_order_entry_refs VALUES(?,?,?,?)',
                ((entry_id, r['provider'], r['issue_id'], r['volume_id']) for r in evidence['refs']))
            self.bump(identifier)
        return self.get(identifier)

    def accept_model(self, model, resolved, provenance, identifier=None, revision=None):
        with transaction(self.db):
            if identifier is None:
                identifier = self.create(model['title'], model['description'])['id']
            else:
                self.guard(identifier, revision, False)
            # Occurrence queues preserve stable entry IDs across source moves,
            # including intentionally repeated references.
            old = defaultdict(deque)
            for row in rows(self.db, 'SELECT id,source FROM reading_order_entries WHERE order_id=? ORDER BY position', (identifier,)):
                old[subject_key(json.loads(row['source']))].append(row['id'])
            self.db.execute('UPDATE reading_order_entries SET position=position+? WHERE order_id=?', (MAX_ENTRIES+1, identifier))
            retained = []
            for position, item in enumerate(resolved):
                source = item['source']
                kind = item['match'] if item['match'] in ('exact', 'manual', 'ambiguous') else 'unresolved'
                key = subject_key(source)
                if old[key]:
                    eid = old[key].popleft()
                    self.db.execute('UPDATE reading_order_entries SET position=?,source=? WHERE id=?', (position, json.dumps(source), eid))
                else:
                    eid = self.insert_entry(identifier, position, source, item['issue_id'], kind, provenance)
                if kind == 'manual' and item['issue_id']:
                    extra = self.snapshot_issue(item['issue_id'])
                    self.db.executemany('INSERT OR IGNORE INTO reading_order_entry_refs VALUES(?,?,?,?)',
                        ((eid, r['provider'], r['issue_id'], r['volume_id']) for r in extra['refs']))
                retained.append(eid)
            self.db.execute('DELETE FROM reading_order_entries WHERE order_id=? AND id NOT IN (SELECT value FROM json_each(?))', (identifier, json.dumps(retained)))
            self.db.execute('UPDATE reading_orders SET title=?,description=?,revision=revision+1 WHERE id=?', (model['title'], model['description'], identifier))
        return self.get(identifier)

    def export(self, identifier):
        model = self.get(identifier)
        books = []
        for offset in range(0, MAX_ENTRIES, 100):
            page = self.entries(identifier, offset, 100)
            for item in page['items']:
                source = item['source_fields']
                source.update(series=item['series'], number=item['number'], refs=item['refs'])
                books.append(source)
            if not page['has_next']:
                break
        return export_cbl(dict(title=model['title'], description=model['description'], entries=books))

    def attach(self, identifier, revision, kind, locator):
        if kind not in ('cbl_url', 'metron_list'):
            raise ReadingOrderError('unsupported_source')
        if kind == 'cbl_url':
            from backend.implementations.reading_order_sources import \
                normalize_url
            locator = normalize_url(locator)
        else:
            from backend.base.collections import reference
            reference('metron', locator)
        with transaction(self.db):
            self.guard(identifier, revision)
            if self.db.execute('SELECT 1 FROM reading_order_sources WHERE order_id=?', (identifier,)).fetchone():
                raise ReadingOrderError('source_exists')
            order = self.get(identifier)
            accepted = dict(title=order['title'], description=order['description'], entries=[json.loads(r[0]) for r in self.db.execute(
                'SELECT source FROM reading_order_entries WHERE order_id=? ORDER BY position', (identifier,))], warnings=[])
            self.db.execute('INSERT INTO reading_order_sources(order_id,kind,locator,accepted) VALUES(?,?,?,?)', (identifier, kind, locator, json.dumps(accepted)))
            self.bump(identifier)
        return self.get(identifier)

    def detach(self, identifier, revision, confirmed):
        if confirmed is not True:
            raise ReadingOrderError('confirmation_required')
        with transaction(self.db):
            self.guard(identifier, revision, False)
            self.db.execute('UPDATE reading_order_sources SET enabled=0,pending=NULL,pending_digest=NULL,revision=revision+1 WHERE order_id=?', (identifier,))
            self.bump(identifier)
        return self.get(identifier)

    def source(self, source_id):
        value = rows(self.db, 'SELECT * FROM reading_order_sources WHERE id=?', (integer(source_id),))
        if not value:
            raise ReadingOrderError('not_found')
        return value[0]

    def observe(self, source_id, expected_revision, acquired):
        with transaction(self.db):
            source = self.source(source_id)
            if not source['enabled'] or source['revision'] != expected_revision:
                raise ReadingOrderError('revision_conflict')
            self.db.execute('UPDATE reading_order_sources SET checked_at=?,success_at=?,error=NULL WHERE id=?', (self.clock(), self.clock(), source_id))
            if acquired.get('unchanged') or acquired['digest'] in (source['digest'], source['pending_digest'], source['ignored_digest']):
                return dict(unchanged=True, source_id=source_id)
            self.db.execute('''UPDATE reading_order_sources SET pending=?,pending_digest=?,etag=?,last_modified=?,revision=revision+1 WHERE id=?''',
                (json.dumps(acquired['model']), acquired['digest'], acquired.get('etag'), acquired.get('last_modified'), source_id))
        return dict(unchanged=False, source_id=source_id)

    def pending(self, source_id, offset=0, limit=50):
        source = self.source(source_id)
        # A complete replacement has N added + N removed occurrences. Diff
        # pagination must admit both halves, not just the order's N-row bound.
        offset, limit = integer(offset, 0, MAX_ENTRIES*2), integer(limit, 1, 100)
        if source['pending'] is None:
            return dict(source_id=source_id, revision=source['revision'], pending=False, items=[])
        model = json.loads(source['pending'])
        old = json.loads(source['accepted']) if source['accepted'] else dict(entries=[])
        previous = defaultdict(deque)
        for index, book in enumerate(old['entries']):
            previous[subject_key(book)].append(index)
        changes = []
        matched = self.match(model)
        for item in matched:
            key = subject_key(item['source'])
            before = previous[key].popleft() if previous[key] else None
            item.update(change='added' if before is None else 'moved' if before != item['position'] else 'unchanged', previous_position=before)
            item['match_changed'] = bool(before is not None and old.get('matches') and
                old['matches'][before] != dict(issue_id=item['issue_id'], match=item['match']))
            item['source_fields_changed'] = before is not None and old['entries'][before] != item['source']
            changes.append(item)
        for positions in previous.values():
            for pos in positions:
                changes.append(dict(change='removed', previous_position=pos, source=old['entries'][pos], match='historical'))
        order_revision = self.get(source['order_id'])['revision']
        confirmation = digest(dict(source_digest=source['pending_digest'], order_revision=order_revision,
            matches=[dict(issue_id=i['issue_id'], match=i['match']) for i in matched]))
        return dict(source_id=source_id, revision=source['revision'], order_revision=order_revision,
            pending=True, digest=confirmation, title=model['title'], description=model['description'],
            metadata_changed=any(model.get(k) != old.get(k) for k in ('title', 'description')),
            total=len(changes), items=changes[offset:offset+limit], offset=offset, has_next=offset+limit<len(changes))

    def decide_source(self, source_id, revision, order_revision, expected_digest, decision, confirmed):
        if confirmed is not True or decision not in ('accept', 'reject'):
            raise ReadingOrderError('confirmation_required')
        with transaction(self.db):
            source = self.source(source_id)
            self.guard(source['order_id'], order_revision, False)
            if not source['enabled'] or source['revision'] != revision or not source['pending'] or self.pending(source_id)['digest'] != expected_digest:
                raise ReadingOrderError('revision_conflict')
            if decision == 'accept':
                model = json.loads(source['pending'])
                matched = self.match(model)
                self.accept_model(model, matched, dict(kind=source['kind'], version=1, source_id=source_id,
                    digest=source['pending_digest'], accepted_at=self.clock()), source['order_id'], order_revision)
                model['matches'] = [dict(issue_id=i['issue_id'], match=i['match']) for i in matched]
                self.db.execute('UPDATE reading_order_sources SET accepted=?,digest=pending_digest WHERE id=?', (json.dumps(model), source_id))
            else:
                self.db.execute('UPDATE reading_order_sources SET ignored_digest=pending_digest WHERE id=?', (source_id,))
            self.db.execute('UPDATE reading_order_sources SET pending=NULL,pending_digest=NULL,revision=revision+1 WHERE id=?', (source_id,))
        return self.get(source['order_id'])
