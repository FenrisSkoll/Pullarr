"""GCD-owned local facts use the unchanged production Wanted/SAB/intake stack."""

from asyncio import run
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from Tbackend.features import intake_production, wanted_automation
from Tbackend.features.sab_downloads import SABHarness
from Tbackend.implementations.gcd_client import FakeGcd

from backend.implementations.metadata.gcd import GcdMetadataProvider
from backend.implementations.metadata.gcd_client import GcdClient
from backend.implementations.metadata.snapshot_persistence import \
    persist_snapshot_facts


class GcdWantedTests(SABHarness, TestCase):
    def seed(self):
        registry = patch.dict('backend.implementations.metadata.registry.PROVIDERS', {'gcd': GcdMetadataProvider})
        registry.start()
        self.addCleanup(registry.stop)
        intake_production.SABIntakeProduction.seed(self)
        self.store.db.execute("UPDATE config SET value=60 WHERE key='database_version'")
        fake = FakeGcd()
        try:
            template = fake.issues['1']
            fake.issues = {}
            fake.series.update(name='Batman', year_began=2016, active_issues=[])
            for iid in ('5', '6'):
                row = deepcopy(template)
                row.update(api_url=fake.base + 'issue/' + iid + '/', number=iid,
                           key_date='2016-12-00', on_sale_date=None,
                           isbn='9780306406157', page_count='48',
                           story_set=[{'sequence_number': 1, 'title': 'Batman #5',
                                       'type': 'comic story', 'script': 'Same credit'}])
                fake.issues[iid] = row
                fake.series['active_issues'].append(row['api_url'])
            provider = GcdMetadataProvider(lambda: GcdClient(base=fake.base, session=fake.session(),
                charge=lambda: None, preflight=lambda n: None, limited=lambda value: None))
            snapshot = run(provider.fetch_snapshot('1'))
        finally:
            fake.close()
        db = self.store.db
        # This fixture starts with already-local GCD-owned IDs, as a restored
        # library would. Separate lifecycle tests cover production add/refresh.
        db.execute('BEGIN')
        db.execute("UPDATE volumes SET metadata_provider='gcd' WHERE id=1")
        db.execute("INSERT INTO volume_external_ids VALUES(1,'gcd','1','provider',100)")
        db.executemany("INSERT INTO issue_external_ids VALUES(?,'gcd',?,'provider')", ((5, '5'), (6, '6')))
        with patch('backend.implementations.metadata.snapshot_persistence.get_db', side_effect=db.cursor), \
                patch('backend.internals.provider_identity.get_db', side_effect=db.cursor):
            persist_snapshot_facts(snapshot, 1)
        db.commit()
        # The service is already offline. Any metadata fetch in release search,
        # scoring, organization or satisfaction is an error, not a fallback.
        guard = patch('backend.implementations.metadata.gcd.GcdMetadataProvider.fetch_snapshot',
                      side_effect=AssertionError('No GCD metadata acquisition during Wanted'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_gcd_owned_target_sab_intake_organizer_satisfaction(self):
        wanted_automation.WantedProductionTests.flow(self)

    def test_gcd_wrong_artifact_review_holds_reservation(self):
        wanted_automation.WantedProductionTests.flow(self, wrong=True)
