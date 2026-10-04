# Library, acquisition and archive workflows

Library Import treats each checked row's displayed provider-qualified publication
as the operator's selection. **Import** moves each selected file into the selected
publication's canonical managed folder, preserving its filename. **Import and
Rename** additionally applies the configured file naming policy. One discovery
folder can contain several publications: correcting one row never changes another
row's selection. Existing publications retain their established managed folders.
An invalid existing folder requires repair rather than silent reassignment.

Destinations use the normal Library & Organization folder policy, including its
configured suffixes. Folders needed for incoming files are created even when
**Create Empty Volume Folders** is off; that preference controls empty precreation.
**Delete Empty Folders** (off by default) also controls Library Import source
cleanup. Cleanup only removes the unchanged, empty immediate source directory
after all selected moves succeed; it never recursively removes content or deletes
roots, managed folders or directories referenced by live recovery work. Conflicting
destinations preserve the source for review. Cross-filesystem imports copy in
bounded chunks, verify payloads, then retire only the source directory entry.

Publication registration and a valid folder commit together. Exact embedded issue
identity, ComicInfo numbering and safe unique filename numbering determine issue
associations. Unresolved files remain in review while ready files import; a valid
managed volume can remain even when some issues need review. Related publications
and provider namespaces remain distinct.

From a volume page, **Preview Local Scan** uses that managed volume's authority,
checks contradictory evidence and displays read-only results. **Apply ready
associations** is the mutation boundary. It neither refreshes provider metadata nor
renames or moves files nor removes missing-file records. Ready associations commit
in a scoped database transaction without archive rehashing, quality analysis or
OrganizationJobs. A fresh preview recognizes committed associations. Old transient
previews can expire or be evicted; refresh them before applying.

**Review issue match** opens an inline panel within that preview. It displays the
filename, relevant ComicInfo values, existing associations and issues from the
fixed managed volume. Cancel makes no changes; **Save association** records the
explicit issue selection atomically and returns to the same preview. Other ready
rows remain usable. A stale preview requires a fresh Local Scan; publication
identity, unsafe-path and ownership conflicts cannot be overridden by this picker.
Ordinary issue selection saves immediately. Replacing an existing issue association
or an embedded exact issue identity requires the specifically labelled confirmation.
Publication identity conflicts remain blocked. Compatible punctuation and split
Series/Title evidence can corroborate the fixed managed publication. A `v001`
filename can identify issue 1 only when the managed catalog proves Volume/Book/Part
numbering; ComicInfo Volume is not generally an issue number.

## Release acquisition

Automatic missing-issue and upgrade selection, including Search and grab now,
uses acceptable GetComics candidates only. If none qualify, it abstains. NZB and
torrent candidates remain available in Manual Search, never as automatic fallback.

Manual Search uses the sole enabled compatible client automatically. The existing
stored `sab_client_id` preference is retained as the default manual NZB client;
it no longer authorizes automatic Usenet grabs. When multiple clients are available,
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
must be advertised; known Movie/TV categories are rejected. This follows the
[Newznab category contract](https://newznab.readthedocs.io/en/latest/misc/api.html)
and [Prowlarr standard categories](https://github.com/Prowlarr/Prowlarr/blob/develop/src/NzbDrone.Core/Indexers/NewznabStandardCategory.cs).
Issue-title variants share the existing maximum of three query variants per source.

## Archives and recovery

CBR → CBZ streams unchanged extracted member bytes into a unique prepared ZIP and
verifies SHA-256 hashes and byte counts for each member in source order. ZIP
compression may change; image and admitted metadata payloads do not. Conversion
does not require raster decoding. Ordinary success publishes exclusively, reconciles
database ownership and retires the old library entry and hidden recovery artifacts.

There are no Pullarr admission ceilings for page count, member count, member size,
aggregate expanded size, pixels, image dimensions or Pillow's decompression-bomb
threshold. Large or hostile archives can consume significant CPU, RAM, disk and
time. Copy/hash buffers use 1 MiB chunks; optional image analysis uses a disk spool.
Explicit full raster verification may still require memory proportional to one
image. ZIP/RAR directory records and manifests require memory proportional to member
count. Format/library addressability, supported codecs, OS/filesystem capacity and
available resources remain practical limits. Native extraction retains an inactivity
watchdog and cooperative cancellation; no mutation retry is introduced.

Startup queues one serial recovery task, examining at most 100 unfinished archive
journals. It verifies recorded intent, ownership, paths, hashes and identities under
the cross-process executor lock before continuing the same job. No new destination
or replacement is computed. GET requests, scans and previews never recover or mutate.
Recovery can be repeated safely. Changed or conflicting evidence remains untouched.

Journal-owned workspaces affect their source operation only. Unknown orphan names
do not authorize deletion: without a valid intent, equal bytes alone cannot prove
which publication owns an artifact. A scan reports manual inspection and leaves
them intact; unrelated archives remain usable. **Review Recovery** focuses the
review; **Continue Recovery** displays progress and refreshes the durable result.

Healthy CBZs are not automatically repacked. No PDF conversion or page recompression
is included. Back up application data before updating; schema72 is unchanged.
