"""Offline folder policy: compatibility, ownership, safety and planning contracts."""

import json
from dataclasses import FrozenInstanceError, replace
from unittest import TestCase
from unittest.mock import patch

from Tbackend.features.identification import candidate, reference
from Tbackend.features.organization_plan import (NAMING, context_for,
                                                 identified, records)

from backend.base.definitions import SpecialVersion
from backend.base.folder_policy import (FolderCode, FolderMode, FolderPolicy,
                                        FolderPublication, FolderStatus,
                                        MissingFolderValue)
from backend.base.identification import MatchState, PublicationMatch
from backend.base.organization_plan import PlanningPolicy, PlanStatus
from backend.implementations.folder_policy import (TOKENS, FolderContext,
                                                   decide_folder,
                                                   preview_folder)
from backend.implementations.naming import generate_volume_folder_name
from backend.implementations.organization_plan import (PlanningContext,
                                                       _naming_data, plan_many,
                                                       plan_one, preview_plan)


def publication(**kwargs):
    return replace(FolderPublication('Batman', 2016, 2, 'DC Comics', reference('ABC', 'metron'),
                                     1, 1, None, False, 123), **kwargs)


def context(**kwargs):
    return FolderContext.build(((1, '/library'), (2, '/other')), (), NAMING, **kwargs)


def codes(decision):
    return {d.code for d in decision.diagnostics}


