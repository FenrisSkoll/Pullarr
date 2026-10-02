"""Provider-qualified runtime identity and legacy compatibility diagnostics.

No commits, metadata dispatch or provider network calls belong in this layer.
Only explicit cross-reference assertions may be written here; normal CV
synchronization is statement-atomic in the nullable-aware schema-53 triggers.
"""

from dataclasses import dataclass
from re import fullmatch
from sqlite3 import IntegrityError
from typing import Collection, Dict, List, Tuple, Union

from backend.internals.db import get_db


@dataclass(frozen=True)
class ExternalIdentity:
    local_id: int
    provider: str
    provider_id: str
    provenance: str
    last_fetch: Union[float, None] = None


class MetadataIdentityError(IntegrityError):
    """Persisted metadata identity is missing or violates compatibility parity."""


@dataclass(frozen=True)
class VolumeMetadataIdentity:
    local_id: int
    provider: str
    provider_id: str
    last_fetch: Union[float, None]


@dataclass(frozen=True)
class NamingIdentityContext:
    """Explicit per-operation snapshot, never a process-global identity cache."""

    volume: VolumeMetadataIdentity
    issues: Dict[int, str]


class ProviderIdentityDB:
    @classmethod
    def naming_identities(
        cls, volume_id: int, registered_providers: Collection[str],
        include_issues: bool = False
    ) -> NamingIdentityContext:
        """Selected authority only; at most two reads for an entire rename."""
        volume = cls.resolve_volume_metadata_identity(volume_id, registered_providers)
        issues: Dict[int, str] = {}
        if include_issues:
            rows = get_db().execute('''SELECT i.id,e.provider_id,i.comicvine_id
                FROM issues i LEFT JOIN issue_external_ids e
                    ON e.issue_id=i.id AND e.provider=?
                WHERE i.volume_id=?''', (volume.provider, volume_id))
            for local_id, provider_id, legacy_id in rows:
                if not isinstance(provider_id, str) or not provider_id:
                    raise MetadataIdentityError(f'Missing selected issue identity: {local_id}')
                if volume.provider == 'comicvine' and (
                    legacy_id is None or provider_id != str(legacy_id)
                ):
                    raise MetadataIdentityError(f'ComicVine issue identity shadow conflict: {local_id}')
                issues[local_id] = provider_id
        return NamingIdentityContext(volume, issues)

    @staticmethod
    def find_selected_volume(provider: str, provider_id: str) -> Union[int, None]:
        """Find selected-source ownership, never treat a cross-reference as authority.

        Preserve first-local-ID behavior for legal duplicate selected identities.
        A reference-only match is ambiguous, not a reason to merge or switch.
        """
        rows = list(get_db().execute('''SELECT v.id,v.metadata_provider
            FROM volume_external_ids e JOIN volumes v ON v.id=e.volume_id
            WHERE e.provider=? AND e.provider_id=? ORDER BY v.id''', (provider, provider_id)))
        for local_id, selected in rows:
            if selected == provider:
                return local_id
        if rows:
            raise ValueError('External reference belongs to a different selected provider')
        return None

    @staticmethod
    def resolve_volume_metadata_identities(
        registered_providers: Collection[str],
        volume_id: Union[int, None] = None
    ) -> List[VolumeMetadataIdentity]:
        """One set-based read; legacy fields are parity checks, never fallbacks.

        Registry keys are supplied by the implementation layer to avoid an
        internals -> implementations dependency. No read repairs storage.
        """
        result = []
        rows = get_db().execute('''
            SELECT v.id,v.metadata_provider,e.provider_id,e.last_fetch,
                v.comicvine_id,v.last_cv_fetch
            FROM volumes v LEFT JOIN volume_external_ids e
                ON e.volume_id=v.id AND e.provider=v.metadata_provider
            WHERE (? IS NULL OR v.id=?)
            ORDER BY e.last_fetch ASC,v.id ASC
        ''', (volume_id, volume_id))
        for local_id, provider, provider_id, fetched, legacy_id, legacy_fetch in rows:
            if provider not in registered_providers:
                raise KeyError(f'Unregistered metadata provider: {provider}')
            if not isinstance(provider_id, str) or not provider_id:
                raise MetadataIdentityError(
                    f'Missing selected metadata identity for volume {local_id}')
            if provider == 'comicvine' and (
                legacy_id is None or provider_id != str(legacy_id) or fetched != legacy_fetch
            ):
                raise MetadataIdentityError(
                    f'ComicVine volume identity shadow conflict: {local_id}')
            result.append(VolumeMetadataIdentity(
                local_id, provider, provider_id, fetched))
        return result

    @classmethod
    def resolve_volume_metadata_identity(
        cls, volume_id: int, registered_providers: Collection[str]
    ) -> VolumeMetadataIdentity:
        identities = cls.resolve_volume_metadata_identities(
            registered_providers, volume_id)
        if not identities:
            raise KeyError(volume_id)
        return identities[0]

    @staticmethod
    def validate_issue_metadata_identities(provider: str) -> None:
        """Validate selected-source issues in one query before reconciliation.

        Global scope preserves legacy globally unique issue matching, including
        legal duplicate local volumes. Other providers' references are ignored.
        """
        row = get_db().execute('''
            SELECT i.id FROM issues i JOIN volumes v ON v.id=i.volume_id
            LEFT JOIN issue_external_ids e
                ON e.issue_id=i.id AND e.provider=v.metadata_provider
            WHERE v.metadata_provider=? AND (
                e.issue_id IS NULL OR
                (?='comicvine' AND (i.comicvine_id IS NULL OR
                    e.provider_id != CAST(i.comicvine_id AS TEXT))))
            LIMIT 1
        ''', (provider, provider)).fetchone()
        if row is not None:
            raise MetadataIdentityError(
                f'ComicVine issue identity shadow conflict: {row[0]}'
                if provider == 'comicvine' else
                f'Missing selected issue identity ({provider}): {row[0]}')

    @staticmethod
    def volume_identities(
        volume_id: int, provider: Union[str, None] = None
    ) -> List[ExternalIdentity]:
        return [ExternalIdentity(*row) for row in get_db().execute(
            """SELECT volume_id,provider,provider_id,provenance,last_fetch
            FROM volume_external_ids WHERE volume_id=?
                AND (? IS NULL OR provider=?) ORDER BY provider""",
            (volume_id, provider, provider))]

    @staticmethod
    def issue_identities(
        issue_id: int, provider: Union[str, None] = None
    ) -> List[ExternalIdentity]:
        return [ExternalIdentity(*row) for row in get_db().execute(
            """SELECT issue_id,provider,provider_id,provenance
            FROM issue_external_ids WHERE issue_id=?
                AND (? IS NULL OR provider=?) ORDER BY provider""",
            (issue_id, provider, provider))]

    @staticmethod
    def selected_provider(volume_id: int) -> str:
        row = get_db().execute(
            'SELECT metadata_provider FROM volumes WHERE id=?',
            (volume_id,)).fetchone()
        if row is None:
            raise KeyError(volume_id)
        return row[0]

    @classmethod
    def set_selected_provider(cls, volume_id: int, provider: str) -> None:
        if provider != 'comicvine':
            raise ValueError(
                'Provider selection is ComicVine-only during shadow storage')
        cls.selected_provider(volume_id)  # Validate local object existence.
        get_db().execute('UPDATE volumes SET metadata_provider=? WHERE id=?',
                         (provider, volume_id))

    @staticmethod
    def _put(identity: ExternalIdentity, volume: bool) -> None:
        if not fullmatch(r'[a-z][a-z0-9_]*', identity.provider):
            raise ValueError('Unknown identity namespace')
        if not isinstance(identity.provider_id, str):
            raise ValueError('External identity must be a nonempty string')
        if not identity.provider_id:
            raise ValueError('External identity must be a nonempty string')
        if not identity.provenance:
            raise ValueError('Identity provenance is required')
        if identity.provider == 'comicvine':
            raise ValueError(
                'ComicVine shadows are maintained by legacy writes')
        if not volume and identity.last_fetch is not None:
            raise ValueError('Issue fetch timestamps are not tracked')
        table = 'volume_external_ids' if volume else 'issue_external_ids'
        key = 'volume_id' if volume else 'issue_id'
        cursor = get_db()
        # A conditional upsert avoids a read/write race. Different IDs are
        # conflicts, not implicit reassignment of an established
        # cross-reference.
        fields = ',last_fetch' if volume else ''
        values = ',?' if volume else ''
        update = ',last_fetch=excluded.last_fetch' if volume else ''
        parameters = (identity.local_id, identity.provider,
                      identity.provider_id, identity.provenance)
        if volume:
            parameters += (identity.last_fetch,)
        cursor.execute(
            f'INSERT INTO {table}({key},provider,provider_id,provenance{fields}) '
            f'VALUES (?,?,?,?{values}) ON CONFLICT({key},provider) DO UPDATE SET '
            f'provenance=excluded.provenance{update} '
            f'WHERE {table}.provider_id=excluded.provider_id', parameters)
        if not cursor.rowcount:
            raise ValueError(
                'External identity conflict; explicit reconciliation required')

    @classmethod
    def put_volume_identity(cls, identity: ExternalIdentity) -> None:
        cls._put(identity, True)

    @classmethod
    def put_issue_identity(cls, identity: ExternalIdentity) -> None:
        cls._put(identity, False)

    @staticmethod
    def put_comicvine_reference(identity: ExternalIdentity, volume: bool) -> None:
        """Explicit verified assertion; never switches selected authority.

        Caller supplies provenance. No matching or network verification is
        implied. Conflicts fail atomically, including the legacy projection.
        """
        if identity.provider != 'comicvine' or not identity.provenance:
            raise ValueError('ComicVine reference and provenance required')
        value = int(identity.provider_id)
        if str(value) != identity.provider_id or not -(2**63) <= value < 2**63:
            raise ValueError('Canonical signed 64-bit ComicVine ID required')
        table = 'volumes' if volume else 'issues'
        external = 'volume_external_ids' if volume else 'issue_external_ids'
        key = 'volume_id' if volume else 'issue_id'
        cursor = get_db()
        cursor.execute('SAVEPOINT cv_reference')
        try:
            row = cursor.execute(f'SELECT comicvine_id FROM {table} WHERE id=?', (identity.local_id,)).fetchone()
            if row is None:
                raise KeyError(identity.local_id)
            if row[0] is not None and row[0] != value:
                raise ValueError('ComicVine reference conflict')
            cursor.execute(f'UPDATE {table} SET comicvine_id=? WHERE id=?', (value, identity.local_id))
            cursor.execute(f"UPDATE {external} SET provenance=? WHERE {key}=? AND provider='comicvine'",
                           (identity.provenance, identity.local_id))
            cursor.execute('RELEASE cv_reference')
        except BaseException:
            cursor.execute('ROLLBACK TO cv_reference')
            cursor.execute('RELEASE cv_reference')
            raise

    @staticmethod
    def audit(registered_providers: Collection[str] = ('comicvine',)) -> List[Tuple[str, int, str]]:
        """Read-only diagnostics, including manual SQL damage; never repair it."""
        result = [tuple(row) for row in get_db().execute("""
            SELECT 'volume',v.id,'missing or inconsistent ComicVine shadow'
            FROM volumes v LEFT JOIN volume_external_ids e
                ON e.volume_id=v.id AND e.provider='comicvine'
            WHERE e.provider_id IS NOT CAST(v.comicvine_id AS TEXT)
                OR (e.volume_id IS NOT NULL AND e.last_fetch IS NOT v.last_cv_fetch)
                OR (v.metadata_provider='comicvine' AND v.comicvine_id IS NULL)
            UNION ALL
            SELECT 'issue',i.id,'missing or inconsistent ComicVine shadow'
            FROM issues i LEFT JOIN issue_external_ids e
                ON e.issue_id=i.id AND e.provider='comicvine'
            WHERE e.provider_id IS NOT CAST(i.comicvine_id AS TEXT)
            UNION ALL
            SELECT 'volume',v.id,'missing selected identity'
            FROM volumes v LEFT JOIN volume_external_ids e
                ON e.volume_id=v.id AND e.provider=v.metadata_provider
            WHERE e.volume_id IS NULL AND v.metadata_provider != 'comicvine'
            UNION ALL
            SELECT 'issue',i.id,'missing selected identity'
            FROM issues i JOIN volumes v ON v.id=i.volume_id
            LEFT JOIN issue_external_ids e ON e.issue_id=i.id AND e.provider=v.metadata_provider
            WHERE e.issue_id IS NULL AND (v.metadata_provider != 'comicvine' OR i.comicvine_id IS NULL)
            UNION ALL
            SELECT 'volume',e.volume_id,'orphan external identity'
            FROM volume_external_ids e LEFT JOIN volumes v ON v.id=e.volume_id
            WHERE v.id IS NULL
            UNION ALL
            SELECT 'issue',e.issue_id,'orphan external identity'
            FROM issue_external_ids e LEFT JOIN issues i ON i.id=e.issue_id
            WHERE i.id IS NULL
            ORDER BY 1,2,3
        """)]
        result.extend(('volume', row[0], 'unregistered selected provider')
                      for row in get_db().execute('SELECT id,metadata_provider FROM volumes')
                      if row[1] not in registered_providers)
        return result
