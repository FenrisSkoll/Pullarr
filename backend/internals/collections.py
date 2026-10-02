"""Transactional local Collections. Queries never infer publication equivalence."""

import json
from contextlib import contextmanager
from hashlib import sha256
from time import time

from backend.base.collections import (KINDS, MAX_CATALOG, MAX_COLLECTIONS,
                                      MAX_DEPTH, MAX_NODES, MAX_PUBLICATIONS,
                                      MAX_REFS, MAX_SUGGESTIONS, MONITORING,
                                      POLICY, CollectionError, choice,
                                      integer, reference, text)


def rows(cursor, sql, args=()):
    result = cursor.execute(sql, args)
    names = [item[0] for item in result.description]
    return [dict(zip(names, row)) for row in result.fetchall()]


@contextmanager
def transaction(cursor, write=False):
    own = not cursor.connection.in_transaction
    cursor.execute('BEGIN IMMEDIATE' if own and write else 'SAVEPOINT collections_operation')
    try:
        if write and not own:
            cursor.execute('UPDATE collections SET revision=revision WHERE 0')
        yield
        if own and write:
            cursor.connection.commit()
        else:
            cursor.execute('RELEASE collections_operation')
    except BaseException:
        if own and write:
            cursor.connection.rollback()
        else:
            cursor.execute('ROLLBACK TO collections_operation')
            cursor.execute('RELEASE collections_operation')
        raise


