"""Smart Rename contracts: synthetic canonical facts, no live libraries/network."""

import json
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from unittest import TestCase
from unittest.mock import patch

from Tbackend.features.identification import candidate, reference
from Tbackend.features.organization_plan import (NAMING, context_for,
                                                 identified, records)

from backend.base.definitions import SpecialVersion
from backend.base.organization_plan import (EffectKind, PlanningPolicy,
                                            PlanStatus)
from backend.base.rename_policy import (NamingContext, RenameCode, RenameIssue,
                                        RenameMode, RenamePolicy, RenameStatus)
from backend.implementations.naming import generate_issue_name
from backend.implementations.organization_plan import (PlanningContext,
                                                       _naming_data, plan_many,
                                                       plan_one, preview_plan)
from backend.implementations.rename_policy import (build_rename_catalog,
                                                   decide_rename,
                                                   decide_renames,
                                                   preview_rename)


def naming_context(raw='1', number=1., title='Story', settings=NAMING, **kwargs):
    v, i = records(raw=raw, number=number)
    issue = RenameIssue(i.identity, title, i.date, i.comicvine_id)
    return replace(NamingContext(v.identity, (issue,), build_rename_catalog(1, (i.identity,), settings.issue_padding),
                                 settings, 'old.CBZ', v.comicvine_id, '/library/Batman'), **kwargs)


def coverage(labels, selected=None, settings=NAMING):
    c = naming_context(settings=settings)
    issues = tuple(replace(c.issues[0], identity=replace(c.issues[0].identity, id=n,
                    raw_number=label, calculated_number=float(label) if label.replace('.', '').isdigit() else 1.01))
                   for n, label in enumerate(labels, 1))
    return replace(c, issues=tuple(i for i in issues if selected is None or i.identity.id in selected),
                   catalog=build_rename_catalog(1, (i.identity for i in issues), settings.issue_padding))


def codes(decision):
    return {d.code for d in decision.diagnostics}


def template_settings(template):
    # Isolate token formatting from the separately characterized parser fallback.
    return replace(NAMING, file_naming=template, file_naming_empty=template)


