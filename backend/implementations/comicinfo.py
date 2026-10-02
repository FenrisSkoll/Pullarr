"""Bounded, network-independent XML parsing and explicit preservation-first merge."""

import re
from datetime import date
from typing import Optional, Tuple
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from xml.parsers import expat

from backend.base.comicinfo import (ComicInfoCode, ComicInfoDate,
                                    ComicInfoDiagnostic, ComicInfoDocument,
                                    ComicInfoError, DatePrecision, XmlField)
from backend.base.import_candidate import (ClaimRole, EvidenceSource,
                                           Provenance, ProviderIdentityClaim,
                                           ProviderReference, ResourceKind)

MAX_XML = 1024 * 1024
MAX_ELEMENTS = 10000
MAX_DEPTH = 64
IDENTITY_NS = 'https://kapowarr.org/ns/comicinfo/1'
IDENTITY_TAG = '{' + IDENTITY_NS + '}Identity'
SCALARS = frozenset(('Title Series Number Count Volume AlternateSeries AlternateNumber '
                     'AlternateCount Summary Notes Year Month Day Writer Penciller Inker '
                     'Colorist Letterer CoverArtist Editor Publisher Imprint Genre Tags Web '
                     'PageCount LanguageISO Format AgeRating Characters Teams Locations '
                     'StoryArc SeriesGroup ScanInformation GTIN').split())


def _element_name(tag: object) -> bool:
    return isinstance(tag, str)


def _tree(raw: bytes, expected_root: Optional[str] = 'ComicInfo') -> ET.Element:
    if len(raw) > MAX_XML:
        raise ComicInfoError(ComicInfoCode.LIMIT_EXCEEDED)
    # Expat sees declarations regardless of UTF-8/16 encoding. No regex-based
    # DTD filter that can be bypassed with a different byte encoding.
    guard = expat.ParserCreate()
    depth = 0
    count = 0

    def reject(*args: object) -> None:
        raise ComicInfoError(ComicInfoCode.XML_UNSAFE)

    def start(name: str, attrs: object) -> None:
        nonlocal depth, count
        depth += 1
        count += 1
        if depth > MAX_DEPTH or count > MAX_ELEMENTS:
            raise ComicInfoError(ComicInfoCode.LIMIT_EXCEEDED)

    def end(name: str) -> None:
        nonlocal depth
        depth -= 1

    guard.StartDoctypeDeclHandler = reject
    guard.EntityDeclHandler = reject
    guard.StartElementHandler = start
    guard.EndElementHandler = end
    try:
        guard.Parse(raw, True)
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        root = ET.fromstring(raw, parser=parser)
    except (expat.ExpatError, ET.ParseError, LookupError, ValueError):
        raise ComicInfoError(ComicInfoCode.XML_MALFORMED) from None
    if expected_root is not None and root.tag != expected_root:
        raise ComicInfoError(ComicInfoCode.UNSUPPORTED_ROOT)
    return root


