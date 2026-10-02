"""Container-only replacement through the existing OrganizationJob journal."""
import json
import os
from itertools import islice
from pathlib import Path
from uuid import uuid4

from backend.base.organization_job import (EXECUTOR_POLICY, ExecutionCode,
                                           JobState, OrganizationError,
                                           StepState)
from backend.implementations.archive_normalization import (ArchiveFailure,
                                                           normalize)
from backend.implementations.comicinfo_archive import _stamp
from backend.implementations.organization_filesystem import (artifact,
                                                             execution_gate,
                                                             matches,
                                                             rename_no_replace,
                                                             sync_directory)
from backend.internals.organization_jobs import canonical, digest
from backend.internals.quality import QualityStore

VERSION = 'archive-normalization/v1'
EFFECTS = ['preserve_archive_original/v1', 'publish_verified_archive/v1',
           'reconcile_archive_ownership/v1', 'retire_archive_original/v1']


def maintained_hash(db, fid, previous):
    from backend.internals.organization_jobs import read_intent
    rows = db.execute('''SELECT id FROM organization_jobs WHERE state='completed'
        AND json_extract(intent,'$.archive_effect')=? AND json_extract(intent,'$.file_id')=?
        ORDER BY created_at,id LIMIT 101''', (VERSION, fid)).fetchall()
    if len(rows)>100:
        return previous  # Bounded history cannot authorize a guessed transition.
    for row in rows:
        value = read_intent(db, row[0])
        if value['old']['sha256'] == previous and value['receipt']['pages_preserved']:
            previous = value['incoming']['sha256']
    return previous


def ownership(db, fid):
    row = db.execute('SELECT id,filepath,size FROM active_files WHERE id=?', (fid,)).fetchone()
    if row is None:
        raise ArchiveFailure('file_unavailable')
    direct = [tuple(r) for r in db.execute('SELECT issue_id FROM issues_files WHERE file_id=? ORDER BY issue_id', (fid,))]
    general = [tuple(r) for r in db.execute('SELECT volume_id,file_type FROM volume_files WHERE file_id=? ORDER BY volume_id,file_type', (fid,))]
    volumes = [tuple(r) for r in db.execute('''SELECT id,folder,root_folder FROM volumes WHERE id IN
        (SELECT volume_id FROM issues WHERE id IN (SELECT issue_id FROM issues_files WHERE file_id=?)
         UNION SELECT volume_id FROM volume_files WHERE file_id=?) ORDER BY id''', (fid, fid))]
    if len(volumes) != 1 or Path(volumes[0][1]) not in Path(row['filepath']).parents:
        raise ArchiveFailure('ownership_review_required')
    return dict(file=dict(row), direct=direct, general=general, volumes=volumes)


def sharing(db, fid, path):
    current = os.stat(path, follow_symlinks=False)
    rows = db.execute('''SELECT DISTINCT s.source_path,s.source_identity,s.import_method
        FROM acquisition_seed_artifacts s JOIN acquisition_artifacts a ON a.intake_id=s.intake_id
        WHERE a.final_file_id=? AND a.state='organized' ORDER BY s.source_path LIMIT 1001''', (fid,)).fetchall()
    if len(rows) > 1000:
        raise ArchiveFailure('sharing_bound')
    sources = []
    for row in rows:
        # Packs may have several source members. Match actual filesystem identity,
        # not a stale flag or filename. Missing seeds are not recreated or deleted.
        if os.path.exists(row['source_path']) and os.path.samefile(path, row['source_path']):
            observed = artifact(row['source_path'])
            recorded = json.loads(row['source_identity'])
            if observed != recorded:
                raise ArchiveFailure('seed_source_changed')
            sources.append(dict(path=row['source_path'], identity=observed))
    return dict(links=current.st_nlink, shared=current.st_nlink > 1, seeds=sources)


def review(executor, fid):
    state = ownership(executor.store.db, fid)
    path = state['file']['filepath']
    executor._scope(path)
    _stamp(path)
    old = artifact(path)
    shares = sharing(executor.store.db, fid, path)
    value = dict(ownership=state, old=old, sharing=shares)
    return value, digest(canonical(value))


def sibling_names(path):
    with os.scandir(Path(path).parent) as entries:
        names = [entry.name for entry in islice(entries, 10001)]
    if len(names)>10000:
        raise ArchiveFailure('directory_bound')
    return names


def target_registered(db, fid, target):
    def compare(first, second):
        first, second = os.path.normpath(first).casefold(), os.path.normpath(second).casefold()
        return (first > second) - (first < second)
    db.create_collation('archive_paths', compare)
    return db.execute('SELECT 1 FROM files WHERE id<>? AND filepath=? COLLATE archive_paths LIMIT 1',
                      (fid, target)).fetchone() is not None


