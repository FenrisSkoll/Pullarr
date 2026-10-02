"""Durable decisions and local-ID claims, never remote locator persistence."""

import json
import sqlite3
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from time import time
from uuid import uuid4

from backend.base.auto_selection import AutoSelectionPolicy
from backend.base.release_evaluation import Compatibility
from backend.implementations.release_explanations import evaluation_identity
from backend.implementations.release_scoring import ranking_key
from backend.internals.download_jobs import canonical


class WantedConflict(Exception):
    """Safe constant category, not raw SQL or downstream response."""


# Existing acquisitions created before automation have no decision receipt.
# Preserve their ownership of work without manufacturing a scored decision.
# Legacy float-covered queue rows conservatively hold their entire volume.
EXTERNAL_HOLD = '''
 AND i.id NOT IN(SELECT member.value FROM acquisition_downloads a,json_each(a.intent,'$.issue_ids') member
     WHERE a.state NOT IN ('failed')
     AND NOT EXISTS(SELECT 1 FROM wanted_acquisitions w WHERE w.kind IN ('sabnzbd','nzbget','qbittorrent') AND w.acquisition_id=a.id))
 AND i.volume_id NOT IN(SELECT q.volume_id FROM download_queue q
     WHERE NOT EXISTS(SELECT 1 FROM wanted_acquisitions w WHERE w.kind='direct_download'
         AND w.acquisition_id=CASE WHEN json_valid(q.covered_issues) THEN json_extract(q.covered_issues,'$.completion_id') END))
'''


