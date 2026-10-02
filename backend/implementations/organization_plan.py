"""Pure preview compiler. No filesystem, database or provider acquisition."""

import ntpath
import posixpath
from dataclasses import dataclass, replace
from datetime import date
from hashlib import sha256
from types import MappingProxyType
from typing import Dict, Iterable, Mapping, Optional, Tuple

from backend.base.comicinfo import ComicInfoError
from backend.base.definitions import FileConstants, IssueData, VolumeData
from backend.base.folder_policy import (FolderDecision,
                                        FolderPublication, FolderStatus)
from backend.base.identification import IdentificationResult, MatchState
from backend.base.import_candidate import InspectionState, ResourceKind
from backend.base.naming_policy import NamingSettings
from backend.base.organization_fingerprint import database_fingerprint
from backend.base.organization_plan import (AssociationDelta, AssociationLink,
                                            EffectKind, MetadataFieldDelta,
                                            MetadataIntent, MetadataMode,
                                            OrganizationBatch,
                                            OrganizationPlan, PathObservation,
                                            PlanCode, PlanDiagnostic,
                                            PlannedEffect, PlanningFile,
                                            PlanningIssue, PlanningPolicy,
                                            PlanningVolume, PlanStatus,
                                            Precondition, Severity)
from backend.base.rename_policy import (NamingContext, RenameCatalog,
                                        RenameIssue, RenameMode, RenameStatus)
from backend.implementations.comicinfo import IDENTITY_NS, parse_comicinfo
from backend.implementations.comicinfo_merge import (ComicInfoUpdates,
                                                     merge_comicinfo)
from backend.implementations.folder_policy import (FolderContext,
                                                   decide_folder,
                                                   preview_folder)
from backend.implementations.rename_policy import (build_rename_catalog,
                                                   decide_rename,
                                                   preview_rename)
from backend.internals.provider_identity import (MetadataIdentityError,
                                                 NamingIdentityContext,
                                                 VolumeMetadataIdentity)


def path_module(policy: PlanningPolicy):
    return ntpath if policy.windows else posixpath


def path_key(path: str, policy: PlanningPolicy) -> str:
    normalized = path_module(policy).normpath(path)
    return normalized if policy.case_sensitive else normalized.casefold()


def contained(root: str, path: str, policy: PlanningPolicy) -> bool:
    mod = path_module(policy)
    try:
        return mod.isabs(root) and mod.isabs(path) and mod.commonpath(
            (path_key(root, policy), path_key(path, policy))) == path_key(root, policy)
    except ValueError:
        return False


