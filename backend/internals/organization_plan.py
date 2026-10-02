"""Read-only planning records. No model constructors that repair or mutate DB."""

from dataclasses import fields
from typing import Any, Collection

from backend.base.naming_policy import NamingSettings
from backend.base.organization_plan import (AssociationLink, PlanningFile,
                                            PlanningIssue, PlanningVolume)
from backend.internals.db import get_db
from backend.internals.identification import load_matching_records


def load_planning_records(registered_providers: Collection[str], cursor: Any = None):
    cursor = get_db() if cursor is None else cursor
    cursor.execute('SAVEPOINT organizer_planning_read')
    try:
        volumes, issues = load_matching_records(registered_providers, cursor)
        extras = cursor.execute('SELECT id,root_folder,folder,custom_folder,comicvine_id FROM volumes ORDER BY id').fetchall()
        issue_extras = cursor.execute('''SELECT i.id,i.title,
            CASE WHEN n.selected_date_field IS NULL THEN i.date
                 WHEN d.uncertainty IS NOT NULL THEN NULL ELSE d.raw_value END,
            i.description,i.comicvine_id
            FROM issues i LEFT JOIN issue_number_facts n ON n.issue_id=i.id
            LEFT JOIN issue_date_facts d ON d.issue_id=i.id AND d.source_field=n.selected_date_field
            ORDER BY i.id''').fetchall()
        roots = tuple(cursor.execute('SELECT id,folder FROM root_folders ORDER BY id').fetchall())
        files = cursor.execute('SELECT id,filepath FROM active_files ORDER BY id').fetchall()
        links = cursor.execute('''SELECT b.file_id,i.volume_id,i.id,b.forced,NULL FROM issues_files b
            JOIN issues i ON i.id=b.issue_id UNION ALL
            SELECT file_id,volume_id,NULL,forced,file_type FROM volume_files''').fetchall()
        keys = tuple(f.name for f in fields(NamingSettings))
        values = dict(cursor.execute('SELECT key,value FROM config WHERE key IN (' + ','.join('?' for _ in keys) + ')', keys).fetchall())
    finally:
        cursor.execute('RELEASE SAVEPOINT organizer_planning_read')
    # Config is required, never initialized or silently repaired by a preview.
    for key in ('replace_illegal_characters', 'long_special_version'):
        values[key] = bool(int(values[key]))
    for key in ('volume_padding', 'issue_padding'):
        values[key] = int(values[key])
    naming = NamingSettings(**values)
    volume_index = {v.id: v for v in volumes}
    issue_index = {i.id: i for i in issues}
    bindings = {}
    general = {}
    for fid, vid, iid, forced, file_type in links:
        if iid is None:
            general.setdefault(fid, []).append((vid, bool(forced), file_type))
        else:
            bindings.setdefault(fid, []).append(AssociationLink(vid, iid, bool(forced)))
    return (tuple(PlanningVolume(volume_index[vid], root, folder, bool(custom), cv) for vid, root, folder, custom, cv in extras),
            tuple(PlanningIssue(issue_index[iid], title, day, description, cv) for iid, title, day, description, cv in issue_extras),
            roots, tuple(PlanningFile(fid, path, tuple(sorted(bindings.get(fid, ()))), tuple(sorted(g[0] for g in general.get(fid, ()))),
                                      tuple(sorted(general.get(fid, ())))) for fid, path in files), naming)
