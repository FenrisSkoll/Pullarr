"""Bounded complete known-ID observation using existing provider clients/admission."""

from backend.base.issue_facts import BibliographicDate, DateKind
from backend.base.provider_switch import ProviderReference
from backend.base.release_calendar import observation
from backend.implementations.metadata.comicvine import \
    ComicVineMetadataProvider
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.registry import get_volume_provider
from backend.implementations.metadata.snapshot import MetadataSnapshotProvider
from backend.implementations.metadata.switch_target import (
    MetadataReviewProvider, admit)


async def acquire_dates(provider_name, provider_id, fetched_at):
    """No library writes; full membership admission prevents partial publication dates."""
    provider = get_volume_provider(provider_name)
    raw_dates = None
    if type(provider) is ComicVineMetadataProvider:
        from backend.implementations.comicvine import ComicVine
        client = ComicVine(safe_search_errors=True, review_issue_limit=1000, retain_issue_dates=True)
        result = VolumeFetchResult(provider._volume_metadata(await client.fetch_volume(provider_id)), ())
        raw_dates = client.issue_dates
    elif isinstance(provider, MetadataSnapshotProvider):
        snapshot = await provider.fetch_snapshot(provider_id)
        result = VolumeFetchResult(snapshot.volume, (), snapshot=snapshot)
    elif isinstance(provider, MetadataReviewProvider):
        result = await provider.fetch_review(provider_id, 1000)
    else:
        from backend.base.release_calendar import CalendarError
        raise CalendarError('unsupported_provider')
    data = admit(result, ProviderReference(provider_name, provider_id)).data.view()
    values = []
    for issue in data['issues']:
        evidence = []
        if raw_dates is not None:
            for field, kind in (('cover_date', DateKind.COVER), ('store_date', DateKind.ON_SALE)):
                fact = BibliographicDate.interpret(raw_dates[issue['provider_id']][field], kind, 'comicvine_api', field)
                evidence.append(observation(fact, provider_name, issue['provider_id'], fetched_at))
        else:
            for item in issue['facts']['dates']:
                fact = BibliographicDate.interpret(item['raw_value'], DateKind(item['kind']),
                    item['provenance'], item['source_field'], zero_placeholders=item['zero_placeholders'], uncertainty=item['uncertainty'])
                evidence.append(observation(fact, provider_name, issue['provider_id'], fetched_at))
        values.append(dict(provider_id=issue['provider_id'], evidence=evidence))
    return values
