"""Pure folder policy. No database, filesystem observations or provider calls."""

import ntpath
import posixpath
from dataclasses import dataclass
from hashlib import sha256
from re import findall, sub
from string import Formatter
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Tuple

from backend.base.definitions import SV_TO_FULL_TERM, SV_TO_SHORT_TERM
from backend.base.files import (clean_filestring_simple,
                                clean_filestring_smartly)
from backend.base.folder_policy import (FolderCode, FolderDecision,
                                        FolderDiagnostic, FolderMode,
                                        FolderPolicy, FolderPublication,
                                        FolderStatus, MissingFolderValue)
from backend.base.import_candidate import ResourceKind
from backend.base.naming_policy import NamingSettings

# No attribute/index traversal, issue/date tokens or implicit aliases.
TOKENS = frozenset(('series_name', 'clean_series_name', 'year', 'publisher',
                    'volume_number', 'special_version', 'comicvine_id',
                    'metadata_provider', 'provider_id'))


@dataclass(frozen=True)
class FolderContext:
    roots: Mapping[int, str]
    owners: Tuple[Tuple[int, str], ...]
    naming: NamingSettings
    windows: bool = False
    case_sensitive: bool = True
    max_path_length: Optional[int] = None

    @classmethod
    def build(cls, roots: Iterable[Tuple[int, str]], owners: Iterable[Tuple[int, str]],
              naming: NamingSettings, windows: bool = False,
              case_sensitive: bool = True, max_path_length: Optional[int] = None) -> 'FolderContext':
        rows = tuple(sorted(roots))
        if len(dict(rows)) != len(rows):
            raise ValueError('Duplicate root identity')
        return cls(MappingProxyType(dict(rows)), tuple(sorted(set(owners))), naming,
                   windows, case_sensitive, max_path_length)

    @property
    def paths(self):
        return ntpath if self.windows else posixpath

    def key(self, path: str) -> str:
        path = self.paths.normpath(path)
        return path if self.case_sensitive else path.casefold()

    def inside(self, root: str, path: str) -> bool:
        # ntpath.commonpath folds case even on a case-sensitive target; use the
        # explicit comparison policy and a separator boundary instead.
        root_key, path_key = self.key(root), self.key(path)
        return self.paths.isabs(root) and self.paths.isabs(path) and (
            path_key == root_key or path_key.startswith(root_key.rstrip(self.paths.sep) + self.paths.sep))


def _clean(value: str, naming: NamingSettings) -> str:
    cleaner = clean_filestring_smartly if naming.replace_illegal_characters else clean_filestring_simple
    cleaned = cleaner(value)
    # The legacy smart helper can retain backslashes or unusual slash contexts.
    # Metadata values must never introduce template structure or drive syntax.
    cleaned = cleaned.replace('\\', '-' if naming.replace_illegal_characters else '')
    cleaned = cleaned.replace('/', '-' if naming.replace_illegal_characters else '').replace(':', '')
    return sub(r'(?<=\s)\s+', '', cleaned).strip()


class _AbsentIdentity(str):
    def __format__(self, spec: str) -> str:
        return ''


def _values(publication: FolderPublication, naming: NamingSettings) -> dict:
    title = publication.title
    clean_title = title
    if title:
        # Legacy article handling follows sanitation, not an alias lookup.
        clean_title = _clean(title, naming)
        for prefix in ('The ', 'A '):
            if clean_title.startswith(prefix):
                clean_title = clean_title[len(prefix):] + ', ' + prefix.strip()
                break
    authority = publication.authority
    return dict(series_name=title, clean_series_name=clean_title, year=publication.year,
                publisher=publication.publisher,
                volume_number=str(publication.volume_number).zfill(naming.volume_padding)
                if publication.volume_number is not None else None,
                special_version=(SV_TO_FULL_TERM if naming.long_special_version else SV_TO_SHORT_TERM).get(publication.special_version),
                comicvine_id=publication.comicvine_id,
                metadata_provider=authority.provider if authority else None,
                provider_id=authority.provider_id if authority else None)


def _segments(path: str) -> Tuple[str, ...]:
    return tuple(path.replace('\\', '/').split('/'))


def _relative_safe(path: str) -> bool:
    return bool(path) and not ntpath.splitdrive(path)[0] and not path.startswith(('/', '\\')) and all(
        p.strip() not in ('', '.', '..') for p in _segments(path))


