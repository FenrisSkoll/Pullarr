# Library import and acquisition

## Import existing comics

**Library Import** scans CBR and CBZ files under configured library roots in
pages of 100. It lists local files even when a metadata provider is unavailable.
Already registered paths are counted and omitted from new import rows. Filter
the visible rows by folder, filename, publication or import status; continue
through the pages to reach the whole configured scope.
Choose **Max folders scanned** before scanning: 20 through 1,000 or **All
(bounded)**, which requests the scanner's 50,000-folder cap. The scanner also
reports partial results if its independent traversal safety bound is reached.
Optional ComicVine publication suggestions have a separate 20–500 eligible-folder
budget and can be stopped without removing local results.

Choose the provider-qualified publication and exact issue or issues represented
by each file. Suggested matches use local filename and ComicInfo evidence, but
the selected issue IDs are authoritative. A collected edition does not imply
ownership of every original serial issue. Only the selected issues are linked.
Incomplete provider catalogues remain incomplete; Pullarr does not invent the
unlisted issues or their release dates.

Choose **Keep in place** to register the existing path without renaming or
creating a destination directory. Files from different publications can remain
in the same source folder. Choose **Move and rename** to use the configured
publication directory and filename. The destination is exclusive: an existing
filename is reported on its row and is never overwritten. The row offers a
different filename or Keep in place on retry. One failed row does not stop
the others. The chosen mode is remembered in the browser.

Same-device moves use a no-clobber hardlink where supported and then retire the
source after the database has recorded the destination and exact issue links.
If a hardlink is unavailable, Pullarr streams an exclusive copy and verifies
its bytes before retiring the source. Interrupted or unverified copies keep
the source. A client-owned seeding source must use the download intake policy.
Local imports preserve their CBR/CBZ contents and ComicInfo.

Deleting a publication with **Delete folder = No** removes its metadata and
issue/file links while retaining files and directories. A provably inactive
old import claim may be abandoned during this operation; active or ambiguous
work remains protected and is reported inline. **Delete folder = Yes** is
destructive and refuses shared or externally located files.

The volume page lists Owned, Missing, Upcoming, Unknown / unconfirmed and
Unmonitored issues. Owned requires a registered file still present under a
configured root. Missing requires a monitored unowned issue with release date
evidence wholly in the past; Upcoming requires wholly future evidence.
Uncertain or incomplete dates remain Unknown.

Historic organization journals and import draft records remain in the database
for compatibility, but ordinary local imports do not create jobs or drafts.
Legacy uncertain operations are not automatically replayed at startup.

## Release acquisition

Automatic missing-issue and upgrade selection, including Search and grab now,
uses acceptable GetComics candidates only. If none qualify, it abstains. NZB and
torrent candidates remain available in Manual Search, never as automatic fallback.

Manual Search uses the sole enabled compatible client automatically. The existing
stored `sab_client_id` preference is retained as the default manual NZB client;
it does not authorize automatic Usenet grabs. When multiple clients are available,
choose the exact client in the result row. Source sorting/filtering changes display
only. Query/category provenance appears under **Why this result?**.

**Download anyway** requires explicit inline confirmation and records
`forced_manual`, preserving the original evaluation. It can override bibliographic
uncertainty, not invalid locators, source/configuration changes, client failures,
blocklists or forbidden quality. Forced coverage is limited to the selected target
issues. Automatic acquisition never forces.

Newznab/Torznab requests use advertised comics categories (standard 7030), falling
back to advertised ebooks/books categories. Without either, the source is reported
unsupported rather than searched across all media. Explicit configured categories
must be advertised; known Movie/TV categories are rejected. Issue-title variants
share the existing maximum of three query variants per source.

## Retained internal compatibility

Download intake, torrent retention, verified upgrades, provider switching and
historical organization jobs still use their existing safety infrastructure.
The former archive maintenance page and its startup recovery task are no longer
part of the normal application workflow. Existing journal tables and artifacts
are preserved during upgrade; no schema reset or bulk deletion is performed.
