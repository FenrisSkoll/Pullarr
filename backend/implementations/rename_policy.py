"""Pure ID-based filename decisions. No DB, archive, filesystem or provider IO.

Legacy template vocabulary, cleaners and classification mappings are reused;
numeric lookup is deliberately not reused as a surrogate for issue identity.
"""

import ntpath
import posixpath
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from re import findall, sub
from string import Formatter
from types import MappingProxyType
from typing import Iterable, Optional

from backend.base.definitions import (SV_TO_FULL_TERM, SV_TO_SHORT_TERM,
                                      Constants, SpecialVersion)
from backend.base.file_extraction import extract_filename_data
from backend.base.identification import LocalMatchIssue
from backend.base.import_candidate import ResourceKind
from backend.base.issue_facts import (BibliographicDate, DateKind,
                                      NumberCatalog, NumericRange,
                                      SemanticState, numeric_label)
from backend.base.rename_policy import (POLICY_ID, NamingContext, RenameBatch,
                                        RenameCatalog, RenameCode,
                                        RenameDecision, RenameDiagnostic,
                                        RenameMode, RenamePolicy, RenameStatus,
                                        RenameToken)
from backend.implementations.naming import (KEY_TO_UNKNOWN, NAMING_MAPPING,
                                            _AbsentIdentity, clean_filepath,
                                            clean_filestring)

SPECIAL = (SpecialVersion.TPB, SpecialVersion.HARD_COVER,
           SpecialVersion.ONE_SHOT, SpecialVersion.OMNIBUS)
EXTENSIONS = frozenset(('.cbz', '.zip', '.cbr', '.rar', '.cbt', '.pdf', '.epub'))


class _Unavailable(ValueError):
    def __init__(self, code: RenameCode, field: str = ''):
        self.diagnostic = RenameDiagnostic(code, field, True)


def _decimal(label: str) -> Optional[Decimal]:
    # Display and range capability are separate; no legacy suffix projection.
    return numeric_label(label)


def _label(label: str, width: int) -> str:
    return label.zfill(width) if _decimal(label) is not None else label


def build_rename_catalog(volume_id: int, issues: Iterable[LocalMatchIssue], padding: int) -> RenameCatalog:
    """One immutable label/ID index per volume and padding configuration."""
    if not 0 <= padding <= 16:
        raise ValueError('Invalid issue padding')
    rows = tuple(sorted((replace(i, references=tuple(sorted(i.references, key=repr))) for i in issues), key=lambda i: i.id))
    if len({i.id for i in rows}) != len(rows) or any(i.volume_id != volume_id for i in rows):
        raise ValueError('Invalid rename catalog identity')
    counts: dict[str, int] = {}
    for i in rows:
        label = _label(i.raw_number, padding)
        counts[label] = counts.get(label, 0) + 1
    return RenameCatalog(volume_id, MappingProxyType({i.id: i for i in rows}), MappingProxyType(counts), padding,
                         sha256(repr((volume_id, rows, padding)).encode()).hexdigest(),
                         NumberCatalog.build((i.id, i.raw_number, i.number_facts) for i in rows))


def _clean(value: str, context: NamingContext) -> str:
    result = clean_filestring(value, context.settings)
    replacement = '-' if context.settings.replace_illegal_characters else ''
    result = result.replace('/', replacement).replace('\\', replacement).replace(':', '')
    return sub(r'[\x00-\x1f\x7f]', '', result).strip(' .')


def _fields(template: str, key: str) -> set[str]:
    if len(template) > 2048 or '/' in template or '\\' in template:
        raise _Unavailable(RenameCode.TEMPLATE)
    allowed = set(NAMING_MAPPING[key].__dataclass_fields__) | {'metadata_provider', 'provider_id'}
    if key != 'file_naming_special_version':
        allowed.add('issue_provider_id')
    requested = set()
    try:
        for _, field, spec, conversion in Formatter().parse(template):
            if field is None:
                continue
            if (field not in allowed or '{' in spec or '}' in spec or len(spec) > 16
                    or any(int(n) > 256 for n in findall(r'\d+', spec))
                    or conversion not in (None, 's', 'r', 'a')):
                raise _Unavailable(RenameCode.TEMPLATE, field)
            requested.add(field)
    except ValueError as error:
        if isinstance(error, _Unavailable):
            raise
        raise _Unavailable(RenameCode.TEMPLATE) from None
    return requested


