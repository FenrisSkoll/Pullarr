"""Bounded canonical evidence acquisition and explicit transactional writes."""

from typing import Any, Collection, Optional, Tuple

from backend.base.issue_facts import (BibliographicDate, DateKind,
                                      DatePrecision, IssueFacts,
                                      IssueNumberFacts, IssueRecord,
                                      NumberKind, VariantOf)


def mapped_facts(label: Optional[str], day: Optional[str], provenance: str) -> IssueFacts:
    """Mapped value only: no claim to reconstruct a discarded provider field."""
    dates = (() if day is None else (BibliographicDate.interpret(
        day, DateKind.LEGACY_SELECTED, provenance, 'date'),))
    return IssueFacts(IssueNumberFacts.interpret(label, provenance, 'issue_number'),
                      dates, 'date' if dates else None)


def write_facts(cursor: Any, issue_id: int, facts: IssueFacts) -> None:
    """Caller owns transaction. Replace this authority's snapshot, never IDs."""
    n = facts.number
    cursor.execute('''INSERT INTO issue_number_facts VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(issue_id) DO UPDATE SET raw_label=excluded.raw_label,
        provenance=excluded.provenance,source_field=excluded.source_field,
        interpretation=excluded.interpretation,numeric_text=excluded.numeric_text,
        policy=excluded.policy,selected_date_field=excluded.selected_date_field,
        provider_ordinal=excluded.provider_ordinal,ordinal_provenance=excluded.ordinal_provenance''',
        (issue_id, n.raw_label, n.provenance, n.source_field, n.interpretation.value,
         n.numeric_text, n.policy, facts.selected_date_field, facts.provider_ordinal, facts.ordinal_provenance))
    cursor.execute('DELETE FROM issue_date_facts WHERE issue_id=?', (issue_id,))
    cursor.executemany('INSERT INTO issue_date_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (
        (issue_id, d.source_field, d.raw_value, d.kind.value, d.provenance, d.year,
         d.month, d.day, d.precision.value, int(d.zero_placeholders), d.uncertainty, d.policy)
        for d in facts.dates))


def load_records(cursor: Any, *, volume_id: Optional[int] = None,
                 issue_ids: Optional[Collection[int]] = None) -> Tuple[IssueRecord, ...]:
    """Three SELECTs per 750-ID chunk, or three for an entire volume.

Missing facts stay missing. This read must not backfill or infer provenance.
"""
    if (volume_id is None) == (issue_ids is None):
        raise ValueError('Supply exactly one explicit issue scope')
    if issue_ids is not None:
        ids = tuple(sorted(set(issue_ids)))
        if any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError('Positive local IDs required')
        if len(ids) > 750:
            cursor.execute('SAVEPOINT issue_facts_batch_read')
            try:
                return tuple(r for start in range(0, len(ids), 750)
                             for r in load_records(cursor, issue_ids=ids[start:start + 750]))
            finally:
                cursor.execute('RELEASE issue_facts_batch_read')
        if not ids:
            return ()
        scope = 'i.id IN (' + ','.join('?' for _ in ids) + ')'
        params = ids
    else:
        if type(volume_id) is not int or volume_id <= 0:
            raise ValueError('Positive volume ID required')
        scope, params = 'i.volume_id=?', (volume_id,)
    cursor.execute('SAVEPOINT issue_facts_read')
    try:
        rows = cursor.execute('''SELECT i.id,i.volume_id,i.issue_number,i.calculated_issue_number,i.date,
            n.raw_label,n.provenance,n.source_field,n.interpretation,n.numeric_text,n.policy,
            n.selected_date_field,n.provider_ordinal,n.ordinal_provenance,
            v.base_provider,v.base_provider_id,v.provenance
            FROM issues i LEFT JOIN issue_number_facts n ON n.issue_id=i.id
            LEFT JOIN issue_variant_of v ON v.issue_id=i.id WHERE ''' + scope + ' ORDER BY i.id', params).fetchall()
        dates = cursor.execute('''SELECT d.* FROM issue_date_facts d JOIN issues i ON i.id=d.issue_id
            WHERE ''' + scope + ' ORDER BY i.id,d.source_field', params).fetchall()
        references = cursor.execute('''SELECT e.issue_id,e.provider,e.provider_id FROM issue_external_ids e
            JOIN issues i ON i.id=e.issue_id WHERE ''' + scope + ' ORDER BY i.id,e.provider,e.provider_id', params).fetchall()
    finally:
        cursor.execute('RELEASE issue_facts_read')
    by_date: dict[int, list[BibliographicDate]] = {}
    for iid, field, raw, kind, provenance, year, month, day, precision, zeros, uncertainty, policy in dates:
        by_date.setdefault(iid, []).append(BibliographicDate(raw, DateKind(kind), field, provenance,
            year, month, day, DatePrecision(precision), bool(zeros), uncertainty, policy))
    by_ref: dict[int, list[Tuple[str, str]]] = {}
    for iid, provider, pid in references:
        by_ref.setdefault(iid, []).append((provider, pid))
    output = []
    for iid, vid, label, legacy, day, raw, provenance, field, kind, numeric, policy, selected, ordinal, ordinal_source, base_provider, base_id, relation_source in rows:
        facts = None if provenance is None else IssueFacts(
            IssueNumberFacts(raw, provenance, field, NumberKind(kind), numeric, policy),
            tuple(by_date.get(iid, ())), selected, ordinal, ordinal_source)
        output.append(IssueRecord(iid, vid, tuple(by_ref.get(iid, ())), facts, label, legacy, day,
            None if base_provider is None else VariantOf(base_provider, base_id, relation_source)))
    return tuple(output)
