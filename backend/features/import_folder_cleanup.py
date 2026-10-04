"""Optional immediate-source-directory retirement; never recursive cleanup."""
import os
from pathlib import Path

from backend.base.organization_job import OrganizationError
from backend.implementations.organization_filesystem import (safe_path,
                                                             sync_directory)
from backend.internals.organization_jobs import read_intent
from backend.internals.organization_reservations import load_reservations
from backend.internals.scan_mutation import _writer_mutation


def cleanup_sources(executor, session, outcomes):
    db=executor.store.db
    enabled=db.execute("SELECT value FROM config WHERE key='delete_empty_folders'").fetchone()
    if not enabled or str(enabled[0]) != '1':
        return
    completed={o['source'] for o in outcomes if o['state']=='completed'}
    required={p.source_path for p in session.batch.plans} | {r['source'] for r in session.reviews}
    key=lambda p: os.path.normcase(os.path.realpath(p))
    for folder, identity in session.source_directories.items():
        children={p for p in required if str(Path(p).parent)==folder}
        if not children or not children.issubset(completed):
            continue
        try:
            with _writer_mutation(db.cursor()):
                protected=[r[0] for r in db.execute("SELECT folder FROM root_folders UNION SELECT folder FROM volumes WHERE folder<>''")]
                if key(folder) in {key(p) for p in protected} or load_reservations(db.cursor()).conflicts(folder):
                    continue
                live=db.execute("SELECT id FROM organization_jobs WHERE state<>'completed'").fetchall()
                if any(any(p and (key(p)==key(folder) or key(folder) in {key(a) for a in Path(p).parents})
                           for p in (intent.get('source'),intent.get('target'),intent.get('folder')))
                       for intent in (read_intent(db,row[0]) for row in live)):
                    continue
                safe_path(folder)
                stat=os.stat(folder,follow_symlinks=False)
                if (stat.st_dev,stat.st_ino)!=identity:
                    continue
                # rmdir is exclusive of ANY remaining content, including hidden files.
                os.rmdir(folder)
                sync_directory(str(Path(folder).parent))
        except (OSError, OrganizationError):
            continue  # optional cleanup never changes the successful import result
