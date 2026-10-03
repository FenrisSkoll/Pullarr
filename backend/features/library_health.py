"""Explicit offline health discovery. No library writers or task registration.

Internal callers own scheduling. Work is serial, bounded and cooperative; no
HTTP request or background thread is installed. Reports are transient values.
"""

import os
import sqlite3
import stat
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Callable, Optional
from uuid import uuid4

from backend.base.import_candidate import FileObservation, InspectionState
from backend.base.library_health import (HealthFinding, HealthLevel,
                                         HealthLimits, HealthReport,
                                         HealthScope,
                                         HealthSeverity as Severity,
                                         InspectionStatus as Status, canonical,
                                         fingerprint)
from backend.base.organization_job import OrganizationError
from backend.implementations.comicinfo_archive import inspect_comicinfo
from backend.implementations.organization_filesystem import (comparison_ctime,
                                                             safe_path)
from backend.internals.library_health import HealthSnapshotLimit, read_snapshot


def _inside(path: str, root: str) -> bool:
    try:
        return bool(root) and os.path.isabs(path) and os.path.isabs(root) and os.path.commonpath((path, root)) == root
    except (ValueError, TypeError):
        return False


def _stamp(value: os.stat_result) -> tuple:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, comparison_ctime(value)


class _Stopped(Exception):
    pass


