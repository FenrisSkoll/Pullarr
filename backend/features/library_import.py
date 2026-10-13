# -*- coding: utf-8 -*-

import os
import sqlite3
from asyncio import run
from contextlib import closing
from fnmatch import fnmatchcase
from glob import glob
from hashlib import sha256
from itertools import chain
from os.path import abspath, basename, dirname, isfile, splitext
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union
from uuid import uuid4

from backend.base.acquisition_intake import IntakeFailure
from backend.base.custom_exceptions import InvalidKeyValue
from backend.base.definitions import (FileConstants, FilenameData,
                                      SpecialVersion)
from backend.base.file_extraction import extract_filename_data
from backend.base.files import folder_is_inside_folder, list_files
from backend.base.import_candidate import (DiscoveryKind, DiscoveryScope,
                                           ImportCandidate, ResourceKind)
from backend.base.logging import LOGGER
from backend.implementations.comicinfo_candidate import enrich_comicinfo
from backend.implementations.comicvine import ComicVine
from backend.implementations.import_candidates import (
    legacy_filename_inputs, observe_import_candidate)
from backend.implementations.matching import match_special_version
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.implementations.metadata.registry import get_volume_provider
from backend.implementations.root_folders import RootFolders
from backend.internals.db_models import FilesDB


def scan_library_import(database: str, *, offset: int = 0, page_size: int = 100,
                        folder_limit: int = 50000, folder_filter: str | None = None) -> dict:
    """Deterministic filesystem discovery, independent of metadata providers.

    A pattern without a separator matches directory basenames. An absolute
    pattern matches complete directory paths. Traversal never follows links.
    The bound is explicit in the response; no truncated page claims completion.
    """
    if (type(offset) is not int or offset < 0 or type(page_size) is not int
            or not 1 <= page_size <= 100 or type(folder_limit) is not int
            or not 1 <= folder_limit <= 50000 or folder_filter is not None
            and (not isinstance(folder_filter, str) or len(folder_filter) > 500  # type: ignore[redundant-expr]
                 or '..' in Path(folder_filter).parts)):
        raise ValueError('invalid_scan_parameters')
    with closing(sqlite3.connect(database)) as db:
        roots = [Path(row[0]) for row in db.execute('SELECT folder FROM root_folders ORDER BY folder')]
    if folder_filter and (os.path.isabs(folder_filter) or '/' in folder_filter or '\\' in folder_filter):
        if not os.path.isabs(folder_filter) or not any(
                Path(root) in Path(folder_filter).parents or Path(root) == Path(folder_filter)
                for root in roots):
            raise ValueError('unsafe_folder_filter')
    folders, invalid = [], 0
    visited = 0
    bounded = False
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            invalid += 1
            continue
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            if len(Path(current).relative_to(root).parts) >= 32:
                bounded = True
                dirs.clear()
            visited += 1 + len(dirs) + len(files)
            if visited > 50000:
                bounded = True
                dirs.clear()
                break
            admitted_dirs = sorted((d for d in dirs if not (Path(current) / d).is_symlink()),
                                   key=str.casefold)
            invalid += len(dirs) - len(admitted_dirs)
            dirs[:] = admitted_dirs
            invalid += len(files) - len([f for f in files if not (Path(current) / f).is_symlink()])
            folders.append((str(Path(current)), tuple(sorted(files, key=str.casefold)), str(root)))
        if bounded:
            break
    def selected(path):
        if not folder_filter:
            return True
        pattern = folder_filter.casefold()
        current = Path(path)
        while True:
            value = str(current).casefold() if os.path.isabs(folder_filter) or '/' in folder_filter or '\\' in folder_filter else current.name.casefold()
            if fnmatchcase(value, pattern):
                return True
            if any(current == root for root in roots) or current.parent == current:
                return False
            current = current.parent
    eligible_folders = [(path, files, root) for path, files, root in folders if selected(path)]
    chosen = eligible_folders[:folder_limit]
    available, already, skipped = [], 0, 0
    extensions = {'.cbr', '.cbz'}
    with closing(sqlite3.connect(database)) as lookup:
        for folder, files, root in chosen:
            for name in files:
                path = str(Path(folder) / name)
                if Path(name).suffix.casefold() not in extensions:
                    skipped += 1
                    continue
                try:
                    from backend.implementations.acquisition_paths import \
                        contained
                    contained(path, root)
                    info = os.stat(path, follow_symlinks=False)
                    if not os.path.isfile(path):
                        invalid += 1
                        continue
                except (OSError, ValueError, IntakeFailure):
                    invalid += 1
                    continue
                if lookup.execute('SELECT 1 FROM files WHERE filepath=? LIMIT 1', (path,)).fetchone():
                    already += 1
                    continue
                stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                available.append((path, root, stamp))
    available.sort(key=lambda row: row[0].casefold())
    items = []
    for path, root, stamp in available[offset:offset + page_size]:
        try:
            parsed = extract_filename_data(path, prefer_folder_year=True)
            evidence = dict(year=parsed.get('year'), issue_number=parsed.get('issue_number'),
                            volume_number=parsed.get('volume_number'), title=parsed.get('series'))
        except (ValueError, IndexError):
            evidence = {}
        embedded = None
        embedded_source = None
        warnings = []
        try:
            candidate = enrich_comicinfo(observe_import_candidate(
                path, DiscoveryScope('library-import-scan', root)))
            document = candidate.comicinfo.document
            if document:
                volume_claims = {(claim.reference.provider, claim.reference.provider_id)
                                 for claim in candidate.claims
                                 if claim.reference.kind == ResourceKind.VOLUME}
                if len(volume_claims) == 1:
                    provider, provider_id = next(iter(volume_claims))
                    if provider in ('comicvine', 'metron', 'gcd'):
                        embedded_source = dict(provider=provider, id=provider_id)
                embedded = dict(series=(document.series or '')[:160],
                    number=(document.number or '')[:80],
                    title=(document.title or '')[:160],
                    year=document.date.year,
                    references=[dict(provider=claim.reference.provider,
                                     kind=claim.reference.kind.value,
                                     id=claim.reference.provider_id)
                                for claim in candidate.claims
                                if claim.reference.kind in (ResourceKind.VOLUME, ResourceKind.ISSUE)][:20])
            if candidate.comicinfo.diagnostics:
                warnings.append('embedded_metadata_needs_review')
        except Exception:
            # Archive inspection is optional evidence; local discovery survives it.
            warnings.append('embedded_metadata_unavailable')
        items.append(dict(row_id=sha256(repr((path, stamp)).encode()).hexdigest(),
                          filepath=path, file_title=splitext(basename(path))[0],
                          folder=str(Path(path).parent), size=stamp[2], extension=Path(path).suffix,
                          stamp=[str(value) for value in stamp], parsed=evidence,
                          embedded=embedded, warnings=warnings,
                          metadata_source=embedded_source,
                          cv=dict(id=None, title=None, issue_count=None, link=None)))
    return dict(version=2, items=items, counts=dict(scanned_folders=len(chosen),
        eligible_folders=len(eligible_folders), not_yet_scanned_folders=max(0, len(eligible_folders)-len(chosen)),
        discovered=len(available), already_managed=already, matched=0,
        unmatched=len(available),
        invalid=invalid, skipped=skipped, limited=bounded),
        next_offset=offset + len(items) if offset + len(items) < len(available) else None,
        enumeration_complete=not bounded and len(chosen) == len(eligible_folders))


