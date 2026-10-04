"""Explicit, serial exact-plan application. Never called by discovery or GET.

The caller supplies an existing application DB and explicitly authorized local
filesystem scopes. No provider, matcher, naming or folder evaluator is invoked.
"""

import base64
import errno
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

from backend.base.comicinfo import ComicInfoError
from backend.base.folder_policy import POLICY_ID as FOLDER_POLICY, FolderStatus
from backend.base.identification import POLICY_ID as MATCH_POLICY, MatchState
from backend.base.import_candidate import InspectionState
from backend.base.logging import LOGGER
from backend.base.organization_fingerprint import database_fingerprint
from backend.base.organization_job import (EXECUTOR_POLICY, ExecutionCode,
                                           JobState, OrganizationError,
                                           OrganizationJob, StepState,
                                           UndoPreview)
from backend.base.organization_plan import (MERGE_POLICY, PLAN_POLICY,
                                            AssociationLink, EffectKind,
                                            OrganizationPlan,
                                            PlanningFile, PlanStatus)
from backend.base.rename_policy import POLICY_ID as RENAME_POLICY, RenameStatus
from backend.implementations.comicinfo import MAX_XML, parse_comicinfo
from backend.implementations.comicinfo_archive import (inspect_comicinfo,
                                                       write_comicinfo)
from backend.implementations.metadata.registry import PROVIDERS
from backend.implementations.organization_filesystem import (
    artifact, cancellation_scope, execution_gate, matches,
    rename_no_replace, safe_path, sync_directory)
from backend.implementations.organization_plan import preview_plan
from backend.internals.organization_jobs import JobStore, canonical, digest
from backend.internals.organization_plan import load_planning_records
from backend.internals.provider_identity import MetadataIdentityError

RESTORE = 'restore_recorded_database_state'
CLEANUP = 'remove_job_created_empty_directories'


def _key(path: str) -> str:
    # Conservative cross-job reservation even on case-sensitive hosts.
    return os.path.normpath(path).casefold()