class CollectionStore:
    def __init__(self, cursor, *, clock=time, fault=lambda stage: None):
        self.c, self.clock, self.fault = cursor, clock, fault

    def _one(self, table, identity):
        # Table is an internal constant, never request input.
        found = rows(self.c, f'SELECT * FROM {table} WHERE id=?', (identity,))
        if not found:
            raise CollectionError('not_found')
        return found[0]

    def _revision(self, collection, expected):
        integer(expected, 0)
        if self._one('collections', collection)['revision'] != expected:
            raise CollectionError('revision_conflict')

    def _bump(self, collection):
        self.c.execute('UPDATE collections SET revision=revision+1 WHERE id=?', (collection,))

    def _tree(self, collection, acquired=None):
        result = rows(self.c, 'SELECT * FROM collection_nodes WHERE collection_id=? ORDER BY position,id', (collection,)) if acquired is None else acquired
        if len(result) > MAX_NODES:
            raise CollectionError('bounded')
        indexed = {r['id']: r for r in result}
        for node in result:
            path, current, monitored = [], node, None
            while current is not None:
                if current['id'] in path or len(path) >= MAX_DEPTH:
                    raise CollectionError('invalid_tree')
                path.append(current['id'])
                if monitored is None and current['monitoring'] != 'inherit':
                    monitored = current['monitoring'] == 'monitored'
                parent = current['parent_id']
                if parent is not None and parent not in indexed:
                    raise CollectionError('invalid_tree')
                current = indexed.get(parent)
            node['path'] = list(reversed(path))
            node['effective_monitored'] = bool(monitored)
        return result

    def _all_nodes(self):
        acquired = rows(self.c, 'SELECT * FROM collection_nodes ORDER BY collection_id,position,id LIMIT ?', (MAX_COLLECTIONS * MAX_NODES + 1,))
        if len(acquired) > MAX_COLLECTIONS * MAX_NODES:
            raise CollectionError('bounded')
        grouped = {}
        for node in acquired:
            grouped.setdefault(node['collection_id'], []).append(node)
        return [node for identity, values in grouped.items() for node in self._tree(identity, values)]

    def create(self, title, description='', monitoring='unmonitored'):
        title, description = text(title), text(description, 4000, True)
        choice(monitoring, MONITORING)
        with transaction(self.c, True):
            if self.c.execute('SELECT count(*) FROM collections').fetchone()[0] >= MAX_COLLECTIONS:
                raise CollectionError('bounded')
            identity = self.c.execute('INSERT INTO collections(created_at) VALUES(?)', (self.clock(),)).lastrowid
            self.c.execute('''INSERT INTO collection_nodes(collection_id,title,description,monitoring)
                VALUES(?,?,?,?)''', (identity, title, description, monitoring))
        return self.tree(identity)

    def page(self, after=0, limit=50):
        integer(after, 0); integer(limit, 1, 100)
        with transaction(self.c):
            values = rows(self.c, '''SELECT c.id,c.revision,c.created_at,n.title,n.description,n.monitoring,n.id AS root_id
                FROM collections c JOIN collection_nodes n ON n.collection_id=c.id AND n.parent_id IS NULL
                WHERE c.id>? ORDER BY c.id LIMIT ?''', (after, limit + 1))
            return dict(items=values[:limit], next_after=values[limit - 1]['id'] if len(values) > limit else None)

    def tree(self, collection):
        integer(collection)
        with transaction(self.c):
            header = self._one('collections', collection)
            nodes = self._tree(collection)
            identities = [r[0] for r in self.c.execute('''SELECT DISTINCT m.publication_id FROM collection_memberships m
                JOIN collection_nodes n ON n.id=m.node_id WHERE n.collection_id=? LIMIT ?''', (collection, MAX_PUBLICATIONS + 1))]
            if len(identities) > MAX_PUBLICATIONS:
                raise CollectionError('bounded')
            publications = self._resolve(identities)
            owned = sum(p['status'] == 'in_library' for p in publications)
            ambiguous = sum(p['status'] == 'ambiguous' for p in publications)
            return dict(header, nodes=nodes, completeness=dict(total=len(identities), in_library=owned,
                external=len(identities) - owned - ambiguous, unresolved=ambiguous,
                percent=round(100 * owned / len(identities), 2) if identities else None,
                meaning='accepted_publications_with_local_volumes_not_downloaded_content'))

    def edit_node(self, collection, revision, node_id, *, title, description, kind, monitoring, parent_id, position):
        integer(collection); integer(revision, 0); integer(position, 0, MAX_NODES)
        title, description = text(title), text(description, 4000, True)
        choice(kind, KINDS); choice(monitoring, MONITORING)
        with transaction(self.c, True):
            self._revision(collection, revision)
            nodes = self._tree(collection)
            by_id = {n['id']: n for n in nodes}
            old = by_id.get(node_id)
            if node_id is not None and old is None:
                raise CollectionError('not_found')
            if parent_id is None:
                if old is None or old['parent_id'] is not None:
                    raise CollectionError('invalid_parent')
            elif parent_id not in by_id or old is not None and old['parent_id'] is None:
                raise CollectionError('invalid_parent')
            if old is None and len(nodes) >= MAX_NODES:
                raise CollectionError('bounded')
            if any(n['parent_id'] == parent_id and n['title'].casefold() == title.casefold() and n['id'] != node_id for n in nodes):
                raise CollectionError('duplicate_sibling_title')
            values = (parent_id, title, description, kind, monitoring, position)
            if old is None:
                node_id = self.c.execute('''INSERT INTO collection_nodes(parent_id,title,description,kind,monitoring,position,collection_id)
                    VALUES(?,?,?,?,?,?,?)''', values + (collection,)).lastrowid
            else:
                self.c.execute('''UPDATE collection_nodes SET parent_id=?,title=?,description=?,kind=?,monitoring=?,position=?
                    WHERE id=?''', values + (node_id,))
            self._tree(collection)  # Full post-state depth/cycle validation, inside rollback boundary.
            self.fault('node')
            self._bump(collection)
        return self.tree(collection)

    def delete_node(self, collection, revision, node_id, confirmed):
        if confirmed is not True:
            raise CollectionError('confirmation_required')
        with transaction(self.c, True):
            self._revision(collection, revision)
            node = self._one('collection_nodes', integer(node_id))
            if node['collection_id'] != collection:
                raise CollectionError('not_found')
            if node['parent_id'] is None:
                self.c.execute('DELETE FROM collections WHERE id=?', (collection,))
            else:
                self.c.execute('DELETE FROM collection_nodes WHERE id=?', (node_id,))
                self._bump(collection)
        return dict(deleted=True, library_unchanged=True)

    def _local_refs(self, volume):
        result = rows(self.c, '''SELECT provider,provider_id FROM volume_external_ids
            WHERE volume_id=? ORDER BY provider''', (volume,))
        if not result or len(result) > MAX_REFS:
            raise CollectionError('invalid_reference')
        for r in result:
            reference(r['provider'], r['provider_id'])
        return result

    def _publication(self, title, year, publisher, refs, local=None):
        # Exact collisions are blocked, not merged by title or rewritten refs.
        ids = set()
        for r in refs:
            reference(r['provider'], r['provider_id'])
            ids.update(row[0] for row in self.c.execute('''SELECT publication_id FROM collection_publication_refs
                WHERE provider=? AND provider_id=?''', (r['provider'], r['provider_id'])))
        if local is not None:
            ids.update(row[0] for row in self.c.execute('SELECT id FROM collection_publications WHERE local_volume_id=?', (local,)))
        if len(ids) > 1:
            raise CollectionError('publication_identity_conflict')
        if ids:
            identity = ids.pop()
            prior = self._one('collection_publications', identity)
            if local is not None and prior['local_volume_id'] not in (None, local):
                raise CollectionError('publication_identity_conflict')
            if local is not None:
                self.c.execute('UPDATE collection_publications SET local_volume_id=? WHERE id=?', (local, identity))
        else:
            if self.c.execute('SELECT count(*) FROM collection_publications').fetchone()[0] >= MAX_CATALOG:
                raise CollectionError('bounded')
            identity = self.c.execute('''INSERT INTO collection_publications(local_volume_id,title,year,publisher,kind_source,created_at)
                VALUES(?,?,?,?,'unknown',?)''', (local, text(title, 500), year, text(publisher or '', 500, True), self.clock())).lastrowid
        for r in refs:
            old = self.c.execute('SELECT provider_id FROM collection_publication_refs WHERE publication_id=? AND provider=?', (identity, r['provider'])).fetchone()
            if old is not None and old[0] != r['provider_id']:
                raise CollectionError('publication_identity_conflict')
            self.c.execute('''INSERT OR IGNORE INTO collection_publication_refs VALUES(?,?,?,?)''',
                (identity, r['provider'], r['provider_id'], 'exact_local_identity' if local else 'provider_search'))
        self.fault('publication')
        return identity

    def _member(self, node, publication, source, evidence, note='', position=0):
        collection = self._one('collection_nodes', node)['collection_id']
        total = self.c.execute('''SELECT count(DISTINCT m.publication_id) FROM collection_memberships m
            JOIN collection_nodes n ON n.id=m.node_id WHERE n.collection_id=? AND m.publication_id!=?''', (collection, publication)).fetchone()[0]
        if total >= MAX_PUBLICATIONS:
            raise CollectionError('bounded')
        if self.c.execute('SELECT count(*) FROM collection_memberships WHERE publication_id=?', (publication,)).fetchone()[0] >= MAX_NODES * MAX_COLLECTIONS:
            raise CollectionError('bounded')
        self.c.execute('''INSERT OR IGNORE INTO collection_memberships VALUES(?,?,?,?,?,?,?)''',
            (node, publication, position, note, source, json.dumps(evidence, sort_keys=True), self.clock()))
        self.fault('membership')

    def add_local(self, node_id, revision, volume_id):
        integer(node_id); integer(volume_id)
        with transaction(self.c, True):
            node = self._one('collection_nodes', node_id)
            self._revision(node['collection_id'], revision)
            volume = self._one('volumes', volume_id)
            publication = self._publication(volume['title'], volume['year'], volume['publisher'], self._local_refs(volume_id), volume_id)
            self._member(node_id, publication, 'manual', dict(version=POLICY, kind='manual_local_volume', volume_id=volume_id))
            self._bump(node['collection_id'])
        return dict(publication_id=publication, collection_id=node['collection_id'])

    def membership(self, node_id, revision, publication, action, target=None, note='', position=0):
        choice(action, ('add', 'remove', 'move', 'edit'))
        integer(node_id); integer(publication); integer(position, 0, MAX_PUBLICATIONS)
        note = text(note, 1000, True)
        with transaction(self.c, True):
            node = self._one('collection_nodes', node_id)
            self._revision(node['collection_id'], revision)
            self._one('collection_publications', publication)
            if action == 'add':
                self._member(node_id, publication, 'manual', dict(version=POLICY, kind='manual_existing_publication'), note, position)
            else:
                prior = rows(self.c, 'SELECT * FROM collection_memberships WHERE node_id=? AND publication_id=?', (node_id, publication))
                if not prior:
                    raise CollectionError('not_found')
                if action == 'edit':
                    self.c.execute('UPDATE collection_memberships SET note=?,position=? WHERE node_id=? AND publication_id=?', (note, position, node_id, publication))
                else:
                    if action == 'move':
                        integer(target)
                        if target == node_id:
                            raise CollectionError('invalid_parent')
                        other = self._one('collection_nodes', target)
                        if other['collection_id'] != node['collection_id']:
                            raise CollectionError('different_collection')
                        self._member(target, publication, prior[0]['source'], json.loads(prior[0]['evidence']), prior[0]['note'], position)
                    self.c.execute('DELETE FROM collection_memberships WHERE node_id=? AND publication_id=?', (node_id, publication))
            self.fault('membership_edit')
            self._bump(node['collection_id'])
        return dict(collection_id=node['collection_id'])

    def edit_publication(self, node_id, revision, publication, kind):
        choice(kind, KINDS)
        with transaction(self.c, True):
            node = self._one('collection_nodes', integer(node_id))
            self._revision(node['collection_id'], revision)
            if not self.c.execute('SELECT 1 FROM collection_memberships WHERE node_id=? AND publication_id=?',
                                  (node_id, integer(publication))).fetchone():
                raise CollectionError('not_found')
            self.c.execute("UPDATE collection_publications SET kind=?,kind_source='manual' WHERE id=?", (kind, publication))
            self._bump(node['collection_id'])
        return dict(publication_id=publication)

    def _resolve(self, identities):
        if not identities:
            return []
        payload = json.dumps(identities)
        publications = rows(self.c, '''SELECT p.*,v.title AS local_title,v.year AS local_year,v.publisher AS local_publisher
            FROM collection_publications p LEFT JOIN volumes v ON v.id=p.local_volume_id
            WHERE p.id IN (SELECT value FROM json_each(?)) ORDER BY p.id''', (payload,))
        references = rows(self.c, '''SELECT r.*,x.volume_id FROM collection_publication_refs r
            LEFT JOIN volume_external_ids x ON x.provider=r.provider AND x.provider_id=r.provider_id
            WHERE r.publication_id IN (SELECT value FROM json_each(?)) ORDER BY r.provider,r.provider_id,x.volume_id''', (payload,))
        by_id = {p['id']: p for p in publications}
        for p in publications:
            p['refs'], p['matches'] = [], set()
        for r in references:
            p = by_id[r['publication_id']]
            ref = {k: r[k] for k in ('provider', 'provider_id', 'source')}
            if ref not in p['refs']:
                p['refs'].append(ref)
            if r['volume_id'] is not None:
                p['matches'].add(r['volume_id'])
        matched = sorted({v for p in publications for v in p['matches']})
        live = {r['id']: r for r in rows(self.c, 'SELECT id,title,year,publisher FROM volumes WHERE id IN (SELECT value FROM json_each(?))', (json.dumps(matched),))}
        for p in publications:
            candidates = p.pop('matches')
            local = p['local_volume_id'] or (next(iter(candidates)) if len(candidates) == 1 else None)
            p['local_volume_id'] = local
            p['status'] = 'in_library' if local is not None else 'ambiguous' if len(candidates) > 1 else 'external'
            p['match_ids'] = sorted(candidates)
            if local in live:
                p.update({k: live[local][k] for k in ('title', 'year', 'publisher')})
            elif p['local_title'] is not None:
                p.update(title=p['local_title'], year=p['local_year'], publisher=p['local_publisher'])
            for k in ('local_title', 'local_year', 'local_publisher'):
                del p[k]
        return publications

    def publications(self, collection, node_id=None, offset=0, limit=50):
        integer(collection); integer(offset, 0, MAX_PUBLICATIONS); integer(limit, 1, 100)
        with transaction(self.c):
            self._one('collections', collection)
            tree = self._tree(collection)
            if node_id is not None and node_id not in {n['id'] for n in tree}:
                raise CollectionError('not_found')
            scope = [n['id'] for n in tree if node_id is None or node_id in n['path']]
            identities = [r[0] for r in self.c.execute('''SELECT publication_id FROM collection_memberships
                WHERE node_id IN (SELECT value FROM json_each(?)) GROUP BY publication_id ORDER BY MIN(position),publication_id
                LIMIT ?''', (json.dumps(scope), MAX_PUBLICATIONS + 1))]
            if len(identities) > MAX_PUBLICATIONS:
                raise CollectionError('bounded')
            page_ids = identities[offset:offset + limit]
            page = {p['id']: p for p in self._resolve(page_ids)}
            links = rows(self.c, '''SELECT m.*,n.title AS node_title,n.collection_id FROM collection_memberships m JOIN collection_nodes n ON n.id=m.node_id
                WHERE m.publication_id IN (SELECT value FROM json_each(?)) ORDER BY m.position,m.publication_id,m.node_id LIMIT 10001''', (json.dumps(page_ids),))
            if len(links) > 10000:
                raise CollectionError('bounded')
            monitors = {n['id']: n['effective_monitored'] for n in self._all_nodes()}
            for p in page.values():
                p['memberships'] = [dict(m, evidence=json.loads(m['evidence'])) for m in links if m['publication_id'] == p['id']]
                p['effective_monitored'] = any(monitors[m['node_id']] for m in p['memberships'])
                p['content_context'] = dict(state='no_known_coverage', scope='known_confirmed_claims_only', claims=0, owned_constituents=0)
            self._content_context(page)
            return dict(items=[page[i] for i in page_ids], total=len(identities), offset=offset, limit=limit, has_next=offset + limit < len(identities))

    def _content_context(self, page):
        # External series membership is available only from exact C2A catalog refs.
        # A complete set of known claims is NOT a complete publication inventory.
        refs = [(p['id'], r['provider'], r['provider_id']) for p in page.values() for r in p['refs']]
        if not refs:
            return
        claims = rows(self.c, '''WITH refs AS (SELECT json_extract(value,'$[0]') AS publication_id,
            json_extract(value,'$[1]') AS provider,json_extract(value,'$[2]') AS series_id FROM json_each(?)),
            issueset AS (
                SELECT r.publication_id,x.provider,x.provider_id FROM refs r JOIN bibliographic_issue_refs x
                ON x.provider=r.provider AND x.series_id=r.series_id AND x.deleted=0
                UNION SELECT r.publication_id,x.provider,x.provider_id FROM refs r JOIN volume_external_ids v
                ON v.provider=r.provider AND v.provider_id=r.series_id JOIN issues i ON i.volume_id=v.volume_id
                JOIN issue_external_ids x ON x.issue_id=i.id AND x.provider=r.provider)
            SELECT DISTINCT s.publication_id,k.id,k.kind,
                EXISTS(SELECT 1 FROM issue_external_ids x JOIN canonical_issue_files o ON o.issue_id=x.issue_id
                    JOIN issues i ON i.id=x.issue_id JOIN volumes v ON v.id=i.volume_id AND v.metadata_provider=x.provider
                    WHERE x.provider=k.source_provider AND x.provider_id=k.source_provider_id) AS source_owned
            FROM issueset s JOIN bibliographic_content_claims k
                ON k.target_provider=s.provider AND k.target_provider_id=s.provider_id
            WHERE k.retired_at IS NULL AND k.policy='kapowarr-collected-content/v1'
                AND k.authority='operator_confirmed' LIMIT 5001''', (json.dumps(refs),))
        if len(claims) > 5000:
            for p in page.values():
                p['content_context']['state'] = 'bounded'
            return
        for identity, p in page.items():
            relevant = [c for c in claims if c['publication_id'] == identity]
            owned = sum(bool(c['source_owned']) and c['kind'] == 'complete_issue_containment' for c in relevant)
            p['content_context'].update(claims=len(relevant), owned_constituents=owned,
                state='complete_known_content' if owned and owned == len(relevant) else 'partial_known_content' if any(c['source_owned'] for c in relevant) else 'no_known_coverage')

    def propose(self, node_id, candidates, query):
        integer(node_id); query = text(query, 500)
        if len(candidates) > 750:
            raise CollectionError('bounded')
        with transaction(self.c, True):
            self._one('collection_nodes', node_id)
            count = self.c.execute('SELECT count(*) FROM collection_suggestions WHERE node_id=?', (node_id,)).fetchone()[0]
            for candidate in candidates:
                provider, identity = reference(candidate['provider'], candidate['provider_id'])
                key = sha256(json.dumps([node_id, provider, identity, 1]).encode()).hexdigest()
                if self.c.execute('SELECT 1 FROM collection_suggestions WHERE id=?', (key,)).fetchone():
                    continue  # Refresh never resets an explicit decision or its evidence.
                count += 1
                if count > MAX_SUGGESTIONS:
                    raise CollectionError('bounded')
                evidence = dict(version=POLICY, kind='provider_search', provider=provider, provider_id=identity,
                    query=query, explanation='Exact provider search result; family membership requires operator acceptance.')
                self.c.execute('''INSERT INTO collection_suggestions(id,node_id,provider,provider_id,title,year,publisher,
                    evidence,version,decision,observed_at) VALUES(?,?,?,?,?,?,?,?,1,'pending',?)''',
                    (key, node_id, provider, identity, text(candidate['title'], 500), candidate['year'],
                     text(candidate.get('publisher') or '', 500, True), json.dumps(evidence), self.clock()))
            self.fault('suggestions')
        return dict(node_id=node_id, count=count)

    def suggestions(self, node_id, decision='pending', offset=0, limit=50):
        integer(node_id); integer(offset, 0, MAX_SUGGESTIONS); integer(limit, 1, 100)
        choice(decision, ('pending', 'accepted', 'rejected', 'review_later'))
        with transaction(self.c):
            self._one('collection_nodes', node_id)
            total = self.c.execute('SELECT count(*) FROM collection_suggestions WHERE node_id=? AND decision=?', (node_id, decision)).fetchone()[0]
            values = rows(self.c, '''SELECT * FROM collection_suggestions WHERE node_id=? AND decision=? ORDER BY id LIMIT ? OFFSET ?''', (node_id, decision, limit, offset))
            identities = json.dumps([[r['provider'], r['provider_id']] for r in values])
            local = rows(self.c, '''SELECT x.provider,x.provider_id,x.volume_id FROM volume_external_ids x
                JOIN json_each(?) j ON x.provider=json_extract(j.value,'$[0]') AND x.provider_id=json_extract(j.value,'$[1]')''', (identities,))
            memberships = rows(self.c, '''SELECT r.provider,r.provider_id,m.publication_id,n.title,n.collection_id,n.id AS node_id
                FROM collection_publication_refs r JOIN json_each(?) j
                ON r.provider=json_extract(j.value,'$[0]') AND r.provider_id=json_extract(j.value,'$[1]')
                JOIN collection_memberships m ON m.publication_id=r.publication_id JOIN collection_nodes n ON n.id=m.node_id LIMIT 10001''', (identities,))
            if len(memberships) > 10000:
                raise CollectionError('bounded')
            for r in values:
                r['evidence'] = json.loads(r['evidence'])
                r['local_match_ids'] = [v['volume_id'] for v in local if (v['provider'], v['provider_id']) == (r['provider'], r['provider_id'])]
                r['existing_memberships'] = [m for m in memberships if (m['provider'], m['provider_id']) == (r['provider'], r['provider_id'])]
            return dict(items=values, total=total, has_next=offset + limit < total)

    def decide(self, identity, revision, collection_revision, decision):
        choice(decision, ('accepted', 'rejected', 'review_later', 'pending'))
        with transaction(self.c, True):
            suggestion = self._one('collection_suggestions', identity)
            if suggestion['revision'] != integer(revision, 0):
                raise CollectionError('revision_conflict')
            node = self._one('collection_nodes', suggestion['node_id'])
            self._revision(node['collection_id'], collection_revision)
            publication = suggestion['publication_id']
            if decision == 'accepted':
                if suggestion['version'] != 1:
                    raise CollectionError('unsupported_evidence')
                refs = [dict(provider=suggestion['provider'], provider_id=suggestion['provider_id'])]
                matches = [r[0] for r in self.c.execute('SELECT volume_id FROM volume_external_ids WHERE provider=? AND provider_id=?', (suggestion['provider'], suggestion['provider_id']))]
                local = matches[0] if len(matches) == 1 else None
                if local:
                    refs = self._local_refs(local)
                publication = self._publication(suggestion['title'], suggestion['year'], suggestion['publisher'], refs, local)
                self._member(node['id'], publication, 'accepted_search_suggestion', json.loads(suggestion['evidence']))
                self._bump(node['collection_id'])
            self.c.execute('''UPDATE collection_suggestions SET decision=?,revision=revision+1,publication_id=?,decided_at=? WHERE id=?''',
                (decision, publication, self.clock(), identity))
            self.fault('decision')
        return dict(collection_id=node['collection_id'], publication_id=publication, decision=decision)

    def link_added(self, publication, volume):
        with transaction(self.c, True):
            old = self._one('collection_publications', integer(publication))
            refs = self._local_refs(integer(volume))
            known = rows(self.c, 'SELECT provider,provider_id FROM collection_publication_refs WHERE publication_id=?', (publication,))
            if not any(r in refs for r in known):
                raise CollectionError('publication_identity_conflict')
            identity = self._publication(old['title'], old['year'], old['publisher'], refs, volume)
            if identity != publication:
                raise CollectionError('publication_identity_conflict')
        return dict(publication_id=publication, local_volume_id=volume)

    def calendar_page(self, after=0, limit=50):
        """8K handoff: accepted identities/provenance only, no dates or acquisition."""
        integer(after, 0); integer(limit, 1, 100)
        with transaction(self.c):
            nodes = self._all_nodes()
            monitored = [n['id'] for n in nodes if n['effective_monitored']]
            ids = [r[0] for r in self.c.execute('''SELECT DISTINCT publication_id FROM collection_memberships
                WHERE node_id IN (SELECT value FROM json_each(?)) AND publication_id>? ORDER BY publication_id LIMIT ?''', (json.dumps(monitored), after, limit + 1))]
            page = self._resolve(ids[:limit])
            memberships = rows(self.c, '''SELECT * FROM collection_memberships WHERE publication_id IN (SELECT value FROM json_each(?))
                AND node_id IN (SELECT value FROM json_each(?)) ORDER BY node_id,publication_id''', (json.dumps(ids[:limit]), json.dumps(monitored)))
            node_map = {n['id']: n for n in nodes}
            for p in page:
                p['effective_monitored'] = True
                p['memberships'] = [dict(m, evidence=json.loads(m['evidence']), node=node_map[m['node_id']]) for m in memberships if m['publication_id'] == p['id']]
            return dict(items=page, next_after=ids[limit - 1] if len(ids) > limit else None)

    def monitored_nodes_page(self, after=0, limit=50):
        """Discovery contexts also include monitored nodes with no publications yet."""
        integer(after, 0); integer(limit, 1, 100)
        with transaction(self.c):
            nodes = sorted((n for n in self._all_nodes() if n['effective_monitored'] and n['id'] > after), key=lambda n: n['id'])
            return dict(items=nodes[:limit], next_after=nodes[limit - 1]['id'] if len(nodes) > limit else None)
