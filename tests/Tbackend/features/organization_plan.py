"""Preview contracts: explicit snapshots and disposable development files only."""

import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fixtures.library_import import ImportHarness
from Tbackend.features.identification import (DB, candidate, comic,
                                              issue, reference, volume)

from backend.base.definitions import SpecialVersion
from backend.base.identification import (IdentificationResult,
                                         MatchState, PublicationMatch)
from backend.base.import_candidate import (ExistingFileIdentity,
                                           InspectionState, LocalAssociation,
                                           ResourceKind)
from backend.base.naming_policy import NamingSettings
from backend.base.organization_plan import (AssociationLink, EffectKind,
                                            MetadataMode, PathObservation,
                                            PlanCode, PlanningFile,
                                            PlanningIssue, PlanningPolicy,
                                            PlanningVolume, PlanStatus)
from backend.features.library_import import import_library
from backend.features.organization_plan import (observe_plan_paths,
                                                preview_organization)
from backend.implementations.comicinfo import parse_comicinfo
from backend.implementations.comicinfo_merge import (ComicInfoUpdates,
                                                     merge_comicinfo)
from backend.implementations.identification import MatchingSnapshot, identify
from backend.implementations.naming import (generate_issue_name,
                                            generate_volume_folder_name)
from backend.implementations.organization_plan import (PlanningContext,
                                                       _naming_data, path_key,
                                                       path_module, plan_many,
                                                       plan_one, preview_plan)

NAMING = NamingSettings(True, '{series_name}', '{series_name} ({year}) #{issue_number}',
                        '{series_name} ({year}) #{issue_number}',
                        '{series_name} ({year}) {special_version}', '{series_name} Volume {issue_number}', False, 2, 3)


def records(special=SpecialVersion.NORMAL, raw='1', number=1.):
    v = PlanningVolume(volume(special_version=special), 1, '/library/Batman', True, 1)
    i = PlanningIssue(issue(raw=raw, calculated=number, references=(reference('301', kind=ResourceKind.ISSUE),)),
                      'Story', '2020-01-02', 'Summary', 301)
    return v, i


def identified(c=None, v=None, issues=None):
    v = v or records()[0]
    issues = issues or (records()[1],)
    return identify(c or candidate(), MatchingSnapshot.build([v.identity], [i.identity for i in issues]))


def context_for(results, volumes=None, issues=None, files=(), policy=PlanningPolicy(), naming=NAMING, observations=()):
    vs = volumes or (records()[0],)
    ins = issues or (records()[1],)
    root = 'C:\\library' if policy.windows else '/library'
    context = PlanningContext.build(vs, ins, ((1, root),), files, naming, policy)
    observed = {}
    for result in results:
        source = result.candidate.file
        observed[path_key(source.path, policy)] = PathObservation(source.path, True, size=source.size, mtime_ns=source.mtime_ns, device=1)
        preliminary = plan_one(result, context)
        for path in (preliminary.target_root, preliminary.target_folder):
            if path:
                observed[path_key(path, policy)] = PathObservation(path, True, directory=True, device=1)
        if preliminary.target_path:
            observed.setdefault(path_key(preliminary.target_path, policy), PathObservation(preliminary.target_path, False, device=1))
    observed.update({path_key(o.path, policy): o for o in observations})
    return PlanningContext.build(vs, ins, ((1, root),), files, naming, policy, observed.values())


