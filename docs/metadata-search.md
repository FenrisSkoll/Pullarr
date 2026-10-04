# Metadata search

Add Comics keeps provider groups and canonical `comicvine:`, `metron:` and `gcd:`
identities. Descriptions are plain text at the search presentation boundary;
stored library descriptions are not rewritten. Related publications are separate
volumes, never automatic merges, provider switches or shared issue lists.

ComicVine description links explicitly introduced by “preceded by”, “continued
from”, “continued in” or “continues as” become predecessor/continuation evidence.
Only recognized ComicVine hosts and exact `4050-<id>` volume paths are admitted.
The volume-number inference and relationship extraction share the same description
module. Unlinked prose can retain its legacy numbering inference but cannot create
a relationship. No title/numbering similarity establishes identity or continuation.

Search expands at most two targets per direct result and four targets overall,
at depth one, within the same provider. Exact IDs already returned are not fetched
again; failures retain primary results. Title punctuation/whitespace normalization
is used locally for ranking, without removing words or rewriting provider queries.
The ordering is exact title/alias, other direct token matches, explicit related
results, then direct results with no query tokens. Token overlap and optional year
break ties; provider order is retained for equivalent candidates. Publisher,
volume number and issue count remain visible facts, not inferred query constraints.

`metadata-search/v2` adds `relations`, `search_origin`, `relation_reason` and
`rank_components`. Add Comics requests `presentation=v2&artwork=true`. Library
Import requests `expand_relations=false` and does not request artwork. Selecting
any candidate always uses its exact provider-qualified ID.

## Artwork and request bounds

Primary search makes **zero artwork enrichment calls**. Visible cards use the
authenticated artwork endpoint, with server-issued ten-minute search tickets:
four identities per batch, twelve per search, one active batch per process.
Each candidate needs at most one metadata lookup and one image GET; no full-volume
fetch, issue enumeration, pagination or automatic retries are used for artwork.
GCD's normal persisted request ledger charges its issue lookup; Metron rate-limit
state is honored, with no artwork sleep while quota is unavailable.

The in-memory LRU cache holds 128 provider-qualified identity/hint entries, at
most 8 MiB of JPEG thumbnails: successful entries expire after 24 hours and
unavailable entries after ten minutes. At most 32 search tickets are retained.
An unavailable first-issue cover is a normal placeholder; later issues are not
crawled for another image. No relation persistence or schema migration is needed:
evidence is re-derived from the search description without an extra source call.

Images use allowlisted HTTPS hosts/paths, public DNS validation and pinned TLS,
no redirects, cookies, proxies or credentials. Input is limited to 2 MiB and
16 million pixels, decoded as JPEG/PNG/WebP and re-encoded to a metadata-free JPEG
of at most 64 KiB. The browser receives only the thumbnail, never provider tokens
or provider image request URLs. Slow/failed artwork cannot fail primary search;
new searches discard stale artwork responses.

## Provider contracts

- **ComicVine:** HTML descriptions and explicit linked volume evidence already
  consumed by Pullarr's numbering inference. Structured continuation fields are
  not assumed. The [API documentation](https://comicvine.gamespot.com/api/documentation)
  was inaccessible during the release audit; deterministic existing/raw-shaped
  fixtures verify the integration. Live authenticated acceptance is optional.
- **GCD:** [official API documentation](https://github.com/GrandComicsDatabase/gcd-django/wiki/API)
  and [serializers](https://github.com/GrandComicsDatabase/gcd-django/blob/0b1a2c948c595e31cdb4864c3c78fc25c0cd4797/apps/api/serializers.py)
  expose `active_issues` in series results and `cover` in issue details. One exact
  member lookup supplies art; both issue ID and parent series are verified.
  The series serializer exposes no predecessor/successor relation. Public issue
  100000 confirmed the `files1.comics.org` cover path during the audit.
- **Metron:** [series serializers](https://github.com/Metron-Project/metron/blob/8b2cc756aee824e03f0a7ff909032c689bd3127c/api/v1_0/serializers/series.py)
  have no series image. The [issue-list serializer](https://github.com/Metron-Project/metron/blob/8b2cc756aee824e03f0a7ff909032c689bd3127c/api/v1_0/serializers/issue.py)
  includes `image` and parent series. Artwork reads only the first issue-list
  page. Detail `associated` series are shown as undirected `related_series`
  evidence (at most two); they are neither inferred continuations nor automatic
  expansion targets. Normal series-list searches omit this detail-only field.