def register_archive(executor, fid, confirmation, batch_id, *, cancelled=lambda: False):
    existing = executor.store.db.execute('SELECT id,intent FROM organization_jobs WHERE batch_id=?', (batch_id,)).fetchone()
    if existing:
        value = json.loads(existing['intent'])
        if value.get('confirmation') != confirmation or value.get('file_id') != fid:
            raise ArchiveFailure('stale_preview')
        return existing['id']
    value, current = review(executor, fid)
    if current != confirmation:
        raise ArchiveFailure('stale_preview')
    original = value['ownership']['file']['filepath']
    suffix = Path(original).suffix.casefold()
    if suffix not in ('.cbr', '.cbz', '.rar', '.zip'):
        raise ArchiveFailure('unsupported_container')
    target = str(Path(original).with_suffix('.cbz'))
    if target_registered(executor.store.db, fid, target):
        raise ArchiveFailure('target_occupied')
    siblings = sibling_names(original)
    if any(name.startswith('.pullarr-archive-') for name in siblings):
        raise ArchiveFailure('archive_workspace_review_required')
    if target != original and any(name.casefold() == Path(target).name.casefold() for name in siblings):
        raise ArchiveFailure('target_occupied')
    token = uuid4().hex
    prepared = str(Path(original).parent / ('.pullarr-archive-' + token + '.cbz'))
    backup = str(Path(original).parent / ('.pullarr-archive-' + token + '.original'))
    registered = False
    try:
        converted = normalize(original, prepared, cancelled=cancelled)
        if cancelled():
            raise ArchiveFailure('cancelled')
        # All archive IO above is outside SQLite writer transactions.
        with execution_gate(executor.store.path):
            fresh, current = review(executor, fid)
            if current != confirmation or converted['old'] != fresh['old']:
                raise ArchiveFailure('stale_preview')
            if target_registered(executor.store.db, fid, target):
                raise ArchiveFailure('target_occupied')
            intent = dict(version=EXECUTOR_POLICY, archive_effect=VERSION, effects=EFFECTS,
                source=prepared, target=target, original=original, backup=backup,
                file_id=fid, volume_id=fresh['ownership']['volumes'][0][0], inverse=False,
                ownership=fresh['ownership'], sharing=fresh['sharing'], confirmation=confirmation,
                old=converted.pop('old'), incoming=converted.pop('incoming'),
                facts=converted.pop('facts'), receipt=converted)
            job = executor.store.create(intent, digest(canonical(intent)),
                tuple(os.path.normpath(p).casefold() for p in (prepared, target, original, backup)), batch_id)
            registered = True
            return job
    finally:
        # Only this invocation's exclusive output; registered evidence belongs
        # to the journal and must survive interruption/recovery.
        if not registered and os.path.isfile(prepared):
            os.unlink(prepared)