def normalize_import_matches(matches: Any) -> List[Tuple[ProviderVolumeIdentity, str]]:
    """Bare legacy id means ComicVine only; reject partial/ambiguous identities."""
    if not isinstance(matches, list):
        raise InvalidKeyValue('matches', 'Expected a list')
    result = []
    validated = set()
    for match in matches:
        if not isinstance(match, dict) or not isinstance(match.get('filepath'), str) or not match['filepath']:
            raise InvalidKeyValue('matches', 'Expected a filepath')
        data: Dict[str, Any] = dict(match)
        if 'provider' in data or 'provider_id' in data:
            if 'id' in data or not isinstance(data.get('provider'), str):
                raise InvalidKeyValue('matches', 'Use one complete provider identity')
            provider, provider_id = data['provider'], data.get('provider_id')
            if not isinstance(provider_id, str) or not provider_id:
                raise InvalidKeyValue('provider_id', 'Expected a nonempty string')
        else:
            legacy = data.get('id')
            if type(legacy) is not int and not (isinstance(legacy, str) and legacy.lstrip('-+').isdigit()):
                raise InvalidKeyValue('id', 'Expected a ComicVine integer')
            provider, provider_id = 'comicvine', str(int(legacy))
        if provider == 'comicvine':
            try:
                provider_id = str(int(provider_id))
            except ValueError:
                raise InvalidKeyValue('provider_id', 'Expected a ComicVine integer') from None
        if provider not in validated:
            try:
                get_volume_provider(provider)
            except KeyError:
                raise InvalidKeyValue('provider', provider) from None
            except TypeError:
                raise MetadataProviderError(provider, 'unsupported_capability') from None
            validated.add(provider)
        result.append((ProviderVolumeIdentity(provider, provider_id), match['filepath']))
    return result