@dataclass(frozen=True)
class PlanningContext:
    volumes: Mapping[int, PlanningVolume]
    issues: Mapping[int, PlanningIssue]
    children: Mapping[int, Tuple[PlanningIssue, ...]]
    roots: Mapping[int, str]
    files: Mapping[str, Tuple[PlanningFile, ...]]
    observations: Mapping[str, PathObservation]
    folder_owners: Mapping[str, Tuple[int, ...]]
    naming: NamingSettings
    policy: PlanningPolicy
    context_id: str
    folder_context: FolderContext
    folder_decisions: Mapping[int, FolderDecision]
    rename_catalogs: Mapping[int, RenameCatalog]

    @classmethod
    def build(cls, volumes: Iterable[PlanningVolume], issues: Iterable[PlanningIssue],
              roots: Iterable[Tuple[int, str]], files: Iterable[PlanningFile],
              naming: NamingSettings, policy: PlanningPolicy,
              observations: Iterable[PathObservation] = ()) -> 'PlanningContext':
        vs = tuple(sorted(volumes, key=lambda v: v.identity.id))
        ins = tuple(sorted(issues, key=lambda i: i.identity.id))
        rs = tuple(sorted(roots))
        fs = tuple(sorted((replace(f, links=tuple(sorted(f.links)), general_volumes=tuple(sorted(f.general_volumes)), general_links=tuple(sorted(f.general_links)))
                           for f in files), key=lambda f: f.id))
        obs = tuple(sorted(observations, key=lambda p: path_key(p.path, policy)))
        if len({v.identity.id for v in vs}) != len(vs) or len({i.identity.id for i in ins}) != len(ins):
            raise ValueError('Duplicate planning identity')
        children: Dict[int, list[PlanningIssue]] = {v.identity.id: [] for v in vs}
        paths: Dict[str, list[PlanningFile]] = {}
        folders: Dict[str, list[int]] = {}
        for v in vs:
            if v.folder:
                folders.setdefault(path_key(v.folder, policy), []).append(v.identity.id)
        for i in ins:
            if i.identity.volume_id not in children:
                raise ValueError('Invalid planning issue parent')
            children[i.identity.volume_id].append(i)
        for f in fs:
            paths.setdefault(path_key(f.path, policy), []).append(f)
        if len({path_key(o.path, policy) for o in obs}) != len(obs):
            raise ValueError('Conflicting filesystem observations')
        digest = sha256(repr((vs, ins, rs, fs, obs, naming, policy)).encode()).hexdigest()
        folder_context = FolderContext.build(rs, ((v.identity.id, v.folder) for v in vs if v.folder),
                                             naming, policy.windows, policy.case_sensitive, policy.max_path_length)
        decisions = {v.identity.id: decide_folder(FolderPublication(
            v.identity.title, v.identity.year, v.identity.volume_number, v.identity.publisher,
            v.identity.authority, v.identity.id, v.root_id, v.folder, v.custom_folder,
            v.comicvine_id, v.identity.special_version), folder_context, policy.folder) for v in vs}
        return cls(MappingProxyType({v.identity.id: v for v in vs}),
                   MappingProxyType({i.identity.id: i for i in ins}),
                   MappingProxyType({k: tuple(v) for k, v in children.items()}),
                   MappingProxyType(dict(rs)), MappingProxyType({k: tuple(v) for k, v in paths.items()}),
                   MappingProxyType({path_key(o.path, policy): o for o in obs}),
                   MappingProxyType({k: tuple(v) for k, v in folders.items()}), naming, policy, digest,
                   folder_context, MappingProxyType(decisions),
                   MappingProxyType({vid: build_rename_catalog(vid, (i.identity for i in rows), naming.issue_padding)
                                     for vid, rows in children.items()}))


def _naming_data(volume: PlanningVolume, issues: Tuple[PlanningIssue, ...]):
    v = volume.identity
    if v.volume_number is None:
        raise ValueError('Volume number unavailable')
    data = VolumeData(v.id, volume.comicvine_id, v.title, None, v.year,
                      v.volume_number, '', '', v.publisher,
                      False, False, volume.root_id, volume.folder, volume.custom_folder,
                      v.special_version, False, 0)
    records = {i.identity.calculated_number: IssueData(i.identity.id, v.id, i.comicvine_id,
        i.identity.raw_number, i.identity.calculated_number, i.title, i.date, i.description or '', False, [])
        for i in issues}
    ids = {i.identity.id: r.provider_id for i in issues for r in i.identity.references
           if r.provider == v.authority.provider and r.kind == ResourceKind.ISSUE}
    identity = NamingIdentityContext(VolumeMetadataIdentity(v.id, v.authority.provider,
                                                           v.authority.provider_id, None), ids)
    return data, records, identity


