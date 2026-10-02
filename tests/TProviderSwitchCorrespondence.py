"""Pure identity contract: no title/number matching or application effects."""

import unittest

from backend.base.provider_switch import (CorrespondenceKind,
                                          ProviderReference,
                                          StoredIssueIdentity,
                                          TargetIssueIdentity, correspondence)


class ProviderSwitchCorrespondenceTests(unittest.TestCase):
    def setUp(self):
        self.source = ProviderReference('metron', 'volume-A')
        self.target = ProviderReference('gcd', 'volume-B')

    def local(self, local_id, source_id, target_id=None):
        selected = ProviderReference(self.source.provider, source_id)
        refs = ((selected, 'provider'),)
        if target_id is not None:
            refs += ((ProviderReference(self.target.provider, target_id), 'source_reported'),)
        return StoredIssueIdentity(local_id, selected, refs)

    def remote(self, target_id, source_id=None):
        assertions = () if source_id is None else ((ProviderReference(
            self.source.provider, source_id), 'target_reported'),)
        return TargetIssueIdentity(ProviderReference(self.target.provider, target_id), self.target, assertions)

    def plan(self, local, remote, overrides=None):
        return correspondence(self.source, self.target, tuple(local), tuple(remote), overrides)

    def test_exact_existing_identity_retains_provenance(self):
        plan = self.plan([self.local(42, 'a', 'b')], [self.remote('b')])
        self.assertTrue(plan.ready)
        self.assertEqual(plan.issues[0].local_id, 42)
        self.assertEqual(plan.issues[0].kind, CorrespondenceKind.EXISTING)
        self.assertEqual(plan.issues[0].evidence, ('source_reported',))

    def test_target_reported_and_mutual_are_distinct(self):
        for existing, kind in ((None, CorrespondenceKind.TARGET_REPORTED), ('b', CorrespondenceKind.MUTUAL)):
            plan = self.plan([self.local(42, 'a', existing)], [self.remote('b', 'a')])
            self.assertTrue(plan.ready)
            self.assertEqual(plan.issues[0].kind, kind)

    def test_manual_exact_ids_and_target_only_addition(self):
        plan = self.plan([self.local(7, '[nn]'), self.local(42, '1A')],
                         [self.remote('01'), self.remote('Annual'), self.remote('new')],
                         {7: 'Annual', 42: '01'})
        self.assertTrue(plan.ready)
        self.assertEqual([r.local_id for r in plan.issues], [7, 42])
        self.assertEqual([r.kind for r in plan.issues], [CorrespondenceKind.OPERATOR] * 2)
        self.assertEqual(plan.target_only, (ProviderReference('gcd', 'new'),))

    def test_equal_ids_in_different_namespaces_do_not_map(self):
        plan = self.plan([self.local(1, '5')], [self.remote('5')])
        self.assertFalse(plan.ready)
        self.assertIsNone(plan.issues[0].target)

    def test_missing_counterpart_cannot_delete_existing_issue(self):
        plan = self.plan([self.local(1, 'a', 'b'), self.local(2, 'c')], [self.remote('b')])
        self.assertFalse(plan.ready)
        self.assertEqual(len(plan.issues), 2)

    def test_established_identity_cannot_be_replaced_manually(self):
        plan = self.plan([self.local(1, 'a', 'b')], [self.remote('b'), self.remote('c')], {1: 'c'})
        self.assertFalse(plan.ready)
        self.assertIn('established_target_identity_conflict', plan.issues[0].blockers)

    def test_missing_established_identity_cannot_be_repaired(self):
        plan = self.plan([self.local(1, 'a', 'b')], [self.remote('c')], {1: 'c'})
        self.assertFalse(plan.ready)
        self.assertIn('established_target_identity_absent', plan.issues[0].blockers)

    def test_competing_assertions_have_no_arbitrary_winner(self):
        plan = self.plan([self.local(1, 'a')], [self.remote('b', 'a'), self.remote('c', 'a')])
        self.assertFalse(plan.ready)
        self.assertEqual(plan.issues[0].kind, CorrespondenceKind.CONFLICT)
        resolved = self.plan([self.local(1, 'a')], [self.remote('b', 'a'), self.remote('c', 'a')], {1: 'c'})
        self.assertTrue(resolved.ready)
        self.assertEqual(resolved.issues[0].target.provider_id, 'c')

    def test_contradictory_established_reference_blocks(self):
        plan = self.plan([self.local(1, 'a', 'b')], [self.remote('b'), self.remote('c', 'a')])
        self.assertFalse(plan.ready)

    def test_one_target_cannot_serve_two_local_issues(self):
        target = TargetIssueIdentity(ProviderReference('gcd', 'b'), self.target,
            ((ProviderReference('metron', 'a'), 'target'), (ProviderReference('metron', 'c'), 'target')))
        plan = self.plan([self.local(1, 'a'), self.local(2, 'c')], [target])
        self.assertFalse(plan.ready)
        self.assertTrue(all(row.kind == CorrespondenceKind.CONFLICT for row in plan.issues))

    def test_duplicate_manual_target_rejected(self):
        with self.assertRaises(ValueError):
            self.plan([self.local(1, 'a'), self.local(2, 'c')], [self.remote('b')], {1: 'b', 2: 'b'})

    def test_unknown_manual_local_or_target_rejected(self):
        for mapping in ({2: 'b'}, {1: 'unknown'}, {True: 'b'}):
            with self.assertRaises(ValueError):
                self.plan([self.local(1, 'a')], [self.remote('b')], mapping)

    def test_duplicate_wrong_parent_or_namespace_rejected(self):
        wrong = TargetIssueIdentity(ProviderReference('gcd', 'b'), ProviderReference('gcd', 'other'))
        for targets in ([self.remote('b'), self.remote('b')], [wrong]):
            with self.assertRaises(ValueError):
                self.plan([self.local(1, 'a')], targets)

    def test_opaque_case_sensitive_ids_remain_distinct(self):
        plan = self.plan([self.local(1, 'x', 'A')], [self.remote('a'), self.remote('A')])
        self.assertTrue(plan.ready)
        self.assertEqual(plan.issues[0].target.provider_id, 'A')
        self.assertEqual(plan.target_only[0].provider_id, 'a')

    def test_same_provider_is_not_switching(self):
        with self.assertRaises(ValueError):
            correspondence(self.source, ProviderReference('metron', 'different'), (), ())

    def test_all_six_provider_directions(self):
        providers = ('comicvine', 'metron', 'gcd')
        for source in providers:
            for target in providers:
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    self.source = ProviderReference(source, 'volume-source')
                    self.target = ProviderReference(target, 'volume-target')
                    plan = self.plan([self.local(91, 'issue-source', 'issue-target')], [self.remote('issue-target')])
                    self.assertTrue(plan.ready)
                    self.assertEqual(plan.issues[0].local_id, 91)

    def test_ten_thousand_linear_correspondences(self):
        plan = self.plan([self.local(i + 1, str(i), str(i)) for i in range(10000)],
                         [self.remote(str(i)) for i in range(10000)])
        self.assertTrue(plan.ready)
        self.assertEqual(len(plan.issues), 10000)
        self.assertFalse(plan.target_only)

    def test_bounds_and_empty_volume(self):
        self.assertTrue(self.plan([], []).ready)
        with self.assertRaises(ValueError):
            self.plan([], [self.remote(str(i)) for i in range(10001)])
        for identity in ('', 'a\n', 'x' * 129):
            with self.assertRaises(ValueError):
                ProviderReference('gcd', identity)