class RenamePolicyTests(TestCase):
    def test_legacy_ordinary_parity_matrix(self):
        for label in ('1', '01', '10', '100', '1.5'):
            for padding in (1, 2, 3, 4):
                for title in ('Story', None, '風: The Story?', 'Book 4'):
                    settings = replace(NAMING, issue_padding=padding,
                                       file_naming='{clean_series_name} ({year}) #{issue_number} {issue_title}')
                    c = naming_context(label, float(label), title, settings)
                    v, i = records(raw=label, number=float(label))
                    i = replace(i, title=title)
                    data, issue_map, ids = _naming_data(v, (i,))
                    legacy = generate_issue_name(data, float(label), identity_context=ids, settings=settings, issues=issue_map)
                    self.assertEqual(decide_rename(c).target_filename, legacy + '.cbz')

    def test_legacy_special_and_vai_parity(self):
        for special in (SpecialVersion.NORMAL, SpecialVersion.VOLUME_AS_ISSUE, SpecialVersion.TPB,
                        SpecialVersion.HARD_COVER, SpecialVersion.OMNIBUS, SpecialVersion.ONE_SHOT):
            for long in (False, True):
                settings = replace(NAMING, long_special_version=long)
                v, i = records(special)
                data, issue_map, ids = _naming_data(v, (i,))
                old = generate_issue_name(data, 1., identity_context=ids, settings=settings, issues=issue_map)
                c = naming_context(settings=settings, publication=v.identity)
                self.assertEqual(decide_rename(c).target_filename, old + '.cbz')

    def test_decimal_padding_is_whole_label_zfill_not_clu_integer_padding(self):
        for width, expected in ((3, '1.5'), (4, '01.5')):
            c = naming_context('1.5', 1.5, settings=replace(NAMING, issue_padding=width))
            self.assertIn('#' + expected, decide_rename(c).target_filename)

    def test_leading_zero_is_not_double_padded(self):
        self.assertIn('#001.', decide_rename(naming_context('01')).target_filename)

    def test_raw_suffix_and_opaque_display_does_not_call_number_parser(self):
        for label in ('1A', '[nn]', 'Annual', 'Special', '½'):
            with patch('backend.implementations.rename_policy.extract_filename_data', side_effect=AssertionError):
                result = decide_rename(naming_context(label, 1.01))
            self.assertTrue(result.target_filename.endswith('#' + label + '.cbz'))
            self.assertEqual(result.raw_labels, (label,))
            self.assertIn(RenameCode.RAW_LABEL, codes(result))

    def test_suffix_and_decimal_projection_collision_does_not_collapse_display(self):
        c = coverage(('1A', '1.01'), (1,))
        first = decide_rename(c)
        second = decide_rename(coverage(('1A', '1.01'), (2,)))
        self.assertNotEqual(first.target_filename, second.target_filename)
        self.assertIn('#1A', first.target_filename)
        self.assertIn('#1.01', second.target_filename)

    def test_padded_raw_label_collision_blocks(self):
        result = decide_rename(coverage(('1', '01'), (1,)))
        self.assertEqual(result.status, RenameStatus.BLOCKED)
        self.assertIn(RenameCode.COLLISION, codes(result))

    def test_safe_range_and_decimal_range(self):
        for labels, expected in ((('1', '2', '3', '4'), '001 - 004'), (('1.5', '2', '3'), '1.5 - 003')):
            result = decide_rename(coverage(labels))
            self.assertEqual(result.status, RenameStatus.CALCULATED)
            self.assertIn(expected, result.target_filename)

    def test_range_does_not_borrow_first_title(self):
        settings = replace(NAMING, file_naming='{issue_number} {issue_title}', file_naming_empty='{issue_number}')
        result = decide_rename(coverage(('1', '2'), settings=settings))
        self.assertEqual(result.target_filename, '001 - 002.cbz')
        self.assertFalse(any(t.name == 'issue_title' for t in result.tokens))

    def test_range_single_identity_token_unavailable(self):
        settings = replace(NAMING, file_naming_empty='{issue_number} {issue_provider_id}')
        result = decide_rename(coverage(('1', '2'), settings=settings))
        self.assertIn(RenameCode.COVERAGE, codes(result))
        self.assertIsNone(result.target_filename)

    def test_noncontiguous_selected_subset_blocks(self):
        self.assertEqual(decide_rename(coverage(('1', '2', '3'), (1, 3))).status, RenameStatus.BLOCKED)

    def test_absent_integer_rows_do_not_invent_continuity(self):
        self.assertEqual(decide_rename(coverage(('1', '3', '5'))).status, RenameStatus.BLOCKED)

    def test_opaque_set_never_numeric_range(self):
        self.assertEqual(decide_rename(coverage(('1A', '1B', '[nn]'))).status, RenameStatus.BLOCKED)

    def test_range_with_legacy_suffix_member_is_unavailable(self):
        self.assertEqual(decide_rename(coverage(('1', '1A', '2'), (1, 3))).status, RenameStatus.BLOCKED)

    def test_vai_range_uses_volume_word_and_no_new_classification(self):
        c = coverage(('1', '2'))
        c = replace(c, publication=replace(c.publication, special_version=SpecialVersion.VOLUME_AS_ISSUE))
        self.assertEqual(decide_rename(c).target_filename, 'Batman Volume 001 - 002.cbz')

    def test_special_multibook_cannot_become_first_book(self):
        c = coverage(('1', '2'))
        c = replace(c, publication=replace(c.publication, special_version=SpecialVersion.HARD_COVER))
        self.assertIn(RenameCode.COVERAGE, codes(decide_rename(c)))

    def test_selected_authority_and_explicit_cv_reference(self):
        settings = template_settings('{metadata_provider}-{provider_id}-{issue_provider_id}-{comicvine_id}-{issue_comicvine_id}')
        c = naming_context(settings=settings)
        v = replace(c.publication, authority=reference('opaque-123', 'metron'), references=(reference('123'),))
        i = replace(c.issues[0], identity=replace(c.issues[0].identity, references=(reference('MI', 'metron', c.issues[0].identity.references[0].kind),)))
        c = replace(c, publication=v, issues=(i,), catalog=build_rename_catalog(1, (i.identity,), 3))
        result = decide_rename(c)
        self.assertEqual(result.target_filename, 'metron-opaque-123-MI-1-301.cbz')
        self.assertEqual(result.authority, v.authority)

    def test_same_raw_provider_id_retains_namespace(self):
        settings = template_settings('{metadata_provider}-{provider_id}')
        c = naming_context(settings=settings)
        outputs = {decide_rename(replace(c, publication=replace(c.publication, authority=reference('123', p)))).target_filename for p in ('comicvine', 'metron')}
        self.assertEqual(outputs, {'comicvine-123.cbz', 'metron-123.cbz'})

    def test_missing_selected_issue_id_never_borrows_reference(self):
        settings = replace(NAMING, file_naming='{issue_provider_id}')
        c = naming_context(settings=settings)
        c = replace(c, publication=replace(c.publication, authority=reference('M', 'metron')))
        self.assertIn(RenameCode.MISSING, codes(decide_rename(c)))

    def test_issue_title_is_canonical_per_file(self):
        c = naming_context(title='I Am Gotham Part One', settings=replace(NAMING, file_naming='{series_name} #{issue_number} - {issue_title}'))
        self.assertEqual(decide_rename(c).target_filename, 'Batman #001 - I Am Gotham Part One.cbz')

    def test_missing_title_selects_existing_empty_template(self):
        c = naming_context(title=None, settings=replace(NAMING, file_naming='{issue_title}', file_naming_empty='Titleless #{issue_number}'))
        self.assertEqual(decide_rename(c).target_filename, 'Titleless #001.cbz')

    def test_unknown_year_publisher_compatibility_and_strict(self):
        c = naming_context(settings=template_settings('{series_name} {year} {publisher}'))
        c = replace(c, publication=replace(c.publication, year=None, publisher=None))
        self.assertEqual(decide_rename(c).target_filename, 'Batman Unknown Year Unknown Publisher.cbz')
        self.assertEqual(decide_rename(c, RenamePolicy(strict=True)).status, RenameStatus.BLOCKED)

    def test_absent_cv_reference_remains_empty_with_numeric_spec(self):
        c = naming_context(settings=template_settings('Book {comicvine_id:05d}'), comicvine_id=None)
        self.assertEqual(decide_rename(c).target_filename, 'Book.cbz')

    def test_missing_volume_number_only_blocks_when_used(self):
        c = naming_context()
        c = replace(c, publication=replace(c.publication, volume_number=None))
        self.assertEqual(decide_rename(c).status, RenameStatus.CALCULATED)
        c = replace(c, settings=replace(c.settings, file_naming='{volume_number}'))
        self.assertIn(RenameCode.MISSING, codes(decide_rename(c)))

    def test_partial_dates_only_require_precision_of_used_token(self):
        c = naming_context(settings=template_settings('{issue_release_year}'))
        for raw in ('2021', '2021-12', '2021-12-00', '2021-12-25'):
            ctx = replace(c, issues=(replace(c.issues[0], date=raw),))
            self.assertEqual(decide_rename(ctx).target_filename, '2021.cbz')
        ctx = replace(ctx, issues=(replace(c.issues[0], date='2021-12-00'),), settings=replace(NAMING, file_naming='{issue_release_date}'))
        self.assertIn(RenameCode.DATE, codes(decide_rename(ctx)))

    def test_invalid_date_does_not_block_unrelated_template(self):
        c = naming_context()
        c = replace(c, issues=(replace(c.issues[0], date='banana'),))
        self.assertEqual(decide_rename(c).status, RenameStatus.CALCULATED)
        c = replace(c, settings=replace(NAMING, file_naming='{issue_release_year}'))
        self.assertIn(RenameCode.DATE, codes(decide_rename(c)))

    def test_all_legacy_issue_tokens_and_escaped_braces(self):
        template = '{{Book}} {series_name} {clean_series_name} {volume_number} {comicvine_id} {year} {publisher} {issue_comicvine_id} {issue_number} {issue_release_date} {issue_release_year} {issue_title} {metadata_provider} {provider_id} {issue_provider_id}'
        result = decide_rename(naming_context('1A', 1.01, settings=replace(NAMING, file_naming=template)))
        self.assertEqual(result.status, RenameStatus.CALCULATED)
        self.assertTrue(result.target_filename.startswith('{Book} Batman Batman 01'))
        self.assertEqual(len(result.tokens), 14)

    def test_fractional_spelling_is_not_invented_numeric_range(self):
        result = decide_rename(naming_context('1/2', .5))
        self.assertEqual(result.raw_labels, ('1/2',))
        self.assertEqual(result.status, RenameStatus.REVIEW)
        self.assertIn(RenameCode.LABEL_CLEANED, codes(result))

    def test_padded_collision_cannot_be_hidden_by_absent_cv_id(self):
        settings = template_settings('{issue_number} {issue_comicvine_id}')
        c = coverage(('1', '01'), (1,), settings)
        c = replace(c, issues=(replace(c.issues[0], comicvine_id=None),))
        self.assertEqual(decide_rename(c).status, RenameStatus.BLOCKED)

    def test_template_safety(self):
        for template in ('{', '{bad}', '{year.real}', '{provider_id[0]}', '{}', '{year:{year}}', '{series_name:999999999}', '../{series_name}', 'C:\\name', '/absolute'):
            result = decide_rename(naming_context(settings=replace(NAMING, file_naming=template)))
            self.assertEqual(result.status, RenameStatus.BLOCKED, template)

    def test_cleaning_cannot_inject_folder_or_controls(self):
        for title in ('A/B\\C', '../absolute', 'C:\\escape', '\\\\host\\share', 'A:\x01?*"<>| B', 'Hero... '):
            c = naming_context(settings=template_settings('{series_name}'))
            c = replace(c, publication=replace(c.publication, title=title))
            result = decide_rename(c)
            self.assertEqual(result.status, RenameStatus.CALCULATED, title)
            self.assertFalse(any(ch in result.target_filename for ch in '/\\:\x01?*"<>|'))
            self.assertEqual(result.raw_basename, title)

    def test_smart_simple_cleaning_and_unicode_parity(self):
        for smart in (True, False):
            settings = replace(NAMING, replace_illegal_characters=smart, file_naming='{series_name} #{issue_number}')
            c = naming_context(settings=settings)
            c = replace(c, publication=replace(c.publication, title="風 Étoile: Hero / Two's?"))
            v, i = records()
            v = replace(v, identity=c.publication)
            data, issue_map, ids = _naming_data(v, (i,))
            old = generate_issue_name(data, 1., identity_context=ids, settings=settings, issues=issue_map)
            self.assertEqual(decide_rename(c).target_filename, old + '.cbz')

    def test_windows_reserved_names_block_linux_retains(self):
        for name in ('CON', 'PRN', 'AUX', 'NUL', 'COM1', 'LPT1', 'con.txt'):
            c = naming_context(settings=template_settings(name))
            self.assertIn(RenameCode.RESERVED, codes(decide_rename(replace(c, windows=True))))
            self.assertEqual(decide_rename(c).status, RenameStatus.CALCULATED)

    def test_empty_or_meaningless_basename_blocks(self):
        for template in ('', '.', '..', '???'):
            result = decide_rename(naming_context(settings=replace(NAMING, file_naming=template)))
            self.assertEqual(result.status, RenameStatus.BLOCKED)

    def test_extension_case_matches_legacy_without_conversion(self):
        for extension in ('.cbz', '.CBZ', '.cbr', '.pdf'):
            c = naming_context(current_filename='old' + extension)
            self.assertTrue(decide_rename(c).target_filename.endswith(extension.lower()))

    def test_general_files_and_images_not_issue_named(self):
        for name in ('cover.jpg', 'ComicInfo.xml', 'series.json', 'no-extension'):
            self.assertIn(RenameCode.FORMAT, codes(decide_rename(naming_context(current_filename=name))))

    def test_preserve_mode_does_not_evaluate_invalid_template_or_coverage(self):
        c = naming_context(settings=replace(NAMING, file_naming='{bad}'), issues=())
        result = decide_rename(c, RenamePolicy(RenameMode.PRESERVE_EXISTING))
        self.assertEqual(result.target_filename, 'old.CBZ')
        self.assertEqual(result.status, RenameStatus.UNCHANGED)

    def test_unchanged_exact_filename(self):
        c = naming_context(current_filename='Batman (2020) #001.cbz')
        self.assertEqual(decide_rename(c).status, RenameStatus.UNCHANGED)

    def test_case_only_explicit_platform_semantics(self):
        c = naming_context(current_filename='BATMAN (2020) #001.cbz')
        self.assertEqual(decide_rename(c).status, RenameStatus.CALCULATED)
        self.assertEqual(decide_rename(replace(c, case_sensitive=False)).status, RenameStatus.REVIEW)

    def test_length_not_silently_truncated(self):
        c = naming_context(settings=replace(NAMING, file_naming='x' * 260, file_naming_empty='x' * 260))
        result = decide_rename(c)
        self.assertIn(RenameCode.LENGTH, codes(result))
        self.assertEqual(len(result.safe_basename), 260)
        self.assertIn(RenameCode.LENGTH, codes(decide_rename(naming_context(max_path_length=10))))

    def test_legacy_titleless_length_fallback_explained(self):
        c = naming_context(title='Long' * 100, settings=replace(NAMING, file_naming='{issue_number} {issue_title}'))
        result = decide_rename(c)
        self.assertEqual(result.target_filename, 'Batman (2020) #001.cbz')
        self.assertIn(RenameCode.TITLELESS, codes(result))

    def test_wrong_parent_or_changed_issue_blocks(self):
        c = naming_context()
        c = replace(c, issues=(replace(c.issues[0], identity=replace(c.issues[0].identity, volume_id=2)),))
        self.assertIn(RenameCode.IDENTITY, codes(decide_rename(c)))

    def test_no_coverage_does_not_invent_issue_one(self):
        self.assertIn(RenameCode.IDENTITY, codes(decide_rename(naming_context(issues=()))))

    def test_deterministic_reordered_coverage_and_catalog(self):
        c = coverage(('1', '2', '3'))
        reversed_context = replace(c, issues=tuple(reversed(c.issues)), catalog=build_rename_catalog(1, reversed(tuple(c.catalog.issues.values())), 3))
        self.assertEqual(decide_rename(c), decide_rename(reversed_context))

    def test_settings_and_mode_fingerprints_change(self):
        c = naming_context()
        self.assertNotEqual(decide_rename(c).fingerprint, decide_rename(c, RenamePolicy(RenameMode.PRESERVE_EXISTING)).fingerprint)
        self.assertNotEqual(decide_rename(c).fingerprint, decide_rename(replace(c, settings=replace(NAMING, file_naming='Other'))).fingerprint)

    def test_immutable_and_safe_json_projection(self):
        result = decide_rename(naming_context())
        with self.assertRaises(FrozenInstanceError):
            result.target_filename = 'changed'
        preview = json.loads(json.dumps(preview_rename(result)))
        self.assertEqual(preview['policy'], 'kapowarr-rename-policy/v1')
        self.assertNotIn('raw_bytes', preview)

    def test_evaluator_no_io(self):
        c = naming_context()
        with ExitStack() as stack:
            for target in ('builtins.open', 'os.rename', 'os.replace', 'os.mkdir', 'os.remove', 'shutil.move', 'shutil.copy', 'socket.socket', 'backend.internals.db.get_db', 'backend.implementations.naming.Settings'):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            self.assertEqual(decide_rename(c).status, RenameStatus.CALCULATED)

    def test_bulk_order_counts_and_catalog_reuse(self):
        c = naming_context()
        other = replace(c, current_filename='Batman (2020) #001.cbz')
        self.assertEqual(decide_renames((c, other)), decide_renames((other, c)))
        with patch('backend.implementations.rename_policy.build_rename_catalog', side_effect=AssertionError):
            for count in (1, 100, 1000):
                result = decide_renames((c,) * count)
                self.assertEqual(dict(result.counts)['calculated'], count)


