"""Owned 8G child reviews; execution evidence is added by the trusted adapter."""

import json
from collections import defaultdict
from dataclasses import asdict, replace
from pathlib import Path
from secrets import token_urlsafe
from threading import RLock
from time import monotonic

from backend.base.duplicate_review import (DuplicateAction, DuplicateChoice,
                                           DuplicateGroup, DuplicateKind,
                                           DuplicateReview,
                                           DuplicateReviewError)
from backend.base.library_health import canonical, fingerprint
from backend.base.maintenance_review import Action, Capability
from backend.implementations.duplicate_evidence import HashBudget
from backend.implementations.maintenance_review import file_state
from backend.internals.duplicate_review import read_duplicate_state
from backend.internals.organization_reservations import ReservationIndex

KINDS = dict(exact_byte_duplicate=DuplicateKind.EXACT,
             same_publication_files=DuplicateKind.PUBLICATION,
             overlapping_collected_coverage=DuplicateKind.COVERAGE,
             probable_duplicate=DuplicateKind.WEAK, path_collision=DuplicateKind.COLLISION)


def ownership_impact(ownership, issues, removed):
    """Remove only proposed file contributions, including outside-volume C2."""
    affected = {r['issue_id'] for r in ownership if r['file_id'] in removed}
    before, after = defaultdict(set), defaultdict(set)
    for row in ownership:
        before[row['issue_id']].add(row['file_id'])
        if row['file_id'] not in removed:
            after[row['issue_id']].add(row['file_id'])
    return [dict(issue_id=i, before_files=sorted(before[i]), after_files=sorted(after[i]),
                 loses_ownership=bool(before[i]) and not after[i],
                 becomes_wanted=bool(before[i]) and not after[i] and bool(issues[i]['monitored'])
                 and bool(issues[i]['volume_monitored'])) for i in sorted(affected)]


