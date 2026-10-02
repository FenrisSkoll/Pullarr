"""Acquire a coherent matching snapshot with four read-only SELECTs."""

from typing import Any, Collection, Optional, Tuple

from backend.base.definitions import SpecialVersion
from backend.base.identification import LocalMatchIssue, LocalMatchVolume
from backend.base.import_candidate import ProviderReference, ResourceKind
from backend.base.issue_facts import IssueNumberFacts, NumberKind
from backend.internals.db import get_db
from backend.internals.provider_identity import MetadataIdentityError


def load_matching_records(registered_providers: Collection[str], cursor: Any = None,
                          *, volume_ids: Optional[Collection[int]] = None) -> Tuple[
    Tuple[LocalMatchVolume, ...], Tuple[LocalMatchIssue, ...]
]:
    cursor = get_db() if cursor is None else cursor
    ids = () if volume_ids is None else tuple(sorted(set(volume_ids)))
    if volume_ids is not None and (not ids or len(ids) > 1000 or any(type(i) is not int or i <= 0 for i in ids)):
        raise ValueError('Explicit bounded local publication scope required')
    slots = ','.join('?' for _ in ids)
    volume_scope = '' if volume_ids is None else f' WHERE id IN ({slots})'
    child_scope = '' if volume_ids is None else f' WHERE volume_id IN ({slots})'
    reference_scope = '' if volume_ids is None else f' WHERE issue_id IN (SELECT id FROM issues WHERE volume_id IN ({slots}))'
    # Nest safely in a caller transaction. Never commit unrelated pending work.
    cursor.execute('SAVEPOINT organizer_matching_read')
    try:
        volumes = cursor.execute('''SELECT id,title,year,volume_number,publisher,
            special_version,metadata_provider,comicvine_id,last_cv_fetch FROM volumes''' + volume_scope + ' ORDER BY id', ids).fetchall()
        issues = cursor.execute('''SELECT i.id,i.volume_id,i.issue_number,i.calculated_issue_number,
            n.raw_label,n.provenance,n.source_field,n.interpretation,n.numeric_text,n.policy
            FROM issues i LEFT JOIN issue_number_facts n ON n.issue_id=i.id'''
            + child_scope + ' ORDER BY i.id', ids).fetchall()
        volume_refs = cursor.execute('''SELECT volume_id,provider,provider_id,last_fetch
            FROM volume_external_ids''' + child_scope + ' ORDER BY volume_id,provider', ids).fetchall()
        issue_refs = cursor.execute('''SELECT issue_id,provider,provider_id
            FROM issue_external_ids''' + reference_scope + ' ORDER BY issue_id,provider', ids).fetchall()
    finally:
        cursor.execute('RELEASE SAVEPOINT organizer_matching_read')
    vr = {}
    for vid, provider, pid, fetched in volume_refs:
        vr.setdefault(vid, []).append((ProviderReference(provider, ResourceKind.VOLUME, pid), fetched))
    ir = {}
    for iid, provider, pid in issue_refs:
        ir.setdefault(iid, []).append(ProviderReference(provider, ResourceKind.ISSUE, pid))
    result = []
    for vid, title, year, number, publisher, special, provider, cv_id, cv_fetch in volumes:
        selected = [(ref, fetched) for ref, fetched in vr.get(vid, ()) if ref.provider == provider]
        if provider not in registered_providers or len(selected) != 1:
            raise MetadataIdentityError('Missing or unregistered selected volume identity')
        authority, fetched = selected[0]
        if provider == 'comicvine' and (str(cv_id) != authority.provider_id or fetched != cv_fetch):
            raise MetadataIdentityError('ComicVine volume identity shadow conflict')
        result.append(LocalMatchVolume(vid, authority, title, year, number, publisher,
                                      SpecialVersion(special), tuple(ref for ref, _ in vr[vid])))
    return tuple(result), tuple(LocalMatchIssue(iid, vid, (label or '') if provenance is not None else raw, calculated, tuple(ir.get(iid, ())),
        None if provenance is None else IssueNumberFacts(label, provenance, field, NumberKind(kind), numeric, policy))
        for iid, vid, raw, calculated, label, provenance, field, kind, numeric, policy in issues)