def _metadata(result: IdentificationResult, volume: PlanningVolume,
              issues: Tuple[PlanningIssue, ...]) -> MetadataIntent:
    observation = result.candidate.comicinfo
    if observation.state not in (InspectionState.PRESENT, InspectionState.ABSENT):
        raise ValueError('ComicInfo must be inspected explicitly before planning a write')
    if len(issues) != 1:
        raise ValueError('No single authoritative issue for metadata')
    issue = issues[0]
    refs = tuple(r for r in issue.identity.references if r.provider == volume.identity.authority.provider)
    if len(refs) != 1:
        raise ValueError('Selected issue identity unavailable')
    values = tuple((k, v) for k, v in (
        ('Series', volume.identity.title), ('Number', issue.identity.raw_number),
        ('Title', issue.title), ('Summary', issue.description), ('Publisher', volume.identity.publisher)) if v)
    if issue.date:
        parsed = date.fromisoformat(issue.date)
        if parsed.isoformat() != issue.date:
            raise ValueError('No partial authoritative date projection')
        values += (('Year', str(parsed.year)), ('Month', str(parsed.month)), ('Day', str(parsed.day)))
    updates = ComicInfoUpdates(volume.identity.authority, values, (volume.identity.authority,) + refs)
    old = observation.document
    if observation.state == InspectionState.PRESENT and old is None:
        raise ValueError('Parsed ComicInfo required')
    xml = merge_comicinfo(old, updates)
    new = parse_comicinfo(xml)
    deltas = []
    for name, _ in values:
        before, after = old.text(name) if old else None, new.text(name)
        deltas.append(MetadataFieldDelta(name, before, after,
                                        'preserve' if before == after else 'add' if before is None else 'replace'))
    for field in old.fields if old else ():
        if field.name not in dict(values) and field.name != '{' + IDENTITY_NS + '}Identity':
            # Structured/custom content is retained internally, not dumped into preview.
            deltas.append(MetadataFieldDelta(field.name, None, None, 'preserve'))
    old_ids = tuple(sorted(tuple(sorted(f.attributes)) for f in old.fields if f.name == '{' + IDENTITY_NS + '}Identity')) if old else ()
    new_ids = tuple(sorted(tuple(sorted(f.attributes)) for f in new.fields if f.name == '{' + IDENTITY_NS + '}Identity'))
    if old_ids != new_ids:
        deltas.append(MetadataFieldDelta('Provider identities', None,
            ', '.join(f'{r.provider}/{r.kind.value}/{r.provider_id}' for r in updates.identities), 'merge'))
    changed = any(d.action != 'preserve' for d in deltas)
    return MetadataIntent('add' if old is None else 'merge' if changed else 'no_change', tuple(deltas),
                          xml if changed else None, sha256(old.raw_bytes).hexdigest() if old else None)


