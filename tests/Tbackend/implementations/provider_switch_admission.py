"""Production acquisition adapters with deterministic provider transports."""

from asyncio import run
from copy import deepcopy
from unittest import TestCase

from fixtures.comicvine_fetch import (ComicVineFetchHarness,
                                      envelope, issue_response)
from fixtures.comicvine_search import volume_response
from Tbackend.implementations.gcd_lifecycle import GcdLifecycleHarness
from Tbackend.implementations.metron_lifecycle import MetronHarness

from backend.base.provider_switch import ProviderReference
from backend.features.provider_switch_review import ProviderSwitchReviews
from backend.implementations.metadata.comicvine import \
    ComicVineMetadataProvider
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.implementations.metadata.metron_client import MetronError
from backend.implementations.metadata.switch_target import (acquire_target,
                                                            admit)


class ComicVineReviewAdmission(ComicVineFetchHarness, TestCase):
    def prepare_complete(self, issues=None):
        values = [issue_response()] if issues is None else issues
        self.prepare_fetch(volume_response(count_of_issues=len(values), issues=[{'id': row['id']} for row in values]), values)

    def acquire(self):
        result = run(ComicVineMetadataProvider().fetch_review('2127', 10000))
        return admit(result, ProviderReference('comicvine', '2127'))

    def test_complete_exact_normal_cost(self):
        self.prepare_complete()
        result = self.acquire()
        self.assertEqual(len(result.data.view()['issues']), 1)
        self.assertEqual(self.session.get.await_count, 2)
        self.session.get_content.assert_awaited_once()

    def test_membership_absent_rejected(self):
        self.prepare_fetch(volume_response(count_of_issues=1))
        with self.assertRaises(MetadataProviderError):
            self.acquire()

    def test_duplicate_rejected(self):
        self.prepare_complete([issue_response(), issue_response()])
        with self.assertRaises(MetadataProviderError):
            self.acquire()

    def test_wrong_parent_rejected(self):
        self.prepare_complete([issue_response(volume={'id': '999'})])
        with self.assertRaises(ValueError):
            self.acquire()

    def test_partial_page_rejected(self):
        self.response.json.side_effect = [
            envelope(volume_response(count_of_issues=2, issues=[{'id': '301'}, {'id': '302'}])),
            envelope([issue_response()], number_of_total_results=2)]
        with self.assertRaises(MetadataProviderError):
            self.acquire()

    def test_error_page_is_not_empty_success(self):
        self.response.json.side_effect = [
            envelope(volume_response(count_of_issues=1, issues=[{'id': '301'}])),
            envelope([], status_code=999, number_of_total_results=0)]
        with self.assertRaises(MetadataProviderError):
            self.acquire()

    def test_complete_two_pages_and_missing_second_page(self):
        issues = [issue_response(id=str(301 + i)) for i in range(101)]
        volume = envelope(volume_response(count_of_issues=101, issues=[{'id': row['id']} for row in issues]))
        self.response.json.side_effect = [volume,
            envelope(issues[:100], number_of_total_results=101, offset=0),
            envelope(issues[100:], number_of_total_results=101, offset=100)]
        self.assertEqual(len(self.acquire().data.view()['issues']), 101)
        self.assertEqual(self.session.get.await_count, 3)
        self.response.json.side_effect = [volume,
            envelope(issues[:100], number_of_total_results=101),
            envelope([], number_of_total_results=101, offset=100)]
        with self.assertRaises(MetadataProviderError):
            self.acquire()

    def test_changed_pagination_count_rejected(self):
        issues = [issue_response(id=str(301 + i)) for i in range(101)]
        self.response.json.side_effect = [
            envelope(volume_response(count_of_issues=101, issues=[{'id': row['id']} for row in issues])),
            envelope(issues[:100], number_of_total_results=101),
            envelope(issues[100:], number_of_total_results=102, offset=100)]
        with self.assertRaises(MetadataProviderError):
            self.acquire()


