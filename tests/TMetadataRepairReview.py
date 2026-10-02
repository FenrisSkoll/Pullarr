"""Fresh read-only repair review from actual health/worklist fixtures."""

import asyncio
from dataclasses import replace
from unittest import TestCase

import TMaintenanceReview as maintenance_fixture
import TProviderSwitchApply as provider_fixture

from backend.base.maintenance_review import Action
from backend.base.metadata_repair import Field, FieldSelection, RepairError
from backend.features.metadata_repair import MetadataRepairReviews
from backend.implementations.metadata.switch_target import admit


class MetadataRepairReviewTests(TestCase):
    def setUp(self):
        self.fixture = maintenance_fixture.MaintenanceReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.fixture.comic('noncanonical.cbz')
        self.worklist = self.fixture.assign(self.fixture.review(), Action.METADATA)
        self.db = self.fixture.db
        self.cursor = self.db.cursor()
        self.fetches = 0
        self.clock = 0
        async def acquire(reference):
            self.fetches += 1
            return admit(provider_fixture.remote(reference.provider, parent=reference.provider_id, first=101, count=1), reference)
        self.service = MetadataRepairReviews(self.fixture.service, acquire=acquire, clock=lambda: self.clock)

    def create(self):
        return asyncio.run(self.service.create(self.cursor, self.worklist.id, self.worklist.revision,
            self.worklist.manifest_digest, tuple(i.finding.id for i in self.worklist.items if i.selected)))

    def test_owned_handoff_selection_and_no_mutation(self):
        before = tuple(self.db.iterdump()), self.fixture.fixture.filesystem()
        session = self.create()
        self.assertEqual(self.fetches, 1)
        self.assertFalse(session.preview.view()['apply_available'])
        revised = self.service.revise(self.cursor, session.id, 0, (FieldSelection('volume', 1, Field.TITLE),))
        self.assertEqual(len(revised.preview.view()['changes']), 1)
        self.assertNotEqual(session.digest, revised.digest)
        self.assertEqual(self.fetches, 1)
        self.assertEqual(before, (tuple(self.db.iterdump()), self.fixture.fixture.filesystem()))
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_revision_stale_expiry_and_restart(self):
        session = self.create()
        self.service.revise(self.cursor, session.id, 0, ())
        with self.assertRaisesRegex(RepairError, 'revision'):
            self.service.revise(self.cursor, session.id, 0, ())
        self.clock = 1000
        with self.assertRaisesRegex(RepairError, 'expired'):
            self.service.get(self.cursor, session.id)
        with self.assertRaisesRegex(RepairError, 'unavailable'):
            MetadataRepairReviews(self.fixture.service).get(self.cursor, session.id)

    def test_aba_stale_latch(self):
        session = self.create()
        self.db.execute('UPDATE volumes SET authority_generation=2 WHERE id=1')
        self.db.commit()
        with self.assertRaisesRegex(RepairError, 'stale'):
            self.service.get(self.cursor, session.id)
        # Even artificially restoring state cannot revive an observed stale session.
        self.db.execute('UPDATE volumes SET authority_generation=0 WHERE id=1')
        self.db.commit()
        with self.assertRaisesRegex(RepairError, 'stale'):
            self.service.revise(self.cursor, session.id, 0, ())

    def test_changes_during_acquisition_rejected(self):
        old = self.service.acquire
        async def acquire(reference):
            target = await old(reference)
            self.db.execute("UPDATE issues SET title='changed' WHERE id=1")
            self.db.commit()
            return target
        self.service.acquire = acquire
        with self.assertRaisesRegex(RepairError, 'source_changed'):
            self.create()
        self.assertEqual(self.service._sessions, {})

    def test_unsupported_client_fields_and_unknown_findings(self):
        with self.assertRaisesRegex(RepairError, 'handoff'):
            asyncio.run(self.service.create(self.cursor, self.worklist.id, self.worklist.revision,
                self.worklist.manifest_digest, ('0' * 64,)))
        self.assertEqual(self.fetches, 0)
        session = self.create()
        with self.assertRaisesRegex(RepairError, 'unsupported'):
            self.service.revise(self.cursor, session.id, 0, (FieldSelection('volume', 1, Field.FACTS),))

    def test_locked_and_excluded_classification_inputs(self):
        self.db.execute('UPDATE volumes SET special_version_locked=1 WHERE id=1')
        self.db.commit()
        self.worklist = self.fixture.assign(self.fixture.review(), Action.METADATA)
        session = self.create()
        selected = (FieldSelection('volume', 1, Field.TITLE),)
        revised = self.service.revise(self.cursor, session.id, 0, selected)
        self.assertEqual(revised.preview.view()['classification']['action'], 'preserve_locked_value_and_receipt')
        self.assertEqual(revised.local.view()['volume']['title'], 'Example')
        self.assertEqual(revised.preview.view()['changes'][0]['after'], 'Target comicvine')

    def test_bounds_and_pagination_are_detached(self):
        session = self.create()
        view = session.page(limit=1)
        view['items'][0]['after'] = 'not stored'
        self.assertNotEqual(session.page(limit=1)['items'][0]['after'], 'not stored')
        with self.assertRaises(RepairError):
            session.page(limit=101)
        self.service.MAX_BYTES = 1
        with self.assertRaisesRegex(RepairError, 'size_limit'):
            self.service.revise(self.cursor, session.id, 0, ())
        self.assertEqual(self.service.get(self.cursor, session.id).revision, 0)

    def test_no_refetch_on_unlocked_field_selection(self):
        session = self.create()
        revised = self.service.revise(self.cursor, session.id, 0, (FieldSelection('volume', 1, Field.PUBLISHER),))
        self.assertEqual(revised.preview.view()['classification']['action'], 'preserve_no_classification_input_change')
        self.assertEqual(self.fetches, 1)