def plan_one(result: IdentificationResult, context: PlanningContext) -> OrganizationPlan:
    candidate, policy = result.candidate, context.policy
    mod = path_module(policy)
    plan = OrganizationPlan(result, PlanStatus.BLOCKED, context.context_id, policy, context.naming)
    if result.state != MatchState.AUTOMATIC:
        status = (PlanStatus.UNRESOLVED if result.state == MatchState.UNRESOLVED else
                  PlanStatus.REVIEW if result.state == MatchState.REVIEW else PlanStatus.BLOCKED)
        code = PlanCode.NEW_VOLUME if any(a.local_volume_id is None for a in result.alternatives) else PlanCode.IDENTIFICATION
        plan = replace(plan, status=status, diagnostics=(PlanDiagnostic(Severity.REVIEW, code),))
        if result.state == MatchState.REVIEW and len(result.alternatives) == 1 and result.alternatives[0].local_volume_id is None:
            external = result.alternatives[0]
            decision = decide_folder(FolderPublication(external.title, external.year, None, None,
                                                       external.provider_identity), context.folder_context, policy.folder)
            plan = replace(plan, folder_decision=decision, target_root=decision.root,
                           target_folder=decision.target_folder, folder_reason='prospective_external_publication')
        return plan
    selected = result.selected
    if selected is None or selected.local_volume_id is None:
        return replace(plan, status=PlanStatus.REVIEW, diagnostics=(PlanDiagnostic(Severity.REVIEW, PlanCode.NEW_VOLUME),))
    if selected.rejections or selected.review_reasons:
        return replace(plan, status=PlanStatus.REVIEW, diagnostics=(PlanDiagnostic(Severity.REVIEW, PlanCode.IDENTIFICATION),))
    if mod.splitext(candidate.file.path)[1].lower() in FileConstants.IMAGE_EXTENSIONS:
        return replace(plan, status=PlanStatus.REVIEW, diagnostics=(PlanDiagnostic(Severity.REVIEW, PlanCode.NAMING, 'Complete image-group discovery required'),))
    volume = context.volumes.get(selected.local_volume_id)
    issues = tuple(context.issues[i] for i in selected.local_issue_ids if i in context.issues)
    if (volume is None or selected.provider_identity != volume.identity.authority
            or len(issues) != len(selected.local_issue_ids)
            or any(i.identity.volume_id != volume.identity.id for i in issues)):
        return replace(plan, diagnostics=(PlanDiagnostic(Severity.BLOCKING, PlanCode.INVALID_IDENTITY),))
    decision = context.folder_decisions[volume.identity.id]
    plan = replace(plan, database_fingerprint=database_fingerprint(
        volume, context.children[volume.identity.id], context.roots.items(),
        ((v.identity.id, v.folder) for v in context.volumes.values() if v.folder), context.naming),
        volume_folder_before=volume.folder)
    plan = replace(plan, folder_decision=decision, target_root=decision.root,
                   target_folder=decision.target_folder)
    root = decision.root
    if root is None or not mod.isabs(root) or not mod.isabs(candidate.file.path):
        return replace(plan, diagnostics=(PlanDiagnostic(Severity.BLOCKING, PlanCode.ROOT),))
    diagnostics = [PlanDiagnostic(Severity.BLOCKING if d.blocking else Severity.WARNING,
                                  PlanCode.FOLDER, d.code.value + (':' + d.field if d.field else ''))
                   for d in decision.diagnostics]
    checks = []
    effects = []
    if decision.status == FolderStatus.BLOCKED and decision.target_folder is None:
        return replace(plan, preconditions=tuple(checks), diagnostics=tuple(diagnostics))
    if decision.status == FolderStatus.REVIEW:
        diagnostics.append(PlanDiagnostic(Severity.REVIEW, PlanCode.FOLDER, 'Folder transition requires review'))
    try:
        folder = decision.target_folder
        if folder is None:
            raise ValueError('Folder unavailable')
        if not contained(root, folder, policy):
            raise ValueError('Folder outside configured root')
        target_folder = folder if policy.move else mod.dirname(candidate.file.path)
        rename = decide_rename(NamingContext(volume.identity,
            tuple(RenameIssue(i.identity, i.title, i.date, i.comicvine_id) for i in issues),
            context.rename_catalogs[volume.identity.id], context.naming,
            mod.basename(candidate.file.path), volume.comicvine_id, target_folder,
            policy.windows, policy.case_sensitive, policy.max_path_length),
            policy.naming if policy.rename else replace(policy.naming, mode=RenameMode.PRESERVE_EXISTING))
        plan = replace(plan, rename_decision=rename)
        diagnostics.extend(PlanDiagnostic(Severity.BLOCKING if d.blocking else Severity.WARNING,
                                          PlanCode.NAMING, d.code.value + ':' + d.field) for d in rename.diagnostics)
        if rename.status == RenameStatus.REVIEW:
            diagnostics.append(PlanDiagnostic(Severity.REVIEW, PlanCode.NAMING, 'Rename requires review'))
        if rename.target_filename is None:
            return replace(plan, diagnostics=tuple(diagnostics), preconditions=tuple(checks))
        filename = rename.target_filename
        target = mod.normpath(mod.join(target_folder, filename))
        if not contained(target_folder, target, policy) or target == mod.normpath(target_folder):
            raise ValueError('Template escaped target folder')
    except (ValueError, KeyError, TypeError, MetadataIdentityError) as error:
        return replace(plan, target_root=root, diagnostics=(PlanDiagnostic(Severity.BLOCKING, PlanCode.NAMING, type(error).__name__),))
    plan = replace(plan, target_root=root, target_folder=mod.dirname(target), target_path=target,
                   folder_reason='reuse_managed_folder' if decision.retained else
                   'explicit_custom_target' if policy.folder.custom_relative is not None else 'legacy_folder_template')
    # Explicit platform semantics; do not use the host OS to compare foreign paths.
    source_key, target_key = path_key(candidate.file.path, policy), path_key(target, policy)
    same = mod.normpath(candidate.file.path) == target
    case_only = source_key == target_key and not same
    parts = target.replace('\\', '/').split('/') if policy.windows else target.split('/')
    if '\x00' in target:
        diagnostics.append(PlanDiagnostic(Severity.BLOCKING, PlanCode.PATH))
    if any(len(p.encode('utf-8')) > 255 for p in parts) or (policy.max_path_length is not None and len(target) > policy.max_path_length):
        diagnostics.append(PlanDiagnostic(Severity.BLOCKING, PlanCode.LENGTH))
    if policy.windows:
        reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
        components = ntpath.splitdrive(target)[1].replace('\\', '/').split('/')
        if any(p.rstrip(' .') != p or p.split('.')[0].upper() in reserved or any(c in p for c in '<>:"|?*')
               for p in components if p):
            diagnostics.append(PlanDiagnostic(Severity.BLOCKING, PlanCode.PATH))
    if any(vid != volume.identity.id for vid in context.folder_owners.get(path_key(folder, policy), ())):
        diagnostics.append(PlanDiagnostic(Severity.REVIEW, PlanCode.OWNERSHIP))
    observed = context.observations.get(source_key)
    source_valid = bool(candidate.file.stat_state == InspectionState.PRESENT and observed and observed.exists and not observed.directory
                        and observed.size == candidate.file.size and observed.mtime_ns == candidate.file.mtime_ns)
    checks.append(Precondition('source_stat', (candidate.file.path, str(candidate.file.size), str(candidate.file.mtime_ns)), source_valid))
    if not source_valid:
        diagnostics.append(PlanDiagnostic(Severity.BLOCKING, PlanCode.SOURCE if observed else PlanCode.OBSERVATION))
    target_obs = context.observations.get(target_key)
    folder_obs = context.observations.get(path_key(mod.dirname(target), policy))
    root_obs = context.observations.get(path_key(root, policy))
    for obs in (observed, target_obs, folder_obs, root_obs):
        if obs and obs.unsafe_link:
            diagnostics.append(PlanDiagnostic(Severity.BLOCKING, PlanCode.SYMLINK))
    if not root_obs or not root_obs.exists or not root_obs.directory:
        diagnostics.append(PlanDiagnostic(Severity.BLOCKING, PlanCode.ROOT))
    if target_obs is None or target_obs.exists is None or folder_obs is None or folder_obs.exists is None:
        diagnostics.append(PlanDiagnostic(Severity.BLOCKING, PlanCode.OBSERVATION))
    elif not same and not case_only and target_obs.exists:
        diagnostics.append(PlanDiagnostic(Severity.BLOCKING, PlanCode.TARGET_EXISTS))
    if folder_obs and folder_obs.exists and not folder_obs.directory:
        diagnostics.append(PlanDiagnostic(Severity.BLOCKING, PlanCode.PATH))
    checks.append(Precondition('target_vacancy_or_same_source', (target,), bool(target_obs and (not target_obs.exists or source_key == target_key))))
    if case_only:
        diagnostics.append(PlanDiagnostic(Severity.REVIEW, PlanCode.CASE_ONLY))
    if observed and folder_obs and observed.device is not None and folder_obs.device is not None:
        if observed.device != folder_obs.device:
            diagnostics.append(PlanDiagnostic(Severity.INFO, PlanCode.CROSS_DEVICE))
    elif not same:
        diagnostics.append(PlanDiagnostic(Severity.INFO, PlanCode.DEVICE_UNKNOWN))
    existing_files = context.files.get(source_key, ())
    if len(existing_files) > 1:
        diagnostics.append(PlanDiagnostic(Severity.BLOCKING, PlanCode.STALE_ASSOCIATIONS))
    existing = existing_files[0] if len(existing_files) == 1 else None
    if any(f.id != (existing.id if existing else None) for f in context.files.get(target_key, ())):
        diagnostics.append(PlanDiagnostic(Severity.BLOCKING, PlanCode.DB_TARGET))
    before = existing.links if existing else ()
    expected_links = {AssociationLink(a.volume_id, a.issue_id, a.forced) for a in candidate.existing.associations if a.issue_id is not None} if candidate.existing else set()
    expected_general = {a.volume_id for a in candidate.existing.associations if a.issue_id is None} if candidate.existing else set()
    if candidate.existing and (existing is None or existing.id != candidate.existing.file_id or set(before) != expected_links
                               or set(existing.general_volumes) != expected_general):
        diagnostics.append(PlanDiagnostic(Severity.REVIEW, PlanCode.STALE_ASSOCIATIONS))
    if candidate.existing and existing and existing.general_links and {
        (a.volume_id, a.forced) for a in candidate.existing.associations if a.issue_id is None
    } != {(vid, forced) for vid, forced, _ in existing.general_links}:
        diagnostics.append(PlanDiagnostic(Severity.REVIEW, PlanCode.STALE_ASSOCIATIONS))
    desired = tuple(sorted(next((l for l in before if l.issue_id == i.identity.id and l.volume_id == volume.identity.id),
                               AssociationLink(volume.identity.id, i.identity.id)) for i in issues)) if policy.associate else before
    delta = AssociationDelta(existing.id if existing else None, before, desired,
                             tuple(l for l in desired if l not in before), tuple(l for l in before if l not in desired))
    if delta.removed or (existing and any(v != volume.identity.id for v in existing.general_volumes)):
        diagnostics.append(PlanDiagnostic(Severity.REVIEW, PlanCode.ASSOCIATIONS))
    checks.extend((Precondition('selected_authority', (str(volume.identity.id), repr(volume.identity.authority)), True),
                   Precondition('rename_policy_settings_and_coverage', (rename.policy_id, rename.fingerprint), True),
                   Precondition('folder_policy_root_and_ownership', (decision.fingerprint, repr(policy.folder)), True),
                   Precondition('issue_parents', tuple(str(i.identity.id) for i in issues), True),
                   Precondition('existing_file_links', (repr(existing),), True),
                   Precondition('naming_settings_and_folder', (repr(context.naming), folder, root), True)))
    metadata = MetadataIntent('not_requested')
    if policy.metadata != MetadataMode.OFF:
        severity = Severity.BLOCKING if policy.metadata == MetadataMode.REQUIRED else Severity.WARNING
        if mod.splitext(candidate.file.path)[1].casefold() not in ('.cbz', '.zip'):
            metadata = MetadataIntent('unsupported')
            diagnostics.append(PlanDiagnostic(severity, PlanCode.METADATA_UNSUPPORTED))
        else:
            try:
                metadata = _metadata(result, volume, issues)
            except (ValueError, ComicInfoError):
                metadata = MetadataIntent('blocked_merge')
                diagnostics.append(PlanDiagnostic(severity, PlanCode.METADATA))
        checks.append(Precondition('comicinfo_source_and_archive_write_admission',
                                   (candidate.file.path, metadata.source_digest or 'absent_or_unavailable'), False))
    if folder_obs and folder_obs.exists is False and not same:
        effects.append(PlannedEffect(EffectKind.DIRECTORY, 'Target directory does not exist'))
    if not same:
        effects.append(PlannedEffect(EffectKind.RELOCATE, 'Configured filename differs' if mod.dirname(candidate.file.path) == mod.dirname(target)
                                     else 'File is outside selected target folder', tuple(e.kind for e in effects)))
    if metadata.xml is not None:
        effects.append(PlannedEffect(EffectKind.COMICINFO, 'Selected-authority merge changes embedded metadata', tuple(e.kind for e in effects)))
    if not volume.folder and policy.move:
        effects.append(PlannedEffect(EffectKind.VOLUME_FOLDER, 'Record calculated folder only where no managed folder exists', tuple(e.kind for e in effects)))
    if (not same or delta.added or delta.removed or metadata.xml is not None) and (existing or policy.associate and issues):
        effects.append(PlannedEffect(EffectKind.FILE_RECORD, 'Reconcile path/size after filesystem effects', tuple(e.kind for e in effects)))
    if delta.added or delta.removed:
        effects.append(PlannedEffect(EffectKind.ASSOCIATIONS, 'Identified issue set differs from existing links', tuple(e.kind for e in effects)))
    status = PlanStatus.BLOCKED if any(d.severity == Severity.BLOCKING for d in diagnostics) else PlanStatus.REVIEW if any(
        d.severity == Severity.REVIEW for d in diagnostics) else PlanStatus.READY if effects else PlanStatus.NO_CHANGES
    return replace(plan, status=status, associations=delta, metadata=metadata, preconditions=tuple(checks),
                   effects=tuple(effects) if status in (PlanStatus.READY, PlanStatus.NO_CHANGES) else (),
                   diagnostics=tuple(dict.fromkeys(diagnostics)))