class RenamePlanIntegration(TestCase):
    def test_decision_preconditions_and_preview(self):
        r = identified()
        p = plan_one(r, context_for((r,)))
        self.assertEqual(p.rename_decision.target_filename, 'Batman (2020) #001.cbz')
        self.assertEqual(p.naming_policy, 'kapowarr-rename-policy/v1')
        self.assertIn('rename_policy_settings_and_coverage', [c.name for c in p.preconditions])
        self.assertEqual(preview_plan(p)['rename_decision']['target'], p.rename_decision.target_filename)

    def test_preserve_filename_and_move_only(self):
        r = identified()
        p = plan_one(r, context_for((r,), policy=PlanningPolicy(naming=RenamePolicy(RenameMode.PRESERVE_EXISTING))))
        self.assertEqual(p.rename_decision.status, RenameStatus.UNCHANGED)
        self.assertIn(EffectKind.RELOCATE, [e.kind for e in p.effects])

    def test_explicit_rename_only_independent_of_folder(self):
        r = identified()
        p = plan_one(r, context_for((r,), policy=PlanningPolicy(move=False)))
        self.assertEqual(p.target_folder, '/incoming')
        self.assertEqual(p.folder_decision.target_folder, '/library/Batman')
        self.assertEqual(p.rename_decision.status, RenameStatus.CALCULATED)

    def test_blocked_rename_never_has_effects(self):
        r = identified()
        p = plan_one(r, context_for((r,), naming=replace(NAMING, file_naming='{invalid}')))
        self.assertEqual(p.status, PlanStatus.BLOCKED)
        self.assertFalse(p.effects)

    def test_catalog_built_once_not_per_file(self):
        v, i = records()
        with patch('backend.implementations.organization_plan.build_rename_catalog', wraps=build_rename_catalog) as build:
            ctx = PlanningContext.build((v,), (i,), ((1, '/library'),), (), NAMING, PlanningPolicy())
            for count in (1, 100, 1000):
                for _ in range(count):
                    plan_one(identified(), ctx)
            self.assertEqual(build.call_count, 1)

    def test_three_file_cycle_is_symmetric(self):
        v, original = records()
        issues = tuple(replace(original, title=title, identity=replace(original.identity, id=n, raw_number=title, calculated_number=float(n))) for n, title in enumerate(('B', 'C', 'A'), 1))
        results = []
        for n, source in enumerate(('A', 'B', 'C'), 1):
            r = identified()
            r = replace(r, candidate=replace(r.candidate, candidate_id=source, file=replace(r.candidate.file, path='/library/Batman/' + source + '.cbz')),
                        selected=replace(r.selected, local_issue_ids=(n,)))
            results.append(r)
        ctx = context_for(results, issues=issues, naming=replace(NAMING, file_naming='{issue_title}', file_naming_empty='{issue_title}'))
        batch = plan_many(results, ctx)
        self.assertEqual(batch, plan_many(reversed(results), ctx))
        self.assertTrue(all(p.status == PlanStatus.BLOCKED and not p.effects for p in batch.plans))
        self.assertTrue(all(any(d.code.value == 'target_is_another_plan_source' for d in p.diagnostics) for p in batch.plans))
