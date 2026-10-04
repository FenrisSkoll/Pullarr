# Release notes

## 1.5.0 — library and acquisition workflow reliability

- Stream CBR → CBZ conversion and exact member verification without page, member,
  expanded-size, pixel or dimension admission ceilings. Preserve page and metadata
  payloads, member order, containment, exclusive publication and shared seed bytes.
- Scope archive workspaces to their journal owners. Recover proven interrupted
  operations at startup under one durable executor claim; ambiguous/orphan evidence
  remains for inspection. Successful conversion cleans its recovery artifacts;
  healthy CBZs are never automatically repacked. Improve recovery focus and status.
- Honor explicit Library Import publication selections and managed-volume scan
  authority while retaining issue evidence and conflict checks. Adopt one existing
  immediate-parent folder atomically with new registration, preserve existing
  volume folders, and retain ready rows alongside issue-level review.
- Replace native import/local-scan dialogs with inline outcomes and a read-only
  local scan preview followed by explicit Apply. Handle unestablished legacy
  folders without an uncaught volume-page error.
- Restrict automatic grabs to acceptable GetComics candidates, with no NZB/torrent
  fallback. Resolve manual clients independently, offer explicit client selection,
  and support confirmed bibliographic overrides for NZB and torrent downloads
  while retaining original evaluations, exact targets and operational gates.
- Add manual source sorting/filtering, readable sizes and source/query provenance.
  Use advertised comic/book categories and bounded canonical issue-title queries;
  normalize harmless punctuation without discarding meaningful publication words.
- No database migration (schema72). Large archives can consume substantial host
  resources; processing buffers and worker concurrency remain bounded. The Unraid
  CA template continues to follow `ghcr.io/fenrisskoll/pullarr:latest`.

## 1.4.0 — metadata search artwork and relationships

- Present ComicVine search descriptions as readable, bounded plain text; stored
  library descriptions are unchanged.
- Load GCD and Metron search artwork lazily for visible cards, with bounded
  provider requests, validated image thumbnails and an identity-keyed cache.
- Show explicit ComicVine predecessor/continuation links and discover related
  volumes through bounded, one-hop exact-ID lookups. Related publications remain
  independently addable provider-qualified identities.
- Preserve Metron's detail-only associated-series evidence as undirected related
  series, without inferring continuation or identity equivalence.
- Rank direct title/alias matches ahead of related results, retaining provider
  groups and local-library annotations. Library Import keeps direct search only.
- No database/schema migration (schema72). The Unraid CA template continues to
  follow `ghcr.io/fenrisskoll/pullarr:latest`.

## 1.3.3 — qBittorrent 5.2 compatibility

- Support qBittorrent 5.2 / WebAPI 2.15, including 204 no-content login and
  removal responses, while retaining earlier supported 5.x behaviour.
- Preserve current port-qualified and legacy session cookies, including upstream
  base64 session values. HTTP success statuses remain explicitly endpoint-scoped.
- Validate modern torrent-add receipts and resolve pending submissions with bounded
  reads, preserving exact hashes, candidate tags and ambiguous-outcome safeguards.
- Show bounded managed-client failure codes instead of hiding structured API
  failures behind a generic unavailable message.
- No database/schema migration; schema72, retention and reviewed cleanup gates
  are unchanged. The Unraid template continues to follow the stable `latest` image.

## 1.3.2 — first public release

Unraid Community Applications metadata, icon, validation and materialization
tooling are included alongside the application source. CA listing/approval is
still pending official Validate/Scan and review.

The application retains its inherited version 1.3.2 and schema72; the deliberate
release tag is v1.3.2. This snapshot includes the completed provider abstraction,
canonical identity, reviewed import/organization, monitoring, Collections,
Calendar, Reading Orders/CBL, Quality Profiles/upgrades/provenance, Discover,
Pullarr UI, expanded clients/indexers and archive-maintenance programmes.

First-publication CI corrected narrow Linux form overflow and a Windows CPython
path-versus-descriptor timestamp mismatch during health/duplicate hashing.
Identity checks and descriptor change detection remain enforced. Windows CI uses
runner scratch storage for disposable fixtures; no durability checks are disabled.

Publication hardening removes proprietary trial RAR executables from the public
source and image in favor of separately licensed UnRAR readers. Legacy RAR
creation requires the user's own licensed tool. Synthetic archive fixtures no
longer need an archive writer during tests. Unclear-provenance unused artwork
is excluded and UI icons use original geometric drawings.

Generated API keys and setting values are no longer logged. Fresh native startup
binds to loopback; Docker's explicit internal bind remains behind a loopback-only
published port. Existing configured hosts/settings are preserved. Public CI,
upload gates, third-party notices and clean-export checks are provided.

Back up persistent data before upgrades. No schema migration or automatic archive
scan is introduced by publication hardening. No PDF/page-editor features added.