class FolderPolicyTests(TestCase):
    def test_legacy_default_nested_template(self):
        ctx = replace(context(), naming=replace(NAMING, volume_folder_naming='{series_name}/Volume {volume_number} ({year})'))
        result = decide_folder(publication(), ctx)
        self.assertEqual(result.target_folder, '/library/Batman/Volume 02 (2016)')
        self.assertEqual(result.status, FolderStatus.CALCULATED)

    def test_legacy_parity_matrix(self):
        for title in ('Batman', 'The Batman', 'A Hero', '風物: Étoile / Deux', 'Batman?'):
            for special in (SpecialVersion.NORMAL, SpecialVersion.VOLUME_AS_ISSUE, SpecialVersion.TPB,
                            SpecialVersion.HARD_COVER, SpecialVersion.ONE_SHOT, SpecialVersion.OMNIBUS):
                for smart in (True, False):
                    for year, publisher in ((2020, 'DC / Comics'), (None, None)):
                        v, issues = records(special)
                        v = replace(v, identity=replace(v.identity, title=title, year=year, publisher=publisher))
                        naming = replace(NAMING, replace_illegal_characters=smart,
                                         volume_folder_naming='{clean_series_name}/Volume {volume_number} ({year}) {publisher} {special_version}')
                        data, _, identity = _naming_data(v, (issues,))
                        old = generate_volume_folder_name(data, identity, naming).replace('\\', '/')
                        p = publication(title=title, year=year, publisher=publisher, volume_number=1, special_version=special)
                        result = decide_folder(p, replace(context(), naming=naming))
                        self.assertEqual(result.relative_folder, old)

    def test_preserve_exposes_policy_difference(self):
        p = publication(current_folder='/library/Batman [2016]')
        result = decide_folder(p, context(), FolderPolicy(template='{series_name} ({year})'))
        self.assertEqual(result.target_folder, p.current_folder)
        self.assertEqual(result.policy_folder, '/library/Batman (2016)')
        self.assertEqual(result.status, FolderStatus.RETAINED)
        self.assertTrue(result.retained)

    def test_custom_preserved_when_settings_changed(self):
        p = publication(current_folder='/library/User Choice', custom_folder=True)
        self.assertEqual(decide_folder(p, context()).target_folder, p.current_folder)

    def test_invalid_template_does_not_relocate_existing(self):
        result = decide_folder(publication(current_folder='/library/Keep'), context(), FolderPolicy(template='{bad}'))
        self.assertEqual(result.status, FolderStatus.RETAINED)
        self.assertIsNone(result.policy_folder)
        self.assertIn(FolderCode.CANONICAL_UNAVAILABLE, codes(result))

    def test_recalculate_is_explicit_and_reviewable(self):
        p = publication(current_folder='/library/Old')
        result = decide_folder(p, context(), FolderPolicy(mode=FolderMode.RECALCULATE))
        self.assertEqual(result.target_folder, '/library/Batman')
        self.assertEqual(result.current_folder, '/library/Old')
        self.assertEqual(result.status, FolderStatus.REVIEW)
        self.assertIn(FolderCode.REORGANIZE, codes(result))

    def test_recalculate_same_path_is_not_transition(self):
        result = decide_folder(publication(current_folder='/library/Batman'), context(), FolderPolicy(mode=FolderMode.RECALCULATE))
        self.assertEqual(result.status, FolderStatus.CALCULATED)

    def test_custom_recalculation_requires_review(self):
        result = decide_folder(publication(current_folder='/library/Custom', custom_folder=True), context(), FolderPolicy(mode=FolderMode.RECALCULATE))
        self.assertIn(FolderCode.CUSTOM, codes(result))
        self.assertEqual(result.status, FolderStatus.REVIEW)

    def test_missing_custom_folder_is_not_silently_owned(self):
        result = decide_folder(publication(custom_folder=True), context())
        self.assertEqual(result.status, FolderStatus.REVIEW)

    def test_existing_root_not_changed_by_override_in_preserve(self):
        result = decide_folder(publication(current_folder='/library/Keep'), context(), FolderPolicy(root_id=2))
        self.assertEqual(result.root_id, 1)
        self.assertEqual(result.target_folder, '/library/Keep')
        self.assertEqual(result.status, FolderStatus.REVIEW)

    def test_explicit_recalculate_root(self):
        result = decide_folder(publication(current_folder='/library/Keep'), context(), FolderPolicy(mode=FolderMode.RECALCULATE, root_id=2))
        self.assertEqual(result.root, '/other')
        self.assertEqual(result.target_folder, '/other/Batman')
        self.assertEqual(result.status, FolderStatus.REVIEW)

    def test_missing_owned_root_never_falls_back(self):
        result = decide_folder(publication(root_id=99), context(), FolderPolicy(default_root_id=2))
        self.assertEqual(result.status, FolderStatus.BLOCKED)
        self.assertIsNone(result.root)

    def test_root_change_requires_review_even_before_folder_is_created(self):
        result = decide_folder(publication(current_folder=None), context(), FolderPolicy(mode=FolderMode.RECALCULATE, root_id=2))
        self.assertEqual(result.root_id, 2)
        self.assertEqual(result.status, FolderStatus.REVIEW)
        self.assertIn(FolderCode.ROOT_CHANGE, codes(result))

    def test_new_volume_requires_explicit_or_supplied_default_root(self):
        p = publication(local_id=None, root_id=None)
        self.assertEqual(decide_folder(p, context()).status, FolderStatus.BLOCKED)
        self.assertEqual(decide_folder(p, context(), FolderPolicy(default_root_id=2)).target_folder, '/other/Batman')
        self.assertEqual(decide_folder(p, context(), FolderPolicy(root_id=1, default_root_id=2)).root_id, 1)

    def test_all_tokens_and_selected_authority(self):
        template = '{series_name} {clean_series_name} {year} {publisher} {volume_number} {special_version} {comicvine_id} {metadata_provider} {provider_id}'
        self.assertEqual(TOKENS, {t.split('}')[0] for t in template.split('{')[1:]})
        result = decide_folder(publication(title='The Hero', special_version=SpecialVersion.HARD_COVER), context(), FolderPolicy(template=template))
        self.assertEqual(result.relative_folder, 'The Hero Hero, The 2016 DC Comics 02 HC 123 metron ABC')

    def test_same_id_different_namespaces_and_no_suffix_invention(self):
        for provider in ('comicvine', 'metron'):
            p = publication(authority=reference('123', provider))
            result = decide_folder(p, context(), FolderPolicy(template='{metadata_provider}/{provider_id}/{series_name}'))
            self.assertEqual(result.relative_folder, provider + '/123/Batman')
            self.assertEqual(decide_folder(p, context()).relative_folder, 'Batman')

    def test_legacy_missing_year_publisher_are_explicit_labels(self):
        result = decide_folder(publication(year=None, publisher=None), context(), FolderPolicy(template='{publisher}/{series_name} ({year})'))
        self.assertEqual(result.relative_folder, 'Unknown Publisher/Batman (Unknown Year)')
        self.assertIn(FolderCode.FALLBACK, codes(result))

    def test_empty_cleaned_publisher_uses_legacy_unknown_label(self):
        for value in ('', '   ', '???'):
            result = decide_folder(publication(publisher=value), context(), FolderPolicy(template='{publisher}/{series_name}'))
            self.assertEqual(result.relative_folder, 'Unknown Publisher/Batman')

    def test_strict_missing_value_blocks(self):
        for field, token in (('year', 'year'), ('publisher', 'publisher'), ('volume_number', 'volume_number'),
                             ('authority', 'provider_id'), ('authority', 'metadata_provider')):
            result = decide_folder(publication(**{field: None}), context(), FolderPolicy(template='{' + token + '}', missing=MissingFolderValue.BLOCK))
            self.assertEqual(result.status, FolderStatus.BLOCKED)
            self.assertIn(FolderCode.MISSING, codes(result))

    def test_unused_missing_fields_do_not_block_folder(self):
        result = decide_folder(publication(volume_number=None, year=None, publisher=None, authority=None), context())
        self.assertEqual(result.target_folder, '/library/Batman')

    def test_absent_cv_identity_empty_even_with_format_spec(self):
        result = decide_folder(publication(comicvine_id=None), context(), FolderPolicy(template='{series_name} {comicvine_id:05d}'))
        self.assertEqual(result.relative_folder, 'Batman')

    def test_malformed_unknown_nested_and_attribute_tokens(self):
        for template in ('{', '{bad}', '{year.real}', '{provider_id[0]}', '{}', '{year:{year}}', '{series_name:99999999}'):
            result = decide_folder(publication(), context(), FolderPolicy(template=template))
            self.assertEqual(result.status, FolderStatus.BLOCKED, template)

    def test_literal_braces_and_integer_format(self):
        result = decide_folder(publication(), context(), FolderPolicy(template='{{Books}}/{series_name} {year:04d}'))
        self.assertEqual(result.relative_folder, '{Books}/Batman 2016')

    def test_separator_values_cannot_create_segments(self):
        result = decide_folder(publication(title='Hero/Part\\Other', publisher='../Publisher'), context(), FolderPolicy(template='{publisher}/{series_name}'))
        self.assertEqual(len(result.safe_components), 2)
        self.assertTrue(all('/' not in s and '\\' not in s for s in result.safe_components))
        self.assertTrue(result.target_folder.startswith('/library/'))

    def test_escape_templates_blocked_before_cleaning(self):
        for template in ('../escape', 'a/../b', '/absolute', 'C:\\absolute', '\\\\server\\share', '.', '..', '', 'a//b'):
            result = decide_folder(publication(), context(), FolderPolicy(template=template))
            self.assertEqual(result.status, FolderStatus.BLOCKED, template)

    def test_empty_token_segment_blocks(self):
        result = decide_folder(publication(), context(), FolderPolicy(template='{special_version}/{series_name}'))
        self.assertEqual(result.status, FolderStatus.BLOCKED)

    def test_unicode_and_case_preserved(self):
        result = decide_folder(publication(title='風 Étoile'), context())
        self.assertEqual(result.relative_folder, '風 Étoile')

    def test_windows_reserved_and_case_only_transition(self):
        ctx = FolderContext.build(((1, 'C:\\Library'),), (), NAMING, True, False)
        for title in ('CON', 'nul.txt', 'COM1'):
            result = decide_folder(publication(title=title), ctx)
            self.assertEqual(result.status, FolderStatus.BLOCKED)
            self.assertIn(FolderCode.RESERVED, codes(result))
        result = decide_folder(publication(current_folder='c:\\library\\BATMAN'), ctx, FolderPolicy(mode=FolderMode.RECALCULATE))
        self.assertIn(FolderCode.CASE, codes(result))
        self.assertEqual(result.status, FolderStatus.REVIEW)

    def test_linux_distinct_case_ownership(self):
        ctx = FolderContext.build(((1, '/library'),), ((2, '/library/BATMAN'),), NAMING)
        self.assertEqual(decide_folder(publication(), ctx).status, FolderStatus.CALCULATED)
        self.assertEqual(decide_folder(publication(), replace(ctx, case_sensitive=False)).status, FolderStatus.BLOCKED)

    def test_trailing_punctuation_sanitized_and_unsafe_custom_declined(self):
        result = decide_folder(publication(title='Batman... '), context())
        self.assertEqual(result.relative_folder, 'Batman')
        ctx = FolderContext.build(((1, 'C:\\Library'),), (), NAMING, True, False)
        result = decide_folder(publication(), ctx, FolderPolicy(custom_relative='Bad. '))
        self.assertEqual(result.status, FolderStatus.BLOCKED)

    def test_explicit_custom_target_is_not_a_template(self):
        result = decide_folder(publication(), context(), FolderPolicy(custom_relative='User/{literal}'))
        self.assertEqual(result.target_folder, '/library/User/{literal}')
        self.assertIn('explicit_custom_target', result.reasons)

    def test_explicit_custom_target_can_bypass_unavailable_canonical_values(self):
        result = decide_folder(publication(year=None), context(), FolderPolicy(
            template='{year}', missing=MissingFolderValue.BLOCK, custom_relative='Chosen'))
        self.assertEqual(result.target_folder, '/library/Chosen')
        self.assertEqual(result.status, FolderStatus.CALCULATED)
        self.assertIn(FolderCode.CANONICAL_UNAVAILABLE, codes(result))

    def test_windows_case_sensitive_containment_is_explicit(self):
        ctx = FolderContext.build(((1, 'C:\\Library'),), (), NAMING, True, True)
        self.assertFalse(ctx.inside('C:\\Library', 'C:\\library\\Book'))
        self.assertTrue(replace(ctx, case_sensitive=False).inside('C:\\Library', 'C:\\library\\Book'))

    def test_existing_outside_root_blocked(self):
        result = decide_folder(publication(current_folder='/outside/Keep'), context())
        self.assertEqual(result.status, FolderStatus.BLOCKED)

    def test_length_without_truncation(self):
        result = decide_folder(publication(title='A' * 256), context())
        self.assertEqual(result.status, FolderStatus.BLOCKED)
        self.assertIn(FolderCode.LENGTH, codes(result))
        result = decide_folder(publication(), context(max_path_length=10))
        self.assertEqual(result.status, FolderStatus.BLOCKED)

    def test_owned_folder_exact_parent_child_conflicts(self):
        for owner in ('/library/Batman', '/library/Batman/Sub', '/library'):
            ctx = FolderContext.build(((1, '/library'),), ((2, owner),), NAMING)
            result = decide_folder(publication(), ctx)
            self.assertEqual(result.status, FolderStatus.BLOCKED)
            self.assertIn(FolderCode.OWNERSHIP, codes(result))

    def test_same_owner_allowed(self):
        ctx = FolderContext.build(((1, '/library'),), ((1, '/library/Batman'),), NAMING)
        self.assertEqual(decide_folder(publication(current_folder='/library/Batman'), ctx).status, FolderStatus.RETAINED)

    def test_years_distinguish_without_hidden_suffixes(self):
        policy = FolderPolicy(template='{series_name} ({year})')
        self.assertEqual({decide_folder(publication(year=y), context(), policy).relative_folder for y in (1940, 2011, 2016)},
                         {'Batman (1940)', 'Batman (2011)', 'Batman (2016)'})

    def test_missing_full_date_or_issue_token_not_invented(self):
        for token in ('date', 'issue_number', 'physical_format', 'publication_kind'):
            result = decide_folder(publication(), context(), FolderPolicy(template='{' + token + '}'))
            self.assertEqual(result.status, FolderStatus.BLOCKED)

    def test_pure_evaluation(self):
        ctx = context()
        with patch('builtins.open', side_effect=AssertionError), patch('os.mkdir', side_effect=AssertionError), \
                patch('os.rename', side_effect=AssertionError), patch('os.remove', side_effect=AssertionError), \
                patch('socket.socket', side_effect=AssertionError), patch('backend.internals.db.get_db', side_effect=AssertionError):
            self.assertEqual(decide_folder(publication(), ctx).status, FolderStatus.CALCULATED)

    def test_determinism_immutability_and_fingerprint(self):
        a = FolderContext.build(((2, '/other'), (1, '/library')), ((9, '/other/X'), (8, '/other/Y')), NAMING)
        b = FolderContext.build(reversed(tuple(a.roots.items())), reversed(a.owners), NAMING)
        first = decide_folder(publication(), a)
        self.assertEqual(first, decide_folder(publication(), b))
        self.assertNotEqual(first.fingerprint, decide_folder(publication(), a, FolderPolicy(template='Other')).fingerprint)
        with self.assertRaises(FrozenInstanceError):
            first.target_folder = 'changed'
        with self.assertRaises(TypeError):
            a.roots[9] = 'changed'
        self.assertEqual(json.loads(json.dumps(preview_folder(first)))['policy'], 'kapowarr-folder-policy/v1')


