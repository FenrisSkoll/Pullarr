"""Local-only Calendar projection and atomic observational evidence persistence."""

import json
from datetime import date, timedelta
from time import time

from backend.base.collections import KINDS as PUBLICATION_KINDS
from backend.base.issue_facts import presentation_key
from backend.base.release_calendar import (MAX_EVENTS, MAX_EVIDENCE, MAX_SCOPE,
                                           CalendarError, bounds, day,
                                           effective, integer, observation)
from backend.internals.collections import CollectionStore, rows, transaction
from backend.internals.issue_facts import load_records


class CalendarStore:
    def __init__(self, cursor, *, clock=time):
        self.c, self.clock = cursor, clock

    def publications(self):
        store, after, result = CollectionStore(self.c), 0, []
        while True:
            page = store.calendar_page(after, 100)
            result.extend(page['items'])
            if len(result) > MAX_SCOPE:
                raise CalendarError('bounded')
            after = page['next_after']
            if after is None:
                return result

    def subjects(self):
        """Accepted Collection refs and monitored local authority only; no suggestions."""
        publications = self.publications()
        local = rows(self.c, '''SELECT v.id,v.metadata_provider,v.authority_generation,x.provider_id
            FROM volumes v JOIN volume_external_ids x ON x.volume_id=v.id AND x.provider=v.metadata_provider
            WHERE v.monitored=1 ORDER BY v.id LIMIT ?''', (MAX_SCOPE + 1,))
        if len(local) > MAX_SCOPE:
            raise CalendarError('bounded')
        result = {}
        for v in local:
            key = (v['metadata_provider'], v['provider_id'])
            result.setdefault(key, dict(provider=key[0], provider_id=key[1], volumes=[], publications=[]))['volumes'].append(v)
        for p in publications:
            for ref in p['refs']:
                key = (ref['provider'], ref['provider_id'])
                result.setdefault(key, dict(provider=key[0], provider_id=key[1], volumes=[], publications=[]))['publications'].append(p['id'])
        return [result[k] for k in sorted(result)]

    def persist(self, subject, acquired):
        """Recheck exact identities after provider IO; never persist canonical issues."""
        with transaction(self.c, True):
            current = next((s for s in self.subjects() if (s['provider'], s['provider_id']) ==
                            (subject['provider'], subject['provider_id'])), None)
            if current != subject:
                raise CalendarError('stale_authority')
            identities = {i['provider_id']: i['evidence'] for i in acquired}
            local_ids = [v['id'] for v in subject['volumes']]
            local = rows(self.c, '''SELECT x.issue_id,x.provider_id FROM issue_external_ids x
                JOIN issues i ON i.id=x.issue_id WHERE x.provider=? AND i.monitored=1
                AND i.volume_id IN (SELECT value FROM json_each(?))''', (subject['provider'], json.dumps(local_ids)))
            observed_ids = [i['issue_id'] for i in local if i['provider_id'] in identities]
            self.c.execute('''INSERT OR IGNORE INTO release_events(issue_id)
                SELECT value FROM json_each(?)''', (json.dumps(observed_ids),))
            self.c.execute('''INSERT OR IGNORE INTO release_events(publication_id)
                SELECT value FROM json_each(?)''', (json.dumps(subject['publications']),))
            event_rows = rows(self.c, '''SELECT id,issue_id,publication_id FROM release_events
                WHERE issue_id IN (SELECT value FROM json_each(?))
                OR publication_id IN (SELECT value FROM json_each(?))''',
                (json.dumps([i['issue_id'] for i in local]), json.dumps(subject['publications'])))
            event_ids = [e['id'] for e in event_rows]
            self.c.execute('''UPDATE release_event_evidence SET current=0 WHERE provider=?
                AND event_id IN (SELECT value FROM json_each(?))''', (subject['provider'], json.dumps(event_ids)))
            issue_events = {e['issue_id']: e['id'] for e in event_rows if e['issue_id']}
            publication_events = {e['publication_id']: e['id'] for e in event_rows if e['publication_id']}
            for item in local:
                if item['provider_id'] in identities:
                    self._write_evidence(issue_events[item['issue_id']], identities[item['provider_id']])
            # Complete single-issue publication only. No series-year/first-issue inference.
            publication_evidence = acquired[0]['evidence'] if len(acquired) == 1 else None
            for publication in subject['publications']:
                self._write_evidence(publication_events[publication], publication_evidence)
            if self.c.execute('SELECT count(*) FROM release_events').fetchone()[0] > MAX_EVENTS:
                raise CalendarError('bounded')
            if self.c.execute('''SELECT event_id FROM release_event_evidence WHERE event_id IN
                (SELECT value FROM json_each(?)) GROUP BY event_id HAVING count(*)>? LIMIT 1''',
                (json.dumps(event_ids), MAX_EVIDENCE)).fetchone():
                raise CalendarError('bounded')

    def _write_evidence(self, eid, evidence):
        for item in evidence or ():
            self.c.execute('''INSERT INTO release_event_evidence(event_id,provider,provider_id,source_field,
                date,precision,kind,provenance,fetched_at,current) VALUES(?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(event_id,provider,provider_id,source_field) DO UPDATE SET
                previous_date=CASE WHEN excluded.date IS NOT NULL AND excluded.date IS NOT release_event_evidence.date THEN release_event_evidence.date ELSE previous_date END,
                previous_precision=CASE WHEN excluded.date IS NOT NULL AND excluded.date IS NOT release_event_evidence.date THEN release_event_evidence.precision ELSE previous_precision END,
                date=CASE WHEN excluded.date IS NULL THEN release_event_evidence.date ELSE excluded.date END,
                precision=CASE WHEN excluded.date IS NULL THEN release_event_evidence.precision ELSE excluded.precision END,
                kind=excluded.kind,provenance=excluded.provenance,fetched_at=excluded.fetched_at,
                current=CASE WHEN excluded.date IS NULL AND release_event_evidence.date IS NOT NULL THEN 0 ELSE 1 END''',
                (eid, item['provider'], item['provider_id'], item['source_field'], item['date'], item['precision'],
                 item['kind'], item['provenance'], item['fetched_at']))

    def _events(self):
        publications = self.publications()
        p_by_volume = {p['local_volume_id']: p for p in publications if p['local_volume_id'] is not None}
        records = rows(self.c, '''SELECT i.id,i.volume_id,i.title,i.issue_number,i.monitored issue_monitored,
            v.monitored volume_monitored,v.title publication_title,v.metadata_provider,v.authority_generation,
            (SELECT count(*) FROM issues sibling WHERE sibling.volume_id=v.id) issue_count,
            EXISTS(SELECT 1 FROM issues_files f JOIN active_files a ON a.id=f.file_id WHERE f.issue_id=i.id) file_owned,
            EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id) canonical_owned
            FROM issues i JOIN volumes v ON v.id=i.volume_id
            WHERE (v.monitored=1 AND i.monitored=1) OR v.id IN (SELECT value FROM json_each(?))
            ORDER BY i.id LIMIT ?''', (json.dumps(list(p_by_volume)), MAX_EVENTS + 1))
        if len(records) > MAX_EVENTS:
            raise CalendarError('bounded')
        facts = load_records(self.c, issue_ids=[r['id'] for r in records]) if records else ()
        fact_map = {f.id: f for f in facts}
        evidence = rows(self.c, '''SELECT e.*,r.issue_id,r.publication_id FROM release_event_evidence e
            JOIN release_events r ON r.id=e.event_id ORDER BY e.event_id,e.provider,e.source_field LIMIT ?''', (MAX_EVENTS * MAX_EVIDENCE + 1,))
        if len(evidence) > MAX_EVENTS * MAX_EVIDENCE:
            raise CalendarError('bounded')
        by_subject = {}
        for e in evidence:
            key = 'issue:' + str(e['issue_id']) if e['issue_id'] else 'publication:' + str(e['publication_id'])
            by_subject.setdefault(key, []).append(dict(e, origin='provider', current=bool(e['current'])))
        result = {}
        for p in publications:
            key = 'publication:' + str(p['id'])
            result[key] = dict(id=key, subject='publication', publication_id=p['id'], issue_id=None,
                volume_id=p['local_volume_id'], title=p['title'], publication_title=p['title'], issue_number=None,
                kind=p['kind'], status=p['status'], refs=p['refs'], memberships=p['memberships'],
                monitoring_sources=['collection'], file_owned=None, content_represented=None, wanted=None,
                evidence=by_subject.get(key, []), preferred_provider=None, _order=(2, p['id']))
        for r in records:
            p = p_by_volume.get(r['volume_id'])
            single = p is not None and r['issue_count'] == 1
            if not single and not (r['issue_monitored'] and r['volume_monitored']):
                continue
            key = 'publication:' + str(p['id']) if single else 'issue:' + str(r['id'])
            canonical = fact_map.get(r['id'])
            dates = []
            if canonical and canonical.facts:
                qualified_id = next((pid for provider, pid in canonical.provider_identities if provider == r['metadata_provider']), None)
                if qualified_id:
                    dates = [observation(f, r['metadata_provider'], qualified_id, None, origin='canonical') for f in canonical.facts.dates]
            old = result.get(key)
            result[key] = dict(id=key, subject='publication' if single else 'issue',
                publication_id=p['id'] if single else None, issue_id=r['id'], volume_id=r['volume_id'],
                title=r['title'] or r['publication_title'], publication_title=r['publication_title'], issue_number=r['issue_number'],
                kind=p['kind'] if single else 'series_issue', status='in_library', refs=p['refs'] if p else [],
                memberships=p['memberships'] if p else [],
                monitoring_sources=(['library'] if r['volume_monitored'] and r['issue_monitored'] else []) + (['collection'] if p else []),
                file_owned=bool(r['file_owned']), content_represented=bool(r['canonical_owned']),
                wanted=bool(r['volume_monitored'] and r['issue_monitored'] and not r['canonical_owned']),
                evidence=dates + by_subject.get('issue:' + str(r['id']), []) + (old['evidence'] if old else []),
                preferred_provider=r['metadata_provider'], _order=presentation_key(canonical, legacy_order=False) if canonical else (2, r['id']))
            if p and not single:
                # A registered multi-issue family is context, not another issue release.
                result.pop('publication:' + str(p['id']), None)
        if len(result) > MAX_EVENTS:
            raise CalendarError('bounded')
        for entry in result.values():
            chosen = effective(entry['evidence'], entry['preferred_provider'])
            entry['effective'] = {k: chosen.get(k) for k in ('date', 'precision', 'kind', 'provider', 'current', 'origin', 'fetched_at')}
            entry['multiple_source_dates'] = len({e['date'] for e in entry['evidence'] if e['date'] and e['current']}) > 1
            entry['stale'] = bool(entry['evidence']) and (not chosen['current'] or
                (chosen['fetched_at'] is not None and self.clock() - chosen['fetched_at'] > 7 * 86400))
        return list(result.values())

    def page(self, *, start=None, end=None, unknown=False, offset=0, limit=50, scope=None,
             collection=None, volume=None, provider=None, precision=None, kind=None, ownership=None):
        integer(offset, 0, MAX_EVENTS); integer(limit, 1, 100)
        if type(unknown) is not bool or scope not in (None, 'local', 'external') or provider not in (None, 'comicvine', 'metron', 'gcd'):
            raise CalendarError('invalid_request')
        if precision not in (None, 'day', 'month', 'year', 'unknown') or ownership not in (None, 'in_library', 'external'):
            raise CalendarError('invalid_request')
        if kind is not None and kind not in (*PUBLICATION_KINDS, 'series_issue'):
            raise CalendarError('invalid_request')
        for value in (collection, volume):
            if value is not None:
                integer(value)
        first, last = day(start) if start else date.today() - timedelta(days=30), day(end) if end else date.today() + timedelta(days=90)
        if not 0 <= (last - first).days <= 366:
            raise CalendarError('range_bounded')
        with transaction(self.c):
            items = []
            for e in self._events():
                effective_date = e['effective']
                interval = bounds(effective_date['date'])
                if (unknown and interval) or (not unknown and (not interval or interval[1] < first or interval[0] > last)):
                    continue
                if scope and (e['volume_id'] is not None) != (scope == 'local'):
                    continue
                if collection and not any(m['node']['collection_id'] == collection for m in e['memberships']):
                    continue
                if volume and e['volume_id'] != volume:
                    continue
                if provider and not any(d['provider'] == provider for d in e['evidence']):
                    continue
                if precision and effective_date['precision'] != precision or kind and e['kind'] != kind or ownership and e['status'] != ownership:
                    continue
                items.append(e)
            items.sort(key=lambda e: (e['effective']['date'] or '9999', e['publication_title'].casefold(), e['_order'], e['id']))
            selected = items[offset:offset + limit]
            summaries = [{k: v for k, v in e.items() if k not in ('evidence', 'refs', 'preferred_provider', '_order')} for e in selected]
            latest = rows(self.c, 'SELECT * FROM calendar_sync_state ORDER BY started_at DESC,id DESC LIMIT 1')
            if latest:
                latest[0]['providers'] = json.loads(latest[0]['providers'])
            return dict(items=summaries, total=len(items), has_next=offset + limit < len(items), latest_sync=latest[0] if latest else None,
                bounds=dict(max_events=MAX_EVENTS, max_page=100, max_range_days=366), complete=True)

    def detail(self, identity):
        with transaction(self.c):
            found = next((e for e in self._events() if e['id'] == identity), None)
            if found is None:
                raise CalendarError('not_found')
            found.pop('_order', None)
            found['date_policy'] = 'Current on-sale/store/release, publication, cover, then legacy/unknown. Selected authority and precision break ties. Conflicts remain evidence.'
            if found['publication_id']:
                context = {found['publication_id']: dict(id=found['publication_id'], refs=found['refs'], content_context=dict(state='no_known_coverage', scope='known_confirmed_claims_only', claims=0, owned_constituents=0))}
                CollectionStore(self.c)._content_context(context)
                found['content_context'] = context[found['publication_id']]['content_context']
            return found

    def sync_status(self, identity):
        found = rows(self.c, 'SELECT * FROM calendar_sync_state WHERE id=?', (identity,))
        if not found:
            raise CalendarError('not_found')
        found[0]['providers'] = json.loads(found[0]['providers'])
        return found[0]