class DuplicateReviews:
    MAX_FINDINGS = 1000
    MAX_GROUPS = 100
    MAX_FILES = 256
    MAX_GROUP_FILES = 128
    MAX_SESSIONS = 4
    MAX_BYTES = 16 * 1024 * 1024
    TTL = 900

    def __init__(self, maintenance, *, clock=monotonic):
        self.maintenance, self.clock = maintenance, clock
        self._lock = RLock()
        self._sessions = {}

    def _purge(self):
        for key in tuple(self._sessions):
            if self._sessions[key].expires_at <= self.clock():
                del self._sessions[key]

    def get(self, identifier):
        with self._lock:
            self._purge()
            if identifier not in self._sessions:
                raise DuplicateReviewError('duplicate_review_expired_or_unavailable')
            return self._sessions[identifier]

    def _retain(self, session):
        size = sum(len(canonical(asdict(s))) for k, s in self._sessions.items() if k != session.id)
        if size + len(canonical(asdict(session))) > self.MAX_BYTES:
            raise DuplicateReviewError('duplicate_review_size_limit')
        self._sessions[session.id] = session
        return session

    def create(self, worklist_id, revision, manifest_digest, selected, *, hash_budget=None):
        # Serial acquisition bounds concurrent hashing without a second queue.
        # Trusted internal worker only; no HTTP acquisition endpoint.
        with self._lock:
            self._purge()
            if len(self._sessions) >= self.MAX_SESSIONS:
                raise DuplicateReviewError('duplicate_review_capacity')
            if (type(selected) is not tuple or not 1 <= len(selected) <= self.MAX_FINDINGS
                    or any(type(i) is not str or len(i) != 64 for i in selected)
                    or len(set(selected)) != len(selected)):
                raise DuplicateReviewError('invalid_duplicate_selection')
            worklist = self.maintenance.get(worklist_id)
            if type(revision) is not int or (revision, manifest_digest) != (worklist.revision, worklist.manifest_digest):
                raise DuplicateReviewError('stale_worklist_handoff')
            owned = {i.finding.id: i for i in worklist.items}
            if any(k not in owned or not owned[k].selected or owned[k].excluded
                   or owned[k].action != Action.DUPLICATE or owned[k].capability == Capability.STALE
                   or owned[k].finding.category != 'duplicate' or owned[k].finding.code not in KINDS
                   for k in selected):
                raise DuplicateReviewError('exact_duplicate_intent_required')
            health, extra, state_digest = read_duplicate_state(self.maintenance.database, worklist.report.scope)
            if health['digest'] != worklist.report.state_digest:
                raise DuplicateReviewError('stale_worklist_handoff')
            files = {f['id']: f for f in health['files']}
            paths = defaultdict(list)
            for fid, file in files.items():
                paths[file['filepath']].append(fid)
            observations, file_ids, hash_ids = [], set(), set()
            for key in sorted(selected):
                finding = owned[key].finding
                evidence = json.loads(finding.evidence_json)['evidence']
                members = set(evidence.get('file_ids', ()))
                if finding.file_id is not None:
                    members.add(finding.file_id)
                unresolved = False
                for path in evidence.get('paths', ()):
                    if len(paths[path]) != 1:
                        unresolved = True
                    members.update(paths[path])
                if not members.issubset(files):
                    raise DuplicateReviewError('duplicate_file_no_longer_registered')
                if len(members) > self.MAX_GROUP_FILES:
                    raise DuplicateReviewError('duplicate_group_file_limit')
                kind = KINDS[finding.code]
                if kind == DuplicateKind.EXACT:
                    if evidence.get('algorithm') != 'sha256/v1':
                        raise DuplicateReviewError('unsupported_duplicate_hash_algorithm')
                    hash_ids.update(members)
                observations.append((finding, kind, tuple(sorted(members)), evidence, unresolved))
                file_ids.update(members)
            if len(file_ids) > self.MAX_FILES:
                raise DuplicateReviewError('duplicate_file_limit')
            direct, general, coverage = defaultdict(list), defaultdict(list), defaultdict(list)
            for row in health['direct']:
                direct[row['file_id']].append(row)
            for row in health['general']:
                general[row['file_id']].append(row)
            for row in extra['coverage_history']:
                if row['file_id'] is not None:
                    coverage[row['file_id']].append(row)
            issues = {r['id']: r for r in health['issues']}
            volumes = {r['id']: r for r in health['volumes']}
            roots = {r['id']: r['folder'] for r in health['roots']}
            reservations = ReservationIndex((r['path_key'], r['job_id']) for r in extra['reservations'])
            budget = hash_budget if hash_budget is not None else HashBudget()
            details, hashes = {}, {}
            for fid in sorted(file_ids):
                file = files[fid]
                vids = {issues[r['issue_id']]['volume_id'] for r in direct[fid] if r['issue_id'] in issues}
                vids.update(r['volume_id'] for r in general[fid])
                blockers = []
                path = Path(file['filepath'])
                if len(vids) != 1 or any(v not in volumes or volumes[v]['root_folder'] not in roots
                        or Path(roots[volumes[v]['root_folder']]) not in path.parents
                        or Path(volumes[v]['folder']) not in path.parents for v in vids):
                    blockers.append('incoherent_managed_file_ownership')
                observation = file_state(str(path))
                if observation['state'] != 'file':
                    blockers.append('duplicate_file_unavailable')
                if reservations.conflicts(str(path)):
                    blockers.append('duplicate_path_reserved')
                if any(r['owner_volume_id'] in vids for r in extra['dependencies']):
                    blockers.append('active_volume_dependency')
                if fid in hash_ids and not blockers:
                    hashes[fid] = budget.inspect(str(path))
                details[fid] = dict(**file, observation=observation, hash=hashes.get(fid),
                    direct=direct[fid], general=general[fid], coverage=coverage[fid], blockers=blockers,
                    volumes=[volumes[v] for v in sorted(vids) if v in volumes],
                    identities=[r for r in health['volume_refs'] if r['volume_id'] in vids],
                    issues=[issues[r['issue_id']] for r in direct[fid] if r['issue_id'] in issues],
                    issue_identities=[r for r in health['issue_refs'] if r['issue_id'] in
                                      {d['issue_id'] for d in direct[fid]}],
                    history=[r for r in extra['jobs'] if r['source'] == str(path) or r['target'] == str(path)])
            grouped = {}
            for finding, kind, members, evidence, unresolved in observations:
                blockers = {b for fid in members for b in details[fid]['blockers']}
                if unresolved or len(members) < 2:
                    blockers.add('incomplete_registered_group')
                if finding.inspection.value != 'complete':
                    blockers.add('incomplete_finding_evidence')
                if kind == DuplicateKind.EXACT:
                    current = {(hashes[i]['algorithm'], hashes[i]['digest'], hashes[i]['size'])
                               for i in members if i in hashes}
                    if len(current) != 1 or any(i not in hashes for i in members):
                        blockers.add('fresh_exact_hash_unavailable')
                    elif next(iter(current))[1] != evidence.get('digest'):
                        raise DuplicateReviewError('duplicate_hash_evidence_changed')
                    group_key = (kind.value, evidence.get('algorithm'), evidence.get('digest'),
                                 next(iter(current))[2] if len(current) == 1 else None)
                elif kind == DuplicateKind.PUBLICATION:
                    identities = {tuple(sorted(r['issue_id'] for r in direct[i])) for i in members}
                    if len(identities) != 1 or () in identities:
                        blockers.add('direct_publication_set_changed')
                    group_key = (kind.value, tuple(sorted(identities)))
                else:
                    # Coverage/probable overlap is not transitive equivalence.
                    group_key = (kind.value, finding.id)
                entry = grouped.setdefault(group_key, dict(kind=kind, ids=set(), findings=[], evidence=[], blockers=set()))
                entry['ids'].update(members)
                entry['findings'].append(finding.id)
                entry['evidence'].append(finding.view())
                entry['blockers'].update(blockers)
            if len(grouped) > self.MAX_GROUPS or any(len(e['ids']) > self.MAX_GROUP_FILES for e in grouped.values()):
                raise DuplicateReviewError('duplicate_group_limit')
            groups = tuple(sorted((DuplicateGroup(fingerprint(key), e['kind'], tuple(sorted(e['findings'])),
                tuple(sorted(e['ids'])), canonical(e['evidence']), tuple(sorted(e['blockers'])))
                for key, e in grouped.items()), key=lambda g: g.id))
            if read_duplicate_state(self.maintenance.database, worklist.report.scope)[2] != state_digest:
                raise DuplicateReviewError('duplicate_state_changed_during_read')
            if self.maintenance.get(worklist_id) is not worklist:
                raise DuplicateReviewError('stale_worklist_handoff')
            if any(file_state(d['filepath']) != d['observation'] for d in details.values()):
                raise DuplicateReviewError('duplicate_source_changed_during_review')
            for i in issues.values():
                i['volume_monitored'] = volumes[i['volume_id']]['monitored']
            ownership = dict(rows=extra['canonical'], issues=issues, claims=extra['claims'])
            return self._retain(DuplicateReview(token_urlsafe(24), (worklist.id, revision, manifest_digest),
                0, self.clock() + self.TTL, worklist.report.state.value, worklist.report.reasons,
                state_digest, canonical(list(details.values())), canonical(ownership), groups, budget.used))

    def revise(self, identifier, revision, choices):
        with self._lock:
            session = self.get(identifier)
            if session.stale:
                raise DuplicateReviewError('stale_duplicate_review')
            if type(revision) is not int or revision != session.revision or revision >= 1000:
                raise DuplicateReviewError('stale_duplicate_revision')
            if type(choices) is not tuple or not choices or any(not isinstance(c, DuplicateChoice) for c in choices):
                raise DuplicateReviewError('invalid_duplicate_choices')
            edits = {c.group_id: c for c in choices}
            if len(edits) != len(choices) or not set(edits).issubset(g.id for g in session.groups):
                raise DuplicateReviewError('unknown_or_repeated_duplicate_group')
            ownership = json.loads(session.ownership_json)
            issues = {int(k): v for k, v in ownership['issues'].items()}
            groups = []
            for group in session.groups:
                choice = edits.get(group.id)
                if choice is None:
                    groups.append(group)
                    continue
                if choice.quarantine and (group.kind != DuplicateKind.EXACT
                        or not set(choice.quarantine) < set(group.file_ids)):
                    raise DuplicateReviewError('unsupported_duplicate_removal_choice')
                blockers = tuple(b for b in group.blockers if b not in (
                    'quarantine_recovery_not_implemented', 'ownership_loss_requires_separate_contract', 'overlapping_resolution_groups'))
                impact = ownership_impact(ownership['rows'], issues, set(choice.quarantine))
                if choice.quarantine:
                    blockers += ('quarantine_recovery_not_implemented',)
                    if any(i['loses_ownership'] for i in impact):
                        blockers += ('ownership_loss_requires_separate_contract',)
                groups.append(replace(group, action=choice.action, quarantine=tuple(sorted(choice.quarantine)),
                                      impact_json=canonical(impact), blockers=blockers))
            selected = defaultdict(list)
            for group in groups:
                if group.quarantine:
                    for fid in group.file_ids:
                        selected[fid].append(group.id)
            overlap = {g for values in selected.values() if len(values) > 1 for g in values}
            groups = [replace(g, blockers=tuple(sorted(set(g.blockers) | {'overlapping_resolution_groups'})))
                      if g.id in overlap else g for g in groups]
            # Any choice edit retires the prepared execution evidence. Even
            # unchanged sibling choices need a fresh whole-selection admission.
            groups = [replace(g, blockers=tuple(sorted(set(g.blockers) | {'quarantine_recovery_not_implemented'})))
                      if g.quarantine else g for g in groups]
            return self._retain(replace(session, revision=revision + 1, groups=tuple(groups), execution_json='[]'))

    def revalidate(self, identifier, revision):
        with self._lock:
            session = self.get(identifier)
            if type(revision) is not int or revision != session.revision or revision >= 1000:
                raise DuplicateReviewError('stale_duplicate_revision')
            worklist = self.maintenance.get(session.origin[0])
            changed = read_duplicate_state(self.maintenance.database, worklist.report.scope)[2] != session.state_digest
            changed |= (worklist.id, worklist.revision, worklist.manifest_digest) != session.origin
            changed |= any(file_state(f['filepath']) != f['observation'] for f in json.loads(session.files_json))
            # This is a stat/domain recheck, NOT renewed hash/execution authority.
            return self._retain(replace(session, revision=revision + 1, stale=session.stale or changed))

    def delete(self, identifier):
        with self._lock:
            self._sessions.pop(identifier, None)