def parse_comicinfo(raw: bytes) -> ComicInfoDocument:
    root = _tree(raw)
    fields = tuple(XmlField(e.tag, e.text, tuple(e.attrib.items()), bool(len(e)))
                   for e in root if _element_name(e.tag))
    diagnostics = []
    for field in fields:
        if field.name == IDENTITY_TAG:
            attrs = dict(field.attributes)
            try:
                ProviderReference(attrs['provider'], ResourceKind(attrs['kind']), attrs['id'])
            except (KeyError, ValueError):
                diagnostics.append(ComicInfoDiagnostic(ComicInfoCode.INVALID_FIELD, IDENTITY_TAG))
    values = {}
    for name in SCALARS:
        found = [f for f in fields if f.name == name]
        if len(found) > 1:
            diagnostics.append(ComicInfoDiagnostic(ComicInfoCode.DUPLICATE_FIELD, name))
        elif found and found[0].structured:
            diagnostics.append(ComicInfoDiagnostic(ComicInfoCode.INVALID_FIELD, name))
        elif found:
            values[name] = found[0].text or ''
    components = {}
    invalid_date = False
    for name, maximum in (('Year', 9999), ('Month', 12), ('Day', 31),
                          ('Count', 2147483647), ('Volume', 2147483647),
                          ('AlternateCount', 2147483647), ('PageCount', 2147483647)):
        raw_value = values.get(name)
        value = None
        invalid = False
        if raw_value is not None and raw_value.strip() not in ('', '-1'):
            text = raw_value.strip()
            if re.fullmatch(r'[0-9]{1,10}', text):
                value = int(text)
                invalid = not (0 <= value <= maximum)
                if name in ('Year', 'Month', 'Day') and value == 0:
                    invalid = True
            else:
                invalid = True
        if invalid:
            diagnostics.append(ComicInfoDiagnostic(ComicInfoCode.INVALID_FIELD, name))
            value = None
        if name in ('Year', 'Month', 'Day'):
            components[name] = value
            invalid_date |= invalid or any(d.field == name for d in diagnostics)
    year, month, day = (components[n] for n in ('Year', 'Month', 'Day'))
    if (month is not None and year is None) or (day is not None and (year is None or month is None)):
        invalid_date = True
    if year is not None and month is not None and day is not None:
        try:
            date(year, month, day)
        except ValueError:
            invalid_date = True
    precision = (DatePrecision.INVALID if invalid_date else DatePrecision.DAY if day is not None
                 else DatePrecision.MONTH if month is not None else DatePrecision.YEAR
                 if year is not None else DatePrecision.UNKNOWN)
    if invalid_date:
        diagnostics.append(ComicInfoDiagnostic(ComicInfoCode.INVALID_FIELD, 'date'))
    version = root.get('version')
    if version is not None and version not in ('1.0', '2.0', '2.1'):
        diagnostics.append(ComicInfoDiagnostic(ComicInfoCode.UNKNOWN_VERSION, 'version'))
    # Encoding is informational, not used to override the XML parser.
    declaration = re.match(br'\s*<\?xml[^>]*encoding=[\'"]([^\'"]+)', raw)
    encoding = declaration[1].decode('ascii', errors='replace') if declaration else None
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        encoding = 'UTF-16 BOM'
    return ComicInfoDocument(raw, fields, ComicInfoDate(year, month, day, precision),
                             tuple(sorted(diagnostics, key=lambda d: (d.code.value, d.field or ''))), encoding)


def provider_url(url: str) -> Optional[ProviderReference]:
    """Exact public URL grammars, never title/Notes guessing or network lookup."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or parsed.username or parsed.password
                or parsed.port is not None or parsed.query or parsed.fragment):
            return None
    except ValueError:
        return None
    host, path = parsed.hostname, parsed.path
    if host in ('comicvine.gamespot.com', 'www.comicvine.gamespot.com'):
        match = re.fullmatch(r'/(?:[^/]+/)?(4050|4000)-([0-9]+)/?', path)
        if match:
            return ProviderReference('comicvine', ResourceKind.VOLUME if match[1] == '4050'
                                     else ResourceKind.ISSUE, match[2])
    for provider, hosts, volume_path in (
        ('metron', ('metron.cloud', 'www.metron.cloud'), 'series'),
        ('gcd', ('comics.org', 'www.comics.org'), 'series')
    ):
        match = re.fullmatch(r'/(series|issue)/([0-9]+)/?', path)
        if host in hosts and match:
            return ProviderReference(provider, ResourceKind.VOLUME if match[1] == volume_path
                                     else ResourceKind.ISSUE, match[2])
    return None


def comicinfo_claims(document: ComicInfoDocument, origin: Provenance) -> Tuple[ProviderIdentityClaim, ...]:
    claims = []
    for field in document.fields:
        references = []
        if field.name == 'Web' and not field.structured:
            references = [r for url in (field.text or '').split() if (r := provider_url(url)) is not None]
        elif field.name == IDENTITY_TAG:
            attrs = dict(field.attributes)
            try:
                references = [ProviderReference(attrs['provider'], ResourceKind(attrs['kind']), attrs['id'])]
            except (KeyError, ValueError):
                continue
        for reference in references:
            claim = ProviderIdentityClaim(reference, ClaimRole.EMBEDDED,
                                          Provenance(EvidenceSource.COMICINFO,
                                                     origin.locator + '/' + field.name,
                                                     origin.policy, origin.observed_at))
            if claim not in claims:
                claims.append(claim)
    return tuple(claims)