class FolderPlanIntegration(TestCase):
    def test_plan_retains_folder_decision_and_preconditions(self):
        r = identified()
        plan = plan_one(r, context_for([r]))
        self.assertEqual(plan.folder_policy, 'kapowarr-folder-policy/v1')
        self.assertTrue(plan.folder_decision.retained)
        self.assertIn('folder_policy_root_and_ownership', [p.name for p in plan.preconditions])
        self.assertEqual(preview_plan(plan)['folder_decision']['target'], plan.target_folder)

    def test_reorganize_does_not_apply_or_upgrade_review(self):
        r = identified()
        policy = PlanningPolicy(folder=FolderPolicy(template='New/{series_name}', mode=FolderMode.RECALCULATE))
        plan = plan_one(r, context_for([r], policy=policy))
        self.assertEqual(plan.target_folder, '/library/New/Batman')
        self.assertEqual(plan.status, PlanStatus.REVIEW)
        self.assertFalse(plan.effects)

    def test_new_external_prospective_folder_keeps_creation_prerequisite(self):
        r = replace(identified(), state=MatchState.REVIEW, selected=None,
                    alternatives=(PublicationMatch(None, reference('external', 'metron'), (), (), title='New Book', year=2020),))
        policy = PlanningPolicy(folder=FolderPolicy(root_id=1))
        plan = plan_one(r, context_for([r], policy=policy))
        self.assertEqual(plan.target_folder, '/library/New Book')
        self.assertEqual(plan.status, PlanStatus.REVIEW)
        self.assertFalse(plan.effects)
        self.assertIsNone(plan.target_path)

    def test_new_external_missing_metadata_not_fetched(self):
        r = replace(identified(), state=MatchState.REVIEW, selected=None,
                    alternatives=(PublicationMatch(None, reference(), (), (), title='New'),))
        policy = PlanningPolicy(folder=FolderPolicy(root_id=1, template='{publisher}/{series_name}', missing=MissingFolderValue.BLOCK))
        plan = plan_one(r, context_for([r], policy=policy))
        self.assertEqual(plan.folder_decision.status, FolderStatus.BLOCKED)
        self.assertFalse(plan.effects)

    def test_many_files_reuse_one_volume_decision(self):
        r = identified()
        v, i = records()
        with patch('backend.implementations.organization_plan.decide_folder', wraps=decide_folder) as evaluator:
            ctx = PlanningContext.build((v,), (i,), ((1, '/library'),), (), NAMING, PlanningPolicy())
            for count in (1, 100, 1000):
                for _ in range(count):
                    plan_one(r, ctx)
            self.assertEqual(evaluator.call_count, 1)

    def test_different_volumes_same_folder_different_names_block_symmetrically(self):
        v1, i1 = records()
        v1 = replace(v1, folder='', custom_folder=False)
        v2 = replace(v1, identity=replace(v1.identity, id=2, authority=reference('2')))
        i2 = replace(i1, identity=replace(i1.identity, id=2, volume_id=2))
        r1 = identified(v=v1, issues=(i1,))
        r2 = identified(replace(candidate(), candidate_id='two', file=replace(candidate().file, path='/incoming/Other.cbz')), v=v2, issues=(i2,))
        policy = PlanningPolicy(rename=False)
        ctx = context_for([r1, r2], volumes=(v1, v2), issues=(i1, i2), policy=policy)
        a, b = plan_many([r1, r2], ctx), plan_many([r2, r1], ctx)
        self.assertEqual(a, b)
        self.assertTrue(all(p.status == PlanStatus.BLOCKED for p in a.plans))

    def test_rename_only_shared_incoming_folder_does_not_claim_ownership(self):
        v1, i1 = records()
        v2 = replace(v1, folder='/library/Other', identity=replace(v1.identity, id=2, authority=reference('2'), title='Other'))
        i2 = replace(i1, identity=replace(i1.identity, id=2, volume_id=2))
        c2 = replace(candidate(), candidate_id='two', file=replace(candidate().file, path='/incoming/Other.cbz'))
        c2 = replace(c2, filename=replace(c2.filename, series='Other'))
        r1, r2 = identified(v=v1, issues=(i1,)), identified(c2, v=v2, issues=(i2,))
        policy = PlanningPolicy(move=False)
        ctx = context_for([r1, r2], volumes=(v1, v2), issues=(i1, i2), policy=policy)
        batch = plan_many([r1, r2], ctx)
        self.assertTrue(all(p.status == PlanStatus.READY for p in batch.plans))

    def test_folder_not_dependent_on_opaque_issue_number(self):
        v, i = records(raw='[nn]', number=.1414)
        ctx = PlanningContext.build((v,), (i,), ((1, '/library'),), (), NAMING, PlanningPolicy())
        self.assertEqual(ctx.folder_decisions[1].target_folder, '/library/Batman')

    def test_nested_prospective_volume_folders_conflict(self):
        v1, i1 = records()
        v1 = replace(v1, folder='', custom_folder=False)
        v2 = replace(v1, identity=replace(v1.identity, id=2, authority=reference('2'), publisher='Batman'))
        i2 = replace(i1, identity=replace(i1.identity, id=2, volume_id=2))
        r1 = identified(v=v1, issues=(i1,))
        r2 = identified(replace(candidate(), candidate_id='two', file=replace(candidate().file, path='/incoming/Other.cbz')), v=v2, issues=(i2,))
        # Explicit contexts allow per-volume targets, just as existing custom ownership does.
        ctx = context_for([r1, r2], volumes=(v1, v2), issues=(i1, i2))
        from types import MappingProxyType
        decisions = dict(ctx.folder_decisions)
        decisions[2] = replace(decisions[2], target_folder='/library/Batman/Child')
        ctx = replace(ctx, folder_decisions=MappingProxyType(decisions))
        batch = plan_many([r1, r2], ctx)
        self.assertTrue(all(any(d.code.value == 'managed_folder_ownership_conflict' for d in p.diagnostics) for p in batch.plans))

    def test_rename_only_does_not_change_folder(self):
        r = identified()
        plan = plan_one(r, context_for([r], policy=PlanningPolicy(move=False)))
        self.assertEqual(plan.target_folder, '/incoming')
        self.assertEqual(plan.folder_decision.target_folder, '/library/Batman')
