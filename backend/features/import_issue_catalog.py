"""Read-only publication catalogue and local-file suggestions for Library Import."""

import json
import os
import re
import sqlite3
import time
from asyncio import run
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from threading import RLock

from backend.base.definitions import SpecialVersion
from backend.base.import_candidate import DiscoveryScope, ResourceKind
from backend.implementations.acquisition_paths import (contained,
                                                       observe_artifacts)
from backend.implementations.comicinfo_candidate import enrich_comicinfo
from backend.implementations.import_candidates import observe_import_candidate
from backend.implementations.metadata.enrichment import fetch_volume_result
from backend.implementations.metadata.persistence import fetch_input
from backend.implementations.metadata.registry import get_volume_provider

_cache = {}
_lock = RLock()


def cached_metadata_result(database: str, provider: str, provider_id: str):
    """Reuse the selected catalogue's provider result while it is fresh."""
    key = (os.path.abspath(database), provider, provider_id)
    with _lock:
        cached = _cache.get(key)
        return cached[2] if cached and cached[0] > time.monotonic() and len(cached) > 2 else None


def _number(value):
    """Exact decimal spelling class; never convert 6.1/6.2 through float."""
    value = str(value or '').strip().casefold().lstrip('#')
    value = re.sub(r'^(?:issue|book|part|volume|vol\.?|v)\s*', '', value)
    match = re.fullmatch(r'0*(-?\d+)(?:\.(\d+))?', value)
    return (str(int(match[1])), match[2] or '') if match else None