def _group_matches(
    file_data: FilenameData,
    group_files: Dict[str, FilenameData]
) -> bool:
    """Check whether a file should be grouped with a set of existing files."""
    reference = next(iter(group_files.values()))
    if (
        file_data['series'] != reference['series']
        or file_data['annual'] != reference['annual']
        or not match_special_version(
            reference['special_version'],
            file_data['special_version'],
            reference['series']
        )
    ):
        return False

    years = {
        group_file_data['year']
        for group_file_data in group_files.values()
        if group_file_data['year'] is not None
    }

    if (
        years
        and file_data['year'] is not None
        and not min(years) - 1 <= file_data['year'] <= max(years) + 1
    ):
        return False

    if file_data['special_version'] in (
        SpecialVersion.METADATA, SpecialVersion.COVER
    ):
        # Don't check volume/issue number match for volume files
        # so short circuit
        return True

    only_volume_numbers = all(
        group_file_data['issue_number'] is None
        for group_file_data in group_files.values()
    )

    return only_volume_numbers == (file_data['issue_number'] is None)


def create_groups(
    files: Dict[str, FilenameData]
) -> Dict[int, Dict[str, FilenameData]]:
    """Group files together that seem like they are for the same volume.

    Args:
        files (Dict[str, FilenameData]): The files in the form of a mapping from
            their filename to their filename data.

    Returns:
        Dict[int, Dict[str, FilenameData]]: A mapping from the group number
            (which doesn't cary any meaning except for identifying the group)
            to the files that are in the group, where the files are in the form
            of a mapping from the filename to their filename data.
    """
    groups: Dict[int, Dict[str, FilenameData]] = {}

    for file, file_data in files.items():
        for group_files in groups.values():
            if _group_matches(file_data, group_files):
                group_files[file] = file_data
                break
        else:
            new_group_number = max(groups or (0,)) + 1
            groups.setdefault(new_group_number, {})[file] = file_data

    # Convert VAI groups to issue groups
    for group in groups.values():
        if any(
            group_file_data['issue_number'] is not None
            for group_file_data in group.values()
        ):
            continue

        for group_file_data in group.values():
            group_file_data['issue_number'] = group_file_data['volume_number']
            group_file_data['volume_number'] = None
            group_file_data['special_version'] = SpecialVersion.NORMAL.value

    LOGGER.debug('File groupings: %s', groups)
    return groups


