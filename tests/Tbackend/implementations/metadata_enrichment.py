"""Identity enrichment must never extend the core serialization contract."""

from asyncio import run
from copy import deepcopy
from dataclasses import asdict
from unittest import TestCase
from unittest.mock import patch

from fixtures.metron import SERIES, issue, issue_summary

from backend.base.definitions import DateType
from backend.implementations.metadata.metron import MetronMetadataProvider


class MetadataEnrichment(TestCase):
    def test_exact_serialized_core_shape_and_separate_owner_qualified_enrichment(self):
        data = deepcopy(SERIES)
        data['cv_id'] = 900
        result = MetronMetadataProvider.volume_result(
            data, [issue(cv_id=901, gcd_id=902)], DateType.COVER_DATE)
        expected_issue = {
            'provider': 'metron', 'provider_id': '701', 'volume_provider_id': '700',
            'issue_number': '1', 'calculated_issue_number': 1.0,
            'title': 'A meaningful collection', 'date': '2020-02-01',
            'description': 'Issue description.'}
        expected_volume = {
            'provider': 'metron', 'provider_id': '700',
            'title': 'Example Collection', 'year': 2020, 'volume_number': 2,
            'cover_link': None, 'cover': None, 'description': 'A fictional collection.',
            'site_url': SERIES['resource_url'], 'aliases': ['Example Alias'],
            'publisher': 'Example Publisher', 'issue_count': 2, 'translated': False,
            'issues': [expected_issue]}
        self.assertEqual(asdict(result.metadata.issues[0]), expected_issue)
        self.assertEqual(vars(result.metadata.issues[0]), expected_issue)
        self.assertEqual(asdict(result.metadata), expected_volume)
        self.assertEqual(set(vars(result.metadata)), set(expected_volume))
        self.assertEqual([
            (r.entity, r.owner_provider, r.owner_id,
             r.provider, r.provider_id, r.provenance) for r in result.enrichment], [
                ('volume', 'metron', '700', 'comicvine', '900', 'metron'),
                ('volume', 'metron', '700', 'gcd', '800', 'metron'),
                ('issue', 'metron', '701', 'comicvine', '901', 'metron'),
                ('issue', 'metron', '701', 'gcd', '902', 'metron')])

    def test_absent_references_and_issue_references_never_infer_volume_references(self):
        data = dict(SERIES, cv_id=None, gcd_id=None)
        absent = MetronMetadataProvider.volume_result(data, [issue()], DateType.COVER_DATE)
        self.assertEqual(absent.enrichment, ())
        enriched = MetronMetadataProvider.volume_result(
            data, [issue(cv_id=901)], DateType.COVER_DATE)
        self.assertEqual(len(enriched.enrichment), 1)
        self.assertEqual(enriched.enrichment[0].entity, 'issue')
        self.assertEqual(asdict(absent.metadata), asdict(enriched.metadata))

    def test_fetch_envelope_transports_same_snapshot_without_hidden_provider_state(self):
        provider = MetronMetadataProvider()
        raw_issue = issue(cv_id=901)
        series = dict(SERIES, issue_count=1)
        with patch('backend.implementations.metadata.metron.MetronClient') as client_type, \
                patch('backend.implementations.metadata.metron.Settings') as settings, \
                patch.object(provider, 'cache_directory', return_value=None):
            settings.return_value.sv.date_type = DateType.COVER_DATE
            client = client_type.return_value
            client.pages.return_value = [issue_summary(raw_issue)]
            client.get.side_effect = [deepcopy(series), deepcopy(raw_issue)]
            first = run(provider.fetch_volume_enriched('700'))
            self.assertEqual(client.get.call_count, 2)
            client.get.side_effect = [deepcopy(series), issue()]
            second = run(provider.fetch_volume_enriched('700'))
            self.assertEqual(len(first.enrichment), 2)
            self.assertEqual(len(second.enrichment), 1)
            client.get.side_effect = [deepcopy(series), deepcopy(raw_issue)]
            core = run(provider.fetch_volume('700'))
            self.assertEqual(asdict(core), asdict(first.metadata))
            self.assertNotIn('enrichment', vars(core))
