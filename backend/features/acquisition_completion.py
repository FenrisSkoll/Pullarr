"""Production completion adapters. Persist provenance; never import inline."""

import json
from time import time
from uuid import uuid4

from backend.base.acquisition_intake import (AcquisitionCompletion,
                                             AcquisitionKind)
from backend.internals.acquisition_intakes import (completion_receipt,
                                                   ensure_intake)
from backend.internals.organization_jobs import canonical, now


def intake_settings(db) -> tuple[bool, dict]:
    values = dict(db.execute('''SELECT key,value FROM config WHERE key IN
        ('rename_downloaded_files','convert','format_preference','extract_issue_ranges',
         'change_file_date','chmod_folder','chown_group')'''))
    rename = str(values.get('rename_downloaded_files', '0')).lower() in ('1', 'true')
    return rename, values


def ensure_sab_completion(db, row: dict) -> str | None:
    """Called in the SAB checkpoint transaction or bounded restart repair."""
    if row['state'] != 'completed':
        return None
    intent, observation = json.loads(row['intent']), json.loads(row['observation'])
    kind = AcquisitionKind(intent.get('client_kind', 'sabnzbd'))
    completion = AcquisitionCompletion(kind, row['id'], intent['candidate_id'],
        intent['volume_id'], tuple(intent['issue_ids']),
        tuple(observation.get('paths') or ((observation['storage'],) if observation.get('storage') else ())), row['completed_at'],
        title=intent['title'], source_key=intent['source_key'], client_id=row['client_id'],
        client_instance=row['client_instance'], remote_job_id=row['nzo_id'],
        evaluation_id=intent['evaluation_id'], mechanism=intent.get('protocol', 'nzb'))
    rename, preparation = intake_settings(db)
    identifier = ensure_intake(db, completion, rename=rename, auto_apply=True)
    db.execute("UPDATE acquisition_intakes SET preparation=? WHERE id=? AND preparation='{}'",
               (canonical(preparation), identifier))
    return identifier


def repair_sab_completions(db, limit: int = 16) -> int:
    rows = db.execute('''SELECT d.* FROM acquisition_downloads d
        LEFT JOIN acquisition_intakes i ON i.kind=COALESCE(json_extract(d.intent,'$.client_kind'),'sabnzbd') AND i.download_id=d.id
        WHERE d.state='completed' AND i.id IS NULL ORDER BY d.completed_at,d.id LIMIT ?''', (limit,)).fetchall()
    columns = [d[0] for d in db.execute('SELECT * FROM acquisition_downloads LIMIT 0').description]
    for row in rows:
        ensure_sab_completion(db, dict(zip(columns, row)))
    return len(rows)


def handoff_direct_download(download) -> str:
    """Exact successful worker output, persisted before queue ownership release.

    No move, scan, extraction, issue lookup, provider call or recursive cleanup.
    A failed transaction leaves the queue/provenance and artifact intact.
    """
    from backend.internals.db import get_db
    from backend.internals.settings import Settings

    receipt = download.selected_release
    if not receipt or receipt.get('version') != 'ddl-selection/v1':
        raise ValueError('Unified selection receipt required')
    cursor = get_db()
    if 'completion' not in receipt:
        completion_id = receipt.get('completion_id') or uuid4().hex
        completion = AcquisitionCompletion(AcquisitionKind.DIRECT_DOWNLOAD, completion_id,
            receipt['candidate_id'], download.volume_id, tuple(receipt['target_ids']),
            tuple(download.files), now(), title=download.web_title or '',
            source_key=receipt['source'], evaluation_id=receipt['evaluation_id'],
            offering_id=receipt['offering_id'], forced=receipt['forced'],
            original_state=receipt['original_state'], queue_id=download.id,
            mechanism=download.download_service.value)
        rename, preparation = intake_settings(cursor)
        receipt = dict(receipt, completion_id=completion_id, completion=completion_receipt(completion),
                       local_root=Settings().sv.download_folder, rename=rename, preparation=preparation,
                       file_title=download.title, issue_id=download.issue_id)
        cursor.execute('UPDATE download_queue SET covered_issues=? WHERE id=?', (canonical(receipt), download.id))
        cursor.connection.commit()
        download.selected_release = receipt
    return handoff_queued_completion(cursor, download.id, receipt)


def handoff_queued_completion(cursor, queue_id: int, receipt: dict) -> str:
    """Restart-safe: a completed queue row never starts its download again."""
    data = dict(receipt['completion'])
    data['kind'] = AcquisitionKind(data['kind'])
    data['issue_ids'] = tuple(data['issue_ids'])
    data['reported_paths'] = tuple(data['reported_paths'])
    completion = AcquisitionCompletion(**data)
    cursor.execute('SAVEPOINT ddl_intake_handoff')
    released = False
    try:
        existing = cursor.execute("SELECT id FROM acquisition_intakes WHERE kind='direct_download' AND download_id=?",
                                  (completion.download_id,)).fetchone()
        identifier = ensure_intake(cursor, completion, rename=receipt['rename'], auto_apply=True,
                                   local_root=receipt['local_root'])
        if not existing:
            cursor.execute('UPDATE acquisition_intakes SET preparation=? WHERE id=?',
                           (canonical(receipt['preparation']), identifier))
            # Keep legacy history presentation, without source or mirror URLs.
            cursor.execute('''INSERT INTO download_history
                (web_link,web_title,web_sub_title,file_title,volume_id,issue_id,source,downloaded_at,success)
                VALUES(NULL,?,NULL,?,?,?,?,?,1)''', (completion.title, receipt['file_title'],
                completion.volume_id, receipt['issue_id'], completion.mechanism, round(time())))
        cursor.execute('DELETE FROM download_queue WHERE id=?', (queue_id,))
        cursor.execute('RELEASE ddl_intake_handoff')
        released = True
        cursor.connection.commit()
        return identifier
    except BaseException:
        if released:
            cursor.connection.rollback()
        else:
            cursor.execute('ROLLBACK TO ddl_intake_handoff')
            cursor.execute('RELEASE ddl_intake_handoff')
        raise