class MetronReviewAdmission(MetronHarness, TestCase):
    def test_complete_once_no_cache_write_or_background_budget(self):
        before = self.state()
        result = run(MetronMetadataProvider().fetch_review('700', 10000))
        admitted = admit(result, ProviderReference('metron', '700'))
        self.assertEqual(len(admitted.data.view()['issues']), 2)
        self.assertEqual(self.http.get.call_count, 4)
        self.assertFalse((self.root / 'cache').exists())
        self.assertNotIn(b'unit-not-a-credential', admitted.data.payload)
        self.assertEqual(self.state(), before)

    def test_count_mismatch(self):
        self.series['issue_count'] = 3
        with self.assertRaises(MetronError):
            run(MetronMetadataProvider().fetch_review('700', 10000))
        self.assertEqual(self.http.get.call_count, 2)

    def test_duplicate(self):
        self.issues[1] = self.issues[0]
        with self.assertRaises(MetronError):
            run(MetronMetadataProvider().fetch_review('700', 10000))
        self.assertEqual(self.http.get.call_count, 2)

    def test_warm_cache_read_retains_cost_and_bytes(self):
        run(MetronMetadataProvider().fetch_volume_enriched('700'))
        before = {p.name: p.read_bytes() for p in (self.root / 'cache').iterdir()}
        self.http.get.reset_mock()
        run(MetronMetadataProvider().fetch_review('700', 10000))
        self.assertEqual(self.http.get.call_count, 2)
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / 'cache').iterdir()}, before)


class GcdReviewAdmission(GcdLifecycleHarness, TestCase):
    def test_reuses_admitted_rich_snapshot_no_library_write(self):
        before = self.state()
        result = run(acquire_target(ProviderReference('gcd', '1')))
        value = result.data.view()
        self.assertEqual(value['issues'][0]['issue_number'], '[nn]')
        self.assertIsNone(value['issues'][0]['calculated_issue_number'])
        self.assertEqual(self.state(), before)
        self.assertTrue(value['receipt']['policy'])
        self.assertEqual(len(self.fake.requests), 4)  # series twice, publisher, one issue
        self.scan.assert_not_called()
        self.assertFalse(list(self.root.iterdir()))

    def test_rich_snapshot_in_complete_preview(self):
        variant = deepcopy(self.fake.issues['1'])
        variant.update(api_url=self.fake.base + 'issue/2/', number='1A',
                       variant_of=self.fake.base + 'issue/1/')
        self.fake.issues['2'] = variant
        self.fake.series['active_issues'].append(self.fake.base + 'issue/2/')
        self.db.execute("""INSERT INTO volumes(id,title,root_folder,folder,metadata_provider)
            VALUES(1,'Source',1,'/fixture/source','metron')""")
        self.db.execute("INSERT INTO volume_external_ids(volume_id,provider,provider_id,provenance) VALUES(1,'metron','100','fixture')")
        self.db.execute("INSERT INTO issues(id,volume_id,issue_number) VALUES(1,1,'[nn]')")
        self.db.execute("INSERT INTO issue_external_ids VALUES(1,'metron','101','fixture')")
        self.db.commit()
        service = ProviderSwitchReviews(task_observer=lambda _: ())
        session = run(service.create(self.db.cursor(), 1, 'gcd', '1'))
        before_requests = len(self.fake.requests)
        result = service.revise(self.db.cursor(), session.id, 1, {1: '1'}).preview.view()
        row = result['issues'][0]['target']
        self.assertEqual(row['facts']['number']['raw_label'], '[nn]')
        self.assertIsNone(row['calculated_issue_number'])
        self.assertIsNone(row['date'])
        self.assertTrue(row['facts']['dates'])
        self.assertEqual(result['target_only'][0]['variant_of']['provider_id'], '1')
        self.assertEqual(before_requests, 5)
        self.assertEqual(len(self.fake.requests), before_requests)
        self.assertTrue(result['apply_available'])
