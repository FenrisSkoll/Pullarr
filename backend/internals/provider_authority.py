"""Durable authority tokens and write-serialized refresh/application guards.

A token is captured before provider IO. Provider equality alone is insufficient:
after CV -> Metron -> CV, an old CV worker must still lose. These helpers do not
switch authority, commit caller work, or authorize a correspondence plan.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterable

from backend.base.provider_switch import ProviderReference
from backend.base.switch_review import SwitchReviewError


@dataclass(frozen=True)
class AuthorityToken:
    volume_id: int
    provider: str
    provider_id: str
    generation: int


def capture(cursor, volume_ids: Iterable[int]) -> Dict[int, AuthorityToken]:
    """Batched selected identities; missing identity is never a wildcard."""
    ids = sorted(set(volume_ids))
    if len(ids) > 10000 or any(type(value) is not int or value <= 0 for value in ids):
        raise SwitchReviewError('invalid_authority_scope')
    result = {}
    for start in range(0, len(ids), 350):
        batch = ids[start:start + 350]
        rows = cursor.execute('''SELECT v.id,v.metadata_provider,x.provider_id,v.authority_generation
            FROM volumes v JOIN volume_external_ids x
            ON x.volume_id=v.id AND x.provider=v.metadata_provider
            WHERE v.id IN (''' + ','.join('?' for _ in batch) + ') ORDER BY v.id', batch)
        for local, provider, identity, generation in rows:
            ProviderReference(provider, identity)
            if type(generation) is not int or generation < 0:
                raise SwitchReviewError('invalid_authority_generation')
            result[local] = AuthorityToken(local, provider, identity, generation)
    return result


def require_current(cursor, tokens: Iterable[AuthorityToken]) -> None:
    """Must run under the caller's write lock for mutation authorization."""
    expected = tuple(tokens)
    if len({token.volume_id for token in expected}) != len(expected):
        raise SwitchReviewError('duplicate_authority_scope')
    current = capture(cursor, (token.volume_id for token in expected))
    if any(current.get(token.volume_id) != token for token in expected):
        raise SwitchReviewError('stale_metadata_authority')


def capture_refresh(cursor, provider, identities):
    """Bind the already selected refresh group before its first remote request."""
    tokens = capture(cursor, (value[0] for value in identities.values()))
    for identity, (local, _) in identities.items():
        token = tokens.get(local)
        if token is None or (token.provider, token.provider_id) != (provider, identity):
            raise SwitchReviewError('stale_metadata_authority')
    return tokens


@contextmanager
def serialized(cursor):
    """Own one IMMEDIATE transaction, rejecting rather than committing pending work.

    Do not call legacy helpers which commit internally from this context. The
    final transaction check detects that programming error, but cannot undo an
    already committed write. Callers must audit all callees before integrating.
    """
    connection = cursor.connection
    if connection.in_transaction:
        raise SwitchReviewError('authority_write_requires_transaction_boundary')
    cursor.execute('BEGIN IMMEDIATE')
    try:
        yield
        if not connection.in_transaction:
            raise RuntimeError('Authority transaction was ended by a callee')
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


@contextmanager
def guarded_stage(cursor, tokens: Iterable[AuthorityToken]):
    """One serialized stage; no provider/network work belongs inside this guard."""
    with serialized(cursor):
        require_current(cursor, tokens)
        yield


class RefreshStages:
    """Legacy multi-commit refresh: explicit stages, never a lock across HTTP.

    Owns only transactions it started. On any exception the current stage rolls
    back; previously finished stages retain the established refresh semantics.
    """
    def __init__(self, cursor, tokens, commit=None):
        self.cursor, self.tokens = cursor, tuple(tokens)
        self.commit = commit or cursor.connection.commit
        self.active = False

    def __enter__(self):
        if self.cursor.connection.in_transaction:
            raise SwitchReviewError('authority_write_requires_transaction_boundary')
        return self

    def begin(self):
        if self.active or self.cursor.connection.in_transaction:
            raise SwitchReviewError('authority_write_requires_transaction_boundary')
        self.cursor.execute('BEGIN IMMEDIATE')
        self.active = True
        require_current(self.cursor, self.tokens)

    def finish(self, notify=True):
        if not self.active or not self.cursor.connection.in_transaction:
            raise RuntimeError('Refresh transaction ended outside its stage')
        (self.commit if notify else self.cursor.connection.commit)()
        self.active = False

    def __exit__(self, kind, value, traceback):
        if self.active:
            self.cursor.connection.rollback()
            self.active = False
            if kind is None:
                raise RuntimeError('Unfinished refresh transaction')
