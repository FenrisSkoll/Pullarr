"""Same-authority field characterization; no mutation service is invoked."""

from dataclasses import FrozenInstanceError
from unittest import TestCase

import TProviderSwitchApply as fixture

from backend.base.metadata_repair import (Field, FieldSelection,
                                          RepairError, validate_selection)
from backend.base.provider_switch import ProviderReference
from backend.base.switch_review import FrozenReviewData
from backend.implementations.metadata.repair_fields import field_review
from backend.implementations.metadata.switch_target import admit
from backend.internals.switch_review import load_local


class MetadataRepairFieldTests(TestCase):
    def make(self, provider='comicvine'):
        source = fixture.SwitchApplyTests()
        source.setUp()
        self.addCleanup(source.doCleanups)
        source.source(provider)
        target = admit(fixture.remote(provider, parent='100', first=101), ProviderReference(provider, '100'))
        local = load_local(source.cursor, 1, target)
        return source, local, target

    def test_provider_ownership_and_no_mutation(self):
        for provider in ('comicvine', 'metron', 'gcd'):
            with self.subTest(provider=provider):
                source, local, target = self.make(provider)
                before = tuple(source.db.iterdump()), source.unchanged_files()
                fields, owners = field_review(local, target)
                rows = {f.selection.key: f.view() for f in fields}
                self.assertEqual(owners, {'101': 1, '102': 2})
                self.assertEqual(rows['volume:1:title']['after'], 'Target ' + provider)
                expected = 'unsupported' if provider == 'gcd' else 'supported'
                for key in ('volume:1:alt_title', 'volume:1:volume_number', 'issue:1:description'):
                    self.assertEqual(rows[key]['support'], expected)
                for key in ('metadata_provider', 'authority_generation', 'monitored', 'root_folder', 'special_version_locked'):
                    self.assertFalse(any(f.selection.field.value == key for f in fields))
                self.assertEqual(before, (tuple(source.db.iterdump()), source.unchanged_files()))

    def test_exact_identity_required(self):
        _, local, target = self.make()
        value = local.view()
        value['selected']['provider_id'] = 'other'
        with self.assertRaisesRegex(RepairError, 'same_authority'):
            field_review(FrozenReviewData.create(value), target)
        value = local.view()
        value['issue_refs'] = []
        with self.assertRaisesRegex(RepairError, 'issue_identity'):
            field_review(FrozenReviewData.create(value), target)

    def test_rich_facts_are_coupled_not_flattened(self):
        _, local, target = self.make('gcd')
        fields, _ = field_review(local, target)
        row = next(f for f in fields if f.selection.key == 'issue:1:canonical_facts').view()['after']
        self.assertEqual(row['issue_number'], '1A')
        self.assertIsNone(row['calculated_issue_number'])
        self.assertIsNone(row['date'])
        self.assertEqual(row['facts']['dates'][0]['precision'], 'month')
        variant = next(f for f in fields if f.selection.key == 'issue:2:canonical_facts').view()['after']
        self.assertEqual(variant['variant_of']['provider_id'], '101')

    def test_variant_change_with_claim_is_blocked(self):
        _, local, target = self.make('gcd')
        value = local.view()
        value['claims'] = [dict(retired_at=None, target_provider='gcd', target_provider_id='102',
                                source_provider='gcd', source_provider_id='101')]
        fields, _ = field_review(FrozenReviewData.create(value), target)
        row = next(f for f in fields if f.selection.key == 'issue:2:canonical_facts')
        self.assertEqual(row.support, 'blocked')
        with self.assertRaisesRegex(RepairError, 'unsupported'):
            validate_selection(fields, (row.selection,))

    def test_selection_fixed_keys_and_immutable_views(self):
        _, local, target = self.make()
        fields, _ = field_review(local, target)
        selection = FieldSelection('volume', 1, Field.TITLE)
        self.assertEqual(validate_selection(fields, (selection,)), ('volume:1:title',))
        for bad in ((selection, selection), (FieldSelection('volume', 99, Field.TITLE),)):
            with self.assertRaises(RepairError):
                validate_selection(fields, bad)
        with self.assertRaises(RepairError):
            FieldSelection('volume', 1, 'metadata_provider')
        with self.assertRaises(FrozenInstanceError):
            fields[0].support = 'anything'
        view = fields[0].view()
        view['after'] = 'modified'
        self.assertNotEqual(fields[0].view()['after'], 'modified')

    def test_missing_values_follow_owned_scalar_contract(self):
        _, local, target = self.make()
        payload = target.data.view()
        payload['volume']['publisher'] = None
        payload['volume']['site_url'] = None
        from dataclasses import replace
        target = replace(target, data=FrozenReviewData.create(payload))
        fields, _ = field_review(local, target)
        rows = {f.selection.key: f.view() for f in fields}
        self.assertIsNone(rows['volume:1:publisher']['after'])
        self.assertEqual(rows['volume:1:publisher']['support'], 'supported')
        self.assertEqual(rows['volume:1:site_url']['support'], 'unsupported')
        self.assertEqual(rows['volume:1:site_url']['after'], rows['volume:1:site_url']['before'])
        value = local.view()
        value['volume']['site_url'] = 'https://fixture:private@example.invalid/title'
        with self.assertRaisesRegex(RepairError, '^unsafe_existing_site_url_requires_separate_review$'):
            field_review(FrozenReviewData.create(value), target)