def _suggest(candidate, issues, provider, provider_id):
    document = candidate.comicinfo.document
    refs = [c.reference for c in candidate.claims if c.reference.kind == ResourceKind.VOLUME]
    if any(r.provider == provider and r.provider_id != provider_id for r in refs):
        return dict(issue_ids=[], confidence='none', reason='Embedded publication identity conflicts with the selected publication.')
    by_id = {i['id']: i for i in issues}
    exact = [c.reference.provider_id for c in candidate.claims
             if c.reference.kind == ResourceKind.ISSUE and c.reference.provider == provider]
    if exact:
        unique = set(exact)
        if len(unique) == 1 and next(iter(unique)) in by_id:
            issue = by_id[next(iter(unique))]
            number = document.number if document else None
            filename_number = re.search(r'(?i)(?:#|\bissue\s+)\s*(\d+(?:\.\d+)?)\b',
                                        candidate.file.raw_stem)
            if ((number and _number(number) and _number(number) != _number(issue['number']))
                    or (filename_number and _number(filename_number[1]) != _number(issue['number']))):
                return dict(issue_ids=list(unique), confidence='review_needed',
                            reason='Embedded exact issue identity conflicts with ComicInfo or filename number; review explicitly.')
            return dict(issue_ids=list(unique), confidence='exact',
                        reason=f'Embedded {provider} issue identity belongs to this publication.')
        return dict(issue_ids=[], confidence='none', reason='Embedded issue identity is conflicting or absent from this catalogue.')
    if document and document.number:
        filename_number = re.search(r'(?i)(?:#|\bissue\s+|\bbook\s+|\bpart\s+)\s*(\d+(?:\.\d+)?)\b',
                                    candidate.file.raw_stem)
        if (filename_number and _number(document.number) and
                _number(filename_number[1]) != _number(document.number)):
            return dict(issue_ids=[], confidence='review_needed',
                        reason='ComicInfo issue number conflicts with the filename; choose the reviewed issue.')
        raw = document.number.strip().casefold().lstrip('#')
        found = [i for i in issues if i['number'].strip().casefold().lstrip('#') == raw]
        if len(found) == 1:
            return dict(issue_ids=[found[0]['id']], confidence='strong',
                        reason=f'ComicInfo Number = {document.number}, unique literal issue label.')
        normalized = _number(document.number)
        found = [i for i in issues if normalized and _number(i['number']) == normalized]
        if len(found) == 1:
            return dict(issue_ids=[found[0]['id']], confidence='strong',
                        reason=f'ComicInfo Number = {document.number}, unique compatible catalogue number.')
        if found:
            return dict(issue_ids=[], confidence='review_needed', reason='ComicInfo number matches several catalogue issues.')
    stem = candidate.file.raw_stem
    volume_token = re.search(r'(?i)(?:^|[\s_-])v0*(\d+)(?:[\s_.-]|$)', stem)
    if volume_token:
        number = str(int(volume_token[1]))
        pattern = r'(?i)^\s*(?:volume|vol\.?|book|part)\s*0*(\d+)\s*$'
        titled = [(i, re.fullmatch(pattern, i['title'] or '')) for i in issues]
        if titled and all(match for _, match in titled):
            found = [i for i, match in titled if str(int(match[1])) == number]
            if len(found) == 1:
                return dict(issue_ids=[found[0]['id']], confidence='strong',
                            reason=f'v{number} matches a unique numbered volume/book title in this catalogue.')
        return dict(issue_ids=[], confidence='review_needed',
                    reason='v-number could denote a volume; catalogue does not prove issue semantics.')
    book = re.search(r'(?i)\b(book|part|vol(?:ume)?)[ ._-]*0*(\d+)\b', stem)
    if book:
        number = str(int(book[2]))
        found = [i for i in issues if re.fullmatch(
            rf'(?i)\s*{re.escape(book[1])}\.?\s*0*{number}\s*', i['title'] or '')]
        if len(found) == 1:
            return dict(issue_ids=[found[0]['id']], confidence='strong',
                        reason=f'Filename {book[1]} {number} matches a unique provider issue title.')
        if stem.casefold().find('v0') >= 0 or book[1].casefold().startswith('vol'):
            return dict(issue_ids=[], confidence='review_needed',
                        reason='Volume notation could denote a collected volume, not an issue.')
    explicit = re.search(r'(?i)(?:#|\bissue\s+)\s*(\d+(?:\.\d+)?)\b', stem)
    if not explicit:
        explicit = re.search(r'(?:^|[\s_-])(\d{3})(?:[\s_.-]|$)', stem)
    if explicit:
        normalized = _number(explicit[1])
        found = [i for i in issues if normalized and _number(i['number']) == normalized]
        if len(found) == 1:
            return dict(issue_ids=[found[0]['id']], confidence='strong',
                        reason=f'Filename number {explicit[1]} matches a unique issue in the selected catalogue.')
        if found:
            return dict(issue_ids=[], confidence='review_needed', reason='Number matches multiple provider issues.')
    return dict(issue_ids=[], confidence='review_needed', reason='No unique issue evidence; choose an issue from this catalogue.')


