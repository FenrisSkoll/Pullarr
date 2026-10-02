"""Transactional bounded observation cache. No remote or acquisition effects."""

import json
from time import time

from backend.base.discovery import (MAX_POSTS, DiscoveryError,
                                    canonical, digest, integer, text)
from backend.internals.collections import transaction


class DiscoveryStore:
    def __init__(self, cursor, clock=time):
        self.c, self.clock = cursor, clock

    def status(self):
        value = dict(self.c.execute("SELECT * FROM discovery_sources WHERE key='getcomics'").fetchone())
        value['receipt'] = json.loads(value['receipt'])
        value['validators'] = dict(etag=bool(value.pop('etag')), last_modified=bool(value.pop('last_modified')))
        value['retained'] = self.c.execute('SELECT COUNT(*) FROM discovery_posts').fetchone()[0]
        return value

    def settings(self, *, revision, enabled, automatic, interval_minutes):
        integer(revision)
        integer(interval_minutes, 30, 1440)
        if type(enabled) is not bool or type(automatic) is not bool or (automatic and not enabled):
            raise DiscoveryError('invalid_request')
        with transaction(self.c, write=True):
            self.c.execute('''UPDATE discovery_sources SET enabled=?,automatic=?,interval_minutes=?,revision=revision+1,
                next_poll=MAX(next_poll,?) WHERE key='getcomics' AND revision=?''',
                (enabled, automatic, interval_minutes, self.clock()+60, revision))
            if not self.c.rowcount:
                raise DiscoveryError('revision_conflict')
        return self.status()

    def post(self, identifier):
        integer(identifier)
        row = self.c.execute('SELECT * FROM discovery_posts WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise DiscoveryError('not_found')
        value = dict(row)
        value['categories'] = json.loads(value['categories'])
        return value

    def known(self, values):
        keys = canonical([v['url'] for v in values])
        guids = canonical([v['guid'] for v in values if v['guid']])
        return bool(self.c.execute('''SELECT 1 FROM discovery_posts WHERE url IN (SELECT value FROM json_each(?))
            OR guid IN (SELECT value FROM json_each(?)) LIMIT 1''', (keys, guids)).fetchone())

    def ingest(self, values):
        if len(values) > 2500:
            raise DiscoveryError('bounded')
        now, new, changed, unchanged = self.clock(), 0, 0, 0
        with transaction(self.c, write=True):
            # One identity lookup for the complete bounded poll, never per post.
            rows = self.c.execute('''SELECT * FROM discovery_posts WHERE url IN (SELECT value FROM json_each(?))
                OR guid IN (SELECT value FROM json_each(?))''',
                (canonical([v['url'] for v in values]), canonical([v['guid'] for v in values if v['guid']]))).fetchall()
            urls, guids = {r['url']: dict(r) for r in rows}, {r['guid']: dict(r) for r in rows if r['guid']}
            for value in values:
                old_url, old_guid = urls.get(value['url']), guids.get(value['guid'])
                if old_url and old_guid and old_url['id'] != old_guid['id']:
                    raise DiscoveryError('source_identity_conflict')
                old = old_guid or old_url
                value = dict(value)
                if old and not value['guid']:
                    value['guid'] = old['guid']
                # A weaker HTML observation must not erase precise feed dates.
                if old and value['source_kind'] == 'html' and old['source_kind'] in ('rss', 'atom'):
                    for key in ('published_at', 'published_precision', 'source_updated_at', 'summary', 'source_kind'):
                        value[key] = old[key]
                semantic = digest({k: v for k, v in value.items() if k != 'source_kind'})
                fields = {**value, 'categories': canonical(value['categories']), 'digest': semantic, 'last_seen': now}
                if old:
                    identifier = old['id']
                    if semantic != old['digest']:
                        fields.update(last_changed=now, revision=old['revision']+1)
                        changed += 1
                    else:
                        unchanged += 1
                    self.c.execute('UPDATE discovery_posts SET ' + ','.join(k+'=?' for k in fields) + ' WHERE id=?', (*fields.values(), identifier))
                    if old['url'] != value['url']:
                        urls.pop(old['url'], None)
                else:
                    fields.update(source='getcomics', first_seen=now, last_changed=now, revision=1)
                    self.c.execute('INSERT INTO discovery_posts ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')', tuple(fields.values()))
                    identifier = self.c.lastrowid
                    new += 1
                self.c.execute('DELETE FROM discovery_categories WHERE post_id=?', (identifier,))
                self.c.executemany('INSERT INTO discovery_categories VALUES(?,?)', ((identifier, v) for v in value['categories']))
                merged = {**(old or {}), **fields, 'id': identifier}
                urls[value['url']] = merged
                if value['guid']:
                    guids[value['guid']] = merged
            # Provenance owns immutable acquisition snapshots; this is only cache.
            self.c.execute('''DELETE FROM discovery_posts WHERE id IN (SELECT id FROM discovery_posts
                ORDER BY first_seen DESC,id DESC LIMIT -1 OFFSET ?)''', (MAX_POSTS,))
        return dict(observed=len(values), new=new, changed=changed, duplicates=unchanged)

    def receipt(self, receipt, *, error=None, transport=None, validators=None, gap=False, retry_after=0):
        with transaction(self.c, write=True):
            row = self.c.execute("SELECT failures,interval_minutes FROM discovery_sources WHERE key='getcomics'").fetchone()
            failures = min(10, row[0]+1) if error else 0
            delay = max(row[1]*60, min(86400, 1800*2**failures), retry_after) if error else row[1]*60
            self.c.execute('''UPDATE discovery_sources SET last_checked=?,next_poll=?,failures=?,error=?,
                receipt=?,transport=COALESCE(?,transport),gap=? WHERE key='getcomics' ''',
                (self.clock(),self.clock()+delay,failures,error,canonical(receipt),transport,gap))
            if not error:
                self.c.execute("UPDATE discovery_sources SET last_success=? WHERE key='getcomics'", (self.clock(),))
            if validators is not None:
                self.c.execute("UPDATE discovery_sources SET etag=?,last_modified=? WHERE key='getcomics'",
                    (validators.get('etag'), validators.get('last_modified')))

    def page(self, *, offset=0, limit=50, q='', category='', year='', quality=''):
        integer(offset, 0, MAX_POSTS)
        integer(limit, 1, 100)
        text(q, 200, True); text(category, 100, True); text(year, 40, True)
        conditions, args = [], []
        if q:
            conditions.append("title LIKE ? ESCAPE '\\'")
            args.append('%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%')
        if category:
            conditions.append('id IN (SELECT post_id FROM discovery_categories WHERE category=?)')
            args.append(category)
        if year:
            conditions.append('year_text=?'); args.append(year)
        where = ' WHERE '+' AND '.join(conditions) if conditions else ''
        items = [dict(r) for r in self.c.execute('SELECT * FROM discovery_posts'+where+' ORDER BY first_seen DESC,id DESC LIMIT ? OFFSET ?', (*args,limit+1,offset))]
        more = len(items)>limit
        items = items[:limit]
        for item in items:
            item['categories'] = json.loads(item['categories'])
        return dict(items=items, offset=offset, next_offset=offset+limit if more else None)
