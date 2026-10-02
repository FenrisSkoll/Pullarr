"""Deterministic metadata providers: no credentials and no network."""

from backend.base.issue_facts import (BibliographicDate, DateKind,
                                      IssueFacts, IssueNumberFacts)
from backend.implementations.metadata.enrichment import (ProviderIssueFacts,
                                                         VolumeFetchResult)
from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata,
                                                     VolumeSearchResult)
from backend.implementations.metadata.provider import (MetadataSearchProvider,
                                                       MetadataVolumeProvider)
from backend.implementations.metadata.switch_target import \
    MetadataReviewProvider

HOSTILE = '<img src=x onerror="window.calendarHostile=true">'


class CalendarFixture(MetadataVolumeProvider, MetadataReviewProvider, MetadataSearchProvider):
    provider = 'comicvine'
    dates = {'100': '2027-03-17', '900': '2027-03-24', '910': '2027-03', '920': '2027', '930': None}
    failed = False

    async def search_volumes(self, query):
        return [VolumeSearchResult(self.provider, '900', 'External 900 ' + HOSTILE, 2027, 1,
            None, None, None, [], 'Fixture', 1, False, None)]

    async def fetch_volume(self, provider_id):
        if self.failed:
            from backend.implementations.metadata.errors import \
                MetadataProviderError
            raise MetadataProviderError(self.provider, 'rate_limited')
        value = self.dates.get(provider_id)
        return VolumeMetadata(self.provider, provider_id, 'Publication ' + provider_id + ' ' + HOSTILE,
            2027, 1, None, None, '', 'https://example.invalid', [], 'Fixture', 1, False,
            [IssueMetadata(self.provider, str(int(provider_id) + 1), provider_id, '1', 1.0, 'Issue ' + HOSTILE, value, '')])

    async def fetch_review(self, provider_id, issue_limit):
        volume = await self.fetch_volume(provider_id)
        issue = volume.issues[0]
        facts = IssueFacts(IssueNumberFacts.interpret('1', 'fixture', 'number'), (
            BibliographicDate.interpret(issue.date, DateKind.ON_SALE, 'fixture', 'store_date'),
            BibliographicDate.interpret('2027-04' if issue.date else None, DateKind.COVER, 'fixture', 'cover_date')),
            'store_date')
        return VolumeFetchResult(volume, (), issue_facts=(ProviderIssueFacts(self.provider, issue.provider_id, provider_id, facts),))


class CalendarMetron(CalendarFixture):
    provider = 'metron'


class CalendarGCD(CalendarFixture):
    provider = 'gcd'