def _path_problem(path: str, context: FolderContext) -> Optional[FolderCode]:
    if '\x00' in path or any(ord(c) < 32 for c in path):
        return FolderCode.PATH
    parts = _segments(ntpath.splitdrive(path)[1] if context.windows else path)
    if any(len(p.encode('utf-8')) > 255 for p in parts) or (
            context.max_path_length is not None and len(path) > context.max_path_length):
        return FolderCode.LENGTH
    if context.windows:
        reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
        if any(p.split('.')[0].upper() in reserved for p in parts):
            return FolderCode.RESERVED
        if any(p.rstrip(' .') != p or any(c in p for c in '<>:"|?*') for p in parts if p):
            return FolderCode.PATH
    return None


def _render(publication: FolderPublication, context: FolderContext, policy: FolderPolicy):
    template = policy.template if policy.template is not None else context.naming.volume_folder_naming
    diagnostics = []
    raw = []
    safe = []
    if len(template) > 2048 or not _relative_safe(template):
        return (), (), (FolderDiagnostic(FolderCode.ESCAPE, blocking=True),)
    values = _values(publication, context.naming)
    try:
        for segment in _segments(template):
            supplied = {}
            raw_values = {}
            for _, token, spec, conversion in Formatter().parse(segment):
                if token is None:
                    continue
                if token not in TOKENS or '{' in spec or '}' in spec or len(spec) > 16:
                    raise ValueError('Unknown token or nested format')
                if any(int(n) > 256 for n in findall(r'\d+', spec)):
                    raise ValueError('Oversized format')
                value = values[token]
                if value is None or (isinstance(value, str) and not _clean(value, context.naming)
                                     and token != 'special_version'):
                    diagnostics.append(FolderDiagnostic(FolderCode.MISSING, token))
                    if policy.missing == MissingFolderValue.LEGACY and token in ('year', 'publisher', 'comicvine_id'):
                        value = {'year': 'Unknown Year', 'publisher': 'Unknown Publisher', 'comicvine_id': _AbsentIdentity()}[token]
                        diagnostics.append(FolderDiagnostic(FolderCode.FALLBACK, token))
                    else:
                        return (), (), tuple(diagnostics + [FolderDiagnostic(FolderCode.MISSING, token, True)])
                raw_values[token] = value
                supplied[token] = _clean(value, context.naming) if isinstance(value, str) and not isinstance(value, _AbsentIdentity) else value
            raw.append(segment.format_map(raw_values))
            rendered = segment.format_map(supplied)
            # Values are individual components; literals alone define nesting.
            cleaned = _clean(rendered, context.naming)
            if cleaned in ('', '.', '..') or not _relative_safe(cleaned):
                return tuple(raw), tuple(safe), (FolderDiagnostic(FolderCode.PATH, blocking=True),)
            safe.append(cleaned)
    except (ValueError, KeyError, TypeError):
        return tuple(raw), tuple(safe), (FolderDiagnostic(FolderCode.TEMPLATE, blocking=True),)
    return tuple(raw), tuple(safe), tuple(diagnostics)


