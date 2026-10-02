"""Bounded local observation and explicit foreign-path mapping. No mutation."""

import os
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Tuple

from backend.base.acquisition_intake import (ArtifactObservation,
                                             DownloaderPathMapping,
                                             IntakeErrorCode as
                                             E, IntakeFailure)
from backend.base.organization_job import OrganizationError
from backend.implementations.organization_filesystem import safe_path

MAX_ARTIFACTS = 1000
MAX_ENTRIES = 10000
MAX_DEPTH = 32
COMIC_EXTENSIONS = frozenset(('.cbz', '.zip', '.cbr', '.rar', '.pdf'))


def contained(path: str, root: str) -> str:
    """Lexical containment followed by ancestor lstat; never resolve symlinks."""
    target, scope = Path(path), Path(root)
    if (not target.is_absolute() or not scope.is_absolute()
            or scope.parent == scope or '..' in target.parts or '..' in scope.parts
            or not (target == scope or scope in target.parents)):
        raise IntakeFailure(E.UNSAFE_PATH)
    if os.name == 'nt' and any(not _portable_component(p) for p in target.parts[1:]):
        raise IntakeFailure(E.UNSAFE_PATH)
    try:
        safe_path(str(scope))
        safe_path(str(target))
    except OrganizationError:
        raise IntakeFailure(E.UNSAFE_PATH) from None
    except PermissionError:
        raise IntakeFailure(E.PERMISSION) from None
    except OSError:
        raise IntakeFailure(E.PATH_UNAVAILABLE) from None
    return str(target)


def _portable_component(value: str) -> bool:
    # Windows normalizes trailing dots/spaces and reserves device basenames;
    # reject them even when translating a Windows remote path on Linux.
    stem = value.split('.')[0].upper()
    return (bool(value) and value == value.rstrip(' .')
            and not any(c in value for c in ':\\*?"<>|')
            and stem not in {'CON', 'PRN', 'AUX', 'NUL',
                             *(f'COM{i}' for i in range(1, 10)),
                             *(f'LPT{i}' for i in range(1, 10))})


def _remote(value: str, style: str):
    if (not value or len(value) > 4096
            or any(ord(c) < 32 for c in value)):
        raise IntakeFailure(E.PATH_MAPPING)
    if style == 'posix':
        if '\\' in value or value.startswith('//'):
            raise IntakeFailure(E.PATH_MAPPING)
        path = PurePosixPath(value)
    elif style == 'windows':
        path = PureWindowsPath(value)
        # Device namespaces and alternate data streams are not download paths.
        if value.startswith(('\\\\?\\', '\\\\.\\')) or any(':' in p for p in path.parts[1:]):
            raise IntakeFailure(E.PATH_MAPPING)
    else:
        raise IntakeFailure(E.CONFIGURATION)
    if not path.is_absolute() or '..' in path.parts:
        raise IntakeFailure(E.PATH_MAPPING)
    return path


def map_download_path(reported: str, client_id: str, instance: str,
                      mappings: Iterable[DownloaderPathMapping]) -> Tuple[str, str, str]:
    """Unique longest component-prefix match; no unmapped same-path fallback."""
    matches = []
    for mapping in mappings:
        if not mapping.enabled or (mapping.client_id, mapping.client_instance) != (client_id, instance):
            continue
        source = _remote(reported, mapping.remote_style)
        prefix = _remote(mapping.remote_prefix, mapping.remote_style)
        try:
            suffix = source.relative_to(prefix)
        except ValueError:
            continue
        # Foreign path components must also be safe under local Windows rules.
        if any(part in ('.', '..') or not _portable_component(part) for part in suffix.parts):
            raise IntakeFailure(E.UNSAFE_PATH)
        prefix_local = mapping.local_prefix or mapping.local_root
        contained(prefix_local, mapping.local_root)
        target = contained(str(Path(prefix_local).joinpath(*suffix.parts)), mapping.local_root)
        fingerprint = sha256(repr((mapping.key, mapping.client_id, mapping.client_instance,
            str(prefix), mapping.local_root, prefix_local, mapping.remote_style)).encode()).hexdigest()
        matches.append((len(prefix.parts), target, mapping.local_root, fingerprint))
    if not matches:
        raise IntakeFailure(E.PATH_MAPPING)
    longest = max(m[0] for m in matches)
    selected = [m for m in matches if m[0] == longest]
    if len(selected) != 1:
        raise IntakeFailure(E.PATH_MAPPING)
    return selected[0][1:]


def observe_artifacts(paths: Iterable[str], root: str, *, admit_payloads: bool = False) -> Tuple[ArtifactObservation, ...]:
    """Complete bounded enumeration or a typed failure, never partial success.

    Unknown/ancillary files are not comic artifacts. A zero-comic result is an
    explicit unsupported-artifact outcome. All tree entries receive link checks.
    """
    results = {}
    pending = [(contained(p, root), 0) for p in paths]
    if not 1 <= len(pending) <= MAX_ARTIFACTS:
        raise IntakeFailure(E.ENUMERATION)
    entries = 0
    seen = set()
    while pending:
        path, depth = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        entries += 1
        if entries > MAX_ENTRIES or depth > MAX_DEPTH:
            raise IntakeFailure(E.ENUMERATION)
        contained(path, root)
        try:
            info = os.lstat(path)
            if stat.S_ISDIR(info.st_mode):
                children = []
                with os.scandir(path) as directory:
                    for child in directory:
                        if len(children) + len(pending) + entries >= MAX_ENTRIES:
                            raise IntakeFailure(E.ENUMERATION)
                        children.append(child.path)
                pending.extend((p, depth + 1) for p in sorted(children, reverse=True))
            elif not stat.S_ISREG(info.st_mode):
                raise IntakeFailure(E.UNSAFE_PATH)
            elif Path(path).suffix.lower() in COMIC_EXTENSIONS or admit_payloads and _payload(path):
                if len(results) >= MAX_ARTIFACTS:
                    raise IntakeFailure(E.ENUMERATION)
                results[path] = ArtifactObservation(path, info.st_size, info.st_mtime_ns,
                                                     info.st_dev, info.st_ino)
        except PermissionError:
            raise IntakeFailure(E.PERMISSION) from None
        except OSError:
            raise IntakeFailure(E.PATH_UNAVAILABLE) from None
    if not results:
        raise IntakeFailure(E.UNSUPPORTED_ARTIFACT)
    return tuple(results[p] for p in sorted(results))


def _payload(path: str) -> bool:
    with open(path, 'rb') as stream:
        magic = stream.read(8)
    return magic.startswith((b'PK', b'Rar!\x1a\x07', b'%PDF-'))