def plan_many(results: Iterable[IdentificationResult], context: PlanningContext) -> OrganizationBatch:
    plans = [plan_one(r, context) for r in results]
    sources: Dict[str, list[int]] = {}
    targets: Dict[str, list[int]] = {}
    for index, plan in enumerate(plans):
        sources.setdefault(path_key(plan.source_path, context.policy), []).append(index)
        if plan.target_path:
            targets.setdefault(path_key(plan.target_path, context.policy), []).append(index)
    extra: Dict[int, set[PlanCode]] = {}
    # Different filenames must not hide two publications claiming one directory.
    folder_groups: Dict[str, list[int]] = {}
    for index, plan in enumerate(plans):
        if plan.policy.move and plan.target_folder and plan.identification.selected:
            folder_groups.setdefault(path_key(plan.target_folder, context.policy), []).append(index)
    for members in folder_groups.values():
        if len({plans[i].identification.selected.local_volume_id for i in members if plans[i].identification.selected}) > 1:
            for index in members:
                extra.setdefault(index, set()).add(PlanCode.OWNERSHIP)
    # Parent/child prospective ownership is also incompatible. A sorted sweep
    # retains only active ancestors rather than comparing every file pair.
    ancestors: list[Tuple[str, list[int]]] = []
    for folder_key, members in sorted(folder_groups.items(), key=lambda item: tuple(item[0].split(path_module(context.policy).sep))):
        ancestors = [(key, indexes) for key, indexes in ancestors
                     if context.folder_context.inside(key, folder_key)]
        for _, indexes in ancestors:
            involved = indexes + members
            if len({plans[i].identification.selected.local_volume_id for i in involved if plans[i].identification.selected}) > 1:
                for index in involved:
                    extra.setdefault(index, set()).add(PlanCode.OWNERSHIP)
        ancestors.append((folder_key, members))
    for members in sources.values():
        if len(members) > 1:
            for index in members:
                extra.setdefault(index, set()).add(PlanCode.DUPLICATE_SOURCE)
    for path, members in targets.items():
        if len(members) > 1:
            for index in members:
                extra.setdefault(index, set()).add(PlanCode.SHARED_TARGET)
        for index in members:
            for other in sources.get(path, ()):
                if other != index:
                    extra.setdefault(index, set()).add(PlanCode.PATH_DEPENDENCY)
                    extra.setdefault(other, set()).add(PlanCode.PATH_DEPENDENCY)
    for index, codes in extra.items():
        plans[index] = replace(plans[index], status=PlanStatus.BLOCKED, effects=(),
            diagnostics=plans[index].diagnostics + tuple(PlanDiagnostic(Severity.BLOCKING, c) for c in sorted(codes, key=lambda c: c.value)))
    return OrganizationBatch(tuple(sorted(plans, key=lambda p: (path_key(p.source_path, context.policy),
                                                                p.identification.candidate.candidate_id))))