class ArchiveEffect:
    def __init__(self, executor):
        self.e, self.store = executor, executor.store

    def validate(self, intent):
        if (intent.get('archive_effect') != VERSION or intent.get('version') != EXECUTOR_POLICY
                or intent.get('effects') != EFFECTS or intent.get('inverse') is not False
                or not intent.get('receipt', {}).get('pages_preserved')):
            raise OrganizationError(ExecutionCode.CORRUPT)
        for key in ('source', 'target', 'original', 'backup'):
            self.e._scope(intent[key])
        if (len({intent[k] for k in ('source', 'target', 'backup')}) != 3
                or Path(intent['target']).suffix != '.cbz'
                or any(Path(intent[k]).parent != Path(intent['original']).parent for k in ('source', 'target', 'backup'))
                or not Path(intent['source']).name.startswith('.pullarr-archive-')
                or not Path(intent['backup']).name.startswith('.pullarr-archive-')):
            raise OrganizationError(ExecutionCode.CORRUPT)

    def check_database(self, job, intent):
        expected = json.loads(canonical(intent['ownership']))
        if job.steps[2].state == StepState.SUCCEEDED:
            expected['file'].update(filepath=intent['target'], size=intent['incoming']['size'])
        try:
            current = ownership(self.store.db, intent['file_id'])
        except ArchiveFailure:
            raise OrganizationError(ExecutionCode.CONFLICT) from None
        if canonical(current) != canonical(expected):
            raise OrganizationError(ExecutionCode.CONFLICT)
        if target_registered(self.store.db, intent['file_id'], intent['target']):
            raise OrganizationError(ExecutionCode.CONFLICT)
        if not self.store.db.in_transaction:
            for seed in intent['sharing']['seeds']:
                if not matches(seed['path'], seed['identity']):
                    raise OrganizationError(ExecutionCode.SOURCE)
        return {}

    def initial(self, job, intent, record=True):
        self.check_database(job, intent)
        try:
            current_sharing = sharing(self.store.db, intent['file_id'], intent['original'])
        except (ArchiveFailure, OSError, ValueError):
            raise OrganizationError(ExecutionCode.SOURCE) from None
        if (not matches(intent['original'], intent['old']) or not matches(intent['source'], intent['incoming'])
                or os.path.lexists(intent['backup'])
                or (intent['original'] != intent['target'] and os.path.lexists(intent['target']))
                or current_sharing != intent['sharing']):
            raise OrganizationError(ExecutionCode.SOURCE)
        value = dict(artifact=intent['incoming'], artifact_digest=digest(canonical(intent['incoming'])))
        if record:
            with self.store.transaction():
                self.store.event(job.id, 'validated', value)
        return value

    def start(self, job, intent, ordinal, validation):
        self.check_database(job, intent)
        value = dict(artifact_before=intent['incoming'])
        with self.store.transaction():
            self.store.checkpoint(job.id, ordinal, StepState.STARTED, value)
        self.e.hook('after_started', job.id, ordinal)
        return value

    def reconcile(self, job, intent, ordinal, value):
        if ordinal == 0:
            if matches(intent['backup'], intent['old']):
                return True
            if os.path.lexists(intent['backup']) or not matches(intent['original'], intent['old']):
                raise OrganizationError(ExecutionCode.SOURCE)
            return False
        if ordinal == 1:
            if not matches(intent['backup'], intent['old']):
                raise OrganizationError(ExecutionCode.SOURCE)
            if not os.path.lexists(intent['source']) and matches(intent['target'], intent['incoming']):
                return True
            if matches(intent['source'], intent['incoming']) and (not os.path.lexists(intent['target'])
                    or intent['original'] == intent['target'] and matches(intent['original'], intent['old'])):
                return False
            raise OrganizationError(ExecutionCode.CONFLICT)
        if not matches(intent['target'], intent['incoming']):
            raise OrganizationError(ExecutionCode.SOURCE)
        if ordinal == 2:
            return False
        for path in (intent['backup'],) + ((intent['original'],) if intent['original'] != intent['target'] else ()):
            if os.path.lexists(path) and not matches(path, intent['old']):
                raise OrganizationError(ExecutionCode.CONFLICT)
        return not os.path.lexists(intent['backup']) and (intent['original'] == intent['target'] or not os.path.lexists(intent['original']))

    def execute(self, job, intent, ordinal, value):
        self.check_database(job, intent)
        if ordinal == 0:
            if not matches(intent['original'], intent['old']):
                raise OrganizationError(ExecutionCode.SOURCE)
            os.link(intent['original'], intent['backup'])
        elif ordinal == 1:
            if not matches(intent['source'], intent['incoming']) or not matches(intent['backup'], intent['old']):
                raise OrganizationError(ExecutionCode.SOURCE)
            if intent['target'] == intent['original']:
                if not matches(intent['original'], intent['old']):
                    raise OrganizationError(ExecutionCode.SOURCE)
                os.replace(intent['source'], intent['target'])
            else:
                rename_no_replace(intent['source'], intent['target'])
        elif ordinal == 2:
            if not matches(intent['target'], intent['incoming']):
                raise OrganizationError(ExecutionCode.SOURCE)
            with self.store.transaction():
                self.check_database(job, intent)
                self.store.db.execute('UPDATE files SET filepath=?,size=? WHERE id=?',
                    (intent['target'], intent['incoming']['size'], intent['file_id']))
                QualityStore(self.store.db.cursor()).assessment(intent['file_id'], intent['facts'])
                self.store.checkpoint(job.id, ordinal, StepState.SUCCEEDED, value)
            self.e.hook('after_effect', job.id, ordinal)
            return
        else:
            if not matches(intent['target'], intent['incoming']):
                raise OrganizationError(ExecutionCode.SOURCE)
            for path in ((intent['original'],) if intent['original'] != intent['target'] else ()) + (intent['backup'],):
                if os.path.lexists(path):
                    if not matches(path, intent['old']):
                        raise OrganizationError(ExecutionCode.SOURCE)
                    os.unlink(path)  # Only library-owned directory entries.
        sync_directory(str(Path(intent['target']).parent))
        self.e.hook('after_effect', job.id, ordinal)
        self.e._receipt(job.id, ordinal, value)

    def finish(self, job, intent, validation):
        self.check_database(job, intent)
        if not self.reconcile(job, intent, 3, {}) or os.path.lexists(intent['source']):
            raise OrganizationError(ExecutionCode.CONFLICT)
        with self.store.transaction():
            self.store.state(job.id, JobState.COMPLETED)
            self.store.db.execute('DELETE FROM organization_reservations WHERE job_id=?', (job.id,))
            self.store.db.execute('UPDATE organization_jobs SET claim=NULL WHERE id=?', (job.id,))
