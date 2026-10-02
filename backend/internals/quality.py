"""Local policy/observations. No provider, filesystem, or download calls."""

import json
from functools import wraps
from hashlib import sha256
from time import time
from uuid import uuid4

from backend.base.quality import (ClaimedQuality, QualityError,
                                  classify, compare, cutoff_satisfied,
                                  group_for, integer, validate_policy)
from backend.internals.collections import rows, transaction


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def title(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
        raise QualityError('invalid_request')
    return value.strip()


def snapshot(function):
    """A policy revision and its groups must come from one SQLite snapshot."""
    @wraps(function)
    def read(self, *args, **kwargs):
        with transaction(self.c):
            return function(self, *args, **kwargs)
    return read


class QualityStore:
    def __init__(self, cursor, *, clock=time):
        self.c, self.clock = cursor, clock

    @snapshot
    def profiles(self):
        profiles = rows(self.c, '''SELECT p.*,d.profile_id IS NOT NULL is_default,
            (SELECT COUNT(*) FROM volume_quality_profiles v WHERE v.profile_id=p.id) volume_assignments,
            (SELECT COUNT(*) FROM collection_quality_profiles n WHERE n.profile_id=p.id) node_assignments
            FROM quality_profiles p LEFT JOIN quality_default d ON d.profile_id=p.id ORDER BY p.id LIMIT 101''')
        if len(profiles) > 100:
            raise QualityError('bounded')
        groups = rows(self.c, 'SELECT * FROM quality_groups ORDER BY profile_id,position')
        classes = rows(self.c, 'SELECT * FROM quality_classes ORDER BY profile_id,position,class')
        for profile in profiles:
            profile['groups'] = [dict(name=g['name'], allowed=bool(g['allowed']),
                classes=[v['class'] for v in classes if (v['profile_id'],v['position']) == (g['profile_id'],g['position'])])
                for g in groups if g['profile_id'] == profile['id']]
            profile['upgrades'] = bool(profile['upgrades'])
        return profiles

    def profile(self, identifier):
        integer(identifier, 1)
        found = next((p for p in self.profiles() if p['id'] == identifier), None)
        if found is None:
            raise QualityError('not_found')
        return found

    @staticmethod
    def policy(profile):
        return {k: profile[k] for k in ('groups', 'cutoff', 'upgrades', 'minimum_p10')}

    def save(self, name, policy, *, identifier=None, revision=None):
        name, policy = title(name), validate_policy(policy)
        with transaction(self.c, write=True):
            if identifier is None:
                if self.c.execute('SELECT COUNT(*) FROM quality_profiles').fetchone()[0] >= 100:
                    raise QualityError('bounded')
                identifier = self.c.execute('''INSERT INTO quality_profiles(name,revision,upgrades,cutoff,minimum_p10)
                    VALUES(?,1,?,?,?)''', (name, policy['upgrades'], policy['cutoff'], policy['minimum_p10'])).lastrowid
            else:
                integer(revision, 1)
                if self.profile(identifier)['revision'] != revision:
                    raise QualityError('revision_conflict')
                self.c.execute('''UPDATE quality_profiles SET name=?,revision=revision+1,upgrades=?,cutoff=?,minimum_p10=? WHERE id=?''',
                    (name, policy['upgrades'], policy['cutoff'], policy['minimum_p10'], identifier))
                self.c.execute('DELETE FROM quality_groups WHERE profile_id=?', (identifier,))
            for position, group in enumerate(policy['groups']):
                self.c.execute('INSERT INTO quality_groups VALUES(?,?,?,?)', (identifier, position, group['name'], group['allowed']))
                self.c.executemany('INSERT INTO quality_classes VALUES(?,?,?)', ((identifier, position, c) for c in group['classes']))
            return self.profile(identifier)

    def delete(self, identifier, revision, confirmed):
        if confirmed is not True:
            raise QualityError('confirmation_required')
        with transaction(self.c, write=True):
            profile = self.profile(identifier)
            if profile['revision'] != integer(revision, 1):
                raise QualityError('revision_conflict')
            if profile['is_default'] or profile['volume_assignments'] or profile['node_assignments']:
                raise QualityError('profile_in_use')
            self.c.execute('DELETE FROM quality_profiles WHERE id=?', (identifier,))
        return dict(deleted=True)

    def set_default(self, profile_id, revision):
        with transaction(self.c, write=True):
            self.profile(profile_id)
            if self.c.execute('SELECT revision FROM quality_default').fetchone()[0] != integer(revision):
                raise QualityError('revision_conflict')
            self.c.execute('UPDATE quality_default SET profile_id=?,revision=revision+1', (profile_id,))
        return self.default()

    def default(self):
        return rows(self.c, 'SELECT profile_id,revision FROM quality_default')[0]

    def assign(self, kind, identifier, profile_id, expected_profile_id):
        if kind not in ('volume', 'node'):
            raise QualityError('invalid_request')
        integer(identifier, 1)
        table, key, parent = (('volume_quality_profiles', 'volume_id', 'volumes') if kind == 'volume'
                              else ('collection_quality_profiles', 'node_id', 'collection_nodes'))
        if expected_profile_id is not None:
            integer(expected_profile_id, 1)
        with transaction(self.c, write=True):
            if not self.c.execute(f'SELECT 1 FROM {parent} WHERE id=?', (identifier,)).fetchone():
                raise QualityError('not_found')
            existing = self.c.execute(f'SELECT profile_id FROM {table} WHERE {key}=?', (identifier,)).fetchone()
            if (existing[0] if existing else None) != expected_profile_id:
                raise QualityError('revision_conflict')
            if profile_id is None:
                self.c.execute(f'DELETE FROM {table} WHERE {key}=?', (identifier,))
            else:
                self.profile(profile_id)
                self.c.execute(f'''INSERT INTO {table} VALUES(?,?) ON CONFLICT({key}) DO UPDATE SET profile_id=excluded.profile_id''',
                               (identifier, profile_id))
        return dict(profile_id=profile_id)

    @snapshot
    def effective(self, volume_ids):
        if not isinstance(volume_ids, (tuple, list)) or not 1 <= len(volume_ids) <= 1000:
            raise QualityError('bounded')
        for identifier in volume_ids:
            integer(identifier, 1)
        encoded = canonical(volume_ids)
        explicit = dict(self.c.execute('''SELECT volume_id,profile_id FROM volume_quality_profiles
            WHERE volume_id IN (SELECT value FROM json_each(?))''', (encoded,)).fetchall())
        nodes = {r['id']: r for r in rows(self.c, '''SELECT n.id,n.parent_id,q.profile_id FROM collection_nodes n
            LEFT JOIN collection_quality_profiles q ON q.node_id=n.id''')}
        if len(nodes) > 25600:
            raise QualityError('bounded')
        memberships = rows(self.c, '''SELECT p.local_volume_id volume_id,m.node_id FROM collection_publications p
            JOIN collection_memberships m ON m.publication_id=p.id
            WHERE p.local_volume_id IN (SELECT value FROM json_each(?))''', (encoded,))
        inherited = {v: set() for v in volume_ids}
        for member in memberships:
            node, seen = nodes[member['node_id']], set()
            while node is not None:
                if node['id'] in seen or len(seen) >= 8:
                    raise QualityError('invalid_tree')
                seen.add(node['id'])
                if node['profile_id'] is not None:
                    inherited[member['volume_id']].add(node['profile_id'])
                    break
                node = nodes.get(node['parent_id'])
        default = self.default()['profile_id']
        profiles = {p['id']: p for p in self.profiles()}
        result = {}
        for identifier in volume_ids:
            choices = inherited[identifier]
            conflict = identifier not in explicit and len(choices) > 1
            chosen = explicit.get(identifier) or (next(iter(choices)) if len(choices) == 1 else default)
            result[identifier] = dict(profile=None if conflict else profiles[chosen], conflict=conflict,
                source='volume' if identifier in explicit else 'conflict' if conflict else 'collection' if choices else 'default',
                candidates=sorted(choices), override=explicit.get(identifier))
        return result

    def assessment(self, file_id, facts):
        integer(file_id, 1)
        # Trusted analyzer caller only; transport has no facts parameter.
        payload = canonical(facts)
        if len(payload) > 16384 or not self.c.execute('SELECT 1 FROM active_files WHERE id=?', (file_id,)).fetchone():
            raise QualityError('file_changed')
        with transaction(self.c, write=True):
            self.c.execute('''INSERT OR IGNORE INTO file_quality_assessments
                (file_id,fingerprint,analyzer,facts,observed_at) VALUES(?,?,?,?,?)''',
                (file_id, facts['sha256'], facts['analyzer']+'/'+facts['validation'], payload, self.clock()))
            row = self.c.execute('''SELECT id FROM file_quality_assessments WHERE file_id=? AND fingerprint=? AND analyzer=?''',
                (file_id, facts['sha256'], facts['analyzer']+'/'+facts['validation'])).fetchone()
        return row[0]

    def issue_page(self, volume_id, offset=0, limit=50):
        integer(volume_id, 1)
        integer(offset, 0, 1000000)
        integer(limit, 1, 100)
        issues = rows(self.c, 'SELECT id FROM issues WHERE volume_id=? ORDER BY id LIMIT ? OFFSET ?', (volume_id, limit+1, offset))
        return dict(items=self.issue_states([r['id'] for r in issues[:limit]]) if issues else [],
                    has_next=len(issues)>limit, offset=offset)

    @snapshot
    def issue_states(self, issue_ids):
        if not issue_ids:
            return []
        if len(issue_ids) > 1000:
            raise QualityError('bounded')
        for identifier in issue_ids:
            integer(identifier, 1)
        selected = canonical(issue_ids)
        issues = rows(self.c, '''SELECT i.id,i.volume_id,i.issue_number,i.monitored,v.monitored volume_monitored,v.title,
            EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id) content_owned
            FROM issues i JOIN volumes v ON v.id=i.volume_id WHERE i.id IN (SELECT value FROM json_each(?)) ORDER BY i.id''', (selected,))
        if not issues:
            return []
        effective = self.effective(sorted({r['volume_id'] for r in issues}))
        files = rows(self.c, '''SELECT b.issue_id,f.id file_id,f.size,a.id assessment_id,a.facts,
            p.id acquisition_id,p.claims,
            ((SELECT COUNT(*) FROM issues_files shared WHERE shared.file_id=f.id)<>1
              OR EXISTS(SELECT 1 FROM volume_files general WHERE general.file_id=f.id)
              OR EXISTS(SELECT 1 FROM file_content_coverage coverage WHERE coverage.file_id=f.id AND coverage.retired_at IS NULL)) shared_file
            FROM issues_files b JOIN active_files f ON f.id=b.file_id
            LEFT JOIN file_quality_assessments a ON a.id=(SELECT MAX(a2.id) FROM file_quality_assessments a2 WHERE a2.file_id=f.id)
            LEFT JOIN acquisition_provenance p ON p.id=(SELECT p2.id FROM acquisition_provenance p2
                WHERE p2.file_id=f.id AND p2.state='imported' ORDER BY p2.created_at DESC,p2.id DESC LIMIT 1)
            WHERE b.issue_id IN (SELECT value FROM json_each(?)) ORDER BY b.issue_id,f.id''', (selected,))
        for issue in issues:
            direct = [f for f in files if f['issue_id'] == issue['id']]
            assignment = effective[issue['volume_id']]
            issue.update(assignment=assignment, direct_owned=bool(direct), files=direct, reason=None,
                         content_elsewhere=bool(issue['content_owned']) and not direct,
                         upgrade_eligible=False, cutoff_satisfied=False, quality_group=None)
            for f in direct:
                f['facts'] = json.loads(f['facts']) if f['facts'] else None
                f['claims'] = json.loads(f['claims']) if f['claims'] else ClaimedQuality(origin='legacy_unknown').preview()
                # Size mismatch is detectably stale without filesystem IO during paging.
                if f['facts'] and f['facts']['size'] != f['size']:
                    f['facts'] = None
            if assignment['conflict']:
                issue['reason'] = 'profile_conflict'
            elif not direct:
                issue['reason'] = 'content_elsewhere' if issue['content_elsewhere'] else 'missing'
            elif len(direct) != 1:
                issue['reason'] = 'multiple_direct_files'
            elif direct[0]['shared_file']:
                issue['reason'] = 'shared_or_content_claim_file'
            else:
                p = assignment['profile']
                claims = ClaimedQuality(**direct[0]['claims'])
                issue['quality_group'] = group_for(p, claims)
                issue['cutoff_satisfied'] = cutoff_satisfied(p, claims, direct[0]['facts'])
                issue['reason'] = ('unmonitored' if not issue['monitored'] or not issue['volume_monitored'] else
                    'upgrades_disabled' if not p['upgrades'] else 'cutoff_satisfied' if issue['cutoff_satisfied'] else
                    'analysis_required' if not direct[0]['facts'] else 'current_quality_unresolved' if claims.conflict else 'below_cutoff')
                issue['upgrade_eligible'] = issue['reason'] == 'below_cutoff'
        return issues

    def selected(self, issue_id, candidate, *, reason, decision, client_kind=None, client_job=None, identifier=None):
        state = self.issue_states([issue_id])
        if len(state) != 1 or reason not in ('missing', 'upgrade', 'manual'):
            raise QualityError('invalid_request')
        issue = state[0]
        if issue['assignment']['conflict']:
            raise QualityError('profile_conflict')
        profile = issue['assignment']['profile']
        claims = classify(candidate.raw_title)
        source = candidate.source.key[:200]
        from backend.base.release_candidate import acquisition_identity
        key = acquisition_identity(candidate)
        identifier = identifier or uuid4().hex
        if getattr(candidate, 'torrent_facts', ()):
            decision = dict(decision, torrent_source_facts=dict(candidate.torrent_facts), protocol='torrent')
        with transaction(self.c, write=True):
            self.c.execute('''INSERT OR IGNORE INTO acquisition_provenance
                (id,volume_id,issue_id,reason,state,candidate_key,release_title,source,claims,
                 profile_id,profile_revision,profile_snapshot,decision,client_kind,client_job,created_at,updated_at)
                VALUES(?,?,?,?,'selected',?,?,?,?,?,?,?,?,?,?,?,?)''',
                (identifier, issue['volume_id'], issue_id, reason, key, candidate.raw_title, source, canonical(claims.preview()),
                 profile['id'], profile['revision'], canonical(profile), canonical(decision), client_kind, client_job, self.clock(), self.clock()))
        return identifier

    def history(self, issue_id, offset=0, limit=50):
        integer(issue_id, 1)
        integer(offset, 0, 1000000)
        integer(limit, 1, 100)
        values = rows(self.c, '''SELECT id,reason,state,release_title,source,profile_id,profile_revision,
            file_id,assessment_id,supersedes,error,created_at,updated_at FROM acquisition_provenance
            WHERE issue_id=? OR ? IN (SELECT value FROM json_each(decision,'$.issue_ids'))
            ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?''', (issue_id, issue_id, limit+1, offset))
        return dict(items=values[:limit], has_next=len(values)>limit, offset=offset)

    def detail(self, identifier):
        found = rows(self.c, 'SELECT * FROM acquisition_provenance WHERE id=?', (identifier,))
        if not found:
            raise QualityError('not_found')
        result = found[0]
        for key in ('claims', 'profile_snapshot', 'decision'):
            result[key] = json.loads(result[key])
        if result['assessment_id']:
            result['verified'] = json.loads(self.c.execute('SELECT facts FROM file_quality_assessments WHERE id=?', (result['assessment_id'],)).fetchone()[0])
            result['verified'].pop('stamp', None)
        if result['client_kind'] in ('sabnzbd','nzbget','qbittorrent') and result['client_job']:
            job = self.c.execute('SELECT nzo_id FROM acquisition_downloads WHERE id=?', (result['client_job'],)).fetchone()
            result['remote_job_id'] = job[0] if job else None
        if result['client_kind'] == 'qbittorrent' and self.c.execute("SELECT 1 FROM sqlite_master WHERE name='acquisition_torrents'").fetchone():
            torrent = rows(self.c, 'SELECT infohash_v1,infohash_v2,policy,requirements,state,cleanup_receipt FROM acquisition_torrents WHERE download_id=?', (result['client_job'],))
            if torrent:
                result['torrent'] = torrent[0]
                for key in ('policy','requirements','cleanup_receipt'):
                    result['torrent'][key] = json.loads(torrent[0][key]) if torrent[0][key] else None
                result['torrent']['import_methods'] = [r[0] for r in self.c.execute('''SELECT DISTINCT s.import_method
                    FROM acquisition_seed_artifacts s JOIN acquisition_intakes i ON i.id=s.intake_id
                    WHERE i.kind='qbittorrent' AND i.download_id=? AND s.import_method IS NOT NULL''', (result['client_job'],))]
        return result
