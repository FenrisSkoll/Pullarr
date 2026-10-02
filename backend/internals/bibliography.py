"""Normalized bibliography writes and bounded, local-only read projections."""

import json
from dataclasses import asdict, fields
from hashlib import sha256

from backend.base.bibliography import POLICY, EditionFacts, PublicationFacts


def update_scalars(cursor, table, key, owner, provider, value):
    if (table, key) not in (('issue_bibliography', 'issue_id'), ('volume_bibliography', 'volume_id')):
        raise ValueError('Unsupported bibliography owner')
    kind = EditionFacts if table == 'issue_bibliography' else PublicationFacts
    allowed = {f.name for f in fields(kind)} - {'supplied', 'diagnostics'}
    supplied = set(value.supplied)
    if not supplied <= allowed:
        raise ValueError('Unsupported bibliography field')
    if 'isbn' in supplied:
        supplied.update(('isbn_normalized', 'isbn_validity'))
    if 'page_count' in supplied:
        supplied.add('page_count_numeric')
    cursor.execute(f'INSERT OR IGNORE INTO {table}({key},provider,policy) VALUES(?,?,?)',
                   (owner, provider, POLICY))
    names = sorted(supplied)
    if names:
        cursor.execute(f'UPDATE {table} SET ' + ','.join(name + '=?' for name in names)
                       + f' WHERE {key}=? AND provider=?',
                       (*(getattr(value, name) for name in names), owner, provider))


