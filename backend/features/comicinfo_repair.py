"""Reviewed in-place ComicInfo repair through the existing organization journal."""

from dataclasses import dataclass, replace
from hashlib import sha256
from secrets import token_urlsafe
from threading import RLock
from time import monotonic

from backend.base.import_candidate import ProviderReference, ResourceKind
from backend.base.library_health import fingerprint
from backend.base.maintenance_review import Action, Capability
from backend.base.metadata_repair import RepairError
from backend.base.organization_plan import MetadataFieldDelta, MetadataIntent
from backend.features.organization_execution import OrganizationExecutor
from backend.implementations.comicinfo import parse_comicinfo
from backend.implementations.comicinfo_merge import (ComicInfoUpdates,
                                                     merge_comicinfo)
from backend.implementations.maintenance_review import (build_previews,
                                                        file_state)
from backend.implementations.organization_plan import preview_plan
from backend.internals.library_health import read_snapshot
from backend.internals.provider_authority import AuthorityToken, capture
from backend.internals.switch_review import dependencies

FIELDS = frozenset(('Series', 'Number', 'Title', 'Summary', 'Publisher', 'Date', 'Provider identities'))


def scoped_digest(snapshot, plan):
    """Exclude sibling archive size changes, not metadata or own associations."""
    vid = plan.associations.before[0].volume_id
    fid = plan.associations.file_id
    volumes, issues, roots, _, naming = snapshot['planning']
    ids = {i.identity.id for i in issues if i.identity.volume_id == vid}
    return fingerprint(dict(
        volume=repr(tuple(v for v in volumes if v.identity.id == vid)),
        issues=repr(tuple(i for i in issues if i.identity.id in ids)),
        roots=roots, ownership=tuple((v.identity.id, v.folder) for v in volumes), naming=repr(naming),
        control=[v for v in snapshot['volumes'] if v['id'] == vid],
        references=[r for r in snapshot['volume_refs'] if r['volume_id'] == vid],
        issue_references=[r for r in snapshot['issue_refs'] if r['issue_id'] in ids],
        classification=[r for r in snapshot['classification'] if r['volume_id'] == vid],
        file=[r for r in snapshot['files'] if r['id'] == fid],
        direct=[r for r in snapshot['direct'] if r['file_id'] == fid],
        general=[r for r in snapshot['general'] if r['file_id'] == fid],
        coverage=[r for r in snapshot['coverage'] if r['file_id'] == fid]))


@dataclass(frozen=True)
class ComicInfoRepairReview:
    id: str
    origin: tuple
    authority: object
    snapshot_digest: str
    scope: object
    stamp: object
    base_plan: object
    plan: object
    identities: tuple
    selected: tuple
    revision: int
    expires_at: float

    @property
    def digest(self):
        return fingerprint((self.id, self.origin, repr(self.authority), self.snapshot_digest,
                            self.stamp, repr(self.plan), self.selected, self.revision))

    def view(self):
        return dict(id=self.id, revision=self.revision, digest=self.digest, selected=self.selected,
            preview=preview_plan(self.plan), apply_available=bool(self.selected and self.plan.metadata.xml),
            recovery='Journaled forward recovery; no lossless undo. Archive bytes, inode and timestamps can change.',
            identities='Selected qualified identities are mandatory, additive and conflict checked.',
            cross_system_atomic=False)