def propose_library_import(
    folder_filter: Union[str, None] = None,
    limit: int = 20,
    limit_parent_folder: bool = False,
    only_english: bool = True,
    auto_match: bool = True
) -> List[Dict[str, Any]]:
    """Get list of unimported files and their suggestion for a matching volume
    on CV.

    Args:
        folder_filter (Union[str, None], optional): Only scan the folders that
            match the given value. Can either be a folder or a glob pattern.
            Defaults to None.

        limit (int, optional): The max amount of folders to scan.
            Defaults to 20.

        limit_parent_folder (bool, optional): Base the folder limit on parent
            folder, not folder. Useful if each issue has their own sub-folder.
            Defaults to False.

        only_english (bool, optional): Only match with english releases.
            Defaults to True.

    Raises:
        InvalidKeyValue: The file filter matches to folders outside
            the root folders.
        InvalidKeyValue: The API key of the metadata source is invalid.

    Returns:
        List[Dict[str, Any]]: The list of files and their matches.
    """
    LOGGER.info('Loading library import')

    # Get all files in all root folders (with filter applied if given)
    root_folders = {
        abspath(r)
        for r in RootFolders().get_folder_list()
    }

    if folder_filter:
        scan_folders = set((
            f
            for f in glob(folder_filter, recursive=True)
            if not isfile(f) # Glob pattern could match to a file
        ))
        for f in scan_folders:
            if not any(folder_is_inside_folder(r, f) for r in root_folders):
                # Folder is not inside a root folder
                raise InvalidKeyValue('folder_filter', folder_filter)
    else:
        scan_folders = root_folders.copy()

    try:
        all_files = chain.from_iterable(
            list_files(f, FileConstants.CONTENT_EXTENSIONS)
            for f in scan_folders
        )

    except NotADirectoryError:
        raise InvalidKeyValue('folder_filter', folder_filter)

    # Get imported files
    imported_files = {
        f["filepath"]
        for f in FilesDB.fetch()
    }

    # Filter away imported files and apply limit
    folders = set()
    image_folders = set()
    candidates: Dict[str, ImportCandidate] = {}
    run_id = uuid4().hex
    for f in all_files:
        if f in imported_files:
            continue

        d = abspath(dirname(f))
        if d in root_folders:
            # File directly in root folder is not allowed
            continue

        file_data = extract_filename_data(f, prefer_folder_year=True)
        candidate = observe_import_candidate(
            f, DiscoveryScope(run_id, next((r for r in sorted(root_folders)
                                           if folder_is_inside_folder(r, f)), None),
                              manual_only=not auto_match,
                              kind=DiscoveryKind.LIBRARY_SCAN),
            parsed=file_data)

        if (
            f.endswith(FileConstants.IMAGE_EXTENSIONS)
            and file_data["special_version"] != SpecialVersion.COVER
        ):
            if d in image_folders:
                continue
            image_folders.add(d)
            d, f = dirname(d), d

        folders.add(
            dirname(d)
            if limit_parent_folder else
            d
        )

        if len(folders) > limit:
            break

        candidates[f] = candidate

    # Sort by filename
    candidates = {
        f: d
        for f, d in sorted(
            candidates.items(),
            key=lambda e: e[0].lower()
        )
    }

    # Find a match for the groups on CV
    group_to_files = create_groups(legacy_filename_inputs(candidates))
    group_to_cv = run(ComicVine().filenames_to_cvs(
        group_to_files,
        only_english=only_english
    )) if auto_match else {
        group: {'id': None, 'title': None, 'issue_count': None, 'link': None}
        for group in group_to_files
    }

    # Build result
    result = [
        {
            'filepath': file,
            'file_title': (
                splitext(basename(file))[0]
                if isfile(file) else
                basename(file)
            ),
            'cv': group_to_cv[group_number],
            'metadata_source': {
                'provider': 'comicvine', 'id': str(group_to_cv[group_number]['id'])
            } if group_to_cv[group_number]['id'] is not None else None,
            'group_number': group_number
        }
        for group_number, files in group_to_files.items()
        for file in files
    ]

    return result


def import_library(matches: object, rename_files: bool = False) -> None:
    """Retired mutating entry point; callers must review exact issues in a draft."""
    raise ValueError('legacy_import_requires_review')
