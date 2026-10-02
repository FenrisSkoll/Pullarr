"""Precision, exact identities, observational preservation and migration gates."""

import sqlite3
from unittest import TestCase
from unittest.mock import patch

import TCollections as fixtures

from backend.base.issue_facts import (BibliographicDate, DateKind,
                                      IssueFacts, IssueNumberFacts)
from backend.base.release_calendar import CalendarError, observation
from backend.internals.db import DB_SCHEMA, SCHEMA_67
from backend.internals.db_migration import _migrate_release_calendar
from backend.internals.issue_facts import write_facts
from backend.internals.release_calendar import CalendarStore


class CalendarTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.CollectionsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.store = CalendarStore(self.db.cursor(), clock=lambda: 1000)
        self.node = self.fixture.child(monitoring='monitored')

    def external(self, identity='900'):
        return self.fixture.accept(self.fixture.suggestion(identity, self.node))

    def observed(self, publication, value, provider='comicvine', kind=DateKind.ON_SALE):
        subject = next(s for s in self.store.subjects() if publication in s['publications'] and s['provider'] == provider)
        e = observation(BibliographicDate.interpret(value, kind, 'fixture', 'store_date'), provider, '9001', 1000)
        self.store.persist(subject, [dict(provider_id='9001', evidence=[e])])

    def page(self, **kwargs):
        return self.store.page(start='2027-01-01', end='2027-12-31', **kwargs)

    def issue(self, value='2027-03-17'):
        self.db.execute('UPDATE volumes SET monitored=1 WHERE id=1')
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored,date) VALUES(1,1,1001,'1',1,1,?)", (value,))
        write_facts(self.db.cursor(), 1, IssueFacts(IssueNumberFacts.interpret('1', 'fixture', 'number'),
            (BibliographicDate.interpret(value, DateKind.ON_SALE, 'fixture', 'store_date'),), 'store_date'))
        self.db.commit()

    def test_precision_movement_stable_subject_and_no_fake_days(self):
        publication = self.external()
        for value, precision in ((None, 'unknown'), ('2027', 'year'), ('2027-03', 'month'), ('2027-03-17', 'day'), ('2027-03-24', 'day')):
            self.observed(publication, value)
            item = self.page(unknown=value is None)['items'][0]
            self.assertEqual(item['id'], 'publication:' + str(publication))
            self.assertEqual(item['effective']['date'], value)
            self.assertEqual(item['effective']['precision'], precision)
        detail = self.store.detail('publication:' + str(publication))
        self.assertEqual(detail['evidence'][0]['previous_date'], '2027-03-17')
        self.observed(publication, None)
        self.assertTrue(self.page()['items'][0]['stale'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM release_events').fetchone()[0], 1)

    def test_local_canonical_refresh_monitoring_and_read_only(self):
        self.issue()
        before = list(self.db.iterdump())
        item = self.page()['items'][0]
        self.assertEqual(item['id'], 'issue:1')
        self.assertTrue(item['wanted'])
        self.assertFalse(item['file_owned'])
        self.assertEqual(before, list(self.db.iterdump()))
        self.db.execute('UPDATE issues SET monitored=0 WHERE id=1')
        self.assertFalse(self.page()['items'])

    def test_pending_rejected_and_empty_nodes_are_not_events(self):
        suggestion = self.fixture.suggestion(node=self.node)
        self.assertFalse(self.store.subjects())
        self.assertFalse(self.page(unknown=True)['items'])
        self.fixture.store.decide(suggestion['id'], 0, self.fixture.revision(), 'rejected')
        self.assertFalse(self.store.subjects())

    def test_external_add_exact_link_preserves_event(self):
        publication = self.external('101')
        self.observed(publication, '2027-03-17')
        self.issue()
        # Exact historical provider ID links to existing volume without fuzzy matching.
        item = self.page()['items'][0]
        self.assertEqual(item['id'], 'publication:' + str(publication))
        self.assertEqual(item['status'], 'in_library')
        self.assertEqual(len(self.page()['items']), 1)

    def test_repeat_sync_domain_preservation_and_multi_issue_unavailable(self):
        publication = self.external()
        before = self.db.execute('SELECT * FROM volumes').fetchall()
        self.observed(publication, '2027-03')
        self.observed(publication, '2027-03')
        self.assertEqual(self.db.execute('SELECT count(*) FROM release_event_evidence').fetchone()[0], 1)
        self.assertEqual(before, self.db.execute('SELECT * FROM volumes').fetchall())
        subject = self.store.subjects()[0]
        self.store.persist(subject, [dict(provider_id='1', evidence=[]), dict(provider_id='2', evidence=[])])
        self.assertTrue(self.page()['items'][0]['stale'])

    def test_bounds(self):
        for kwargs in ({'limit': 0}, {'limit': 101}, {'offset': -1}, {'start': '2027-03'}, {'start': '2020-01-01', 'end': '2027-01-01'}):
            with self.assertRaises(CalendarError):
                self.store.page(**kwargs)

    def test_c2_coverage_never_establishes_external_publication_ownership(self):
        from backend.base.content_claims import ClaimKind, PublicationRef
        from backend.internals.content_claims import (claim_preview,
                                                      confirm_claim)
        self.issue()
        self.db.execute("INSERT INTO files(id,filepath,size) VALUES(1,'fixture.cbz',1)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder) VALUES(2,900,'Omnibus',1,'omnibus')")
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number) VALUES(2,2,901,'1')")
        self.db.commit()
        preview = claim_preview(self.db.cursor(), 2, PublicationRef('comicvine', '1001'), ClaimKind.COMPLETE, manual=True)
        confirm_claim(self.db.cursor(), 2, PublicationRef('comicvine', '1001'), ClaimKind.COMPLETE, preview['preview_token'], manual=True)
        self.db.execute("INSERT INTO bibliographic_graph_snapshots VALUES('fixture','comicvine','fixture','fixture','fixture',0,0,0,0,0,0,'fixture')")
        self.db.execute("INSERT INTO bibliographic_issue_refs VALUES('comicvine','901','900','Omnibus','1','',0,'fixture')")
        self.db.execute('DELETE FROM volumes WHERE id=2')
        self.db.commit()
        publication = self.external()
        self.observed(publication, '2027-03-17')
        before = list(self.db.iterdump())
        detail = self.store.detail('publication:' + str(publication))
        self.assertEqual(detail['status'], 'external')
        self.assertEqual(detail['content_context']['state'], 'complete_known_content')
        self.assertEqual(before, list(self.db.iterdump()))

    def test_actual_switch_and_aba_preserve_canonical_event(self):
        import TProviderSwitchApply as switch
        fixture = switch.SwitchApplyTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.source()
        fixture.db.execute('UPDATE volumes SET monitored=1')
        fixture.db.commit()
        store = CalendarStore(fixture.cursor)
        before = store.page(unknown=True)['items'][0]['id']
        fixture.apply(fixture.review(switch.remote('metron')))
        first = store.page(start='2020-01-01', end='2020-12-31')['items'][0]
        self.assertEqual(first['id'], before)
        fixture.apply(fixture.review(switch.remote('comicvine', '100', 101)))
        after = store.page(start='2020-01-01', end='2020-12-31')['items']
        self.assertEqual([e['id'] for e in after], [before])
        fixture.assert_integrity()

    def test_actual_metadata_repair_reflects_canonical_date_and_title(self):
        import TMetadataRepairApply as repair
        fixture = repair.MetadataRepairApplyTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        from backend.internals.collections import CollectionStore
        collections = CollectionStore(fixture.cursor)
        tree = collections.create('Repair display', monitoring='monitored')
        collections.add_local(tree['nodes'][0]['id'], 0, 1)
        store = CalendarStore(fixture.cursor)
        ids = {e['id'] for e in store._events()}
        self.assertTrue(ids)
        fixture.apply(fixture.review())
        self.assertEqual(ids, {e['id'] for e in store._events()})
        self.assertTrue(all(e['publication_title'] == 'Target comicvine' for e in store._events()))

    def test_monitor_inheritance_multi_membership_and_volume_separation(self):
        publication = self.external()
        self.observed(publication, '2027-03-17')
        other = self.fixture.store.create('Other', monitoring='monitored')
        self.fixture.store.membership(other['nodes'][0]['id'], 0, publication, 'add')
        self.assertEqual(len(self.page()['items']), 1)
        self.assertEqual(len(self.page()['items'][0]['memberships']), 2)
        self.assertEqual(self.db.execute('SELECT monitored FROM volumes WHERE id=1').fetchone()[0], 0)
        self.fixture.store.delete_node(other['id'], 1, other['nodes'][0]['id'], True)
        self.assertEqual(len(self.page()['items']), 1)

    def test_two_exact_sources_conflict_preference_and_no_http_in_reads(self):
        publication = self.external()
        self.db.execute("INSERT INTO collection_publication_refs VALUES(?,'metron','950','exact_local_identity')", (publication,))
        self.db.commit()
        self.observed(publication, '2027-03-17')
        self.observed(publication, '2027-04-01', 'metron')
        with patch('backend.implementations.metadata.registry.get_volume_provider', side_effect=AssertionError('No HTTP')):
            item = self.page()['items'][0]
            detail = self.store.detail(item['id'])
        self.assertTrue(item['multiple_source_dates'])
        self.assertEqual(len(detail['evidence']), 2)
        self.assertEqual(item['effective']['date'], '2027-03-17')

    def test_stale_authority_after_acquisition_blocks_all_provider_writes(self):
        self.issue()
        subject = self.store.subjects()[0]
        self.db.execute('UPDATE volumes SET authority_generation=authority_generation+1 WHERE id=1')
        self.db.commit()
        with self.assertRaisesRegex(CalendarError, 'stale_authority'):
            self.store.persist(subject, [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM release_events').fetchone()[0], 0)


class CalendarMigrationTests(TestCase):
    def test_fresh_upgrade_rollback_and_parity(self):
        from backend.internals.release_calendar_schema import STATEMENTS
        for failure in range(len(STATEMENTS) + 1):
            db = sqlite3.connect(':memory:')
            try:
                db.execute('PRAGMA foreign_keys=ON')
                db.executescript(SCHEMA_67)
                db.execute("INSERT INTO config(key,value) VALUES('database_version',67)")
                db.commit()
                original = list(db.iterdump())
                cursor = db.cursor()
                class Failing:
                    count = 0
                    def execute(self, sql, args=()):
                        if sql in STATEMENTS:
                            if self.count == failure:
                                raise RuntimeError('injected statement failure')
                            self.count += 1
                        return cursor.execute(sql, args)
                with patch('backend.internals.db_migration.get_db', return_value=Failing()):
                    if failure < len(STATEMENTS):
                        with self.assertRaises(RuntimeError):
                            _migrate_release_calendar()
                        self.assertEqual(original, list(db.iterdump()))
                    else:
                        _migrate_release_calendar()
                        self.assertEqual(int(db.execute("SELECT value FROM config WHERE key='database_version'").fetchone()[0]), 68)
                        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                        self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
                        fresh = sqlite3.connect(':memory:')
                        try:
                            fresh.executescript(DB_SCHEMA)
                            query = "SELECT name,sql FROM sqlite_master WHERE name LIKE 'release_%' OR name LIKE 'calendar_%' ORDER BY name"
                            self.assertEqual(db.execute(query).fetchall(), fresh.execute(query).fetchall())
                        finally:
                            fresh.close()
            finally:
                db.close()
