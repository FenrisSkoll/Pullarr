"""Safe transport projections over 8D/8G. No new mutation or review engine."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from backend.base.duplicate_review import DuplicateKind, DuplicateReviewError
from backend.base.metadata_repair import RepairError
from backend.features.comicinfo_repair import FIELDS
from backend.features.duplicate_quarantine_tasks import DuplicateQuarantineTask
from backend.features.metadata_repair_tasks import MetadataRepairTask
from backend.implementations.comicinfo import parse_comicinfo
from backend.internals.metadata_repair_history import by_session


class SpecializedActions:
    """Stateless adapter owned by the existing application action coordinator."""

    def __init__(self, runtime):
        self.runtime = runtime

    def connection(self, write=False):
        db = sqlite3.connect(Path(self.runtime.database).absolute().as_uri()
                             + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=10)
        db.execute('PRAGMA foreign_keys=ON')
        return closing(db)

    def repair_result(self, family, identifier, revision, digest):
        """Read-only exact durable lookup before consulting transient review."""
        with self.connection() as db:
            if family == 'metadata':
                receipt = by_session(db.cursor(), identifier)
                if receipt is None:
                    return dict(found=False)
                if (receipt['revision'], receipt['review_digest']) != (revision, digest):
                    raise RepairError('repair_retry_identity_mismatch')
                return dict(found=True, kind='metadata_receipt', id=receipt['id'], state='applied')
            correlation = f'comicinfo-repair:{identifier}:{revision}:{digest}'
            rows = db.execute('SELECT id,state FROM organization_jobs WHERE batch_id=? LIMIT 2', (correlation,)).fetchall()
            if not rows:
                return dict(found=False)
            if len(rows) != 1:
                raise RepairError('repair_retry_identity_mismatch')
            return dict(found=True, kind='organization', entry=self.runtime.history.get('organization', rows[0][0]))

    def repair_page(self, family, identifier, offset=0, limit=50):
        service = self.runtime.metadata if family == 'metadata' else self.runtime.comicinfo
        with self.connection() as db:
            session = service.get(db.cursor(), identifier)
        if family == 'metadata':
            page = session.page(offset, limit)
            effect = session.preview.view()
            page.update(id=identifier, offset=offset, limit=limit, selected_count=len(session.selected),
                provider=session.authority.provider, generation=session.authority.generation,
                origin=session.origin.view(), apply_available=effect['apply_available'],
                mutation_count=len(effect['changes']), blockers=effect['blockers'],
                classification_action=effect['classification']['action'], bibliography_action=effect['bibliography_action'])
            return page
        # Never call preview_plan/view: those contain raw XML and internal plans.
        proposed = parse_comicinfo(session.base_plan.metadata.xml)
        fields = []
        for name in sorted(FIELDS):
            supported = name == 'Provider identities' or (name == 'Date' and all(proposed.text(k) is not None for k in ('Year', 'Month', 'Day'))) or proposed.text(name) is not None
            members = ('Year', 'Month', 'Day') if name == 'Date' else (name,)
            deltas = [f for f in session.base_plan.metadata.fields if f.field in members]
            fields.append(dict(key=name, supported=supported, selected=name in session.selected,
                changes=[dict(field=f.field, before=f.before, after=f.after, action=f.action) for f in deltas]))
        return dict(id=identifier, revision=session.revision, digest=session.digest, origin=session.origin,
            source=session.plan.source_path, provider=session.authority.provider, generation=session.authority.generation,
            selected=session.selected, items=fields[offset:offset + limit], total=len(fields), offset=offset, limit=limit,
            apply_available=bool(session.selected and session.plan.metadata.xml),
            lossless_inverse=False, unknown_metadata_preserved=True)

    def execute(self, operation, payload):
        family, verb = operation.split('_', 1)
        if family == 'duplicate':
            return self.duplicate_execute(verb, payload)
        service = self.runtime.metadata if family == 'metadata' else self.runtime.comicinfo
        if verb == 'create' and family == 'metadata':
            task = MetadataRepairTask(service, handoff=(payload['worklist_id'], payload['revision'], payload['digest'], tuple(payload['selected'])))
            task.run()
            return dict(kind='metadata_review', id=task.result.id)
        with self.connection(write=verb == 'apply') as db:
            cursor = db.cursor()
            if verb == 'create':
                session = service.create(cursor, payload['worklist_id'], payload['revision'], payload['digest'], payload['finding_id'])
            elif verb == 'revise':
                session = service.get(cursor, payload['id'])
                if family == 'metadata':
                    allowed = {f.selection.key: f.selection for f in session.fields if f.support == 'supported'}
                    keys = set(session.selected)
                    for edit in payload['edits']:
                        if edit['key'] not in allowed:
                            raise RepairError('unsupported_repair_field')
                        if edit['selected']:
                            keys.add(edit['key'])
                        else:
                            keys.discard(edit['key'])
                    selection = tuple(allowed[key] for key in sorted(keys))
                else:
                    selection = tuple(payload['selected'])
                session = service.revise(cursor, payload['id'], payload['revision'], selection)
            else:
                previous = self.repair_result(family, payload['id'], payload['revision'], payload['digest'])
                if previous['found']:
                    return previous
                session = service.get(cursor, payload['id'])
                # Authority is server-owned; exact digest binds origin/selection.
                result = service.apply(cursor, payload['id'], payload['revision'], payload['digest'],
                    confirmed=True, expected_authority=session.authority)
                if result['state'] == 'no_changes':
                    return dict(kind=family, state='no_changes')
                if family == 'metadata':
                    return dict(kind='metadata_receipt', id=result['id'], state=result['state'])
                return dict(kind='organization', entry=self.runtime.history.get('organization', result['job_id']))
        return dict(kind=family + '_review', id=session.id)

    def duplicate_execute(self, verb, payload):
        if verb == 'create':
            session = self.runtime.duplicates.create(payload['worklist_id'], payload['revision'], payload['digest'], tuple(payload['selected']))
        elif verb == 'revise':
            session = self.runtime.duplicates.revise(payload['id'], payload['revision'], tuple(payload['choices']))
        elif verb == 'prepare':
            with self.connection() as db:
                session = self.runtime.quarantine.prepare(db.cursor(), payload['id'], payload['revision'])
        else:
            task = DuplicateQuarantineTask(self.runtime.quarantine, payload['id'], payload['revision'], payload['digest'],
                origin=tuple(payload['origin']), selected=tuple(payload['selected']), confirmed=True)
            task.run()
            return dict(kind='duplicate_batch', id=task.result['batch_id'], state=task.result['state'])
        return dict(kind='duplicate_review', id=session.id)

    def duplicate_page(self, identifier, offset=0, limit=50, group_id=None):
        session = self.runtime.duplicates.get(identifier)
        if group_id is not None:
            detail = session.detail(group_id, offset, limit)
            # No execution intents, targets, locations or raw historical evidence.
            return dict(group=detail['group'], total=detail['total'], offset=offset, limit=limit,
                impact=detail['impact'], members=[dict(id=f['id'], path=f['filepath'], size=f['size'],
                    hash_verified=f['hash'] is not None, direct_issue_ids=[r['issue_id'] for r in f['direct']],
                    general_volume_ids=[r['volume_id'] for r in f['general']],
                    coverage_count=len(f['coverage']), blockers=f['blockers']) for f in detail['members']])
        page = session.page(offset, limit)
        summary = session.summary()
        groups = []
        for group, view in zip(session.groups[offset:offset + limit], page['items']):
            groups.append(dict(view, quarantine_choice_available=group.kind == DuplicateKind.EXACT,
                requires_prepare='quarantine_recovery_not_implemented' in group.blockers))
        removed = sum(len(g.quarantine) for g in session.groups)
        intents = json.loads(session.execution_json)
        return dict(summary, items=groups, total=page['total'], offset=offset, limit=limit,
            selected=[g.id for g in session.groups], mutation_count=removed,
            prepare_available=bool(removed) and not session.stale and not any(
                set(g.blockers) - {'quarantine_recovery_not_implemented'} for g in session.groups if g.quarantine),
            journal_bytes=sum(len(json.dumps(i).encode()) for i in intents),
            batch_id=self.runtime.quarantine.correlation(session.id, session.revision, session.digest))