def decide_folder(publication: FolderPublication, context: FolderContext,
                  policy: FolderPolicy = FolderPolicy()) -> FolderDecision:
    """Describe current/canonical/selected folders; never inspect or mutate paths."""
    mod = context.paths
    current = publication.current_folder or None
    preserve = policy.mode == FolderMode.PRESERVE_EXISTING and current is not None
    root_id = publication.root_id
    reasons = []
    diagnostics = []
    if publication.local_id is not None:
        reasons.append('existing_volume_root')
        if root_id not in context.roots:
            diagnostics.append(FolderDiagnostic(FolderCode.ROOT, blocking=True))
        elif policy.root_id is not None and policy.root_id != root_id:
            if policy.mode == FolderMode.RECALCULATE:
                root_id = policy.root_id
                reasons.append('explicit_root_selection')
                diagnostics.append(FolderDiagnostic(FolderCode.ROOT_CHANGE))
            else:
                diagnostics.append(FolderDiagnostic(FolderCode.ROOT_CHANGE))
    else:
        root_id = policy.root_id if policy.root_id is not None else policy.default_root_id
        reasons.append('explicit_root_selection' if policy.root_id is not None else 'supplied_default_root')
    root = context.roots.get(root_id) if root_id is not None else None
    if not root or not mod.isabs(root):
        diagnostics.append(FolderDiagnostic(FolderCode.ROOT, blocking=True))
    if publication.authority is not None and publication.authority.kind != ResourceKind.VOLUME:
        diagnostics.append(FolderDiagnostic(FolderCode.MISSING, 'volume_authority', True))
    raw, safe, rendering = _render(publication, context, policy)
    relative = mod.join(*safe) if safe and not any(d.blocking for d in rendering) else None
    canonical = mod.normpath(mod.join(root, relative)) if root and relative else None
    if canonical:
        problem = _path_problem(canonical, context)
        if problem:
            rendering += (FolderDiagnostic(problem, blocking=True),)
            canonical = None
    if preserve or policy.custom_relative is not None:
        if preserve:
            reasons.append('retain_custom_folder' if publication.custom_folder else 'retain_managed_folder')
        diagnostics.extend(FolderDiagnostic(d.code, d.field) for d in rendering)
        if any(d.blocking for d in rendering):
            diagnostics.append(FolderDiagnostic(FolderCode.CANONICAL_UNAVAILABLE))
    else:
        reasons.append('canonical_template')
        diagnostics.extend(rendering)
    target = current if preserve else canonical
    if policy.custom_relative is not None:
        if not _relative_safe(policy.custom_relative):
            diagnostics.append(FolderDiagnostic(FolderCode.ESCAPE, blocking=True))
        elif preserve:
            diagnostics.append(FolderDiagnostic(FolderCode.CUSTOM))
        elif root:
            target = mod.normpath(mod.join(root, *_segments(policy.custom_relative)))
            reasons.append('explicit_custom_target')
    if publication.custom_folder and not preserve:
        diagnostics.append(FolderDiagnostic(FolderCode.CUSTOM))
    if target and root:
        if not context.inside(root, target) or context.key(root) == context.key(target):
            diagnostics.append(FolderDiagnostic(FolderCode.ESCAPE, blocking=True))
        problem = _path_problem(target, context)
        if problem:
            diagnostics.append(FolderDiagnostic(problem, blocking=True))
        if any(owner != publication.local_id and (context.inside(folder, target) or context.inside(target, folder))
               for owner, folder in context.owners):
            diagnostics.append(FolderDiagnostic(FolderCode.OWNERSHIP, blocking=True))
    if current and target and not preserve and mod.normpath(current) != mod.normpath(target):
        diagnostics.append(FolderDiagnostic(FolderCode.CASE if context.key(current) == context.key(target) else FolderCode.REORGANIZE))
    reviews = {FolderCode.CUSTOM, FolderCode.CASE, FolderCode.REORGANIZE, FolderCode.ROOT_CHANGE}
    status = (FolderStatus.BLOCKED if any(d.blocking for d in diagnostics) or target is None else
              FolderStatus.REVIEW if any(d.code in reviews for d in diagnostics) else
              FolderStatus.RETAINED if preserve else FolderStatus.CALCULATED)
    fingerprint = sha256(repr((publication, policy, tuple(sorted(context.roots.items())), context.owners,
                              context.naming.volume_folder_naming, context.naming.replace_illegal_characters,
                              context.naming.volume_padding, context.naming.long_special_version,
                              context.windows, context.case_sensitive, context.max_path_length)).encode()).hexdigest()
    return FolderDecision(policy.mode, status, root_id, root, current, canonical, target,
                          mod.relpath(target, root) if target and root and context.inside(root, target) else None,
                          raw, safe, publication.authority, tuple(reasons), tuple(dict.fromkeys(diagnostics)), fingerprint)


def preview_folder(decision: FolderDecision) -> dict:
    return dict(mode=decision.mode.value, state=decision.status.value, root_id=decision.root_id,
                root=decision.root, current=decision.current_folder, canonical=decision.policy_folder,
                target=decision.target_folder, relative=decision.relative_folder,
                raw_components=list(decision.raw_components), safe_components=list(decision.safe_components),
                retained=decision.retained, policy=decision.policy_id, fingerprint=decision.fingerprint,
                authority=dict(provider=decision.authority.provider, kind=decision.authority.kind.value,
                               id=decision.authority.provider_id) if decision.authority else None,
                reasons=list(decision.reasons), diagnostics=[dict(code=d.code.value, field=d.field, blocking=d.blocking)
                                                           for d in decision.diagnostics])