def persist_bibliography(cursor, snapshot, volume_id, owners):
    """Caller owns the admitted core metadata transaction; no HTTP/commit here."""
    if not cursor.connection.in_transaction:
        raise ValueError('Bibliography requires caller snapshot transaction')
    provider = snapshot.volume.provider
    previous = cursor.execute('SELECT provider FROM volume_bibliography WHERE volume_id=?',
                              (volume_id,)).fetchone()
    existing = cursor.execute('''SELECT b.issue_id,b.provider FROM issue_bibliography b
        JOIN issues i ON i.id=b.issue_id WHERE i.volume_id=?''', (volume_id,)).fetchall()
    if (previous is not None and previous[0] != provider) or any(p != provider for _, p in existing):
        raise ValueError('Bibliography authority conflict')
    if snapshot.publication is not None:
        update_scalars(cursor, 'volume_bibliography', 'volume_id', volume_id, provider, snapshot.publication)
        cursor.execute('DELETE FROM publication_bibliography_diagnostics WHERE volume_id=?', (volume_id,))
        cursor.executemany('INSERT INTO publication_bibliography_diagnostics VALUES(?,?)',
            ((volume_id, code) for code in sorted(set(snapshot.publication.diagnostics))))
    latest = {iid: (digest, scope, policy) for iid, digest, scope, policy in cursor.execute('''
        SELECT s.issue_id,s.digest,s.scope,s.policy FROM story_observation_sets s
        JOIN issues i ON i.id=s.issue_id WHERE i.volume_id=? AND s.id=(
            SELECT MAX(t.id) FROM story_observation_sets t WHERE t.issue_id=s.issue_id)''', (volume_id,))}
    for issue in snapshot.issues:
        bibliography = issue.bibliography
        if bibliography is None:
            continue  # No evidence supplied, not removal permission.
        iid = owners[issue.provider_id]
        update_scalars(cursor, 'issue_bibliography', 'issue_id', iid, provider, bibliography.edition)
        cursor.execute('DELETE FROM bibliography_diagnostics WHERE issue_id=?', (iid,))
        cursor.executemany('INSERT INTO bibliography_diagnostics VALUES(?,?)',
                           ((iid, code) for code in sorted(set(bibliography.diagnostics))))
        # Whole-set change detection only. Never a story identity or equivalence.
        digest = sha256(json.dumps([asdict(s) for s in bibliography.stories],
            sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
        if latest.get(iid) == (digest, bibliography.story_scope, bibliography.policy):
            continue
        sid = cursor.execute('''INSERT INTO story_observation_sets(
            issue_id,provider,policy,scope,digest,observed_at,observation_count) VALUES(?,?,?,?,?,?,?)''',
            (iid, provider, bibliography.policy, bibliography.story_scope, digest,
             snapshot.receipt.acquired_at, len(bibliography.stories))).lastrowid
        for position, story in enumerate(bibliography.stories):
            oid = cursor.execute('''INSERT INTO story_observations(set_id,position,source_position,mode,
                provider_story_id,sequence,story_type,title,feature,page_count,page_count_numeric,characters,genre)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (sid, position, story.source_position, story.mode.value, story.provider_story_id,
                 story.sequence, story.story_type, story.title, story.feature, story.page_count,
                 story.page_count_numeric, story.characters, story.genre)).lastrowid
            cursor.executemany('INSERT INTO story_credit_observations VALUES(?,?,?)',
                               ((oid, c.role, c.text) for c in story.credits))


def summaries(cursor, volume_id):
    """One indexed SELECT, never story text/credits for volume lists."""
    return {row['issue_id']: dict(row) for row in cursor.execute('''SELECT b.issue_id,b.provider,
        b.isbn,b.barcode,b.page_count,b.page_count_numeric,b.variant_name,
        (b.provider=v.metadata_provider) AS selected_provider_current,
        (b.cover_reference IS NOT NULL AND b.cover_reference!='') AS cover_available,
        (SELECT observation_count FROM story_observation_sets s WHERE s.issue_id=b.issue_id
            ORDER BY s.id DESC LIMIT 1) AS story_count
        FROM issue_bibliography b JOIN issues i ON i.id=b.issue_id
        JOIN volumes v ON v.id=i.volume_id WHERE i.volume_id=?''', (volume_id,))}


def detail(cursor, issue_id, set_id=None):
    """Small fixed number of queries independent of story count. No remote IO."""
    row = cursor.execute('SELECT * FROM issue_bibliography WHERE issue_id=?', (issue_id,)).fetchone()
    result = dict(schema='issue-bibliography/v1', issue_id=issue_id, available=row is not None,
                  edition=dict(row) if row else None, publication=None, stories=[],
                  story_set=None, retained_sets=[], diagnostics=[])
    selected_provider = cursor.execute('''SELECT v.metadata_provider FROM issues i
        JOIN volumes v ON v.id=i.volume_id WHERE i.id=?''', (issue_id,)).fetchone()
    result['selected_provider_current'] = bool(row is not None and selected_provider
                                               and row['provider'] == selected_provider[0])
    variant = cursor.execute('SELECT * FROM issue_variant_of WHERE issue_id=?', (issue_id,)).fetchone()
    result['variant_of'] = dict(variant) if variant else None
    result['dates'] = [dict(r) for r in cursor.execute('''SELECT source_field,raw_value,precision
        FROM issue_date_facts WHERE issue_id=? ORDER BY source_field''', (issue_id,))]
    if row is None:
        return result
    publication = cursor.execute('''SELECT b.* FROM volume_bibliography b
        JOIN issues i ON i.volume_id=b.volume_id WHERE i.id=?''', (issue_id,)).fetchone()
    result['publication'] = dict(publication) if publication else None
    result['publication_diagnostics'] = [r[0] for r in cursor.execute('''
        SELECT d.code FROM publication_bibliography_diagnostics d JOIN issues i ON i.volume_id=d.volume_id
        WHERE i.id=? ORDER BY d.code''', (issue_id,))]
    # Only a bounded receipt page, not full historical observations.
    result['retained_sets'] = [dict(r) for r in cursor.execute('''SELECT id,observed_at,observation_count
        FROM story_observation_sets WHERE issue_id=? ORDER BY id DESC LIMIT 100''', (issue_id,))]
    if set_id is None and result['retained_sets']:
        set_id = result['retained_sets'][0]['id']
    selected = cursor.execute('SELECT * FROM story_observation_sets WHERE issue_id=? AND id=?',
                               (issue_id, set_id)).fetchone()
    if selected is None:
        raise ValueError('Unknown issue-owned observation set')
    result['story_set'] = dict(selected)
    credits = {}
    for r in cursor.execute('''SELECT c.observation_id,c.role,c.text FROM story_credit_observations c
            JOIN story_observations s ON s.id=c.observation_id WHERE s.set_id=? ORDER BY s.position,c.role''', (set_id,)):
        credits.setdefault(r[0], []).append(dict(role=r[1], text=r[2]))
    result['stories'] = [dict(r, credits=credits.get(r['id'], [])) for r in cursor.execute(
        'SELECT * FROM story_observations WHERE set_id=? ORDER BY position', (set_id,))]
    result['diagnostics'] = [r[0] for r in cursor.execute(
        'SELECT code FROM bibliography_diagnostics WHERE issue_id=? ORDER BY code', (issue_id,))]
    return result
