"""Trusted internal history facade. No HTTP, queue or mutation authority."""

import json

from backend.base.maintenance_history import (HistoryError,
                                              HistoryFilter, page_limit)
from backend.internals import (classification_provenance, content_claims,
                               maintenance_history, metadata_repair_history,
                               provider_switch_history)
from backend.internals.organization_jobs import read_intent


class MaintenanceHistory:
    def __init__(self, database: str):
        self.database = database  # Application configuration, never client input.

    def page(self, filters=HistoryFilter(), *, before=None, limit=50):
        with maintenance_history.connection(self.database) as db:
            return maintenance_history.page(db, filters, before=before, limit=limit)

    def get(self, domain, identifier):
        with maintenance_history.connection(self.database) as db:
            return maintenance_history.entry(db, domain, identifier)

    def batch(self, identifier, *, offset=0, limit=50):
        with maintenance_history.connection(self.database) as db:
            return maintenance_history.batch(db, identifier, offset=offset, limit=limit)

    def detail(self, domain, identifier, *, offset=0, limit=50):
        page_limit(limit)
        if type(offset) is not int or not 0 <= offset <= 40000:
            raise HistoryError('invalid_history_page')
        with maintenance_history.connection(self.database) as db:
            summary = maintenance_history.entry(db, domain, identifier)
            cursor = db.cursor()
            if domain == 'metadata_repair':
                detail = dict(receipt=metadata_repair_history.get_receipt(cursor, identifier),
                    fields=metadata_repair_history.field_page(cursor, identifier, offset=offset, limit=limit))
            elif domain == 'provider_switch':
                detail = provider_switch_history.receipt(cursor, identifier, detail=True, offset=offset, limit=limit)
            elif domain == 'content_claim':
                detail = content_claims.claim_history(cursor, identifier)
            elif domain == 'content_coverage':
                row = db.execute('''SELECT c.*,v.id IS NOT NULL AS currently_valid
                    FROM file_content_coverage c LEFT JOIN valid_file_content_coverage v ON v.id=c.id
                    WHERE c.id=?''', (identifier,)).fetchone()
                detail = dict(coverage=dict(row))
            elif domain == 'organization':
                # Journal detail is deliberately allowlisted. XML, prepared
                # archives and arbitrary event detail are not UI projections.
                steps = [dict(row) for row in db.execute('''SELECT ordinal,kind,state
                    FROM organization_steps WHERE job_id=? ORDER BY ordinal LIMIT ? OFFSET ?''',
                    (identifier, limit, offset))]
                events = [dict(row) for row in db.execute('''SELECT id,ordinal,created_at,event
                    FROM organization_events WHERE job_id=? ORDER BY id DESC LIMIT ? OFFSET ?''',
                    (identifier, limit, offset))]
                detail = dict(steps=steps, events=events, artifact_eligibility='not_checked',
                    recorded_intent=self._intent_detail(db, identifier, offset, limit),
                    authoritative_intent_key=['organization_jobs', identifier])
            elif domain == 'intake':
                artifacts = [dict(row) for row in db.execute('''SELECT a.id,a.state,
                    a.organization_job_id,a.final_file_id,j.state AS job_state
                    FROM acquisition_artifacts a LEFT JOIN organization_jobs j ON j.id=a.organization_job_id
                    WHERE a.intake_id=? ORDER BY a.id LIMIT ? OFFSET ?''', (identifier, limit, offset))]
                detail = dict(artifacts=artifacts, operational_only=True)
            else:
                raise HistoryError('unsupported_history_domain')
            return maintenance_history.bounded(dict(entry=summary, detail=detail,
                page=dict(offset=offset, limit=limit)), maintenance_history.MAX_DETAIL_BYTES)

    def classification(self, volume_id):
        """Current provenance only: not invented append-only history or undo."""
        if type(volume_id) is not int or volume_id <= 0:
            raise HistoryError('invalid_history_volume')
        with maintenance_history.connection(self.database) as db:
            result = classification_provenance.details(db.cursor(), volume_id)
            return maintenance_history.bounded(dict(current_provenance=result,
                inverse_capability='unsupported', complete_history=False))

    @staticmethod
    def _intent_detail(db, identifier, offset, limit):
        """Historical identity/path effects only, never XML or raw snapshots."""
        value = read_intent(db, identifier)
        if (value.get('directory_effect', 'volume-tree/v1') != 'volume-tree/v1'
                or value.get('quarantine_effect', 'retained-artifact/v1') != 'retained-artifact/v1'):
            raise HistoryError('unsupported_history_version')
        result = dict(version=value['version'], effects=value['effects'],
                      inverse=bool(value.get('inverse')), volume_id=value.get('volume_id'))
        if 'archive_effect' in value:
            result.update(file_id=value['file_id'], source=value['original'], target=value['target'],
                receipt=value['receipt'], shared_source=value['sharing']['shared'],
                old_hash=value['old']['sha256'], new_hash=value['incoming']['sha256'],
                old_size=value['old']['size'], new_size=value['incoming']['size'])
        elif 'directory_effect' in value:
            before = json.loads(value['tree_database_before'])
            after = json.loads(value['tree_database_after'])
            files = list(zip(before['files'], after['files']))
            result.update(root_id=before['volume']['root_folder'],
                custom_before=before['volume']['custom_folder'], custom_after=after['volume']['custom_folder'],
                file_count=len(files), issue_count=len(before['issues']),
                issue_ids=[i['id'] for i in before['issues'][offset:offset + limit]],
                files=[dict(file_id=a['id'], source=a['filepath'], target=b['filepath'])
                       for a, b in files[offset:offset + limit]])
        elif 'quarantine_effect' in value:
            before = value['quarantine_before']
            fid = value['file_id']
            direct = [r for r in before['direct'] if r['file_id'] == fid]
            general = [r for r in before['general'] if r['file_id'] == fid]
            result.update(file_id=fid, original_path=value['original'], root_id=value['root_id'],
                direct_count=len(direct), general_count=len(general),
                direct_links=direct[offset:offset + limit], general_links=general[offset:offset + limit],
                retained_ids=value['retained_ids'][offset:offset + limit],
                internal_storage_hidden=True)
        else:
            before = value.get('database_before', {}).get('file') or {}
            restored = value.get('database_restore', {}).get('file') or {}
            direct = before.get('links', [])
            desired = restored.get('links', []) if value.get('inverse') else value.get('links_after', [])
            result.update(file_id=before.get('id'), direct_before_count=len(direct),
                direct_after_count=len(desired), direct_before=direct[offset:offset + limit],
                direct_after=desired[offset:offset + limit],
                general_before=before.get('general', [])[offset:offset + limit],
                comicinfo_original_bytes_retained=False if value.get('xml') is not None else None)
        return result