class ComicInfoRepairReviews:
    """No provider acquisition. One file per job; independent batch outcomes."""
    MAX_SESSIONS = 16
    MAX_BYTES = 64 * 1024 * 1024
    def __init__(self, maintenance, *, clock=monotonic, checkpoint=None):
        self.maintenance, self.clock, self.checkpoint = maintenance, clock, checkpoint
        self._lock = RLock()
        self._sessions = {}
        self._stale = set()

    def _expire(self):
        for key in tuple(self._sessions):
            if self._sessions[key].expires_at <= self.clock():
                del self._sessions[key]
                self._stale.discard(key)

    def create(self, cursor, worklist_id, revision, digest, finding_id):
        with self._lock:
            self._expire()
            if len(self._sessions) >= self.MAX_SESSIONS:
                raise RepairError('repair_review_capacity')
            worklist = self.maintenance.get(worklist_id)
            if type(revision) is not int or worklist.revision != revision or worklist.manifest_digest != digest:
                raise RepairError('stale_worklist_handoff')
            item = next((i for i in worklist.items if i.finding.id == finding_id), None)
            if (item is None or not item.selected or item.excluded or item.action != Action.COMICINFO
                    or item.capability != Capability.PREVIEW or item.blockers):
                raise RepairError('unsupported_comicinfo_handoff')
            snapshot = read_snapshot(self.maintenance.database, worklist.report.scope, 20000)
            plans = {}
            revised, collisions = build_previews(self.maintenance.database, worklist.report, (item,), snapshot,
                                                 detached_plans=plans)
            if collisions or revised[0].blockers or revised[0].capability != Capability.PREVIEW or finding_id not in plans:
                raise RepairError('stale_or_blocked_comicinfo_preview')
            plan = plans[finding_id]
            if not plan.metadata.xml or not plan.associations or len(plan.associations.before) != 1:
                raise RepairError('comicinfo_no_supported_changes')
            iid = plan.associations.before[0].issue_id
            vid = plan.associations.before[0].volume_id
            authority = capture(cursor, (vid,))[vid]
            if dependencies(cursor, vid):
                raise RepairError('repair_active_dependency')
            identity = cursor.execute('SELECT provider_id FROM issue_external_ids WHERE issue_id=? AND provider=?',
                                      (iid, authority.provider)).fetchone()
            if identity is None:
                raise RepairError('selected_issue_identity_unavailable')
            identities = (ProviderReference(authority.provider, ResourceKind.VOLUME, authority.provider_id),
                          ProviderReference(authority.provider, ResourceKind.ISSUE, identity[0]))
            if read_snapshot(self.maintenance.database, worklist.report.scope, 20000)['digest'] != snapshot['digest']:
                raise RepairError('stale_comicinfo_review')
            if self.maintenance.get(worklist_id) is not worklist:
                raise RepairError('stale_worklist_handoff')
            empty = replace(plan, metadata=replace(plan.metadata, xml=None), effects=())
            session = ComicInfoRepairReview(token_urlsafe(24), (worklist.id, revision, digest, finding_id), authority,
                scoped_digest(snapshot, plan), worklist.report.scope, fingerprint(file_state(plan.source_path)), plan, empty, identities, (), 0,
                self.clock() + 900)
            if sum(len(repr(s).encode('utf-8')) for s in (*self._sessions.values(), session)) > self.MAX_BYTES:
                raise RepairError('repair_review_size_limit')
            self._sessions[session.id] = session
            return session

    def get(self, cursor, identifier):
        with self._lock:
            self._expire()
            session = self._sessions.get(identifier)
            if session is None:
                raise RepairError('repair_review_expired_or_unavailable')
            if (identifier in self._stale or capture(cursor, (session.authority.volume_id,)).get(session.authority.volume_id) != session.authority
                    or scoped_digest(read_snapshot(self.maintenance.database, session.scope, 20000), session.plan) != session.snapshot_digest
                    or fingerprint(file_state(session.plan.source_path)) != session.stamp):
                self._stale.add(identifier)
                raise RepairError('stale_comicinfo_review')
            return session

    def revise(self, cursor, identifier, revision, selected):
        with self._lock:
            session = self.get(cursor, identifier)
            if type(revision) is not int or revision != session.revision or revision >= 1000:
                raise RepairError('stale_repair_revision')
            if (type(selected) is not tuple or len(selected) > len(FIELDS) or any(not isinstance(s, str) or s not in FIELDS for s in selected)
                    or len(set(selected)) != len(selected) or selected and 'Provider identities' not in selected):
                raise RepairError('unsupported_comicinfo_fields')
            plan = session.base_plan
            old = plan.identification.candidate.comicinfo.document
            proposed = parse_comicinfo(plan.metadata.xml)
            names = set(selected) - {'Date', 'Provider identities'}
            if 'Date' in selected:
                names.update(('Year', 'Month', 'Day'))
            values = []
            for name in sorted(names):
                value = proposed.text(name)
                if value is None:
                    raise RepairError('comicinfo_value_unavailable')
                values.append((name, value))
            xml = merge_comicinfo(old, ComicInfoUpdates(session.identities[0], tuple(values), session.identities)) if selected else None
            fields = []
            for field in plan.metadata.fields:
                if field.field in names or field.field == 'Provider identities' and selected:
                    fields.append(field)
                else:
                    fields.append(MetadataFieldDelta(field.field, field.before, field.before, 'preserve'))
            changed = selected and any(f.action != 'preserve' for f in fields)
            metadata = MetadataIntent('merge' if old else 'add', tuple(fields), xml if changed else None,
                                       sha256(old.raw_bytes).hexdigest() if old else None)
            revised = replace(session, selected=tuple(sorted(selected)), revision=revision + 1,
                              plan=replace(plan, metadata=metadata, effects=plan.effects if changed else ()))
            if sum(len(repr(s).encode('utf-8')) for k, s in self._sessions.items() if k != identifier) + len(repr(revised).encode('utf-8')) > self.MAX_BYTES:
                raise RepairError('repair_review_size_limit')
            self._sessions[identifier] = revised
            return revised

    def apply(self, cursor, identifier, revision, digest, *, confirmed, expected_authority):
        if (confirmed is not True or type(revision) is not int or revision < 0
                or not isinstance(identifier, str) or not 1 <= len(identifier) <= 128
                or not isinstance(digest, str) or len(digest) != 64
                or not isinstance(expected_authority, AuthorityToken)):
            raise RepairError('explicit_repair_confirmation_required')
        correlation = 'comicinfo-repair:' + identifier + ':' + str(revision) + ':' + digest
        with self._lock:
            # Existing job is the durable retry identity, even after session loss.
            old = cursor.execute('SELECT id,intent,state FROM organization_jobs WHERE batch_id=?', (correlation,)).fetchall()
            if old:
                import json
                from dataclasses import asdict
                if len(old) != 1 or json.loads(old[0][1]).get('repair_authority') != asdict(expected_authority):
                    raise RepairError('repair_retry_identity_mismatch')
                return dict(state='already_applied' if old[0][2] == 'completed' else old[0][2],
                            job_id=old[0][0], recovery='Use existing job inspection/recovery')
            session = self.get(cursor, identifier)
            if session.revision != revision or session.digest != digest or session.authority != expected_authority:
                raise RepairError('stale_repair_confirmation')
            if not session.selected or session.plan.metadata.xml is None:
                return dict(state='no_changes', job_id=None)
            if dependencies(cursor, session.authority.volume_id):
                raise RepairError('repair_active_dependency')
            roots = tuple(r[0] for r in cursor.execute('SELECT folder FROM root_folders ORDER BY id'))
            executor = OrganizationExecutor(self.maintenance.database, roots, checkpoint=self.checkpoint)
            try:
                job_id = executor.create_job(session.plan, batch_id=correlation, repair_authority=session.authority,
                    repair_origin=dict(worklist_id=session.origin[0], revision=session.origin[1],
                                       manifest_digest=session.origin[2], finding_id=session.origin[3]))
                result = executor.apply_job(job_id)
                self._sessions.pop(identifier, None)
                return dict(state=result.state.value, job_id=job_id, error=result.error,
                            recovery='Journal forward recovery; no lossless archive undo')
            finally:
                executor.close()
