"""Legacy search and opt-in provider-qualified resource serialization."""

from dataclasses import asdict
from typing import Any, Dict, List

from flask import request

from backend.base.custom_exceptions import InvalidKeyValue
from backend.base.definitions import SpecialVersion
from backend.implementations.issue_presentation import issue_display_title
from backend.implementations.metadata.models import VolumeSearchResult
from backend.internals.db import get_db
from backend.internals.provider_identity import (MetadataIdentityError,
                                                 ProviderIdentityDB)


def metadata_requested() -> bool:
    value = request.args.get('metadata', 'false')
    if value not in ('true', 'false'):
        raise InvalidKeyValue('metadata', value)
    return value == 'true'


def rich_issues_requested() -> bool:
    version = request.args.get('issue_facts')
    if version not in (None, '1'):
        raise InvalidKeyValue('issue_facts', version)
    return version == '1'


def qualified_volume_search_result(result: VolumeSearchResult) -> Dict[str, Any]:
    data = asdict(result)
    data.pop('provider')
    data.pop('provider_id')
    data['comicvine_id'] = int(result.provider_id) if result.provider == 'comicvine' else None
    data['metadata_source'] = {'provider': result.provider, 'id': result.provider_id}
    data['external_ids'] = {result.provider: result.provider_id}
    return data


def require_metadata_access(volume_id: int, qualified: bool) -> None:
    if not qualified and ProviderIdentityDB.selected_provider(volume_id) != 'comicvine':
        raise InvalidKeyValue(
            'metadata', 'true required for non-ComicVine object')


def issue_identity_result(issue: Any, qualified: bool) -> Any:
    require_metadata_access(issue.volume_id, qualified)
    if not qualified:
        return issue
    data = issue.todict()
    provider = ProviderIdentityDB.selected_provider(issue.volume_id)
    refs = {
        i.provider: i.provider_id for i in ProviderIdentityDB.issue_identities(issue.id)}
    if provider not in refs:
        raise MetadataIdentityError('Missing selected issue identity')
    data.update(metadata_source={'provider': provider,
                'id': refs[provider]}, external_ids=refs)
    # Standalone issue response: one context read, no issue/file enumeration.
    parent_title, special_version, issue_count = get_db().execute('''
        SELECT v.title,v.special_version,(SELECT COUNT(*) FROM issues WHERE volume_id=v.id)
        FROM volumes v WHERE v.id=?''', (issue.volume_id,)).fetchone()
    presentation = issue_display_title(
        issue.title, parent_title=parent_title,
        special_version=SpecialVersion(special_version), issue_count=issue_count)
    data.update(display_title=presentation.value, display_title_source=presentation.source)
    return data


def volume_identity_results(rows: List[Dict[str, Any]], qualified: bool) -> List[Dict[str, Any]]:
    # One set-based identity query for list serialization, not one per volume.
    identities: Dict[int, Any] = {}
    for local, provider, ref, value in get_db().execute('''
        SELECT v.id,v.metadata_provider,e.provider,e.provider_id
        FROM volumes v LEFT JOIN volume_external_ids e ON e.volume_id=v.id'''):
        entry = identities.setdefault(local, (provider, {}))
        if ref is not None:
            entry[1][ref] = value
    result = []
    for row in rows:
        provider, refs = identities[row['id']]
        if not qualified:
            if provider == 'comicvine':
                result.append(row)
            continue
        if provider not in refs:
            raise MetadataIdentityError('Missing selected volume identity')
        data = dict(row, metadata_source={
                    'provider': provider, 'id': refs[provider]}, external_ids=refs)
        if 'issues' in data:
            issue_refs: Dict[int, Dict[str, str]] = {}
            for local_issue, namespace, value in get_db().execute('''
                SELECT e.issue_id,e.provider,e.provider_id FROM issue_external_ids e
                JOIN issues i ON i.id=e.issue_id WHERE i.volume_id=?''', (row['id'],)):
                issue_refs.setdefault(local_issue, {})[namespace] = value
            enriched = []
            for issue in data['issues']:
                references = issue_refs.get(issue['id'], {})
                if provider not in references:
                    raise MetadataIdentityError(
                        'Missing selected issue identity')
                presentation = issue_display_title(
                    issue['title'], parent_title=row['title'],
                    special_version=SpecialVersion(row['special_version']),
                    issue_count=row['issue_count'])
                enriched.append(dict(issue, metadata_source={
                                'provider': provider, 'id': references[provider]}, external_ids=references,
                                display_title=presentation.value, display_title_source=presentation.source))
            data['issues'] = enriched
        result.append(data)
    return result


def legacy_volume_search_result(result: VolumeSearchResult) -> Dict[str, Any]:
    """Serialize a neutral search candidate using the existing public schema.

    Only ComicVine identities can be represented in this legacy API. Fail
    rather than mislabel another provider's ID. Supporting other providers
    publicly requires a separately designed API and add-volume identity flow.
    """
    if result.provider != 'comicvine':
        raise ValueError('The legacy search API requires ComicVine identities')

    return {
        'comicvine_id': int(result.provider_id),
        'title': result.title,
        'year': result.year,
        'volume_number': result.volume_number,
        'cover_link': result.cover_link,
        'description': result.description,
        'site_url': result.site_url,
        'aliases': result.aliases,
        'publisher': result.publisher,
        'issue_count': result.issue_count,
        'translated': result.translated,
        'already_added': result.already_added,
        'issues': None
    }