class WantedStore:
    def __init__(self, database, *, clock=time):
        self.path = str(Path(database).absolute())
        self.clock = clock
        self.db = sqlite3.connect(Path(self.path).as_uri() + '?mode=rw', uri=True,
                                 timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        if not self.db.execute("SELECT name FROM sqlite_master WHERE name='wanted_searches'").fetchone():
            self.db.close()
            raise WantedConflict('schema_unavailable')
        self.has_quality = bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='quality_upgrade_issues'").fetchone())

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def due(self, *, limit=4, volume_id=None, issue_id=None, ignore_cooldown=False, requested_only=False):
        """One indexed query, no stat sweep and no float-derived identity."""
        if not 1 <= limit <= 1000:
            raise WantedConflict('invalid_limit')
        upgrade = 'OR i.id IN (SELECT issue_id FROM quality_upgrade_issues)' if self.has_quality else ''
        return tuple(dict(r) for r in self.db.execute(f'''SELECT i.id,i.volume_id,i.issue_number,
            v.title,COALESCE(s.next_search,0) next_search,s.last_search,COALESCE(s.requested,0) requested
            FROM issues i JOIN volumes v ON v.id=i.volume_id
            LEFT JOIN wanted_schedule s ON s.issue_id=i.id
            WHERE i.monitored=1 AND v.monitored=1
            AND (NOT EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id) {upgrade})
            AND NOT EXISTS(SELECT 1 FROM wanted_reservations r WHERE r.issue_id=i.id AND r.active=1)
            AND (? OR COALESCE(s.next_search,0)<=?)
            AND (?=0 OR s.requested=1)
            AND (? IS NULL OR v.id=?) AND (? IS NULL OR i.id=?)
            {EXTERNAL_HOLD}
            ORDER BY COALESCE(s.next_search,0),i.id LIMIT ?''',
            (ignore_cooldown, self.clock(), requested_only, volume_id, volume_id, issue_id, issue_id, limit)))

    def eligible(self, ids, *, monitored=True, allow_owned=False):
        if not ids or len(set(ids)) != len(ids) or len(ids) > 1000:
            return False
        slots = ','.join('?' for _ in ids)
        upgrade = 'OR i.id IN (SELECT issue_id FROM quality_upgrade_issues)' if self.has_quality else ''
        rows = self.db.execute(f'''SELECT i.id FROM issues i JOIN volumes v ON v.id=i.volume_id
            WHERE i.id IN ({slots}) AND (?=0 OR (i.monitored=1 AND v.monitored=1))
            AND (? OR NOT EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id) {upgrade})
            {EXTERNAL_HOLD}
            AND NOT EXISTS(SELECT 1 FROM wanted_reservations r WHERE r.issue_id=i.id AND r.active=1)''',
            (*ids, monitored, allow_owned)).fetchall()
        return len(rows) == len(ids)

    def missing_members(self, volume_id):
        rows = self.db.execute(f'''SELECT i.id,
            (EXISTS(SELECT 1 FROM wanted_reservations r WHERE r.issue_id=i.id AND r.active=1)
             OR NOT(1 {EXTERNAL_HOLD})) held
            FROM issues i JOIN volumes v ON v.id=i.volume_id
            WHERE v.id=? AND i.monitored=1 AND v.monitored=1
            AND NOT EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id)
            ORDER BY i.id LIMIT 1001''', (volume_id,)).fetchall()
        if len(rows) > 1000:
            raise WantedConflict('coverage_target_limit')
        return {r['id']: bool(r['held']) for r in rows}

    def begin_search(self, target, trigger):
        if trigger not in ('manual', 'automatic_missing', 'incremental_discovery', 'retry'):
            raise WantedConflict('invalid_trigger')
        identifier = uuid4().hex
        with self.transaction():
            self.db.execute('''INSERT INTO wanted_searches
                (id,volume_id,issue_ids,trigger,state,target_fingerprint,selection_policy,started_at)
                VALUES(?,?,?,?,?,?,?,?)''', (identifier, target.publication.id, canonical(target.issue_ids),
                trigger, 'searching', sha256(repr(target).encode()).hexdigest(),
                AutoSelectionPolicy().fingerprint, self.clock()))
        return identifier

    def unavailable_target(self, volume_id, issue_id, trigger):
        """A typed local-context failure has no fabricated target/scoring receipt."""
        identifier = uuid4().hex
        with self.transaction():
            self.db.execute('''INSERT INTO wanted_searches
                (id,volume_id,issue_ids,trigger,state,selection_policy,started_at)
                VALUES(?,?,?,?,?,?,?)''', (identifier, volume_id, canonical((issue_id,)), trigger,
                'searching', AutoSelectionPolicy().fingerprint, self.clock()))
        self.finish_search(identifier, 'target_unavailable')
        return identifier

    def finish_search(self, identifier, outcome, *, sources=None, counts=None, cooldown=True):
        with self.transaction():
            row = self.db.execute('SELECT * FROM wanted_searches WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise WantedConflict('search_missing')
            self.db.execute('''UPDATE wanted_searches SET state='finished',outcome=?,source_receipt=?,
                counts=?,finished_at=? WHERE id=?''',
                (outcome, canonical(sources or {}), canonical(counts or {}), self.clock(), identifier))
            if cooldown and row['trigger'] != 'manual':
                for issue in json.loads(row['issue_ids']):
                    # Deleted targets retain history, not a bogus live schedule FK.
                    if not self.db.execute('SELECT 1 FROM issues WHERE id=?', (issue,)).fetchone():
                        continue
                    old = self.db.execute('SELECT attempts FROM wanted_schedule WHERE issue_id=?', (issue,)).fetchone()
                    attempt = min((old[0] if old else 0) + 1, 100)
                    delay = (900, 3600, 21600, 86400)[min(attempt - 1, 3)]
                    self.db.execute('''INSERT INTO wanted_schedule(issue_id,next_search,attempts,last_search) VALUES(?,?,?,?)
                        ON CONFLICT(issue_id) DO UPDATE SET next_search=excluded.next_search,
                        attempts=excluded.attempts,last_search=excluded.last_search,requested=0''',
                        (issue, self.clock() + delay, attempt, identifier))

    def request_search(self, volume_id, issue_id=None):
        """Explicit caller authorizes bounded queued selection, not a synchronous storm."""
        with self.transaction():
            upgrade = 'OR i.id IN (SELECT issue_id FROM quality_upgrade_issues)' if self.has_quality else ''
            rows = self.db.execute(f'''SELECT i.id FROM issues i JOIN volumes v ON v.id=i.volume_id
                WHERE v.id=? AND (? IS NULL OR i.id=?) AND i.monitored=1 AND v.monitored=1
                AND (NOT EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=i.id) {upgrade})
                AND NOT EXISTS(SELECT 1 FROM wanted_reservations r WHERE r.issue_id=i.id AND r.active=1)
                ORDER BY i.id LIMIT 1001''', (volume_id, issue_id, issue_id)).fetchall()
            if len(rows) > 1000:
                raise WantedConflict('request_target_limit')
            self.db.executemany('''INSERT INTO wanted_schedule(issue_id,next_search,requested) VALUES(?,0,1)
                ON CONFLICT(issue_id) DO UPDATE SET next_search=0,requested=1''', ((r[0],) for r in rows))
        return len(rows)

    def reserve(self, run_id, evaluation, *, authorization='automatic', ids=None):
        ids = tuple(ids or evaluation.target.issue_ids)
        if authorization not in ('automatic', 'manual', 'forced_manual'):
            raise WantedConflict('invalid_authorization')
        if authorization != 'forced_manual' and evaluation.state != Compatibility.COMPATIBLE:
            raise WantedConflict('incompatible_selection')
        identifier = uuid4().hex
        with self.transaction():
            if not self.eligible(ids, monitored=authorization == 'automatic', allow_owned=authorization == 'forced_manual'):
                raise WantedConflict('target_no_longer_eligible')
            slots = ','.join('?' for _ in ids)
            if self.db.execute(f'SELECT COUNT(*) FROM issues WHERE id IN ({slots}) AND volume_id=?',
                               (*ids, evaluation.target.publication.id)).fetchone()[0] != len(ids):
                raise WantedConflict('reservation_parent_mismatch')
            run = self.db.execute('SELECT volume_id FROM wanted_searches WHERE id=?', (run_id,)).fetchone()
            if run is None or run[0] != evaluation.target.publication.id:
                raise WantedConflict('search_target_mismatch')
            self.db.execute('''INSERT INTO wanted_decisions
                (id,search_id,authorization,candidate_id,source_kind,source_key,evaluation_id,
                 scoring_fingerprint,selection_fingerprint,quality,issue_ids,title,state,mechanism,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (identifier, run_id, authorization, evaluation.candidate.candidate_id,
                 evaluation.candidate.source.kind.value, evaluation.candidate.source.key,
                 evaluation_identity(evaluation), evaluation.policy_fingerprint,
                 AutoSelectionPolicy().fingerprint, canonical(ranking_key(evaluation)[:5]),
                 canonical(ids), evaluation.candidate.raw_title, 'selected',
                 evaluation.candidate.acquisition.mechanism.value, self.clock(), self.clock()))
            self.db.executemany('INSERT INTO wanted_reservations(decision_id,issue_id) VALUES(?,?)',
                                ((identifier, issue) for issue in ids))
            if self.has_quality:
                quality = json.loads(evaluation.quality_receipt) if evaluation.quality_receipt else {}
                reason = 'upgrade' if quality.get('result') == 'provisional_upgrade' else 'manual' if authorization != 'automatic' else 'missing'
                self.db.execute('UPDATE wanted_decisions SET acquisition_reason=? WHERE id=?', (reason,identifier))
        return identifier

    def transition(self, identifier, state, *, acquisition_id=None, error=None):
        allowed = {
            'selected': {'grabbing', 'abandoned'},
            'grabbing': {'tracking', 'review'},
            'tracking': {'review', 'satisfied'},
            'review': {'satisfied', 'abandoned', 'grabbing'},
        }
        with self.transaction():
            old = self.db.execute('SELECT state FROM wanted_decisions WHERE id=?', (identifier,)).fetchone()
            if not old or state not in allowed.get(old[0], set()):
                raise WantedConflict('invalid_transition')
            self.db.execute('''UPDATE wanted_decisions SET state=?,acquisition_id=COALESCE(?,acquisition_id),
                error=?,updated_at=? WHERE id=?''', (state, acquisition_id, error, self.clock(), identifier))
            if state in ('satisfied', 'abandoned'):
                self.db.execute('UPDATE wanted_reservations SET active=0,closed_reason=? WHERE decision_id=?',
                                (state, identifier))

    def reconcile_ownership(self):
        """Canonical links satisfy individual reservation members, never download state."""
        with self.transaction():
            upgrade = """AND (decision_id NOT IN (SELECT id FROM wanted_decisions WHERE acquisition_reason='upgrade')
                OR EXISTS(SELECT 1 FROM acquisition_provenance p WHERE p.id=decision_id AND p.state='imported'))""" if self.has_quality else ''
            self.db.execute(f'''UPDATE wanted_reservations SET active=0,closed_reason='owned'
                WHERE active=1 AND EXISTS(SELECT 1 FROM canonical_issue_files f WHERE f.issue_id=wanted_reservations.issue_id) {upgrade}''')
            self.db.execute('''UPDATE wanted_reservations SET active=0,closed_reason='target_deleted'
                WHERE active=1 AND NOT EXISTS(SELECT 1 FROM issues i WHERE i.id=wanted_reservations.issue_id)''')
            self.db.execute('''UPDATE wanted_decisions SET state='satisfied',updated_at=?
                WHERE state IN ('selected','grabbing','tracking','review')
                AND NOT EXISTS(SELECT 1 FROM wanted_reservations r WHERE r.decision_id=wanted_decisions.id AND r.active=1)
                AND NOT EXISTS(SELECT 1 FROM wanted_reservations r WHERE r.decision_id=wanted_decisions.id AND r.closed_reason!='owned')''',
                (self.clock(),))

    def recover_claims(self):
        """Caller MUST own the process gate. Lost resolver contexts never replay grabs."""
        with self.transaction():
            self.db.execute("UPDATE wanted_searches SET state='interrupted',outcome='search_interrupted',finished_at=? WHERE state='searching'", (self.clock(),))
            self.db.execute("UPDATE wanted_reservations SET active=0,closed_reason='context_expired' WHERE decision_id IN (SELECT id FROM wanted_decisions WHERE state='selected')")
            self.db.execute("UPDATE wanted_decisions SET state='abandoned',error='context_expired',updated_at=? WHERE state='selected'", (self.clock(),))
            self.db.execute("UPDATE wanted_decisions SET state='review',error='grab_uncertain',updated_at=? WHERE state='grabbing'", (self.clock(),))

    def prune_search_history(self):
        # Decisions/acquisitions and the current cooldown receipt never age out.
        # Bound cleanup work; no source data/files are deleted here.
        with self.transaction():
            self.db.execute('''DELETE FROM wanted_searches WHERE id IN (
                SELECT s.id FROM wanted_searches s WHERE s.started_at<? AND s.state!='searching'
                AND NOT EXISTS(SELECT 1 FROM wanted_decisions d WHERE d.search_id=s.id)
                AND NOT EXISTS(SELECT 1 FROM wanted_schedule c WHERE c.last_search=s.id)
                ORDER BY s.started_at,s.id LIMIT 100)''', (self.clock() - 90 * 86400,))