def _date_value(raw: Optional[str], field: str) -> object:
    if not raw:
        return None
    # Full-date tokens require a real day; year tokens only require an anchored
    # valid partial date. Zero placeholders are not made into calendar days.
    evidence = BibliographicDate.interpret(raw, DateKind.LEGACY_SELECTED,
        'legacy_mapped', 'date', zero_placeholders=True)
    if field == 'issue_release_date' and evidence.exact_day is not None:
        return evidence.exact_day.isoformat()
    if field != 'issue_release_date' and evidence.year is not None:
        return evidence.year
    raise _Unavailable(RenameCode.DATE, field)


def decide_rename(context: NamingContext, policy: RenamePolicy = RenamePolicy()) -> RenameDecision:
    """Calculate one basename from already identified, canonical local facts."""
    c = context
    v, settings = c.publication, c.settings
    issues = tuple(sorted((replace(i, identity=replace(i.identity, references=tuple(sorted(i.identity.references, key=repr)))) for i in c.issues), key=lambda i: i.identity.id))
    universe = c.catalog.issues
    paths = ntpath if c.windows else posixpath
    extension = paths.splitext(c.current_filename)[1]
    # References are sets of qualified identities, not database row order.
    normalized_volume = replace(v, references=tuple(sorted(v.references, key=repr)), aliases=tuple(sorted(v.aliases)))
    fingerprint = sha256(repr((POLICY_ID, normalized_volume, issues, c.catalog.fingerprint, settings, c.current_filename,
                              c.comicvine_id, c.target_folder, c.windows, c.case_sensitive, c.max_path_length, policy)).encode()).hexdigest()
    decision = RenameDecision(c.current_filename, None, None, None, extension,
                              tuple(i.identity.id for i in issues), tuple(i.identity.raw_number for i in issues),
                              v.authority, None, policy.mode, RenameStatus.BLOCKED, fingerprint)
    diagnostics = []
    reasons = []
    try:
        if (not c.current_filename or paths.basename(c.current_filename) != c.current_filename
                or '/' in c.current_filename or '\\' in c.current_filename):
            raise _Unavailable(RenameCode.PATH)
        if policy.mode == RenameMode.PRESERVE_EXISTING:
            target = c.current_filename
            body = paths.splitext(target)[0]
            decision = replace(decision, raw_basename=body, safe_basename=body)
            reasons.append('explicitly_preserve_current_filename')
        else:
            if extension.lower() not in EXTENSIONS:
                raise _Unavailable(RenameCode.FORMAT)
            if (v.authority.kind != ResourceKind.VOLUME or not issues
                    or len({i.identity.id for i in issues}) != len(issues)
                    or c.catalog.volume_id != v.id or c.catalog.padding != settings.issue_padding
                    or any(i.identity.volume_id != v.id or universe.get(i.identity.id) != i.identity for i in issues)):
                raise _Unavailable(RenameCode.IDENTITY)
            if not 0 <= settings.issue_padding <= 16 or not 0 <= settings.volume_padding <= 16:
                raise _Unavailable(RenameCode.TEMPLATE, 'padding')
            special = v.special_version in SPECIAL
            if special and (len(issues) != 1 or len(universe) != 1):
                raise _Unavailable(RenameCode.COVERAGE, 'whole_publication_requires_sole_issue')
            number = None
            expected_projection = None
            if not special:
                if len(issues) == 1:
                    raw = issues[0].identity.raw_number
                    if not raw.strip():
                        raise _Unavailable(RenameCode.MISSING, 'issue_number')
                    number = _label(raw, settings.issue_padding)
                    numeric = _decimal(raw)
                    if numeric is None:
                        diagnostics.append(RenameDiagnostic(RenameCode.RAW_LABEL, 'issue_number'))
                        if _clean(raw, c) != raw:
                            diagnostics.append(RenameDiagnostic(RenameCode.LABEL_CLEANED, 'issue_number'))
                    else:
                        expected_projection = float(numeric)
                else:
                    numbers = [_decimal(i.identity.raw_number) for i in issues]
                    if any(n is None for n in numbers):
                        raise _Unavailable(RenameCode.COVERAGE, 'opaque_issue_set')
                    ordered = sorted(issues, key=lambda i: Decimal(i.identity.raw_number))
                    low, high = Decimal(ordered[0].identity.raw_number), Decimal(ordered[-1].identity.raw_number)
                    members = c.catalog.numbers.range_members(v.id, NumericRange(v.id, low, high))
                    if (members.state != SemanticState.SUPPORTED or len(set(numbers)) != len(numbers)
                            or set(members.issue_ids) != {i.identity.id for i in issues}
                            or any(_decimal(i.raw_number) is None and i.calculated_number is not None
                                   and float(low) <= i.calculated_number <= float(high) for i in universe.values())
                            or any(Decimal(b.identity.raw_number) - Decimal(a.identity.raw_number) > 1 for a, b in zip(ordered, ordered[1:]))):
                        raise _Unavailable(RenameCode.COVERAGE, 'noncontiguous_or_ambiguous_range')
                    number = ' - '.join(_label(i.identity.raw_number, settings.issue_padding) for i in (ordered[0], ordered[-1]))
                    expected_projection = (float(low), float(high))
            single = issues[0] if len(issues) == 1 else None
            # Ranges have no single title, date or issue provider identity.
            title = single.title if single else None
            key = ('file_naming_special_version' if special else 'file_naming_vai'
                   if v.special_version == SpecialVersion.VOLUME_AS_ISSUE else
                   'file_naming' if title and _clean(title, c) else 'file_naming_empty')
            series = clean_filestring(v.title, settings)
            article_title = series
            for article in ('The ', 'A '):
                if series.startswith(article):
                    article_title = series[len(article):] + ', ' + article.strip()
                    break
            values = dict(series_name=v.title or None, clean_series_name=article_title or None,
                          year=v.year, publisher=v.publisher or None,
                          volume_number=str(v.volume_number).zfill(settings.volume_padding) if v.volume_number is not None else None,
                          comicvine_id=c.comicvine_id,
                          special_version=(SV_TO_FULL_TERM if settings.long_special_version else SV_TO_SHORT_TERM).get(v.special_version),
                          metadata_provider=v.authority.provider, provider_id=v.authority.provider_id,
                          issue_number=number, issue_title=title,
                          issue_comicvine_id=single.comicvine_id if single else None)
            refs = tuple(r for r in single.identity.references if r.provider == v.authority.provider and r.kind == ResourceKind.ISSUE) if single else ()
            values['issue_provider_id'] = refs[0].provider_id if len(set(refs)) == 1 else None

            def render(template_key: str):
                template = getattr(settings, template_key)
                requested = _fields(template, template_key)
                if len(issues) > 1 and requested.intersection(('issue_provider_id', 'issue_comicvine_id', 'issue_title', 'issue_release_date', 'issue_release_year')):
                    raise _Unavailable(RenameCode.COVERAGE, 'single_issue_token_for_range')
                # Distinct confirmed IDs must not collapse solely through padding.
                if single and not special and 'issue_number' in requested and c.catalog.display_counts.get(number or '', 0) > 1:
                    # Missing/duplicate reference tokens cannot disambiguate names.
                    portable_unique = ('issue_provider_id' in requested and len(refs) == 1
                                       and sum(refs[0] in i.references for i in universe.values()) == 1)
                    cv_unique = ('issue_comicvine_id' in requested and single.comicvine_id is not None
                                 and sum(any(r.provider == 'comicvine' and r.kind == ResourceKind.ISSUE
                                             and r.provider_id == str(single.comicvine_id) for r in i.references)
                                         for i in universe.values()) == 1)
                    if not portable_unique and not cv_unique:
                        raise _Unavailable(RenameCode.COLLISION, 'issue_number')
                supplied = {}
                raw_values = {}
                tokens = []
                for field in sorted(requested):
                    value = (_date_value(single.date if single else None, field)
                             if field in ('issue_release_date', 'issue_release_year') else values.get(field))
                    if value is None or (isinstance(value, str) and not _clean(value, c) and field != 'special_version'):
                        if policy.strict or field in ('series_name', 'clean_series_name', 'volume_number', 'issue_number', 'metadata_provider', 'provider_id', 'issue_provider_id'):
                            raise _Unavailable(RenameCode.MISSING, field)
                        value = _AbsentIdentity() if field in ('comicvine_id', 'issue_comicvine_id') else KEY_TO_UNKNOWN.get(field, 'Unknown')
                        diagnostics.append(RenameDiagnostic(RenameCode.FALLBACK, field))
                    raw_values[field] = value
                    supplied[field] = _clean(value, c) if isinstance(value, str) and not isinstance(value, _AbsentIdentity) else value
                    tokens.append(RenameToken(field, str(value), 'selected_authority_issue' if field.startswith('issue_') else 'canonical_volume'))
                raw = template.format_map(raw_values)
                # Preserve established punctuation/whitespace cleanup, then close
                # separator, drive-colon and control-character holes at this boundary.
                body = _clean(clean_filepath(template.format_map(supplied), settings), c)
                return template, raw, body, tuple(tokens)

            decision = replace(decision, template=getattr(settings, key))
            template, raw, body, tokens = render(key)
            decision = replace(decision, raw_basename=raw, safe_basename=body, tokens=tokens)
            if not body or body in ('.', '..'):
                raise _Unavailable(RenameCode.PATH)
            if key == 'file_naming':
                fallback = False
                if len(body) > Constants.MAX_FILENAME_LENGTH:
                    fallback = True
                elif expected_projection is not None:
                    fallback = extract_filename_data(body)['issue_number'] != expected_projection
                if fallback:
                    alternative = render('file_naming_empty')
                    if ((len(body) > Constants.MAX_FILENAME_LENGTH and len(alternative[2]) <= Constants.MAX_FILENAME_LENGTH)
                            or (len(body) <= Constants.MAX_FILENAME_LENGTH and extract_filename_data(alternative[2])['issue_number'] == expected_projection)):
                        template, raw, body, tokens = alternative
                        diagnostics.append(RenameDiagnostic(RenameCode.TITLELESS))
            extension = extension.lower()
            target = body + extension
            decision = replace(decision, raw_basename=raw, safe_basename=body,
                               template=template, tokens=tokens, extension=extension)
            reasons.extend(('configured_template:' + key, 'selected_authority_canonical_metadata',
                            'issue_padding:' + str(settings.issue_padding), 'volume_padding:' + str(settings.volume_padding)))
            if len(issues) > 1:
                reasons.append('verified_local_coverage_range')
        if not body or body in ('.', '..') or '/' in target or '\\' in target or any(ord(ch) < 32 or ord(ch) == 127 for ch in target):
            raise _Unavailable(RenameCode.PATH)
        decision = replace(decision, target_filename=target)
        if c.windows:
            reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
            if target.split('.')[0].upper() in reserved:
                raise _Unavailable(RenameCode.RESERVED)
            if target.rstrip(' .') != target or any(ch in target for ch in '<>:"|?*'):
                raise _Unavailable(RenameCode.PATH)
        if len(target.encode('utf-8')) > 255 or (c.target_folder is not None and c.max_path_length is not None
                                                and len(paths.join(c.target_folder, target)) > c.max_path_length):
            raise _Unavailable(RenameCode.LENGTH)
        status = RenameStatus.UNCHANGED if target == c.current_filename else RenameStatus.CALCULATED
        if any(d.code == RenameCode.LABEL_CLEANED for d in diagnostics):
            status = RenameStatus.REVIEW
        if not c.case_sensitive and target != c.current_filename and target.casefold() == c.current_filename.casefold():
            status = RenameStatus.REVIEW
            diagnostics.append(RenameDiagnostic(RenameCode.CASE))
        return replace(decision, target_filename=target, status=status, reasons=tuple(reasons), diagnostics=tuple(dict.fromkeys(diagnostics)))
    except _Unavailable as error:
        diagnostics.append(error.diagnostic)
    except (ValueError, KeyError, TypeError, OverflowError):
        diagnostics.append(RenameDiagnostic(RenameCode.TEMPLATE, blocking=True))
    return replace(decision, reasons=tuple(reasons), diagnostics=tuple(dict.fromkeys(diagnostics)))


def decide_renames(contexts: Iterable[NamingContext], policy: RenamePolicy = RenamePolicy()) -> RenameBatch:
    """Bulk pure evaluation; full-path occupancy/dependencies remain in plan_many."""
    return RenameBatch(tuple(sorted((decide_rename(c, policy) for c in contexts),
                                    key=lambda d: (d.current_filename, d.fingerprint))))


def preview_rename(decision: RenameDecision) -> dict:
    return dict(current=decision.current_filename, target=decision.target_filename,
                raw_basename=decision.raw_basename, safe_basename=decision.safe_basename,
                extension=decision.extension, issue_ids=list(decision.issue_ids), labels=list(decision.raw_labels),
                authority=dict(provider=decision.authority.provider, kind=decision.authority.kind.value, id=decision.authority.provider_id),
                template=decision.template, mode=decision.mode.value, status=decision.status.value,
                policy=decision.policy_id, fingerprint=decision.fingerprint, reasons=list(decision.reasons),
                tokens=[dict(name=t.name, value=t.value, source=t.source) for t in decision.tokens],
                diagnostics=[dict(code=d.code.value, field=d.field, blocking=d.blocking) for d in decision.diagnostics])