class OrganizationExecutor:
    def __init__(self, database: str, allowed_roots: tuple[str, ...], *,
                 checkpoint: Optional[Callable[[str, str, int], None]] = None):
        self.store = JobStore(database)
        self.allowed_roots = tuple(str(Path(p).absolute()) for p in allowed_roots)
        if not self.allowed_roots or any(Path(p).parent == Path(p) for p in self.allowed_roots):
            self.store.close()
            raise OrganizationError(ExecutionCode.UNSAFE_PATH, 'Explicit bounded execution scopes required')
        try:
            for root in self.allowed_roots:
                safe_path(root)
                if not Path(root).is_dir():
                    raise OrganizationError(ExecutionCode.UNSAFE_PATH)
        except BaseException:
            self.store.close()
            raise
        self.hook = checkpoint or (lambda stage, job, ordinal: None)

    def close(self) -> None:
        self.store.close()

    def _scope(self, path: str) -> None:
        if not any(Path(path) != Path(root) and Path(root) in Path(path).parents for root in self.allowed_roots):
            raise OrganizationError(ExecutionCode.UNSAFE_PATH, 'Path outside explicit execution scope')
        safe_path(path)

    def _file(self, fid: Optional[int], source: str, target: str) -> Optional[dict]:
        inactive = ('EXISTS(SELECT 1 FROM quarantined_files q WHERE q.file_id=files.id)'
                    if self.store.has_quarantine_state else '0')
        if os.name == 'nt':
            # SQLite's NOCASE collation is ASCII-only. Windows ownership must
            # also detect Unicode/case-equivalent paths introduced after preview.
            keys = {_key(source), _key(target)}
            rows = [r for r in self.store.db.execute(f'SELECT id,filepath,size,{inactive} FROM files ORDER BY id')
                    if r[0] == fid or _key(r[1]) in keys]
        else:
            rows = self.store.db.execute(f'SELECT id,filepath,size,{inactive} FROM files WHERE id=? OR filepath IN (?,?) ORDER BY id',
                                         (fid, source, target)).fetchall()
        if len(rows) > 1 or rows and fid is not None and rows[0][0] != fid:
            raise OrganizationError(ExecutionCode.CONFLICT, 'Unexpected file-path owner')
        if not rows:
            return None
        row = rows[0]
        # Ordinary move/ComicInfo/import effects cannot reactivate retained
        # identities. A future quarantine inverse has its own versioned guard.
        if row[3]:
            raise OrganizationError(ExecutionCode.STALE, 'quarantined_file_inactive')
        links = [list(r) for r in self.store.db.execute('''SELECT i.volume_id,b.issue_id,b.forced FROM issues_files b
            JOIN issues i ON i.id=b.issue_id WHERE b.file_id=? ORDER BY i.volume_id,b.issue_id''', (row[0],))]
        general = [list(r) for r in self.store.db.execute(
            'SELECT volume_id,forced,file_type FROM volume_files WHERE file_id=? ORDER BY volume_id', (row[0],))]
        return dict(id=row[0], path=row[1], size=row[2], links=links, general=general)

    def _database(self, intent: dict, fid: Optional[int]) -> dict:
        row = self.store.db.execute('SELECT folder FROM volumes WHERE id=?', (intent['volume_id'],)).fetchone()
        if row is None:
            raise OrganizationError(ExecutionCode.STALE, 'Volume no longer exists')
        return dict(file=self._file(fid, intent['source'], intent['target']), folder=row[0])

    def _metadata_guard(self, intent: dict, expected_folder: str) -> None:
        if intent.get('rename_authority') is not None:
            from backend.internals.rename_review import naming_evidence
            try:
                current_evidence = naming_evidence(self.store.db.cursor(), intent['volume_id'])
            except (ValueError, TypeError):
                raise OrganizationError(ExecutionCode.STALE, 'Naming evidence unavailable or bounded') from None
            if current_evidence != intent.get('rename_evidence'):
                raise OrganizationError(ExecutionCode.STALE, 'Rich naming facts or classification control changed')
        authority = intent.get('rename_authority') or intent.get('repair_authority')
        if authority is not None:
            from backend.base.switch_review import SwitchReviewError
            from backend.internals.provider_authority import (AuthorityToken,
                                                              require_current)
            try:
                require_current(self.store.db.cursor(), (AuthorityToken(**authority),))
            except (SwitchReviewError, TypeError, ValueError):
                raise OrganizationError(ExecutionCode.STALE, 'Repair authority generation changed') from None
        try:
            vs, issues, roots, _, naming = load_planning_records(tuple(PROVIDERS), self.store.db.cursor())
        except (MetadataIdentityError, ValueError, KeyError, TypeError):
            raise OrganizationError(ExecutionCode.STALE, 'Canonical metadata or settings unavailable') from None
        volume = next((v for v in vs if v.identity.id == intent['volume_id']), None)
        if volume is None or volume.folder != expected_folder:
            raise OrganizationError(ExecutionCode.STALE, 'Volume ownership changed')
        # Normalize only this job's receipted folder field, never other metadata.
        old = intent['volume_folder_before']
        volume = replace(volume, folder=old)
        owners = tuple((v.identity.id, old if v.identity.id == volume.identity.id else v.folder)
                       for v in vs if (old if v.identity.id == volume.identity.id else v.folder))
        value = database_fingerprint(volume, (i for i in issues if i.identity.volume_id == volume.identity.id),
                                     (tuple(r) for r in roots), owners, naming)
        if value != intent['database_fingerprint']:
            raise OrganizationError(ExecutionCode.STALE, 'Canonical metadata, settings, roots or ownership changed')

    def create_job(self, plan: OrganizationPlan, *, batch_id: Optional[str] = None,
                   repair_authority=None, repair_origin=None, rename_authority=None,
                   rename_origin=None, rename_stamp=None, rename_evidence=None) -> str:
        authority = rename_authority or repair_authority
        plan_id = digest(repr(plan) if authority is None else repr((plan, authority, batch_id)))
        existing = self.store.db.execute('SELECT id FROM organization_jobs WHERE plan_digest=?', (plan_id,)).fetchone()
        if existing:
            self.store.intent(existing[0])
            return str(existing[0])
        with execution_gate(self.store.path):
            intent, plan_id = self._prepare_job(plan, batch_id=batch_id,
                repair_authority=repair_authority, repair_origin=repair_origin,
                rename_authority=rename_authority, rename_origin=rename_origin, rename_stamp=rename_stamp,
                rename_evidence=rename_evidence)
            return self.store.create(intent, plan_id, (_key(plan.source_path), _key(plan.target_path)), batch_id)

    def _prepare_job(self, plan: OrganizationPlan, *, batch_id=None,
                     repair_authority=None, repair_origin=None, rename_authority=None,
                     rename_origin=None, rename_stamp=None, rename_evidence=None):
        if plan.status not in (PlanStatus.READY, PlanStatus.NO_CHANGES):
            raise OrganizationError(ExecutionCode.NOT_AUTHORIZED)
        if plan.identification.state != MatchState.AUTOMATIC:
            raise OrganizationError(ExecutionCode.NOT_AUTHORIZED)
        if (plan.policy_id, plan.folder_policy, plan.naming_policy, plan.identification.policy_id, plan.metadata.policy_id) != (
                PLAN_POLICY, FOLDER_POLICY, RENAME_POLICY, MATCH_POLICY, MERGE_POLICY):
            raise OrganizationError(ExecutionCode.UNSUPPORTED)
        if plan.policy.windows != (os.name == 'nt') or plan.policy.case_sensitive == (os.name == 'nt'):
            raise OrganizationError(ExecutionCode.UNSUPPORTED, 'Foreign filesystem comparison policy')
        selected = plan.identification.selected
        if (not selected or selected.local_volume_id is None or not plan.target_path or not plan.target_folder
                or not plan.target_root or not plan.database_fingerprint or not plan.associations
                or not plan.folder_decision or not plan.rename_decision):
            raise OrganizationError(ExecutionCode.NOT_AUTHORIZED)
        if (selected.rejections or selected.review_reasons
                or plan.folder_decision.status in (FolderStatus.REVIEW, FolderStatus.BLOCKED)
                or plan.rename_decision.status in (RenameStatus.REVIEW, RenameStatus.BLOCKED)):
            raise OrganizationError(ExecutionCode.NOT_AUTHORIZED)
        if (os.path.join(plan.target_folder, plan.rename_decision.target_filename or '') != plan.target_path
                or plan.associations.removed):
            raise OrganizationError(ExecutionCode.NOT_AUTHORIZED)
        for path in (plan.source_path, plan.target_path, plan.target_folder):
            self._scope(path)
        if plan.policy.move and Path(plan.target_root) not in Path(plan.target_path).parents:
            raise OrganizationError(ExecutionCode.UNSAFE_PATH)
        if plan.source_path != plan.target_path and _key(plan.source_path) == _key(plan.target_path):
            raise OrganizationError(ExecutionCode.UNSUPPORTED, 'Case-only transition')
        kinds = [e.kind.value for e in plan.effects]
        order = [EffectKind.DIRECTORY.value, EffectKind.RELOCATE.value, EffectKind.COMICINFO.value,
                 EffectKind.VOLUME_FOLDER.value, EffectKind.FILE_RECORD.value, EffectKind.ASSOCIATIONS.value]
        required = {'source_stat', 'target_vacancy_or_same_source', 'selected_authority',
                    'rename_policy_settings_and_coverage', 'folder_policy_root_and_ownership',
                    'issue_parents', 'existing_file_links', 'naming_settings_and_folder'}
        names = {p.name for p in plan.preconditions}
        if (len(set(kinds)) != len(kinds) or kinds != [k for k in order if k in kinds]
                or not required.issubset(names) or names - required - {'comicinfo_source_and_archive_write_admission'}
                or any(not p.revalidate_at_apply for p in plan.preconditions)
                or (plan.source_path != plan.target_path) != (EffectKind.RELOCATE.value in kinds)
                or (plan.metadata.xml is not None) != (EffectKind.COMICINFO.value in kinds)):
            raise OrganizationError(ExecutionCode.CORRUPT)
        for n, effect in enumerate(plan.effects):
            if any(d.value not in kinds[:n] for d in effect.depends_on):
                raise OrganizationError(ExecutionCode.CORRUPT, 'Invalid effect dependencies')
        observation = plan.identification.candidate.file
        intent = dict(version=EXECUTOR_POLICY, source=plan.source_path, target=plan.target_path,
            root=plan.target_root, folder=plan.target_folder, volume_id=selected.local_volume_id,
            source_size=observation.size, source_mtime_ns=observation.mtime_ns,
            observed_at=observation.observed_at.isoformat(), database_fingerprint=plan.database_fingerprint,
            volume_folder_before=plan.volume_folder_before, effects=kinds,
            links_after=[[l.volume_id, l.issue_id, int(l.forced)] for l in plan.associations.after],
            xml=base64.b64encode(plan.metadata.xml).decode('ascii') if plan.metadata.xml is not None else None,
            xml_before=plan.metadata.source_digest, preview=preview_plan(plan),
            policies=[plan.policy_id, plan.folder_policy, plan.naming_policy, plan.identification.policy_id, plan.metadata.policy_id],
            fingerprints=[plan.folder_decision.fingerprint, plan.rename_decision.fingerprint],
            preconditions=[[p.name, list(p.expected), p.validated_at_plan_time, p.revalidate_at_apply] for p in plan.preconditions],
            inverse=False)
        if repair_authority is not None:
            from dataclasses import asdict

            from backend.internals.provider_authority import AuthorityToken
            if not isinstance(repair_authority, AuthorityToken) or repair_authority.volume_id != selected.local_volume_id:
                raise OrganizationError(ExecutionCode.NOT_AUTHORIZED)
            if (plan.source_path != plan.target_path or any(e.kind not in
                    (EffectKind.COMICINFO, EffectKind.FILE_RECORD) for e in plan.effects)):
                raise OrganizationError(ExecutionCode.NOT_AUTHORIZED)
            intent['repair_authority'] = asdict(repair_authority)
            if repair_origin is not None:
                intent['repair_origin'] = repair_origin
        if rename_authority is not None:
            from dataclasses import asdict

            from backend.internals.provider_authority import AuthorityToken
            if (repair_authority is not None or not isinstance(rename_authority, AuthorityToken)
                    or rename_authority.volume_id != selected.local_volume_id
                    or plan.policy.move or plan.policy.associate or plan.metadata.xml is not None
                    or os.path.dirname(plan.source_path) != os.path.dirname(plan.target_path)
                    or plan.associations.before != plan.associations.after
                    or plan.associations.file_id is None
                    or kinds != [EffectKind.RELOCATE.value, EffectKind.FILE_RECORD.value]):
                raise OrganizationError(ExecutionCode.NOT_AUTHORIZED, 'Filename-only rename required')
            intent['rename_authority'] = asdict(rename_authority)
            intent['rename_origin'] = rename_origin
            intent['rename_stamp'] = rename_stamp
            intent['rename_evidence'] = rename_evidence
        authority = rename_authority or repair_authority
        plan_id = digest(repr(plan) if authority is None else repr((plan, authority, batch_id)))
        before = self._database(intent, plan.associations.file_id)
        f = before['file']
        equivalent = PlanningFile(f['id'], f['path'], tuple(AssociationLink(v, i, bool(b)) for v, i, b in f['links']),
                                  tuple(g[0] for g in f['general']), tuple((v, bool(b), t) for v, b, t in f['general'])) if f else None
        expected = next((p.expected for p in plan.preconditions if p.name == 'existing_file_links'), ())
        if expected != (repr(equivalent),):
            raise OrganizationError(ExecutionCode.STALE, 'Existing file links changed before job creation')
        intent['database_before'] = before
        return intent, plan_id

    def _validate_intent(self, intent: dict) -> None:
        if 'archive_effect' in intent:
            from backend.features.organization_archive import ArchiveEffect
            ArchiveEffect(self).validate(intent)
            return
        if 'seed_copy_effect' in intent:
            from backend.features.organization_seed_copy import SeedCopyEffect
            SeedCopyEffect(self).validate(intent)
            return
        if 'upgrade_effect' in intent:
            from backend.features.organization_upgrade import UpgradeEffect
            UpgradeEffect(self).validate(intent)
            return
        if 'quarantine_effect' in intent:
            from backend.features.organization_quarantine import \
                QuarantineEffect
            QuarantineEffect(self).validate(intent)
            return
        if 'directory_effect' in intent:
            from backend.features.organization_directory import DirectoryEffect
            DirectoryEffect(self).validate(intent)
            return
        try:
            required = {'source', 'target', 'root', 'folder', 'volume_id', 'source_size', 'source_mtime_ns',
                        'database_fingerprint', 'volume_folder_before', 'effects', 'links_after', 'xml',
                        'xml_before', 'preview', 'policies', 'fingerprints', 'preconditions', 'inverse', 'database_before'}
            if not required.issubset(intent) or type(intent['inverse']) is not bool:
                raise ValueError()
            if any(type(intent[k]) is not int or intent[k] < 0 for k in ('source_size', 'source_mtime_ns')):
                raise ValueError()
            if not isinstance(intent['database_before'], dict) or set(intent['database_before']) != {'file', 'folder'}:
                raise ValueError()
            if intent['inverse'] and not {'restore_artifact', 'database_restore', 'created_directories'}.issubset(intent):
                raise ValueError()
            if intent['policies'] != [PLAN_POLICY, FOLDER_POLICY, RENAME_POLICY, MATCH_POLICY, MERGE_POLICY]:
                raise ValueError()
            if not isinstance(intent['volume_id'], int) or intent['volume_id'] <= 0:
                raise ValueError()
            allowed = {e.value for e in EffectKind} | ({RESTORE, CLEANUP} if intent['inverse'] else set())
            if not isinstance(intent['effects'], list) or len(intent['effects']) != len(set(intent['effects'])) or any(e not in allowed for e in intent['effects']):
                raise ValueError()
            if (intent['source'] != intent['target']) != (EffectKind.RELOCATE.value in intent['effects']):
                raise ValueError()
            for path in (intent['source'], intent['target'], intent['folder']):
                self._scope(path)
            if intent['xml'] is not None:
                xml = base64.b64decode(intent['xml'], validate=True)
                if len(xml) > MAX_XML:
                    raise ValueError()
                parse_comicinfo(xml)
        except (KeyError, TypeError, ValueError, ComicInfoError):
            raise OrganizationError(ExecutionCode.CORRUPT) from None

    def _validation(self, job: str) -> Optional[dict]:
        row = self.store.db.execute("SELECT detail FROM organization_events WHERE job_id=? AND event='validated' ORDER BY id DESC LIMIT 1", (job,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            observed = value['artifact']
            if observed.get('algorithm') == 'sha256/v1':
                from backend.features.organization_quarantine import (
                    VERSION, artifact_identity)
                intent = self.store.intent(job)
                if (intent.get('quarantine_effect') != VERSION
                        or set(observed) != {'algorithm', 'digest', 'size', 'stamp'}
                        or artifact_identity(observed) != artifact_identity(intent['hash'])
                        or value['artifact_digest'] != digest(canonical(observed))):
                    raise ValueError()
                return value
            if observed.get('version') == 'volume-tree/v1':
                if (observed != self.store.intent(job).get('tree_projection')
                        or value['artifact_digest'] != digest(canonical(observed))):
                    raise ValueError()
                return value
            if (set(observed) != {'device', 'inode', 'size', 'mtime_ns', 'sha256'}
                    or value['artifact_digest'] != digest(canonical(observed))):
                raise ValueError()
            return value
        except (KeyError, TypeError, ValueError):
            raise OrganizationError(ExecutionCode.CORRUPT, 'Invalid validation evidence') from None

    def _expected_database(self, job: OrganizationJob, intent: dict) -> dict:
        result = intent['database_before']
        for step in job.steps:
            if step.state == StepState.SUCCEEDED:
                evidence = json.loads(step.evidence)
                result = evidence.get('database_after', result)
        return result

    def _check_database(self, job: OrganizationJob, intent: dict) -> dict:
        if 'archive_effect' in intent:
            from backend.features.organization_archive import ArchiveEffect
            return ArchiveEffect(self).check_database(job, intent)
        if 'seed_copy_effect' in intent:
            from backend.features.organization_seed_copy import SeedCopyEffect
            return SeedCopyEffect(self).check_database(job, intent)
        if 'upgrade_effect' in intent:
            from backend.features.organization_upgrade import UpgradeEffect
            return UpgradeEffect(self).check_database(job, intent)
        if 'quarantine_effect' in intent:
            from backend.features.organization_quarantine import \
                QuarantineEffect
            return QuarantineEffect(self).check_database(job, intent)
        if 'directory_effect' in intent:
            from backend.features.organization_directory import DirectoryEffect
            DirectoryEffect(self).check_database(job, intent)
            return {}
        safe_path(intent['root'])
        if not Path(intent['root']).is_dir():
            raise OrganizationError(ExecutionCode.STALE, 'Configured library root unavailable')
        expected = self._expected_database(job, intent)
        fid = expected['file']['id'] if expected['file'] else None
        if self._database(intent, fid) != expected:
            raise OrganizationError(ExecutionCode.CONFLICT, 'Database differs from recorded state')
        self._metadata_guard(intent, expected['folder'])
        return expected

    def _expected_artifact(self, job: OrganizationJob, validation: dict) -> dict:
        result = validation['artifact']
        for step in job.steps:
            if step.state == StepState.SUCCEEDED:
                result = json.loads(step.evidence).get('artifact_after', result)
        return result

    def _current_path(self, job: OrganizationJob, intent: dict) -> str:
        if 'archive_effect' in intent:
            return intent['target'] if job.steps[1].state == StepState.SUCCEEDED else intent['source']
        if 'seed_copy_effect' in intent:
            return intent['target'] if job.steps[0].state == StepState.SUCCEEDED else intent['source']
        if 'upgrade_effect' in intent:
            return intent['target'] if job.steps[1].state == StepState.SUCCEEDED else intent['source']
        return intent['target'] if any(s.kind == EffectKind.RELOCATE.value and s.state == StepState.SUCCEEDED for s in job.steps) else intent['source']

    def _initial_validation(self, job: OrganizationJob, intent: dict, *, record: bool = True) -> dict:
        if 'archive_effect' in intent:
            from backend.features.organization_archive import ArchiveEffect
            return ArchiveEffect(self).initial(job, intent, record=record)
        if 'seed_copy_effect' in intent:
            from backend.features.organization_seed_copy import SeedCopyEffect
            return SeedCopyEffect(self).initial(job, intent, record=record)
        if 'upgrade_effect' in intent:
            from backend.features.organization_upgrade import UpgradeEffect
            return UpgradeEffect(self).initial(job, intent, record=record)
        if 'quarantine_effect' in intent:
            from backend.features.organization_quarantine import \
                QuarantineEffect
            return QuarantineEffect(self).initial(job, intent, record=record)
        if 'directory_effect' in intent:
            from backend.features.organization_directory import DirectoryEffect
            return DirectoryEffect(self).initial(job, intent, record=record)
        self._check_database(job, intent)
        if intent.get('rename_authority') is not None and not intent['inverse']:
            from backend.base.library_health import fingerprint
            from backend.implementations.maintenance_review import file_state
            if fingerprint(file_state(intent['source'])) != intent.get('rename_stamp'):
                raise OrganizationError(ExecutionCode.SOURCE)
        if EffectKind.RELOCATE.value in intent['effects'] and os.name != 'nt' and not sys.platform.startswith('linux'):
            raise OrganizationError(ExecutionCode.UNSUPPORTED, 'No verified exclusive rename primitive')
        current = artifact(intent['source'])
        if (current['size'], current['mtime_ns']) != (intent['source_size'], intent['source_mtime_ns']):
            raise OrganizationError(ExecutionCode.SOURCE)
        if intent['inverse'] and current != intent['restore_artifact']:
            raise OrganizationError(ExecutionCode.UNDO)
        if intent['source'] != intent['target'] and os.path.lexists(intent['target']):
            raise OrganizationError(ExecutionCode.OCCUPIED)
        parent = Path(intent['target']).parent
        while not parent.exists():
            parent = parent.parent
        safe_path(str(parent))
        if parent.stat().st_dev != current['device']:
            raise OrganizationError(ExecutionCode.UNSUPPORTED, 'Cross-filesystem move')
        if intent['xml'] is not None:
            inspection = inspect_comicinfo(intent['source'])
            old = sha256(inspection.document.raw_bytes).hexdigest() if inspection.document else None
            if inspection.state not in (InspectionState.PRESENT, InspectionState.ABSENT) or old != intent['xml_before']:
                raise OrganizationError(ExecutionCode.STALE, 'ComicInfo changed or archive not writable')
        value = dict(artifact=current, artifact_digest=digest(canonical(current)))
        if record:
            with self.store.transaction():
                self.store.event(job.id, 'validated', value)
        return value

    def _start(self, job: OrganizationJob, intent: dict, ordinal: int, validation: dict) -> dict:
        if 'archive_effect' in intent:
            from backend.features.organization_archive import ArchiveEffect
            return ArchiveEffect(self).start(job, intent, ordinal, validation)
        if 'seed_copy_effect' in intent:
            from backend.features.organization_seed_copy import SeedCopyEffect
            return SeedCopyEffect(self).start(job, intent, ordinal, validation)
        if 'upgrade_effect' in intent:
            from backend.features.organization_upgrade import UpgradeEffect
            return UpgradeEffect(self).start(job, intent, ordinal, validation)
        if 'quarantine_effect' in intent:
            from backend.features.organization_quarantine import \
                QuarantineEffect
            return QuarantineEffect(self).start(job, intent, ordinal, validation)
        if 'directory_effect' in intent:
            from backend.features.organization_directory import DirectoryEffect
            return DirectoryEffect(self).start(job, intent, ordinal, validation)
        step = job.steps[ordinal]
        value = dict(database_before=self._check_database(job, intent))
        expected = self._expected_artifact(job, validation)
        if not matches(self._current_path(job, intent), expected):
            raise OrganizationError(ExecutionCode.SOURCE)
        value['artifact_before'] = expected
        if step.kind == EffectKind.DIRECTORY.value:
            missing = []
            part = Path(intent['folder'])
            while not part.exists():
                self._scope(str(part))
                missing.append(str(part))
                part = part.parent
            value.update(missing=list(reversed(missing)), created=[])
        if step.kind == EffectKind.COMICINFO.value:
            value['temporary'] = str(Path(intent['target']).parent / ('.kapowarr-' + job.id + '.tmp'))
        with self.store.transaction():
            self.store.checkpoint(job.id, ordinal, StepState.STARTED, value)
        self.hook('after_started', job.id, ordinal)
        return value

    def _receipt(self, job: str, ordinal: int, value: dict, *, reconciled: bool = False) -> None:
        self.hook('before_receipt', job, ordinal)
        value['disposition'] = 'reconciled' if reconciled else 'executed'
        try:
            with self.store.transaction():
                self.store.checkpoint(job, ordinal, StepState.SUCCEEDED, value)
        except sqlite3.Error:
            raise OrganizationError(ExecutionCode.RECEIPT) from None
        self.hook('after_receipt', job, ordinal)

    def _reconcile_step(self, job: OrganizationJob, intent: dict, ordinal: int, value: dict) -> bool:
        if 'archive_effect' in intent:
            from backend.features.organization_archive import ArchiveEffect
            return ArchiveEffect(self).reconcile(job, intent, ordinal, value)
        if 'seed_copy_effect' in intent:
            from backend.features.organization_seed_copy import SeedCopyEffect
            return SeedCopyEffect(self).reconcile(job, intent, ordinal, value)
        if 'upgrade_effect' in intent:
            from backend.features.organization_upgrade import UpgradeEffect
            return UpgradeEffect(self).reconcile(job, intent, ordinal, value)
        if 'quarantine_effect' in intent:
            from backend.features.organization_quarantine import \
                QuarantineEffect
            return QuarantineEffect(self).reconcile(job, intent, ordinal, value)
        if 'directory_effect' in intent:
            from backend.features.organization_directory import DirectoryEffect
            return DirectoryEffect(self).reconcile(job, intent, ordinal, value)
        kind = job.steps[ordinal].kind
        if kind == EffectKind.RELOCATE.value:
            source, target = os.path.lexists(intent['source']), os.path.lexists(intent['target'])
            if not source and target and matches(intent['target'], value['artifact_before']):
                value['artifact_after'] = value['artifact_before']
                return True
            if source and not target and matches(intent['source'], value['artifact_before']):
                return False
            raise OrganizationError(ExecutionCode.CONFLICT, 'Relocation state not proven')
        if kind == EffectKind.COMICINFO.value:
            prepared = value.get('prepared')
            if prepared and matches(intent['target'], prepared):
                inspection = inspect_comicinfo(intent['target'])
                if inspection.document and inspection.document.raw_bytes == base64.b64decode(intent['xml']):
                    value['artifact_after'] = prepared
                    return True
            if matches(intent['target'], value['artifact_before']):
                return False
            raise OrganizationError(ExecutionCode.CONFLICT, 'Metadata replacement state not proven')
        if kind == EffectKind.DIRECTORY.value:
            safe_path(intent['folder'])
            return Path(intent['folder']).is_dir()
        if kind == CLEANUP:
            return False  # rmdir is conditional; missing directories are skipped.
        # DB mutation and receipt use one SQLite transaction. STARTED with old
        # DB state means it did not commit; anything else is external/conflicted.
        if self._database(intent, value['database_before']['file']['id'] if value['database_before']['file'] else None) != value['database_before']:
            raise OrganizationError(ExecutionCode.CONFLICT)
        return False

    def _database_effect(self, job: str, intent: dict, ordinal: int, kind: str, value: dict) -> None:
        with self.store.transaction():
            before = value['database_before']
            fid = before['file']['id'] if before['file'] else None
            if self._database(intent, fid) != before:
                raise OrganizationError(ExecutionCode.CONFLICT)
            self._metadata_guard(intent, before['folder'])
            db = self.store.db
            if kind == EffectKind.VOLUME_FOLDER.value:
                if before['folder']:
                    raise OrganizationError(ExecutionCode.STALE)
                db.execute('UPDATE volumes SET folder=? WHERE id=?', (intent['folder'], intent['volume_id']))
            elif kind == EffectKind.FILE_RECORD.value:
                size = os.stat(intent['target']).st_size
                if fid is None:
                    fid = db.execute('INSERT INTO files(filepath,size) VALUES(?,?)', (intent['target'], size)).lastrowid
                else:
                    db.execute('UPDATE files SET filepath=?,size=? WHERE id=?', (intent['target'], size, fid))
            elif kind == EffectKind.ASSOCIATIONS.value:
                if fid is None:
                    raise OrganizationError(ExecutionCode.CONFLICT)
                desired = intent['links_after']
                if any(link not in desired for link in before['file']['links']):
                    raise OrganizationError(ExecutionCode.NOT_AUTHORIZED)
                for vid, iid, forced in desired:
                    if [vid, iid, forced] not in before['file']['links']:
                        parent = db.execute('SELECT volume_id FROM issues WHERE id=?', (iid,)).fetchone()
                        if parent is None or parent[0] != vid or vid != intent['volume_id']:
                            raise OrganizationError(ExecutionCode.STALE)
                        db.execute('INSERT INTO issues_files(file_id,issue_id,forced) VALUES(?,?,?)', (fid, iid, forced))
            elif kind == RESTORE:
                restored = intent['database_restore']
                if restored['file'] is None:
                    db.execute('DELETE FROM files WHERE id=?', (fid,))
                    fid = None
                else:
                    old = restored['file']
                    if fid != old['id']:
                        raise OrganizationError(ExecutionCode.UNDO)
                    db.execute('UPDATE files SET filepath=?,size=? WHERE id=?', (old['path'], old['size'], fid))
                    db.execute('DELETE FROM issues_files WHERE file_id=?', (fid,))
                    db.executemany('INSERT INTO issues_files(file_id,issue_id,forced) VALUES(?,?,?)', ((fid, i, b) for _, i, b in old['links']))
                db.execute('UPDATE volumes SET folder=? WHERE id=?', (restored['folder'], intent['volume_id']))
            else:
                raise OrganizationError(ExecutionCode.UNSUPPORTED)
            value['database_after'] = self._database(intent, fid)
            after = value['database_after']
            if kind == EffectKind.FILE_RECORD.value and (
                after['file'] is None or after['file']['path'] != intent['target'] or after['file']['size'] != size
            ):
                raise OrganizationError(ExecutionCode.CONFLICT, 'File record write did not reach intended state')
            if kind == EffectKind.ASSOCIATIONS.value and after['file']['links'] != intent['links_after']:
                raise OrganizationError(ExecutionCode.CONFLICT, 'Association write did not reach intended state')
            if kind == EffectKind.VOLUME_FOLDER.value and after['folder'] != intent['folder']:
                raise OrganizationError(ExecutionCode.CONFLICT, 'Folder write did not reach intended state')
            if kind == RESTORE and after != intent['database_restore']:
                raise OrganizationError(ExecutionCode.CONFLICT, 'Database restoration did not reach intended state')
            self.hook('before_db_commit', job, ordinal)
            value['disposition'] = 'executed'
            self.store.checkpoint(job, ordinal, StepState.SUCCEEDED, value)
        self.hook('after_db_commit', job, ordinal)

    def _execute_step(self, job: OrganizationJob, intent: dict, ordinal: int, value: dict) -> None:
        if 'archive_effect' in intent:
            from backend.features.organization_archive import ArchiveEffect
            ArchiveEffect(self).execute(job, intent, ordinal, value)
            return
        if 'seed_copy_effect' in intent:
            from backend.features.organization_seed_copy import SeedCopyEffect
            SeedCopyEffect(self).execute(job, intent, ordinal, value)
            return
        if 'upgrade_effect' in intent:
            from backend.features.organization_upgrade import UpgradeEffect
            UpgradeEffect(self).execute(job, intent, ordinal, value)
            return
        if 'quarantine_effect' in intent:
            from backend.features.organization_quarantine import \
                QuarantineEffect
            QuarantineEffect(self).execute(job, intent, ordinal, value)
            return
        if 'directory_effect' in intent:
            from backend.features.organization_directory import DirectoryEffect
            DirectoryEffect(self).execute(job, intent, ordinal, value)
            return
        kind = job.steps[ordinal].kind
        self.hook('before_effect', job.id, ordinal)
        for path in (intent['source'], intent['target'], intent['folder']):
            self._scope(path)
        if not matches(self._current_path(job, intent), value['artifact_before']):
            raise OrganizationError(ExecutionCode.SOURCE)
        if kind == EffectKind.DIRECTORY.value:
            for path in value['missing']:
                self._scope(path)
                if not os.path.lexists(path):
                    os.mkdir(path)
                    info = os.stat(path)
                    value['created'].append([path, info.st_dev, info.st_ino])
                    sync_directory(os.path.dirname(path))
                    with self.store.transaction():
                        self.store.checkpoint(job.id, ordinal, StepState.STARTED, value)
                elif not Path(path).is_dir():
                    raise OrganizationError(ExecutionCode.CONFLICT)
        elif kind == EffectKind.RELOCATE.value:
            # A preflight check alone permits provider-switch ABA between check
            # and rename. Serialize the authority-dependent effect with DB writers.
            @contextmanager
            def rename_guard():
                if intent.get('rename_authority') is None:
                    yield
                else:
                    with self.store.transaction():
                        self._check_database(job, intent)
                        if not intent['inverse']:
                            from backend.base.library_health import fingerprint
                            from backend.implementations.maintenance_review import \
                                file_state
                            if fingerprint(file_state(intent['source'])) != intent.get('rename_stamp'):
                                raise OrganizationError(ExecutionCode.SOURCE)
                        yield

            with rename_guard():
                if not matches(intent['source'], value['artifact_before']):
                    raise OrganizationError(ExecutionCode.SOURCE)
                rename_no_replace(intent['source'], intent['target'])
            value['artifact_after'] = artifact(intent['target'])
            if value['artifact_after'] != value['artifact_before']:
                raise OrganizationError(ExecutionCode.CONFLICT)
        elif kind == EffectKind.COMICINFO.value:
            temporary = value['temporary']
            self._scope(temporary)
            if os.path.lexists(temporary):
                info = os.lstat(temporary)
                if (value.get('allocated') != [info.st_dev, info.st_ino]
                        or not value.get('prepared') or not matches(temporary, value['prepared'])):
                    raise OrganizationError(ExecutionCode.CONFLICT, 'Unproven temporary artifact')
                os.unlink(temporary)
            inspection = inspect_comicinfo(intent['target'])
            before_digest = sha256(inspection.document.raw_bytes).hexdigest() if inspection.document else None
            if not matches(intent['target'], value['artifact_before']) or before_digest != intent['xml_before']:
                raise OrganizationError(ExecutionCode.STALE)

            def checkpoint(stage: str, path: str) -> None:
                if stage == 'allocated':
                    info = os.lstat(path)
                    value['allocated'] = [info.st_dev, info.st_ino]
                else:
                    value['prepared'] = artifact(path)
                with self.store.transaction():
                    self.store.checkpoint(job.id, ordinal, StepState.STARTED, value)

            @contextmanager
            def replacement_guard():
                # Prepared-artifact evidence has already committed. Serialize
                # only the final metadata/authority check and path replacement,
                # not archive copying or CRC validation. Normal journal recovery
                # owns any failure after the replacement.
                with self.store.transaction():
                    self._check_database(self.store.get(job.id), intent)
                    yield

            written = write_comicinfo(inspection, base64.b64decode(intent['xml']), temporary_path=temporary,
                checkpoint=checkpoint, replacement_guard=replacement_guard if intent.get('repair_authority') else None)
            if not written.document or written.document.raw_bytes != base64.b64decode(intent['xml']):
                raise OrganizationError(ExecutionCode.METADATA)
            value['artifact_after'] = artifact(intent['target'])
            sync_directory(intent['folder'])
        elif kind == CLEANUP:
            value['left_in_place'] = []
            for path, device, inode in reversed(intent['created_directories']):
                self._scope(path)
                if not os.path.lexists(path):
                    continue
                info = os.stat(path)
                roots = [r[0] for r in self.store.db.execute('SELECT folder FROM root_folders')]
                if (info.st_dev, info.st_ino) != (device, inode) or any(_key(path) == _key(r) for r in roots):
                    value['left_in_place'].append(path)
                    continue
                try:
                    os.rmdir(path)
                except OSError as error:
                    if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        raise
                    value['left_in_place'].append(path)
        else:
            self._database_effect(job.id, intent, ordinal, kind, value)
            return
        self.hook('after_effect', job.id, ordinal)
        self._receipt(job.id, ordinal, value)

    def _finish(self, job: OrganizationJob, intent: dict, validation: dict) -> None:
        if 'archive_effect' in intent:
            from backend.features.organization_archive import ArchiveEffect
            ArchiveEffect(self).finish(job, intent, validation)
            return
        if 'seed_copy_effect' in intent:
            from backend.features.organization_seed_copy import SeedCopyEffect
            SeedCopyEffect(self).finish(job, intent, validation)
            return
        if 'upgrade_effect' in intent:
            from backend.features.organization_upgrade import UpgradeEffect
            UpgradeEffect(self).finish(job, intent, validation)
            return
        if 'quarantine_effect' in intent:
            from backend.features.organization_quarantine import \
                QuarantineEffect
            QuarantineEffect(self).finish(job, intent, validation)
            return
        if 'directory_effect' in intent:
            from backend.features.organization_directory import DirectoryEffect
            DirectoryEffect(self).finish(job, intent, validation)
            return
        self._check_database(job, intent)
        if (not matches(intent['target'], self._expected_artifact(job, validation))
                or intent['source'] != intent['target'] and os.path.lexists(intent['source'])):
            raise OrganizationError(ExecutionCode.CONFLICT, 'Final filesystem predicates failed')
        if intent['xml'] is not None:
            inspection = inspect_comicinfo(intent['target'])
            if not inspection.document or inspection.document.raw_bytes != base64.b64decode(intent['xml']):
                raise OrganizationError(ExecutionCode.CONFLICT)
        from backend.features.quality import record_import
        record_import(self, job, intent)
        with self.store.transaction():
            self.store.state(job.id, JobState.COMPLETED)
            self.store.db.execute('DELETE FROM organization_reservations WHERE job_id=?', (job.id,))
            self.store.db.execute('UPDATE organization_jobs SET claim=NULL WHERE id=?', (job.id,))

    def apply_job(self, job_id: str, *, approved_recovery: Optional[str] = None,
                  automatic_archive_recovery: bool = False, cancelled=lambda: False) -> OrganizationJob:
        with execution_gate(self.store.path), cancellation_scope(cancelled):
            job = self.store.get(job_id)
            if cancelled():
                return job
            intent = self.store.intent(job_id)
            if automatic_archive_recovery:
                from backend.features.organization_archive import ArchiveEffect
                if job.state == JobState.COMPLETED:
                    return job
                # Both proof and execution hold the same cross-process gate.
                # The durable claim is reclaimed only after the OS proves the
                # previous worker no longer owns it.
                ArchiveEffect(self).recovery_admission(job, intent)
                preview = self._preview_recovery(job_id)
                if not preview['eligible']:
                    raise OrganizationError(ExecutionCode.CONFLICT)
                with self.store.transaction():
                    self.store.event(job_id, 'archive_automatic_recovery',
                                     dict(digest=preview['digest']))
            if approved_recovery is not None:
                # Exact replay is journal correlation, not permission to retry a
                # newly failed effect. A new attempt needs a fresh preview.
                previous = self.store.db.execute('''SELECT 1 FROM organization_events
                    WHERE job_id=? AND event='maintenance_recovery_confirmed'
                    AND json_extract(detail,'$.digest')=? LIMIT 1''', (job_id, approved_recovery)).fetchone()
                if previous:
                    return job
                preview = self._preview_recovery(job_id)
                if not preview['eligible'] or preview['digest'] != approved_recovery:
                    raise OrganizationError(ExecutionCode.STALE)
                with self.store.transaction():
                    self.store.event(job_id, 'maintenance_recovery_confirmed',
                                     dict(version='recovery-review/v1', digest=approved_recovery))
            self._validate_intent(intent)
            if job.state == JobState.COMPLETED:
                return job
            if job.state == JobState.FAILED and job.error in {
                ExecutionCode.STALE.value, ExecutionCode.SOURCE.value, ExecutionCode.OCCUPIED.value,
                ExecutionCode.UNSUPPORTED.value, ExecutionCode.UNSAFE_PATH.value
            }:
                return job  # Terminal preflight rejection requires a new preview.
            with self.store.transaction():
                for path in set((_key(intent['source']), _key(intent['target']))):
                    owner = self.store.db.execute('SELECT job_id FROM organization_reservations WHERE path_key=?', (path,)).fetchone()
                    if owner is None or owner[0] != job_id:
                        raise OrganizationError(ExecutionCode.BUSY)
                self.store.db.execute('UPDATE organization_jobs SET claim=? WHERE id=?', (uuid4().hex, job_id))
                self.store.state(job_id, JobState.RUNNING)
            LOGGER.info('Organization job %s started', job_id)
            current_ordinal = None
            try:
                validation = self._validation(job_id) or self._initial_validation(job, intent)
                for ordinal in range(len(job.steps)):
                    if cancelled():
                        raise OrganizationError(ExecutionCode.CANCELLED)
                    current_ordinal = ordinal
                    job = self.store.get(job_id)
                    self._check_database(job, intent)
                    step = job.steps[ordinal]
                    if step.state == StepState.SUCCEEDED:
                        continue
                    value = json.loads(step.evidence) if step.state == StepState.STARTED else self._start(job, intent, ordinal, validation)
                    if self._reconcile_step(job, intent, ordinal, value):
                        self._receipt(job_id, ordinal, value, reconciled=True)
                    else:
                        self._execute_step(job, intent, ordinal, value)
                self._finish(self.store.get(job_id), intent, validation)
            except (OrganizationError, OSError, sqlite3.Error, ComicInfoError) as error:
                code = self._error_code(error)
                state = JobState.RECOVERY if 'archive_effect' in intent or any(s.state != StepState.PENDING for s in self.store.get(job_id).steps) else JobState.FAILED
                with self.store.transaction():
                    self.store.state(job_id, state, code)
                    self.store.event(job_id, 'failure', dict(code=code.value, reason=error.detail if isinstance(error, OrganizationError) else ''), current_ordinal)
                    self.store.db.execute('UPDATE organization_jobs SET claim=NULL WHERE id=?', (job_id,))
                    if state == JobState.FAILED and code in {
                        ExecutionCode.STALE, ExecutionCode.SOURCE, ExecutionCode.OCCUPIED,
                        ExecutionCode.UNSUPPORTED, ExecutionCode.UNSAFE_PATH
                    }:
                        self.store.db.execute('DELETE FROM organization_reservations WHERE job_id=?', (job_id,))
                LOGGER.warning('Organization job %s stopped: %s', job_id, code.value)
                return self.store.get(job_id)
            LOGGER.info('Organization job %s completed after reconciliation', job_id)
            return self.store.get(job_id)

    def preview_recovery(self, job_id: str) -> dict:
        # A persisted RUNNING state may be a dead process, not a live worker.
        # Taking the nonblocking OS gate distinguishes those cases without a
        # force/retry bypass. Opening ordinary history never takes this gate.
        with execution_gate(self.store.path):
            return self._preview_recovery(job_id)

    def _preview_recovery(self, job_id: str) -> dict:
        """Read-only domain inspection; never start/checkpoint/finish an effect.

        Uses the very same initial/reconciliation predicates as execution. It
        can hash/enumerate one requested artifact, unlike overview history.
        Confirmation re-runs this under the existing execution gate. SQLite
        query_only also detects accidental introduction of a writing helper.
        """
        if self.store.db.in_transaction:
            raise OrganizationError(ExecutionCode.DATABASE)
        prior_query_only = self.store.db.execute('PRAGMA query_only').fetchone()[0]
        self.store.db.execute('PRAGMA query_only=ON')
        self.store.db.execute('BEGIN')
        reasons, observations, proposals = [], {}, []
        try:
            job, intent = self.store.get(job_id), self.store.intent(job_id)
            identity = digest(canonical(intent))
            if job.state == JobState.COMPLETED:
                reasons.append('already_completed')
            if job.state == JobState.FAILED and job.error in {
                ExecutionCode.STALE.value, ExecutionCode.SOURCE.value, ExecutionCode.OCCUPIED.value,
                ExecutionCode.UNSUPPORTED.value, ExecutionCode.UNSAFE_PATH.value
            }:
                reasons.append('fresh_forward_review_required')
            if not reasons:
                try:
                    self._validate_intent(intent)
                    if 'archive_effect' in intent:
                        from backend.features.organization_archive import \
                            ArchiveEffect
                        ArchiveEffect(self).recovery_admission(job, intent)
                    from backend.internals.organization_reservations import \
                        load_reservations
                    reservations = load_reservations(self.store.db.cursor())
                    for path in (intent['source'], intent['target']):
                        owner = self.store.db.execute('SELECT job_id FROM organization_reservations WHERE path_key=?',
                                                      (_key(path),)).fetchone()
                        if owner is None or owner[0] != job_id or reservations.conflicts(path, exclude=job_id):
                            raise OrganizationError(ExecutionCode.BUSY)
                    self._check_database(job, intent)
                    validation = self._validation(job_id)
                    if validation is None:
                        validation = self._initial_validation(job, intent, record=False)
                    simulated = job
                    for step in job.steps:
                        if step.state == StepState.SUCCEEDED:
                            continue
                        reconciled = False
                        if step.state == StepState.STARTED:
                            value = json.loads(step.evidence)
                            reconciled = self._reconcile_step(job, intent, step.ordinal, value)
                            if reconciled:
                                steps = list(simulated.steps)
                                steps[step.ordinal] = replace(step, state=StepState.SUCCEEDED, evidence=canonical(value))
                                simulated = replace(simulated, steps=tuple(steps))
                        proposals.append(dict(ordinal=step.ordinal, effect=step.kind,
                                              action='acknowledge_recorded_effect' if reconciled else 'resume_recorded_effect'))
                    observations = dict(source_exists=os.path.lexists(intent['source']),
                                        target_exists=os.path.lexists(intent['target']))
                    if 'directory_effect' in intent:
                        from backend.features.organization_directory import \
                            DirectoryEffect
                        location = intent['target'] if simulated.steps[0].state == StepState.SUCCEEDED else intent['source']
                        if DirectoryEffect(self).observe(intent, location) != intent['tree_projection']:
                            raise OrganizationError(ExecutionCode.CONFLICT)
                    elif 'quarantine_effect' in intent:
                        from backend.features.organization_quarantine import \
                            QuarantineEffect
                        handler = QuarantineEffect(self)
                        handler.paths(intent)
                        moved = simulated.steps[0].state == StepState.SUCCEEDED
                        handler.observe(intent['target'] if moved else intent['source'], intent['hash'],
                                        moved=moved or intent['inverse'])
                    elif not matches(self._current_path(simulated, intent), self._expected_artifact(simulated, validation)):
                        raise OrganizationError(ExecutionCode.SOURCE)
                    if ('archive_effect' not in intent and 'upgrade_effect' not in intent and 'seed_copy_effect' not in intent and intent['source'] != intent['target'] and observations['source_exists']
                            and observations['target_exists']):
                        raise OrganizationError(ExecutionCode.CONFLICT)
                    observations['recorded_artifact_matches'] = True
                    observations['recorded_database_matches'] = True
                except (OrganizationError, OSError, sqlite3.Error, ComicInfoError) as error:
                    reasons.append(self._error_code(error).value)
            value = dict(version='recovery-review/v1', job_id=job_id, state=job.state.value,
                         intent_digest=identity, eligible=not reasons, reasons=reasons,
                         observations=observations, steps=proposals,
                         journal_position=self.store.db.execute(
                             'SELECT MAX(id) FROM organization_events WHERE job_id=?', (job_id,)).fetchone()[0],
                         recorded_steps=[dict(ordinal=s.ordinal, kind=s.kind, state=s.state.value,
                                              evidence_digest=digest(s.evidence)) for s in job.steps])
            db_effects = {EffectKind.FILE_RECORD.value, EffectKind.ASSOCIATIONS.value,
                          'reconcile_verified_upgrade/v1',
                          'reconcile_archive_ownership/v1',
                          EffectKind.VOLUME_FOLDER.value, RESTORE,
                          'reconcile_volume_tree/v1', 'reconcile_quarantine/v1', 'reconcile_restored_file/v1'}
            value['filesystem_mutation_required'] = any(p['action'] == 'resume_recorded_effect'
                and p['effect'] not in db_effects for p in proposals)
            value['database_reconciliation_required'] = any(p['action'] == 'resume_recorded_effect'
                and p['effect'] in db_effects for p in proposals)
            value['digest'] = digest(canonical(value))
            return value
        finally:
            self.store.db.rollback()
            self.store.db.execute('PRAGMA query_only=' + ('ON' if prior_query_only else 'OFF'))

    @staticmethod
    def _error_code(error: BaseException) -> ExecutionCode:
        if isinstance(error, OrganizationError):
            return error.code
        if isinstance(error, ComicInfoError):
            if error.os_error == errno.ENOSPC:
                return ExecutionCode.DISK_FULL
            if error.os_error in (errno.EACCES, errno.EPERM):
                return ExecutionCode.PERMISSION
            return ExecutionCode.METADATA
        if isinstance(error, sqlite3.Error):
            return ExecutionCode.DATABASE
        if isinstance(error, FileNotFoundError):
            return ExecutionCode.SOURCE
        if isinstance(error, FileExistsError):
            return ExecutionCode.OCCUPIED
        if isinstance(error, PermissionError):
            return ExecutionCode.PERMISSION
        if isinstance(error, OSError) and error.errno == errno.ENOSPC:
            return ExecutionCode.DISK_FULL
        return ExecutionCode.IO

    def reconcile_job(self, job_id: str) -> OrganizationJob:
        """Explicit restart inspection: journal writes, no library mutation."""
        with execution_gate(self.store.path):
            job, intent = self.store.get(job_id), self.store.intent(job_id)
            self._validate_intent(intent)
            if job.state == JobState.COMPLETED:
                return job
            if job.state == JobState.FAILED and job.error in {
                ExecutionCode.STALE.value, ExecutionCode.SOURCE.value, ExecutionCode.OCCUPIED.value,
                ExecutionCode.UNSUPPORTED.value, ExecutionCode.UNSAFE_PATH.value
            }:
                return job
            try:
                self._check_database(job, intent)
                for step in job.steps:
                    if step.state == StepState.STARTED:
                        value = json.loads(step.evidence)
                        if self._reconcile_step(job, intent, step.ordinal, value):
                            self._receipt(job_id, step.ordinal, value, reconciled=True)
                job = self.store.get(job_id)
                validation = self._validation(job_id)
                if validation and all(s.state == StepState.SUCCEEDED for s in job.steps):
                    self._finish(job, intent, validation)
                else:
                    with self.store.transaction():
                        self.store.state(job_id, JobState.RECOVERY if validation else JobState.PENDING)
                        self.store.db.execute('UPDATE organization_jobs SET claim=NULL WHERE id=?', (job_id,))
            except (OrganizationError, OSError, sqlite3.Error, ComicInfoError) as error:
                with self.store.transaction():
                    self.store.state(job_id, JobState.RECOVERY, self._error_code(error))
            return self.store.get(job_id)

    def apply_many(self, jobs: tuple[str, ...]) -> tuple[OrganizationJob, ...]:
        """Independent jobs continue; systemic/corrupt store exceptions stop."""
        return tuple(self.apply_job(job) for job in jobs)

    def inspect_incomplete(self) -> tuple[OrganizationJob, ...]:
        """Explicit restart scan; never resumes filesystem or library DB effects."""
        identities = tuple(r[0] for r in self.store.db.execute(
            'SELECT id FROM organization_jobs WHERE state<>? ORDER BY created_at,id', (JobState.COMPLETED.value,)))
        return tuple(self.reconcile_job(identity) for identity in identities)

    def preview_undo(self, job_id: str) -> UndoPreview:
        job, intent = self.store.get(job_id), self.store.intent(job_id)
        if 'archive_effect' in intent:
            return UndoPreview(job_id, False, intent['target'], intent['original'], ('archive_bytes_retired_no_inverse',))
        if 'seed_copy_effect' in intent:
            return UndoPreview(job_id, False, intent['target'], intent['source'], ('seed_payload_retained_no_inverse',))
        if 'upgrade_effect' in intent:
            return UndoPreview(job_id, False, intent['target'], intent['source'], ('replacement_bytes_retired_no_inverse',))
        if 'quarantine_effect' in intent:
            from backend.features.organization_quarantine import \
                QuarantineEffect
            return QuarantineEffect(self).preview_undo(job, intent)
        if 'directory_effect' in intent:
            from backend.features.organization_directory import DirectoryEffect
            return DirectoryEffect(self).preview_undo(job, intent)
        reasons = []
        if job.state != JobState.COMPLETED or intent['inverse']:
            reasons.append('completed_forward_job_required')
        if intent['xml'] is not None:
            reasons.append('lossless_metadata_undo_not_supported')
        if not intent['effects']:
            reasons.append('no_changes_to_reverse')
        if not Path(intent['source']).parent.is_dir():
            reasons.append('restore_parent_unavailable')
        if self.store.db.execute('SELECT 1 FROM organization_jobs WHERE inverse_of=?', (job_id,)).fetchone():
            reasons.append('inverse_job_already_exists')
        if not reasons:
            try:
                validation = self._validation(job_id)
                assert validation is not None
                self._check_database(job, intent)
                expected = self._expected_artifact(job, validation)
                if not matches(intent['target'], expected) or intent['source'] != intent['target'] and os.path.lexists(intent['source']):
                    reasons.append('artifact_changed_or_restore_path_occupied')
            except (OrganizationError, OSError):
                reasons.append('recorded_state_changed')
        inverse = self._inverse(job, intent) if not reasons else None
        return UndoPreview(job_id, not reasons, intent['target'], intent['source'], tuple(reasons),
                           digest(canonical(inverse)) if inverse else None,
                           canonical(inverse['database_before']) if inverse else None,
                           canonical(inverse['database_restore']) if inverse else None,
                           tuple(p for p, _, _ in inverse['created_directories']) if inverse else ())

    def _inverse(self, job: OrganizationJob, intent: dict) -> dict:
        if 'quarantine_effect' in intent:
            from backend.features.organization_quarantine import \
                QuarantineEffect
            return QuarantineEffect(self).inverse(job, intent)
        if 'directory_effect' in intent:
            from backend.features.organization_directory import DirectoryEffect
            return DirectoryEffect(self).inverse(job, intent)
        validation = self._validation(job.id)
        assert validation is not None
        value = dict(intent)
        expected = self._expected_artifact(job, validation)
        created = [entry for step in job.steps for entry in json.loads(step.evidence).get('created', [])]
        effects = ([EffectKind.RELOCATE.value] if intent['source'] != intent['target'] else [])
        if any(s.kind in (EffectKind.FILE_RECORD.value, EffectKind.ASSOCIATIONS.value, EffectKind.VOLUME_FOLDER.value) for s in job.steps):
            effects.append(RESTORE)
        if created:
            effects.append(CLEANUP)
        value.update(source=intent['target'], target=intent['source'], folder=os.path.dirname(intent['source']),
                     effects=effects, inverse=True, source_size=expected['size'], source_mtime_ns=expected['mtime_ns'],
                     restore_artifact=expected, database_before=self._expected_database(job, intent),
                     database_restore=intent['database_before'], created_directories=created)
        return value

    def create_undo_job(self, job_id: str, approved_digest: str) -> str:
        with execution_gate(self.store.path):
            self.store.intent(job_id)
            existing = self.store.db.execute('SELECT id,plan_digest FROM organization_jobs WHERE inverse_of=?', (job_id,)).fetchone()
            if existing:
                self.store.intent(existing[0])
                if existing[1] != 'undo:' + approved_digest:
                    raise OrganizationError(ExecutionCode.UNDO)
                return str(existing[0])
            preview = self.preview_undo(job_id)
            if not preview.eligible or preview.intent_digest != approved_digest:
                raise OrganizationError(ExecutionCode.UNDO)
            job, intent = self.store.get(job_id), self.store.intent(job_id)
            inverse = self._inverse(job, intent)
            return self.store.create(inverse, 'undo:' + approved_digest, (_key(inverse['source']), _key(inverse['target'])), inverse_of=job_id)

    def history(self) -> tuple[dict, ...]:
        """Safe transport: no XML, raw receipt payload or exception internals."""
        result = []
        for identity in self.store.history():
            try:
                job = self.store.get(identity)
                result.append(dict(id=job.id, state=job.state.value, source=job.source, target=job.target,
                    created_at=job.created_at, updated_at=job.updated_at, error=job.error, inverse_of=job.inverse_of,
                    steps=[dict(ordinal=s.ordinal, kind=s.kind, state=s.state.value) for s in job.steps],
                    undo=self.preview_undo(identity).eligible))
            except OrganizationError:
                result.append(dict(id=identity, state='unsupported_history', error=ExecutionCode.CORRUPT.value))
        return tuple(result)

    def inspect_job(self, job_id: str) -> dict:
        """Detailed operator recovery evidence, without archive/XML payloads."""
        job, intent = self.store.get(job_id), self.store.intent(job_id)
        if 'archive_effect' in intent:
            return dict(id=job.id, state=job.state.value, source=intent['original'], target=job.target,
                operation='archive_normalization', file_id=intent['file_id'], receipt=intent['receipt'],
                shared_source=intent['sharing']['shared'], error=job.error, inverse_of=job.inverse_of,
                steps=[dict(ordinal=s.ordinal, kind=s.kind, state=s.state.value) for s in job.steps])
        if 'seed_copy_effect' in intent:
            return dict(id=job.id, state=job.state.value, source=job.source, target=job.target,
                operation='preserve_seed_payload', error=job.error, inverse_of=job.inverse_of,
                steps=[dict(ordinal=s.ordinal, kind=s.kind, state=s.state.value) for s in job.steps])
        if 'upgrade_effect' in intent:
            return dict(id=job.id,state=job.state.value,source=job.source,target=job.target,
                operation='verified_quality_upgrade',file_id=intent['file_id'],issue_id=intent['issue_id'],
                acquisition_id=intent['provenance_id'],steps=[dict(ordinal=s.ordinal,kind=s.kind,state=s.state.value) for s in job.steps],
                error=job.error,inverse_of=job.inverse_of)
        if 'quarantine_effect' in intent:
            from backend.features.organization_quarantine import \
                QuarantineEffect
            return QuarantineEffect(self).inspect(job, intent)
        if 'directory_effect' in intent:
            from backend.features.organization_directory import DirectoryEffect
            return DirectoryEffect(self).inspect(job, intent)
        events = [dict(ordinal=r[0], at=r[1], event=r[2], detail=json.loads(r[3])) for r in self.store.db.execute(
            'SELECT ordinal,created_at,event,detail FROM organization_events WHERE job_id=? ORDER BY id', (job_id,))]
        observations = {}
        for key in ('source', 'target'):
            try:
                self._scope(intent[key])
                observations[key] = artifact(intent[key]) if os.path.lexists(intent[key]) else None
            except (OrganizationError, OSError) as error:
                observations[key] = {'unavailable': self._error_code(error).value}
        expected = self._expected_database(job, intent)
        try:
            observations['database'] = self._database(intent, expected['file']['id'] if expected['file'] else None)
        except OrganizationError as error:
            observations['database'] = {'unavailable': error.code.value}
        return dict(id=job.id, state=job.state.value, source=job.source, target=job.target,
                    plan_digest=job.plan_digest, executor_policy=job.executor_policy,
                    policies=intent['policies'], fingerprints=intent['fingerprints'],
                    preconditions=intent['preconditions'], preview=intent['preview'],
                    steps=[dict(ordinal=s.ordinal, kind=s.kind, state=s.state.value,
                                evidence=json.loads(s.evidence)) for s in job.steps], events=events,
                    observations=observations, error=job.error, inverse_of=job.inverse_of)
