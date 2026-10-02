"""Explicit selected-authority updates, preservation-first merge, serialization."""

from dataclasses import dataclass
from datetime import date
from typing import Optional, Tuple
from xml.dom import minidom

from backend.base.comicinfo import (ComicInfoCode, ComicInfoDocument,
                                    ComicInfoError)
from backend.base.import_candidate import ProviderReference, ResourceKind
from backend.implementations.comicinfo import IDENTITY_NS, parse_comicinfo
from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata)

REPLACE_FIELDS = frozenset(('Series', 'Number', 'Title', 'Summary', 'Publisher',
                            'Year', 'Month', 'Day'))
FILL_FIELDS = frozenset(('Web',))


@dataclass(frozen=True)
class ComicInfoUpdates:
    authority: ProviderReference
    values: Tuple[Tuple[str, str], ...]
    identities: Tuple[ProviderReference, ...]

    def __post_init__(self) -> None:
        if self.authority.kind != ResourceKind.VOLUME or self.authority not in self.identities:
            raise ValueError('Selected volume authority must be explicit')
        names = [n for n, _ in self.values]
        if len(set(names)) != len(names) or any(n not in REPLACE_FIELDS | FILL_FIELDS for n in names):
            raise ValueError('Unsupported or duplicate update field')
        dates = {'Year', 'Month', 'Day'} & set(names)
        if dates:
            if len(dates) != 3:
                raise ValueError('Partial updates must not inherit old date precision')
            values = dict(self.values)
            date(int(values['Year']), int(values['Month']), int(values['Day']))
        for reference in self.identities:
            if (reference.kind == ResourceKind.VOLUME
                    and reference.provider == self.authority.provider and reference != self.authority):
                raise ValueError('Contradictory selected-provider volume references')


def selected_metadata_updates(volume: VolumeMetadata, issue: IssueMetadata,
                              references: Tuple[ProviderReference, ...] = ()) -> ComicInfoUpdates:
    """Single-issue metadata only. A cross-reference never supplies field values.

    Callers must select the authoritative metadata explicitly. This function
    does not resolve a file's coverage or assume a multi-issue file has one title.
    """
    if issue.provider != volume.provider or issue.volume_provider_id != volume.provider_id:
        raise ValueError('Selected metadata owner mismatch')
    authority = ProviderReference(volume.provider, ResourceKind.VOLUME, volume.provider_id)
    identities = (authority, ProviderReference(issue.provider, ResourceKind.ISSUE, issue.provider_id)) + references
    values = []
    for key, value in (('Series', volume.title), ('Number', issue.issue_number),
                       ('Title', issue.title), ('Summary', issue.description),
                       ('Publisher', volume.publisher)):
        if value is not None and value != '':
            values.append((key, value))
    if issue.date:
        try:
            parsed = date.fromisoformat(issue.date)
        except ValueError:
            raise ValueError('Authoritative date must be a complete calendar day') from None
        if parsed.isoformat() != issue.date:
            raise ValueError('Authoritative date must use YYYY-MM-DD')
        values.extend((('Year', str(parsed.year)), ('Month', str(parsed.month)), ('Day', str(parsed.day))))
    return ComicInfoUpdates(authority, tuple(values), tuple(dict.fromkeys(identities)))


def merge_comicinfo(existing: Optional[ComicInfoDocument], updates: ComicInfoUpdates) -> bytes:
    """Return deterministic XML; no IO. Missing updates are NEVER deletion.

    Preserve all unowned elements, namespace declarations, comments, attributes,
    credits and Notes. Only simple, unique owned fields can be replaced.
    Duplicate/structured owned fields or conflicting portable identities block.
    """
    if existing is not None:
        if any(d.code == ComicInfoCode.UNKNOWN_VERSION for d in existing.diagnostics):
            raise ComicInfoError(ComicInfoCode.MERGE_AMBIGUOUS)
        # Do not trust a manually constructed document to bypass XML safety.
        parse_comicinfo(existing.raw_bytes)
        document = minidom.parseString(existing.raw_bytes)
    else:
        document = minidom.parseString(b'<ComicInfo/>')
    root = document.documentElement
    for name, value in updates.values:
        found = [e for e in root.childNodes if e.nodeType == e.ELEMENT_NODE
                 and e.nodeName == name and not e.namespaceURI]
        if len(found) > 1 or (found and any(c.nodeType == c.ELEMENT_NODE for c in found[0].childNodes)):
            raise ComicInfoError(ComicInfoCode.MERGE_AMBIGUOUS)
        if found and name in FILL_FIELDS:
            continue  # Present-but-empty is still existing user content.
        element = found[0] if found else document.createElement(name)
        if not found:
            root.appendChild(element)
        # Retain element attributes and nested comments/PIs, replacing text only.
        for child in tuple(element.childNodes):
            if child.nodeType in (child.TEXT_NODE, child.CDATA_SECTION_NODE):
                element.removeChild(child)
        element.appendChild(document.createTextNode(value))
    extension = [e for e in root.childNodes if e.nodeType == e.ELEMENT_NODE
                 and e.namespaceURI == IDENTITY_NS and e.localName == 'Identity']
    # This namespace owns only these explicit fields. Unknown extension content
    # is left intact; conflicting known volume IDs require operator resolution.
    for old in extension:
        if (old.getAttribute('kind') == 'volume'
                and old.getAttribute('provider') == updates.authority.provider
                and old.getAttribute('id') != updates.authority.provider_id):
            raise ComicInfoError(ComicInfoCode.MERGE_AMBIGUOUS)
        if old.getAttribute('selected') == 'true' and (
            old.getAttribute('provider'), old.getAttribute('kind'), old.getAttribute('id')
        ) != (updates.authority.provider, 'volume', updates.authority.provider_id):
            raise ComicInfoError(ComicInfoCode.MERGE_AMBIGUOUS)
    for reference in dict.fromkeys(updates.identities):
        found = [e for e in extension if (e.getAttribute('provider'), e.getAttribute('kind'), e.getAttribute('id'))
                 == (reference.provider, reference.kind.value, reference.provider_id)]
        if len(found) > 1:
            raise ComicInfoError(ComicInfoCode.MERGE_AMBIGUOUS)
        if found:
            element = found[0]
        else:
            # Per-element namespace declaration avoids hijacking a user prefix.
            element = document.createElementNS(IDENTITY_NS, 'Identity')
            element.setAttribute('xmlns', IDENTITY_NS)
            element.setAttribute('provider', reference.provider)
            element.setAttribute('kind', reference.kind.value)
            element.setAttribute('id', reference.provider_id)
            root.appendChild(element)
        if reference == updates.authority:
            element.setAttribute('selected', 'true')
    result = document.toxml(encoding='utf-8')
    parse_comicinfo(result)
    return result