class OrganizationPlanTests(TestCase):
    def test_move_rename_association_intent_only(self):
        r = identified()
        p = plan_one(r, context_for([r]))
        self.assertEqual(p.status, PlanStatus.READY)
        self.assertEqual(p.target_path, '/library/Batman/Batman (2020) #001.cbz')
        self.assertEqual([e.kind for e in p.effects], [EffectKind.RELOCATE, EffectKind.FILE_RECORD, EffectKind.ASSOCIATIONS])
        self.assertEqual(p.associations.added, (AssociationLink(1, 1),))

    def test_no_changes_with_correct_existing_links(self):
        c = candidate()
        c = replace(c, file=replace(c.file, path='/library/Batman/Batman (2020) #001.cbz'))
        r = identified(c)
        f = PlanningFile(5, c.file.path, (AssociationLink(1, 1, True),))
        p = plan_one(r, context_for([r], files=(f,)))
        self.assertEqual(p.status, PlanStatus.NO_CHANGES)
        self.assertEqual(p.effects, ())
        self.assertEqual(p.associations.after, f.links)

    def test_rename_only(self):
        c = candidate()
        c = replace(c, file=replace(c.file, path='/library/Batman/old.cbz'))
        r = identified(c)
        p = plan_one(r, context_for([r], files=(PlanningFile(5, c.file.path, (AssociationLink(1, 1),)),)))
        self.assertEqual(p.status, PlanStatus.READY)
        self.assertIn('filename', p.effects[0].reason)
        self.assertFalse(p.associations.added)

    def test_move_only_preserves_raw_filename(self):
        r = identified()
        p = plan_one(r, context_for([r], policy=PlanningPolicy(rename=False)))
        self.assertTrue(p.target_path.endswith(r.candidate.file.raw_name))

    def test_association_only(self):
        r = identified()
        p = plan_one(r, context_for([r], policy=PlanningPolicy(move=False, rename=False)))
        self.assertEqual([e.kind for e in p.effects], [EffectKind.FILE_RECORD, EffectKind.ASSOCIATIONS])

    def test_existing_managed_folder_wins_over_template(self):
        r = identified()
        p = plan_one(r, context_for([r], naming=replace(NAMING, volume_folder_naming='Different')))
        self.assertEqual(p.target_folder, '/library/Batman')
        self.assertEqual(p.folder_reason, 'reuse_managed_folder')

    def test_missing_noncustom_folder_uses_current_template(self):
        v, i = records()
        v = replace(v, folder='', custom_folder=False)
        r = identified(v=v)
        p = plan_one(r, context_for([r], volumes=(v,)))
        self.assertEqual(p.target_folder, '/library/Batman')
        self.assertEqual(p.folder_reason, 'legacy_folder_template')
        self.assertIn(EffectKind.VOLUME_FOLDER, [effect.kind for effect in p.effects])

    def test_missing_directory_is_intent_not_creation(self):
        r = identified()
        p = plan_one(r, context_for([r], observations=(PathObservation('/library/Batman', False),)))
        self.assertEqual(p.effects[0].kind, EffectKind.DIRECTORY)
        self.assertIn(EffectKind.DIRECTORY, p.effects[1].depends_on)

    def test_unknown_or_outside_root_blocked(self):
        for folder in ('/elsewhere/Batman', '../escape'):
            v = replace(records()[0], folder=folder)
            r = identified(v=v)
            p = plan_one(r, context_for([r], volumes=(v,)))
            self.assertEqual(p.status, PlanStatus.BLOCKED)
            self.assertFalse(p.effects)

    def test_identification_status_never_upgraded(self):
        for state, expected in ((MatchState.REVIEW, PlanStatus.REVIEW), (MatchState.CONFLICTED, PlanStatus.BLOCKED),
                                (MatchState.BLOCKED, PlanStatus.BLOCKED), (MatchState.UNRESOLVED, PlanStatus.UNRESOLVED)):
            r = replace(identified(), state=state)
            p = plan_one(r, context_for([r]))
            self.assertEqual(p.status, expected)
            self.assertIsNone(p.target_path)
            self.assertFalse(p.effects)

    def test_external_publication_requires_explicit_add(self):
        match = PublicationMatch(None, reference('abc', 'metron'), (), ())
        r = replace(identified(), state=MatchState.REVIEW, selected=None, alternatives=(match,))
        p = plan_one(r, context_for([r]))
        self.assertEqual(p.diagnostics[0].code, PlanCode.NEW_VOLUME)
        self.assertEqual(p.identification.alternatives[0].provider_identity.provider, 'metron')

    def test_changed_authority_blocked(self):
        r = identified()
        v = records()[0]
        v = replace(v, identity=replace(v.identity, authority=reference('m', 'metron')))
        self.assertEqual(plan_one(r, context_for([r], volumes=(v,))).status, PlanStatus.BLOCKED)

    def test_wrong_parent_blocked(self):
        r = identified()
        v, i = records()
        other = replace(v, identity=volume(2))
        i = replace(i, identity=replace(i.identity, volume_id=2))
        self.assertEqual(plan_one(r, context_for([r], volumes=(v, other), issues=(i,))).status, PlanStatus.BLOCKED)

    def test_safe_number_labels(self):
        for raw in ('1', '01', '1.5'):
            v, i = records(raw=raw, number=float(raw))
            r = identified(comic(candidate(), '<Number>' + raw + '</Number>'), v, (i,))
            p = plan_one(r, context_for([r], volumes=(v,), issues=(i,)))
            self.assertEqual(p.status, PlanStatus.READY)
            self.assertIn(raw.zfill(3), p.target_path)

    def test_opaque_naming_never_fabricates_projection(self):
        for raw, number in (('1A', 1.01), ('[nn]', .1414)):
            v, i = records(raw=raw, number=number)
            c = candidate(existing=ExistingFileIdentity(5, (LocalAssociation(1, 1, False, reference('1'), DB),)))
            r = identified(c, v, (i,))
            f = PlanningFile(5, c.file.path, (AssociationLink(1, 1),))
            p = plan_one(r, context_for([r], volumes=(v,), issues=(i,), files=(f,)))
            # 4G replaces numeric lookup with confirmed-ID display, not matching.
            self.assertEqual(p.status, PlanStatus.READY)
            self.assertTrue(p.target_path.endswith('#' + raw + '.cbz'))

    def test_collision_in_numeric_lookup_blocks_naming(self):
        v, i = records()
        other = replace(i, identity=replace(i.identity, id=2, raw_number='01'))
        r = identified(comic(candidate(), '<Number>1</Number>'), v, (i, other))
        p = plan_one(r, context_for([r], issues=(i, other)))
        self.assertEqual(p.status, PlanStatus.BLOCKED)

    def test_range_preserves_padding_but_cannot_claim_one_issue_provider_id(self):
        v, i = records()
        other = replace(i, identity=replace(i.identity, id=2, raw_number='2', calculated_number=2.))
        r = identified(candidate((1., 2.), 'Batman 1-2 (2020)'), v, (i, other))
        settings = replace(NAMING, file_naming_empty='{issue_number} [{issue_provider_id}]')
        p = plan_one(r, context_for([r], issues=(i, other), naming=settings))
        self.assertEqual(p.status, PlanStatus.BLOCKED)
        p = plan_one(r, context_for([r], issues=(i, other)))
        self.assertTrue(p.target_path.endswith('#001 - 002.cbz'))

    def test_noncontiguous_set_not_fabricated_range(self):
        v, i = records()
        ins = tuple(replace(i, identity=replace(i.identity, id=n, raw_number=str(n), calculated_number=float(n))) for n in (1, 2, 3))
        r = identified(v=v, issues=ins)
        r = replace(r, state=MatchState.AUTOMATIC, selected=replace(r.selected, local_issue_ids=(1, 3)))
        self.assertEqual(plan_one(r, context_for([r], issues=ins)).status, PlanStatus.BLOCKED)

    def test_vai_and_special_formats(self):
        for special in (SpecialVersion.VOLUME_AS_ISSUE, SpecialVersion.TPB, SpecialVersion.HARD_COVER,
                        SpecialVersion.OMNIBUS, SpecialVersion.ONE_SHOT):
            v, i = records(special=special)
            c = candidate(existing=ExistingFileIdentity(5, (LocalAssociation(1, 1, False, reference('1'), DB),)))
            r = identified(c, v, (i,))
            f = PlanningFile(5, c.file.path, (AssociationLink(1, 1),))
            p = plan_one(r, context_for([r], volumes=(v,), issues=(i,), files=(f,)))
            self.assertEqual(p.status, PlanStatus.READY)
            self.assertNotIn('#', p.target_path)

    def test_partial_date_does_not_gain_day(self):
        v, i = records()
        i = replace(i, date='2021-12-00')
        r = identified(v=v, issues=(i,))
        p = plan_one(r, context_for([r], issues=(i,)))
        self.assertEqual(p.status, PlanStatus.READY)
        self.assertNotIn('2021-12-01', p.target_path)
        settings = replace(NAMING, file_naming='{issue_release_date}')
        self.assertEqual(plan_one(r, context_for([r], issues=(i,), naming=settings)).status, PlanStatus.BLOCKED)

    def test_target_exists_blocks_without_suffix(self):
        r = identified()
        target = '/library/Batman/Batman (2020) #001.cbz'
        p = plan_one(r, context_for([r], observations=(PathObservation(target, True),)))
        self.assertEqual(p.status, PlanStatus.BLOCKED)
        self.assertIn(PlanCode.TARGET_EXISTS, [d.code for d in p.diagnostics])
        self.assertEqual(p.target_path, target)

    def test_db_target_collision_distinct_from_filesystem(self):
        r = identified()
        f = PlanningFile(9, '/library/Batman/Batman (2020) #001.cbz', ())
        p = plan_one(r, context_for([r], files=(f,)))
        self.assertIn(PlanCode.DB_TARGET, [d.code for d in p.diagnostics])

    def test_shared_target_is_symmetric_order_independent(self):
        a = identified()
        c = replace(a.candidate, candidate_id='b', file=replace(a.candidate.file, path='/incoming/other.cbz'))
        b = replace(a, candidate=c)
        context = context_for([a, b])
        batch = plan_many([a, b], context)
        self.assertEqual(batch, plan_many([b, a], context))
        self.assertTrue(all(p.status == PlanStatus.BLOCKED for p in batch.plans))
        self.assertTrue(all(PlanCode.SHARED_TARGET in [d.code for d in p.diagnostics] for p in batch.plans))

    def test_swap_is_path_dependency_for_both(self):
        a = identified()
        v, i = records()
        j = replace(i, identity=replace(i.identity, id=2, raw_number='2', calculated_number=2.))
        a = replace(a, candidate=replace(a.candidate, file=replace(a.candidate.file, path='/library/Batman/Batman (2020) #002.cbz')))
        b = replace(a, candidate=replace(a.candidate, candidate_id='b', file=replace(a.candidate.file, path='/library/Batman/Batman (2020) #001.cbz')),
                    selected=replace(a.selected, local_issue_ids=(2,)))
        batch = plan_many([a, b], context_for([a, b], issues=(i, j)))
        self.assertTrue(all(PlanCode.PATH_DEPENDENCY in [d.code for d in p.diagnostics] for p in batch.plans))

    def test_windows_case_only_rename_review(self):
        policy = PlanningPolicy(windows=True, case_sensitive=False, rename=False)
        v = replace(records()[0], folder='C:\\library\\Batman')
        r = identified(v=v)
        r = replace(r, candidate=replace(r.candidate, file=replace(r.candidate.file, path='C:\\library\\BATMAN\\issue.cbz')))
        p = plan_one(r, context_for([r], volumes=(v,), policy=policy))
        self.assertEqual(p.status, PlanStatus.REVIEW)
        self.assertIn(PlanCode.CASE_ONLY, [d.code for d in p.diagnostics])

    def test_stale_size_and_mtime_blocked(self):
        r = identified()
        for size, mtime in ((11, 100), (10, 101)):
            p = plan_one(r, context_for([r], observations=(PathObservation(r.candidate.file.path, True, size=size, mtime_ns=mtime),)))
            self.assertEqual(p.status, PlanStatus.BLOCKED)
            self.assertFalse(p.preconditions[0].validated_at_plan_time)

    def test_preconditions_revalidate_everything_at_apply(self):
        r = identified()
        p = plan_one(r, context_for([r]))
        self.assertTrue(all(c.revalidate_at_apply for c in p.preconditions))
        self.assertIn('existing_file_links', [c.name for c in p.preconditions])
        self.assertIn('selected_authority', [c.name for c in p.preconditions])

    def test_symlink_chain_blocked(self):
        r = identified()
        p = plan_one(r, context_for([r], observations=(PathObservation('/library/Batman', True, directory=True, unsafe_link=True),)))
        self.assertIn(PlanCode.SYMLINK, [d.code for d in p.diagnostics])

    def test_cross_device_is_visible_without_copy_effect(self):
        r = identified()
        p = plan_one(r, context_for([r], observations=(PathObservation('/library/Batman', True, directory=True, device=2),)))
        self.assertIn(PlanCode.CROSS_DEVICE, [d.code for d in p.diagnostics])

    def test_destructive_reassociation_review(self):
        r = identified()
        f = PlanningFile(5, r.candidate.file.path, (AssociationLink(1, 99, True),))
        p = plan_one(r, context_for([r], files=(f,)))
        self.assertEqual(p.status, PlanStatus.REVIEW)
        self.assertEqual(p.associations.removed, f.links)
        self.assertFalse(p.effects)

    def test_comicinfo_add_is_pure_payload(self):
        r = identified()
        c = r.candidate
        c = replace(c, comicinfo=replace(c.comicinfo, state=InspectionState.ABSENT, provenance=DB))
        r = replace(r, candidate=c)
        p = plan_one(r, context_for([r], policy=PlanningPolicy(metadata=MetadataMode.REQUIRED)))
        self.assertEqual(p.status, PlanStatus.READY)
        self.assertEqual(p.metadata.state, 'add')
        self.assertEqual(parse_comicinfo(p.metadata.xml).number, '1')
        self.assertIn(EffectKind.COMICINFO, [e.kind for e in p.effects])

    def test_comicinfo_unknown_preserved_in_merge_preview(self):
        r = identified(comic(candidate(), '<Title>Old</Title><Notes>Keep</Notes><Custom flag="yes"><Child/></Custom>'))
        p = plan_one(r, context_for([r], policy=PlanningPolicy(metadata=MetadataMode.REQUIRED)))
        self.assertIn(b'<Custom flag="yes"><Child/></Custom>', p.metadata.xml)
        self.assertIn(b'<Notes>Keep</Notes>', p.metadata.xml)
        self.assertIn('replace', [d.action for d in p.metadata.fields])

    def test_semantic_metadata_no_change_does_not_write(self):
        v, i = records()
        updates = ComicInfoUpdates(reference('1'), (('Series', 'Batman'), ('Number', '1'), ('Title', 'Story'),
            ('Summary', 'Summary'), ('Publisher', 'DC'), ('Year', '2020'), ('Month', '1'), ('Day', '2')),
            (reference('1'), reference('301', kind=ResourceKind.ISSUE)))
        xml = merge_comicinfo(None, updates)
        r = identified()
        c = replace(r.candidate, comicinfo=replace(r.candidate.comicinfo, state=InspectionState.PRESENT,
                                                  document=parse_comicinfo(xml), raw_bytes=xml, provenance=DB))
        r = replace(r, candidate=c)
        p = plan_one(r, context_for([r], policy=PlanningPolicy(metadata=MetadataMode.REQUIRED)))
        self.assertEqual(p.metadata.state, 'no_change')
        self.assertNotIn(EffectKind.COMICINFO, [e.kind for e in p.effects])

    def test_uninspected_metadata_never_treated_as_absent(self):
        r = identified()
        p = plan_one(r, context_for([r], policy=PlanningPolicy(metadata=MetadataMode.REQUIRED)))
        self.assertEqual(p.status, PlanStatus.BLOCKED)
        self.assertEqual(p.metadata.state, 'blocked_merge')

    def test_unsupported_optional_vs_required_metadata(self):
        r = identified()
        r = replace(r, candidate=replace(r.candidate, file=replace(r.candidate.file, path='/incoming/file.cbr')))
        for mode in (MetadataMode.OPTIONAL, MetadataMode.REQUIRED):
            p = plan_one(r, context_for([r], policy=PlanningPolicy(metadata=mode)))
            self.assertEqual(p.status, PlanStatus.READY if mode == MetadataMode.OPTIONAL else PlanStatus.BLOCKED)
            self.assertEqual(p.metadata.state, 'unsupported')

    def test_preview_json_no_raw_xml(self):
        r = identified(comic(candidate(), '<Notes>private unowned content</Notes>'))
        p = plan_one(r, context_for([r], policy=PlanningPolicy(metadata=MetadataMode.REQUIRED)))
        output = json.dumps(preview_plan(p), sort_keys=True)
        self.assertNotIn('private unowned content', output)
        self.assertNotIn('<ComicInfo', output)
        self.assertIn('source_stat', output)

    def test_no_io_during_compilation(self):
        r = identified()
        ctx = context_for([r])
        with patch('builtins.open', side_effect=AssertionError('open')), patch('os.rename', side_effect=AssertionError('rename')), \
                patch('os.remove', side_effect=AssertionError('remove')), patch('os.mkdir', side_effect=AssertionError('mkdir')), \
                patch('shutil.move', side_effect=AssertionError('move')), patch('socket.socket', side_effect=AssertionError('network')), \
                patch('backend.implementations.naming.Settings', side_effect=AssertionError('settings')):
            self.assertEqual(plan_one(r, ctx).status, PlanStatus.READY)

    def test_snapshot_and_policy_determinism(self):
        r = identified()
        context = context_for([r])
        a = plan_one(r, context)
        b = plan_one(r, context_for([r], observations=tuple(reversed(tuple(context.observations.values())))))
        self.assertEqual(a, b)
        self.assertEqual(a.policy_id, 'kapowarr-organization-plan/v1')

    def test_metadata_only_has_no_relocation(self):
        r = identified(comic(candidate(), '<Title>Old</Title>'))
        f = PlanningFile(5, r.candidate.file.path, (AssociationLink(1, 1),))
        p = plan_one(r, context_for([r], files=(f,), policy=PlanningPolicy(move=False, rename=False, metadata=MetadataMode.REQUIRED)))
        self.assertEqual([e.kind for e in p.effects], [EffectKind.COMICINFO, EffectKind.FILE_RECORD])

    def test_conflicting_identity_merge_does_not_write(self):
        xml = '<Identity xmlns="https://kapowarr.org/ns/comicinfo/1" provider="comicvine" kind="volume" id="wrong"/>'
        r = identified(comic(candidate(), xml))
        p = plan_one(r, context_for([r], policy=PlanningPolicy(metadata=MetadataMode.REQUIRED)))
        self.assertEqual(p.status, PlanStatus.BLOCKED)
        self.assertEqual(p.metadata.state, 'blocked_merge')
        self.assertIsNone(p.metadata.xml)

    def test_no_issue_title_invented_for_multi_issue_metadata(self):
        v, i = records()
        j = replace(i, identity=replace(i.identity, id=2, raw_number='2', calculated_number=2.))
        r = identified(comic(candidate((1., 2.), 'Batman 1-2 (2020)'), '<Notes>retain</Notes>'), v, (i, j))
        p = plan_one(r, context_for([r], issues=(i, j), policy=PlanningPolicy(metadata=MetadataMode.REQUIRED)))
        self.assertEqual(p.status, PlanStatus.BLOCKED)
        self.assertIsNone(p.metadata.xml)

    def test_general_existing_binding_is_retained_not_reassigned(self):
        v, i = records()
        c = candidate(existing=ExistingFileIdentity(5, (LocalAssociation(1, None, True, reference('1'), DB),)))
        r = identified(c)
        f = PlanningFile(5, c.file.path, (), (1,))
        p = plan_one(r, context_for([r], files=(f,), policy=PlanningPolicy(move=False, rename=False)))
        self.assertEqual(p.status, PlanStatus.NO_CHANGES)
        self.assertFalse(p.associations.added)

    def test_changed_forced_association_requires_review(self):
        c = candidate(existing=ExistingFileIdentity(5, (LocalAssociation(1, 1, True, reference('1'), DB),)))
        r = identified(c)
        f = PlanningFile(5, c.file.path, (AssociationLink(1, 1, False),))
        p = plan_one(r, context_for([r], files=(f,)))
        self.assertEqual(p.status, PlanStatus.REVIEW)
        self.assertIn(PlanCode.STALE_ASSOCIATIONS, [d.code for d in p.diagnostics])

    def test_windows_case_insensitive_db_collision(self):
        policy = PlanningPolicy(windows=True, case_sensitive=False)
        v = replace(records()[0], folder='C:\\library\\Batman')
        r = identified(v=v)
        r = replace(r, candidate=replace(r.candidate, file=replace(r.candidate.file, path='C:\\incoming\\file.cbz')))
        f = PlanningFile(9, 'c:\\LIBRARY\\BATMAN\\BATMAN (2020) #001.CBZ', ())
        p = plan_one(r, context_for([r], volumes=(v,), files=(f,), policy=policy))
        self.assertIn(PlanCode.DB_TARGET, [d.code for d in p.diagnostics])

    def test_posix_case_sensitive_paths_remain_distinct(self):
        r = identified()
        f = PlanningFile(9, '/library/Batman/BATMAN (2020) #001.CBZ', ())
        p = plan_one(r, context_for([r], files=(f,)))
        self.assertEqual(p.status, PlanStatus.READY)

    def test_reserved_windows_target_blocked(self):
        policy = PlanningPolicy(windows=True, case_sensitive=False)
        v = replace(records()[0], folder='C:\\library\\Batman')
        r = identified(v=v)
        r = replace(r, candidate=replace(r.candidate, file=replace(r.candidate.file, path='C:\\incoming\\file.cbz')))
        p = plan_one(r, context_for([r], volumes=(v,), policy=policy, naming=replace(NAMING, file_naming='CON', file_naming_empty='CON')))
        self.assertIn(PlanCode.PATH, [d.code for d in p.diagnostics])

    def test_path_length_guard_does_not_truncate(self):
        r = identified()
        p = plan_one(r, context_for([r], policy=PlanningPolicy(max_path_length=10)))
        self.assertEqual(p.status, PlanStatus.BLOCKED)
        self.assertTrue(len(p.target_path) > 10)

    def test_template_escape_not_accepted(self):
        r = identified()
        p = plan_one(r, context_for([r], naming=replace(NAMING, file_naming='/outside/issue', file_naming_empty='/outside/issue')))
        self.assertEqual(p.status, PlanStatus.BLOCKED)

    def test_unknown_target_state_never_means_vacant(self):
        r = identified()
        target = '/library/Batman/Batman (2020) #001.cbz'
        p = plan_one(r, context_for([r], observations=(PathObservation(target, None),)))
        self.assertEqual(p.status, PlanStatus.BLOCKED)
        self.assertIn(PlanCode.OBSERVATION, [d.code for d in p.diagnostics])

    def test_shared_folder_ownership_blocked_by_folder_policy(self):
        v, i = records()
        other = replace(v, identity=volume(2))
        r = identified()
        p = plan_one(r, context_for([r], volumes=(v, other)))
        self.assertEqual(p.status, PlanStatus.BLOCKED)
        self.assertIn(PlanCode.OWNERSHIP, [d.code for d in p.diagnostics])

    def test_duplicate_source_blocks_all_plans(self):
        r = identified()
        batch = plan_many([r, replace(r, candidate=replace(r.candidate, candidate_id='other'))], context_for([r]))
        self.assertTrue(all(PlanCode.DUPLICATE_SOURCE in [d.code for d in p.diagnostics] for p in batch.plans))

    def test_provider_variables_and_selected_authority(self):
        v, i = records()
        v = replace(v, identity=replace(v.identity, authority=reference('ABC', 'metron')), comicvine_id=None)
        i = replace(i, identity=replace(i.identity, references=(reference('XYZ', 'metron', ResourceKind.ISSUE),)), comicvine_id=None)
        r = identified(v=v, issues=(i,))
        naming = replace(NAMING, file_naming='{metadata_provider} {provider_id} {issue_provider_id} {issue_number}')
        p = plan_one(r, context_for([r], volumes=(v,), issues=(i,), naming=naming))
        self.assertTrue(p.target_path.endswith('metron ABC XYZ 001.cbz'))

    def test_unavailable_provider_template_data_blocks_without_lookup(self):
        v, i = records()
        i = replace(i, identity=replace(i.identity, references=()))
        r = identified(v=v, issues=(i,))
        p = plan_one(r, context_for([r], issues=(i,), naming=replace(NAMING, file_naming='{issue_provider_id}')))
        self.assertEqual(p.status, PlanStatus.BLOCKED)

    def test_representative_image_does_not_claim_complete_group(self):
        r = identified()
        r = replace(r, candidate=replace(r.candidate, file=replace(r.candidate.file, path='/incoming/page.jpg')))
        self.assertEqual(plan_one(r, context_for([r])).status, PlanStatus.REVIEW)

    def test_batch_summary_counts(self):
        r = identified()
        batch = plan_many([r], context_for([r]))
        self.assertEqual(dict(batch.counts)['ready'], 1)
        self.assertEqual(dict(batch.effect_counts)['relocate_file'], 1)

    def test_legacy_naming_parity_without_settings_or_db(self):
        for sv in (SpecialVersion.NORMAL, SpecialVersion.VOLUME_AS_ISSUE, SpecialVersion.HARD_COVER, SpecialVersion.TPB, SpecialVersion.OMNIBUS, SpecialVersion.ONE_SHOT):
            v, i = records(special=sv)
            data, issues, identity = _naming_data(v, (i,))
            with patch('backend.implementations.naming.Settings') as settings, patch('backend.implementations.naming.Issue') as issue_model:
                settings.return_value.sv = NAMING
                settings.return_value.get_settings.return_value = NAMING
                issue_model.from_volume_and_calc_number.return_value.get_data.return_value = next(iter(issues.values()))
                legacy = generate_issue_name(data, 1., identity_context=identity)
                legacy_folder = generate_volume_folder_name(data, identity)
            with patch('backend.implementations.naming.Settings', side_effect=AssertionError('settings')), patch('backend.implementations.naming.Issue', side_effect=AssertionError('DB')):
                self.assertEqual(generate_issue_name(data, 1., identity_context=identity, settings=NAMING, issues=issues), legacy)
                self.assertEqual(generate_volume_folder_name(data, identity, NAMING), legacy_folder)