def issue_catalog(database: str, provider: str, provider_id: str, files: list[str]) -> dict:
    """Fetch once per selected publication; never register or associate here."""
    if (provider not in ('comicvine', 'metron', 'gcd') or not isinstance(provider_id, str)  # type: ignore[redundant-expr]
            or not re.fullmatch(r'[1-9][0-9]{0,18}', provider_id)
            or not isinstance(files, list) or len(files) > 100  # type: ignore[redundant-expr]
            or any(not isinstance(path, str) for path in files)):
        raise ValueError('invalid_catalog_request')
    with closing(sqlite3.connect(database)) as db:
        roots = [r[0] for r in db.execute('SELECT folder FROM root_folders')]
        validated = []
        for path in files:
            matches = [root for root in roots if Path(root) in Path(path).parents]
            if len(matches) != 1:
                raise ValueError('unsafe_catalog_path')
            safe = contained(path, matches[0])
            observed = observe_artifacts((safe,), matches[0])
            if len(observed) != 1 or observed[0].path != safe:
                raise ValueError('unsupported_catalog_file')
            validated.append((safe, matches[0]))
        rows = db.execute('''SELECT v.id,v.title,v.year,v.publisher,v.folder,v.volume_number,v.special_version
            FROM volumes v JOIN volume_external_ids e ON e.volume_id=v.id
            WHERE v.metadata_provider=? AND e.provider=? AND e.provider_id=?''',
            (provider, provider, provider_id)).fetchall()
        if len(rows) > 1:
            raise ValueError('ambiguous_publication_identity')
        if rows:
            volume_id, title, year, publisher, folder, volume_number, special_version = rows[0]
            issues = [dict(id=r[1], local_id=r[0], number=r[2], title=r[3], date=r[4], owned=bool(r[5]))
                      for r in db.execute('''SELECT i.id,e.provider_id,i.issue_number,i.title,i.date,
                        EXISTS(SELECT 1 FROM issues_files f WHERE f.issue_id=i.id)
                        FROM issues i JOIN issue_external_ids e ON e.issue_id=i.id AND e.provider=?
                        WHERE i.volume_id=? ORDER BY i.calculated_issue_number,i.id''', (provider, volume_id))]
            snapshot = db.execute('SELECT value FROM config WHERE key=?',
                                  ('provider_snapshot:' + str(volume_id),)).fetchone()
            try:
                expected = json.loads(snapshot[0]).get('expected_count') if snapshot else None
                count = expected if isinstance(expected, int) and expected >= 0 else None
            except (ValueError, TypeError, KeyError):
                count = None
            completeness = ('unknown' if count is None else
                            'complete' if count == len(issues) else 'partial')
        else:
            volume_id = None
            key = (os.path.abspath(database), provider, provider_id)
            with _lock:
                cached = _cache.get(key)
                if cached and cached[0] > time.monotonic():
                    title, year, publisher, volume_number, special_version, count, issues = cached[1]
                else:
                    cached = None
            if not cached:
                fetched_result = run(fetch_volume_result(get_volume_provider(provider), provider_id))
                fetched = fetched_result.metadata
                if fetched.provider != provider or fetched.provider_id != provider_id:
                    raise ValueError('publication_identity_mismatch')
                title, year, publisher, volume_number, count = (fetched.title, fetched.year,
                    fetched.publisher, fetched.volume_number, fetched.issue_count)
                fetched_issues = fetch_input(fetched_result, provider)['issues'] or []
                from backend.implementations import volumes as volume_module
                from backend.implementations.classification import (
                    ClassificationIssue, evaluate_special_version)
                special_version = evaluate_special_version(title=title, description=fetched.description,
                    issues=tuple(ClassificationIssue(i['title'], i['date']) for i in fetched_issues),
                    stored_value=SpecialVersion.NORMAL, locked=False,
                    evaluated_at=volume_module.datetime.now(),
                    format_evidence=fetched_result.format_evidence,
                    publication_evidence=fetched_result.publication_evidence).value.value
                issues = [dict(id=i['provider_id'], local_id=None, number=i['issue_number'],
                               title=i['title'], date=i['date'], owned=False)
                          for i in fetched_issues if i['provider'] == provider
                          and i['volume_provider_id'] == provider_id]
                if len({i['id'] for i in issues}) != len(issues):
                    raise ValueError('duplicate_provider_issue')
                with _lock:
                    if len(_cache) >= 16:
                        _cache.pop(next(iter(_cache)))
                    _cache[key] = (time.monotonic() + 600, (title, year, publisher,
                        volume_number, special_version, count, issues), fetched_result)
            completeness = ('unknown' if count is None else 'complete' if count == len(issues)
                            else 'partial' if issues else 'unavailable')
    proposals = {}
    for path, root in validated:
        candidate = enrich_comicinfo(observe_import_candidate(path, DiscoveryScope('catalogue', root)))
        proposals[path] = _suggest(candidate, issues, provider, provider_id)
    revision = sha256(json.dumps((provider, provider_id, volume_id, special_version, issues),
                                sort_keys=True, default=str).encode()).hexdigest()
    return dict(provider=provider, provider_id=provider_id, volume_id=volume_id,
                title=title, year=year, publisher=publisher, volume_number=volume_number,
                special_version=special_version, folder=folder if rows else None,
                reported_issue_count=count, loaded_issue_count=len(issues),
                completeness=completeness, issues=issues, proposals=proposals, revision=revision)