def preview_plan(plan: OrganizationPlan) -> dict:
    """Explicit transport projection; never serializes raw XML or domain objects."""
    mod = path_module(plan.policy)
    selected = plan.identification.selected
    return {'status': plan.status.value, 'policy': plan.policy_id, 'source': plan.source_path,
            'folder_decision': preview_folder(plan.folder_decision) if plan.folder_decision else None,
            'rename_decision': preview_rename(plan.rename_decision) if plan.rename_decision else None,
            'target': plan.target_path, 'old_filename': mod.basename(plan.source_path),
            'new_filename': mod.basename(plan.target_path) if plan.target_path else None,
            'root': plan.target_root, 'current_folder': mod.dirname(plan.source_path),
            'folder': plan.target_folder, 'folder_reason': plan.folder_reason,
            'volume_id': selected.local_volume_id if selected else None,
            'issue_ids': list(selected.local_issue_ids) if selected else [],
            'authority': {'provider': selected.provider_identity.provider, 'id': selected.provider_identity.provider_id} if selected else None,
            'effects': [{'kind': e.kind.value, 'reason': e.reason, 'after': [k.value for k in e.depends_on]} for e in plan.effects],
            'metadata': {'state': plan.metadata.state, 'fields': [dict(field=d.field, before=d.before, after=d.after, action=d.action) for d in plan.metadata.fields]},
            'associations': {'before': [l.issue_id for l in plan.associations.before],
                             'after': [l.issue_id for l in plan.associations.after],
                             'added': [l.issue_id for l in plan.associations.added],
                             'removed': [l.issue_id for l in plan.associations.removed]} if plan.associations else None,
            'diagnostics': [{'severity': d.severity.value, 'code': d.code.value, 'detail': d.detail} for d in plan.diagnostics],
            'identification_reasons': [r.value for r in plan.identification.reasons],
            'candidate_diagnostics': [d.code.value for d in plan.identification.candidate.diagnostics],
            'preconditions': [{'name': c.name, 'validated': c.validated_at_plan_time, 'revalidate': c.revalidate_at_apply} for c in plan.preconditions]}