class PlanningAcquisitionTests(ImportHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.db.executemany('INSERT OR REPLACE INTO config(key,value) VALUES (?,?)',
                            tuple(vars(NamingSettings.capture(self.settings)).items()))

    def test_real_preview_readonly_bounded_queries(self):
        path = self.comic_file()
        import_library([{'id': 2127, 'filepath': path}])
        # Matching can be supplied separately; no automatic matching/provider in preview.
        r = identified()
        r = replace(r, candidate=replace(r.candidate, file=replace(r.candidate.file, path=path,
                    size=os.stat(path).st_size, mtime_ns=os.stat(path).st_mtime_ns)),
                    selected=replace(r.selected, provider_identity=reference('2127')))
        before = self.db.total_changes
        content = Path(path).read_bytes()
        with patch('backend.internals.organization_plan.get_db', side_effect=self.db.cursor), \
                patch('backend.internals.identification.get_db', side_effect=self.db.cursor):
            statements = []
            self.db.set_trace_callback(statements.append)
            batch = preview_organization([r], ('comicvine', 'metron'), PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt'))
            self.db.set_trace_callback(None)
        self.assertEqual(len(batch.plans), 1)
        self.assertEqual(sum(s.lstrip().upper().startswith('SELECT') for s in statements), 10)
        self.assertEqual(before, self.db.total_changes)
        self.assertEqual(Path(path).read_bytes(), content)

    def test_stat_errors_are_not_absence(self):
        with patch('os.lstat', side_effect=PermissionError):
            observations = observe_plan_paths([str(self.root / 'x.cbz')], PlanningPolicy(windows=os.name == 'nt'))
        self.assertTrue(all(o.exists is None for o in observations))

    def test_bulk_query_count_not_per_candidate(self):
        path = self.comic_file()
        import_library([{'id': 2127, 'filepath': path}])
        r = identified()
        r = replace(r, candidate=replace(r.candidate, file=replace(r.candidate.file, path=path)),
                    selected=replace(r.selected, provider_identity=reference('2127')))
        for count in (1, 100, 1000):
            statements = []
            self.db.set_trace_callback(statements.append)
            with patch('backend.internals.organization_plan.get_db', side_effect=self.db.cursor), \
                    patch('backend.internals.identification.get_db', side_effect=self.db.cursor):
                preview_organization([replace(r, candidate=replace(r.candidate, candidate_id=str(n))) for n in range(count)],
                                     ('comicvine', 'metron'), PlanningPolicy(windows=os.name == 'nt', case_sensitive=os.name != 'nt'))
            self.db.set_trace_callback(None)
            self.assertEqual(sum(s.lstrip().upper().startswith('SELECT') for s in statements), 10)