class _Scan:
    def __init__(self, scope: HealthScope, level: HealthLevel, limits: HealthLimits,
                 cancel: Callable[[], bool], progress: Callable[[dict], None]):
        self.scope, self.level, self.limits = scope, level, limits
        self.cancel, self.progress = cancel, progress
        self.id = uuid4().hex
        self.started = datetime.now(timezone.utc).isoformat()
        self.deadline = monotonic() + limits.seconds
        self.findings: list[HealthFinding] = []
        self.reasons: set[str] = set()
        self.counts: Counter = Counter()
        self.digest = ''
        self.bytes = 0
        self.snapshot: dict = {}
        self.volumes: dict = {}
        self.issues: dict = {}
        self.links: dict = defaultdict(list)
        self.general: dict = defaultdict(list)
        self.disk: dict[str, tuple] = {}
        self.hashes: dict[str, list[str]] = defaultdict(list)
        self.volume_refs: dict = defaultdict(list)
        self.enumerated: set[str] = set()

    def check(self) -> None:
        if self.cancel():
            self.reasons.add('cancelled')
            raise _Stopped()
        if monotonic() >= self.deadline:
            self.reasons.add('time_limit')
            raise _Stopped()

    def emit(self, category: str, code: str, severity: Severity, explanation: str,
             *, state: Status = Status.COMPLETE, path: Optional[str] = None,
             file_id: Optional[int] = None, volume_id: Optional[int] = None,
             issue_ids: tuple[int, ...] = (), evidence: Optional[dict] = None) -> None:
        self.check()
        evidence = evidence or {}
        volume = self.volumes.get(volume_id)
        authority = None if volume is None else dict(volume_id=volume_id,
            provider=volume['metadata_provider'], generation=volume['authority_generation'],
            references=self.volume_refs[volume_id])
        observed = dict(evidence=evidence, authority=authority, stat=self.disk.get(path or ''))
        # Filesystem evidence does not use authority as a substitute for stat;
        # selected-provider observations additionally bind the DB snapshot.
        digest = fingerprint((observed, self.digest if category not in ('archive', 'comicinfo', 'filesystem') else None))
        data = canonical(observed)
        finding = HealthFinding(fingerprint((category, code, volume_id, file_id, path, issue_ids, data)),
            category, code, severity, state, self.level, volume_id, issue_ids, file_id, path,
            explanation, data, digest)
        size = len(canonical(finding.view()))
        if len(self.findings) >= self.limits.findings or self.bytes + size > self.limits.result_bytes:
            self.reasons.add('finding_limit' if len(self.findings) >= self.limits.findings else 'result_bytes_limit')
            raise _Stopped()
        self.bytes += size
        self.findings.append(finding)
        self.counts['severity:' + severity.value] += 1
        self.counts['category:' + category] += 1
        if state != Status.COMPLETE:
            self.counts['inspection:' + state.value] += 1

    def observe(self, path: str, *, folder: bool = False, volume_id: Optional[int] = None,
                file_id: Optional[int] = None) -> bool:
        self.check()
        try:
            safe_path(path)
            value = os.lstat(path)
            if not (stat.S_ISDIR(value.st_mode) if folder else stat.S_ISREG(value.st_mode)):
                raise ValueError()
            self.disk[path] = _stamp(value)
            return True
        except FileNotFoundError:
            self.emit('filesystem', 'missing_folder' if folder else 'missing_file', Severity.WARNING,
                      'Configured folder or registered file is absent; no records were removed.',
                      path=path, volume_id=volume_id, file_id=file_id)
            return False
        except (OSError, ValueError, OrganizationError):
            self.reasons.add('path_unavailable')
            self.emit('filesystem', 'path_unavailable', Severity.ERROR, 'Path is inaccessible, unsafe or has an unexpected type.',
                      state=Status.UNAVAILABLE, path=path, volume_id=volume_id, file_id=file_id)
            return False

    def walk(self, folder: str) -> set[str]:
        result: set[str] = set()
        pending = [(folder, 0)]
        while pending:
            self.check()
            current, depth = pending.pop()
            if depth > 32:
                self.reasons.add('depth_limit')
                continue
            try:
                safe_path(current)
                with os.scandir(current) as stream:
                    entries = []
                    for entry in stream:
                        self.check()
                        self.counts['entries'] += 1
                        if self.counts['entries'] > self.limits.entries:
                            self.reasons.add('entry_limit')
                            raise _Stopped()
                        entries.append(entry.name)
                for name in sorted(entries):
                    path = os.path.join(current, name)
                    safe_path(path)
                    value = os.lstat(path)
                    if stat.S_ISDIR(value.st_mode):
                        pending.append((path, depth + 1))
                    elif stat.S_ISREG(value.st_mode):
                        result.add(path)
                        self.enumerated.add(path)
                        self.disk[path] = _stamp(value)
                        if len(self.enumerated) > self.limits.files:
                            self.reasons.add('file_limit')
                            raise _Stopped()
                    else:
                        self.emit('filesystem', 'unsupported_entry', Severity.WARNING,
                                  'Entry is not an ordinary file or directory.', state=Status.UNSUPPORTED, path=path)
            except (OSError, ValueError, OrganizationError):
                self.reasons.add('enumeration_unavailable')
                self.emit('filesystem', 'enumeration_unavailable', Severity.ERROR,
                          'Directory enumeration was incomplete; absence cannot be inferred.',
                          state=Status.UNAVAILABLE, path=current)
        return result

    def identities(self) -> None:
        from backend.implementations.metadata.registry import PROVIDERS

        selected = set(self.snapshot['selected'])
        for vid in sorted(selected):
            if self.volumes[vid]['metadata_provider'] not in PROVIDERS:
                self.emit('identity', 'unknown_selected_provider', Severity.ERROR,
                          'Selected provider is not registered.', volume_id=vid)
        for key, entity, owner in (('volume_refs', 'volume', 'volume_id'), ('issue_refs', 'issue', 'issue_id')):
            qualified: dict = defaultdict(list)
            refs: dict = defaultdict(list)
            for row in self.snapshot[key]:
                qualified[(row['provider'], row['provider_id'])].append(row[owner])
                refs[row[owner]].append(row)
            objects = self.volumes if entity == 'volume' else self.issues
            for local, row in objects.items():
                self.check()
                vid = local if entity == 'volume' else row['volume_id']
                if vid not in selected:
                    continue
                volume = self.volumes[vid]
                matching = [r for r in refs[local] if r['provider'] == volume['metadata_provider'] and r['provider_id']]
                if len(matching) != 1:
                    self.emit('identity', 'selected_identity_missing', Severity.ERROR,
                              'Selected authority has no unique exact identity.', volume_id=vid,
                              issue_ids=() if entity == 'volume' else (local,))
                elif volume['metadata_provider'] == 'comicvine' and str(row['comicvine_id']) != matching[0]['provider_id']:
                    self.emit('identity', 'comicvine_projection_conflict', Severity.ERROR,
                              'Genuine ComicVine identity and compatibility projection disagree.', volume_id=vid)
            for (provider, identity), owners in sorted(qualified.items()):
                affected = [i for i in owners if (i if entity == 'volume' else self.issues.get(i, {}).get('volume_id')) in selected]
                if len(set(owners)) > 1 and affected:
                    self.emit('identity', 'identity_ownership_conflict', Severity.WARNING,
                              'Exact provider identity has multiple local owners; no winner was selected.',
                              evidence=dict(provider=provider, provider_id=identity, entity=entity, owners=owners))

    def metadata(self) -> None:
        from backend.base.issue_facts import (BibliographicDate, DateKind,
                                              DatePrecision, IssueNumberFacts,
                                              NumberKind)

        selected = set(self.snapshot['selected'])
        for row in self.snapshot['classification']:
            vid = row['volume_id']
            if vid in selected and row['applied_value'] != self.volumes[vid]['special_version']:
                self.emit('metadata', 'classification_receipt_mismatch', Severity.ERROR,
                          'Stored application receipt and classification value disagree.', volume_id=vid)
        for table in ('numbers', 'dates'):
            for row in self.snapshot[table]:
                self.check()
                issue = self.issues.get(row['issue_id'])
                if issue is None or issue['volume_id'] not in selected:
                    continue
                try:
                    if table == 'numbers':
                        IssueNumberFacts(row['raw_label'], row['provenance'], row['source_field'],
                            NumberKind(row['interpretation']), row['numeric_text'], row['policy'])
                    else:
                        BibliographicDate(row['raw_value'], DateKind(row['kind']), row['source_field'], row['provenance'],
                            row['year'], row['month'], row['day'], DatePrecision(row['precision']),
                            bool(row['zero_placeholders']), row['uncertainty'], row['policy'])
                except (ValueError, TypeError):
                    self.emit('metadata', 'invalid_canonical_fact', Severity.ERROR,
                              'Persisted canonical fact cannot be represented by its current contract.',
                              volume_id=issue['volume_id'], issue_ids=(row['issue_id'],), evidence=dict(table=table))

    def policies(self, relevant: dict) -> None:
        from backend.base.folder_policy import FolderPolicy, FolderPublication
        from backend.base.rename_policy import (NamingContext,
                                                RenameIssue, RenamePolicy)
        from backend.implementations.folder_policy import (FolderContext,
                                                           decide_folder)
        from backend.implementations.rename_policy import (
            build_rename_catalog, decide_rename)

        records = self.snapshot['planning']
        if records is None:
            self.reasons.add('policy_unavailable')
            self.emit('policy', 'policy_snapshot_unavailable', Severity.INFORMATION,
                      'Canonical policy inputs are unavailable; no replacement naming rules were invented.', state=Status.UNAVAILABLE)
            return
        volumes, issues, roots, _, naming = records
        vs = {v.identity.id: v for v in volumes}
        ins = {i.identity.id: i for i in issues}
        children: dict = defaultdict(list)
        for issue in issues:
            children[issue.identity.volume_id].append(issue.identity)
        folders = FolderContext.build(roots, ((v.identity.id, v.folder) for v in volumes if v.folder),
                                      naming, os.name == 'nt', os.name != 'nt', None)
        catalogs = {}
        for vid in self.snapshot['selected']:
            self.check()
            v = vs[vid]
            identity = v.identity
            decision = decide_folder(FolderPublication(identity.title, identity.year, identity.volume_number,
                identity.publisher, identity.authority, vid, v.root_id, v.folder, v.custom_folder,
                v.comicvine_id, identity.special_version), folders, FolderPolicy())
            catalogs[vid] = build_rename_catalog(vid, children[vid], naming.issue_padding)
            if decision.policy_folder and os.path.normpath(decision.policy_folder) != os.path.normpath(v.folder or ''):
                self.emit('policy', 'folder_deviation', Severity.INFORMATION if v.custom_folder else Severity.DEVIATION,
                          'Canonical policy differs; existing folder ownership remains authoritative.', volume_id=vid,
                          evidence=dict(current=v.folder, canonical=decision.policy_folder, custom=v.custom_folder))
            if decision.diagnostics:
                self.emit('policy', 'folder_policy_diagnostic', Severity.INFORMATION,
                          'Folder policy reported unavailable or constrained values.', volume_id=vid,
                          evidence=dict(codes=[d.code.value for d in decision.diagnostics]))
        targets: dict = defaultdict(list)
        occupied = {os.path.normcase(f['filepath']): f['id'] for f in self.snapshot['files']}
        for fid, file in relevant.items():
            self.check()
            ids = self.links[fid]
            if not ids or any(i not in ins for i in ids):
                continue
            vids = {ins[i].identity.volume_id for i in ids}
            if len(vids) != 1 or next(iter(vids)) not in catalogs:
                continue
            vid = next(iter(vids))
            v = vs[vid]
            path = file['filepath']
            decision = decide_rename(NamingContext(v.identity,
                tuple(RenameIssue(ins[i].identity, ins[i].title, ins[i].date, ins[i].comicvine_id) for i in sorted(ids)),
                catalogs[vid], naming, os.path.basename(path), v.comicvine_id, os.path.dirname(path),
                os.name == 'nt', os.name != 'nt', None), RenamePolicy())
            self.counts['policy_files'] += 1
            if decision.target_filename is None:
                self.emit('policy', 'naming_unavailable', Severity.INFORMATION,
                          'Canonical naming is unavailable for these facts; file identity remains unchanged.',
                          state=Status.UNAVAILABLE, volume_id=vid, file_id=fid, path=path,
                          evidence=dict(codes=[d.code.value for d in decision.diagnostics]))
                continue
            target = os.path.join(os.path.dirname(path), decision.target_filename)
            targets[os.path.normcase(target)].append((fid, path, vid))
            if target != path:
                self.emit('policy', 'filename_deviation', Severity.DEVIATION,
                          'Filename differs from current canonical policy; this is not repair authorization.',
                          path=path, file_id=fid, volume_id=vid, issue_ids=tuple(sorted(ids)), evidence=dict(canonical=target))
            if os.path.normcase(target) != os.path.normcase(path) and (os.path.normcase(target) in occupied or target in self.disk):
                self.emit('duplicate', 'path_collision', Severity.WARNING, 'Canonical destination is occupied.',
                          path=path, file_id=fid, volume_id=vid, evidence=dict(target=target))
        for target, group in sorted(targets.items()):
            if len(group) > 1:
                self.emit('duplicate', 'path_collision', Severity.WARNING,
                          'Several files propose the same canonical path; no suffix or winner was chosen.',
                          path=target, evidence=dict(file_ids=sorted(v[0] for v in group)))

    def archives(self, path: str, fid: Optional[int], vid: Optional[int]) -> None:
        if self.level == HealthLevel.INVENTORY:
            self.counts['archive_skipped_by_policy'] += 1
            return
        if self.counts['archives'] >= self.limits.archives:
            self.reasons.add('archive_limit')
            self.counts['archive_skipped_by_bound'] += 1
            return
        self.check()
        suffix = Path(path).suffix.lower()
        self.counts['archives'] += 1
        if suffix in ('.cbz', '.zip', '.cbr', '.rar', '.pdf'):
            try:
                safe_path(path)
                with open(path, 'rb') as stream:
                    if _stamp(os.fstat(stream.fileno())) != self.disk[path]:
                        raise ValueError()
                    magic = stream.read(8)
                detected = ('zip' if magic.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')) else
                            'rar' if magic.startswith(b'Rar!\x1a\x07') else 'pdf' if magic.startswith(b'%PDF-') else None)
                expected = 'zip' if suffix in ('.zip', '.cbz') else 'rar' if suffix in ('.rar', '.cbr') else 'pdf'
                if detected is not None and detected != expected:
                    self.emit('archive', 'extension_content_mismatch', Severity.WARNING,
                              'Recognized signature differs from filename container; no conversion occurred.',
                              path=path, file_id=fid, volume_id=vid, evidence=dict(detected=detected, expected=expected))
            except (OSError, ValueError, OrganizationError):
                self.reasons.add('inspection_incomplete')
                self.emit('archive', 'archive_unavailable', Severity.WARNING,
                          'Archive changed or became inaccessible after inventory.', state=Status.UNAVAILABLE,
                          path=path, file_id=fid, volume_id=vid)
                return
        if suffix not in ('.cbz', '.zip'):
            self.reasons.add('unsupported_probe')
            self.counts['archive_unsupported'] += 1
            self.emit('archive', 'unsupported_container', Severity.INFORMATION,
                      'This health probe has no supported metadata/container validator; unsupported is not corruption.',
                      state=Status.UNSUPPORTED, path=path, file_id=fid, volume_id=vid,
                      evidence=dict(extension=suffix, rar_preparation_not_metadata_inspection=suffix in ('.rar', '.cbr')))
            return
        stamp = self.disk[path]
        inspection = inspect_comicinfo(path, FileObservation(path, datetime.now(timezone.utc), stamp[2], stamp[3], InspectionState.PRESENT))
        if inspection.state == InspectionState.ABSENT:
            self.emit('comicinfo', 'comicinfo_absent', Severity.WARNING, 'No ComicInfo document was found.', path=path, file_id=fid, volume_id=vid)
        elif inspection.state == InspectionState.PRESENT:
            self.counts['comicinfo_present'] += 1
        for diagnostic in inspection.diagnostics:
            code = diagnostic.code.value
            category = 'comicinfo' if code in ('xml_malformed', 'xml_unsafe', 'unsupported_root', 'unknown_version',
                'invalid_field', 'duplicate_field', 'multiple_documents') else 'archive'
            state = Status.UNSUPPORTED if code in ('unsupported_format', 'encrypted') else (
                Status.BOUNDED if code == 'limit_exceeded' else Status.FAILED if inspection.state == InspectionState.FAILED else Status.COMPLETE)
            if state != Status.COMPLETE:
                self.reasons.add('inspection_incomplete')
            self.emit(category, code, Severity.WARNING if state in (Status.UNSUPPORTED, Status.COMPLETE) else Severity.ERROR,
                      'Bounded archive/metadata inspection reported ' + code.replace('_', ' ') + '.',
                      state=state, path=path, file_id=fid, volume_id=vid, evidence=dict(field=diagnostic.field))
        if inspection.page_count == 0:
            self.emit('archive', 'no_comic_pages', Severity.WARNING,
                      'No supported image-page names in admitted archive headers; page payloads were not decoded.', path=path, file_id=fid, volume_id=vid)
        self.counts['page_payloads_not_checked'] += 1

    def hash_file(self, path: str) -> None:
        if self.level != HealthLevel.DEEP:
            return
        before = self.disk[path]
        if self.counts['hashes'] >= self.limits.hashes or self.counts['hash_bytes'] + before[2] > self.limits.hash_bytes:
            self.reasons.add('hash_limit')
            self.counts['hash_skipped_by_bound'] += 1
            return
        try:
            safe_path(path)
            digest = sha256()
            with open(path, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if _stamp(opened) != before:
                    raise ValueError()
                while True:
                    self.check()
                    allowance = self.limits.hash_bytes - self.counts['hash_bytes']
                    block = stream.read(min(1024 * 1024, allowance + 1))
                    if not block:
                        break
                    self.counts['hash_bytes'] += len(block)
                    if self.counts['hash_bytes'] > self.limits.hash_bytes:
                        self.reasons.add('hash_limit')
                        raise _Stopped()
                    digest.update(block)
                finished = os.fstat(stream.fileno())
                if _stamp(finished) != _stamp(opened) or finished.st_ctime_ns != opened.st_ctime_ns:
                    raise ValueError()
            if _stamp(os.lstat(path)) != before:
                raise ValueError()
            self.counts['hashes'] += 1
            self.hashes[digest.hexdigest()].append(path)
            self.emit('duplicate', 'byte_hash', Severity.INFORMATION,
                      'Exact byte observation, not publication identity.', path=path,
                      evidence=dict(algorithm='sha256/v1', digest=digest.hexdigest(), size=before[2]))
        except (OSError, ValueError, OrganizationError):
            self.reasons.add('hash_unavailable')
            self.emit('filesystem', 'hash_unavailable', Severity.WARNING,
                      'File changed or became unavailable while hashing; no digest accepted.', state=Status.UNAVAILABLE, path=path)

    def run(self, database: str) -> None:
        self.snapshot = read_snapshot(database, self.scope, self.limits.rows)
        self.digest = self.snapshot['digest']
        self.volumes = {r['id']: r for r in self.snapshot['volumes']}
        for row in self.snapshot['volume_refs']:
            self.volume_refs[row['volume_id']].append(row)
        self.issues = {r['id']: r for r in self.snapshot['issues']}
        selected = set(self.snapshot['selected'])
        roots = {r['id']: r['folder'] for r in self.snapshot['roots']}
        for row in self.snapshot['direct']:
            self.links[row['file_id']].append(row['issue_id'])
        for row in self.snapshot['general']:
            self.general[row['file_id']].append(row['volume_id'])
        self.identities()
        self.metadata()
        folders = []
        if self.scope.kind in ('library', 'root'):
            folders = [root for rid, root in roots.items() if self.scope.kind == 'library' or rid in self.scope.ids]
        for vid in sorted(selected):
            self.check()
            volume = self.volumes[vid]
            self.counts['volumes'] += 1
            path = volume['folder']
            if not path or not _inside(path, roots.get(volume['root_folder'], '')):
                self.emit('filesystem', 'volume_outside_root', Severity.ERROR,
                          'Managed folder is absent or outside its configured root.', volume_id=vid, path=path)
                continue
            self.observe(path, folder=True, volume_id=vid)
            if self.scope.kind == 'volumes':
                folders.append(path)
        # Overlapping roots/scopes do not cause repeated archive/hash work.
        minimal: list[str] = []
        for folder in sorted(set(folders), key=lambda p: (len(p), p)):
            if not any(_inside(folder, old) for old in minimal):
                minimal.append(folder)
        disk: set[str] = set()
        unavailable_roots = set()
        for folder in minimal:
            if self.observe(folder, folder=True):
                disk.update(self.walk(folder))
            else:
                self.reasons.add('root_unavailable')
                unavailable_roots.add(folder)
        if len(disk) > self.limits.files:
            self.reasons.add('file_limit')
            raise _Stopped()
        by_path: dict = defaultdict(list)
        files = {r['id']: r for r in self.snapshot['files']}
        relevant = {}
        for fid, row in files.items():
            by_path[os.path.normcase(row['filepath'])].append(fid)
            vids = {self.issues[i]['volume_id'] for i in self.links[fid] if i in self.issues} | set(self.general[fid])
            if vids & selected or any(_inside(row['filepath'], f) for f in minimal):
                relevant[fid] = row
                vid = next(iter(vids)) if len(vids) == 1 else None
                if len(vids) > 1:
                    self.emit('association', 'multiple_direct_volumes', Severity.WARNING,
                              'Direct associations span local volumes; collected coverage is not included.', file_id=fid, path=row['filepath'],
                              evidence=dict(volumes=sorted(vids)))
                invalid = [i for i in self.links[fid] if i not in self.issues]
                if invalid:
                    self.emit('association', 'missing_issue', Severity.ERROR, 'Direct association references a missing issue.', file_id=fid, evidence=dict(issues=invalid))
                if vid is not None and (vid not in self.volumes or not _inside(row['filepath'], self.volumes[vid]['folder'] or '')):
                    self.emit('association', 'outside_managed_folder', Severity.WARNING,
                              'Registered path lies outside its associated managed volume folder.', file_id=fid, path=row['filepath'], volume_id=vid)
                if not any(_inside(row['filepath'], root) for root in roots.values()):
                    self.emit('filesystem', 'file_outside_root', Severity.ERROR, 'Registered path is outside configured roots; it was not opened.', file_id=fid, path=row['filepath'], volume_id=vid)
                elif row['filepath'] not in disk:
                    if any(_inside(row['filepath'], root) for root in unavailable_roots):
                        self.emit('filesystem', 'file_probe_unavailable', Severity.INFORMATION,
                                  'Root/folder is unavailable; individual file absence is not inferred.',
                                  state=Status.UNAVAILABLE, file_id=fid, path=row['filepath'], volume_id=vid)
                    else:
                        self.observe(row['filepath'], file_id=fid, volume_id=vid)
                if not vids:
                    self.emit('association', 'unassociated_file_row', Severity.WARNING, 'Registered file has no direct or general association.', file_id=fid, path=row['filepath'])
        for path, ids in sorted(by_path.items()):
            if len(ids) > 1 and any(i in relevant for i in ids):
                self.emit('filesystem', 'duplicate_database_path', Severity.ERROR, 'Multiple registered files share a platform-equivalent path.', path=path, evidence=dict(file_ids=ids))
        for fid, issue_ids in self.links.items():
            if fid not in files and any(self.issues.get(i, {}).get('volume_id') in selected for i in issue_ids):
                self.emit('association', 'missing_file_row', Severity.ERROR, 'Direct association references a missing file row.', file_id=fid, issue_ids=tuple(sorted(issue_ids)))
        for path in sorted(disk):
            self.check()
            ids = by_path.get(os.path.normcase(path), [])
            fid = ids[0] if len(ids) == 1 else None
            vids = {self.issues[i]['volume_id'] for i in self.links.get(fid, ()) if i in self.issues}
            vid = next(iter(vids)) if len(vids) == 1 else None
            self.counts['files'] += 1
            if not ids:
                self.emit('filesystem', 'untracked_file', Severity.WARNING, 'Disk file is not represented in the file table; no registration occurred.', path=path)
            self.archives(path, fid, vid)
            self.hash_file(path)
            self.progress(dict(self.counts))
        publications: dict = defaultdict(list)
        for fid in relevant:
            if self.links[fid]:
                publications[tuple(sorted(self.links[fid]))].append(fid)
        for issues, ids in publications.items():
            if len(ids) > 1:
                self.emit('duplicate', 'same_publication_files', Severity.INFORMATION,
                          'Several files have the same exact direct local issue set; bytes/quality need separate review.',
                          issue_ids=issues, evidence=dict(file_ids=sorted(ids)))
        covered: dict = defaultdict(set)
        for row in self.snapshot['coverage']:
            covered[row['source_issue_id']].add(row['file_id'])
        for issue, ids in covered.items():
            if len(ids) > 1 and (self.issues.get(issue, {}).get('volume_id') in selected or ids & relevant.keys()):
                self.emit('duplicate', 'overlapping_collected_coverage', Severity.INFORMATION,
                          'Valid collected coverage overlaps; this is not duplicate publication ownership.', issue_ids=(issue,), evidence=dict(file_ids=sorted(ids)))
        for digest, paths in sorted(self.hashes.items()):
            if len(paths) > 1:
                self.emit('duplicate', 'exact_byte_duplicate', Severity.INFORMATION,
                          'Identical streamed SHA-256 evidence; no deletion or identity merge is authorized.', evidence=dict(algorithm='sha256/v1', digest=digest, paths=paths))
        self.policies(relevant)

    def report(self) -> HealthReport:
        state = Status.FAILED if 'database_unavailable' in self.reasons else Status.CANCELLED if 'cancelled' in self.reasons else (
            Status.BOUNDED if any('limit' in r for r in self.reasons) else Status.PARTIAL if self.reasons else Status.COMPLETE)
        return HealthReport(self.id, self.scope, self.level, self.started, datetime.now(timezone.utc).isoformat(),
                            state, tuple(sorted(self.reasons)), canonical(dict(self.counts)),
                            tuple(sorted(self.findings, key=lambda f: (f.category, f.code, f.path or '', f.id))), self.digest)


def scan_health(database: str, scope: object = HealthScope(), level: object = HealthLevel.INVENTORY,
                limits: object = HealthLimits(), *, cancel: Callable[[], bool] = lambda: False,
                progress: Callable[[dict], None] = lambda _: None) -> HealthReport:
    """Synchronous internal worker boundary; never call from an HTTP handler.

    Callback cancellation/deadline checks occur between entries and hash chunks;
    an already blocked OS call cannot be forcibly interrupted. No global store.
    """
    if not isinstance(scope, HealthScope) or not isinstance(level, HealthLevel) or not isinstance(limits, HealthLimits):
        raise ValueError('Invalid health request')
    scan = _Scan(scope, level, limits, cancel, progress)
    try:
        scan.run(database)
    except _Stopped:
        pass
    except HealthSnapshotLimit:
        scan.reasons.add('database_snapshot_limit')
    except sqlite3.Error:
        scan.reasons.add('database_unavailable')
    return scan.report()
