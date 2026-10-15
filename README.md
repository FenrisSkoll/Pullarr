# Pullarr

Manage, monitor and organize your comic library.

Pullarr is a comic library and acquisition manager derived from
[Kapowarr](https://github.com/Casvt/Kapowarr). This is an actively developed,
single-user application, not a claim of production maturity. Back up your
database and comics before upgrades or filesystem operations.

## Capabilities

- ComicVine, Metron and GCD metadata with explicit provider identity and reviewed
  Provider Switching. Configure your own provider credentials in Settings.
- Library monitoring and Wanted acquisition, distinguishing Missing from Upgrade.
- Prowlarr/Newznab and direct Torznab search; SABnzbd and NZBGet Usenet clients;
  qBittorrent torrent acquisition; existing GetComics direct downloads.
- Source-preserving torrent import, exact job correlation, seeding policies and
  reviewed cleanup. Client-managed retention is the safe default.
- Collections, precision-aware release Calendar, exact-issue Reading Orders,
  CBL import/export and reviewed subscriptions.
- Revisioned Quality Profiles, verified upgrades and acquisition provenance.
  Release labels are claims, not verified image quality.
- GetComics Discover: bounded recent observations, local browsing and opt-in
  polling. Discover polling never automatically grabs a release.
- Library Import discovers local archives independently of metadata availability,
  lets you choose a provider or create a manually managed local publication with
  exact issue slots on one page, then
  registers in place or moves and renames without a draft or extra approval.
  Explicitly action-selected files can also be ignored and later restored from
  Ignored Files. Ignores use exact path, size, modification time and change time; a moved or
  changed archive is offered again. Local publications do not receive provider
  refreshes or automatic acquisition.
  Original CBR/CBZ formats and embedded metadata are preserved. See
  [workflow details](docs/reliability-workflows.md).
- Existing publications can use a manually uploaded JPEG, PNG or WebP cover.
  Pullarr keeps the provider cover separately so removing the override restores
  the latest provider artwork when available.

Use sources only for content you are entitled to obtain. Provider and indexer
availability, terms and rate limits are outside Pullarr's control.

## Docker quick start

The Unraid Community Applications template is included in this repository.
Listing remains pending official CA Validate/Scan and review.
Use persistent appdata and one common `/data` mount for media and downloads;
hardlinks also require compatible underlying storage. See the [Unraid guide](docs/unraid.md).

Docker must be installed. From this source checkout:

```sh
docker build -t pullarr:local .
docker compose -f docker-compose.pullarr.yml up -d
```

Open **http://127.0.0.1:5658**. The example exposes only the host's loopback
interface and uses persistent named volumes. Add `/comics` as a library root.
The stable image reference is `ghcr.io/fenrisskoll/pullarr:latest`.

Set authentication in **Settings → General → Security** before permitting any
other machine to connect. A fresh installation has no login password: anyone who
can reach it can obtain its API key. Copy/Show/Hide are explicit API-key controls;
Regenerate is separate. Do not expose Pullarr directly to the Internet.

Read [configuration and deployment](docs/deployment.md) for bind mounts,
download visibility, permissions, backups and upgrades. Do not run two Pullarr
processes against the same database.

Custom publications and persistent Library Import ignores add database schema 75.
Back up the database before upgrading normally; a schema-74 or older binary cannot
open the upgraded database without restoring its matching pre-upgrade backup.
No library reset or reimport is required.

For Unraid storage, UID/GID and hardlink guidance, see [Unraid deployment](docs/unraid.md).
Community Applications availability remains pending official Validate/Scan and review.

## Source installation

Tested with Python 3.11 on Windows and Python 3.13 on Linux. Python 3.11+ is
required. Install declared dependencies in an isolated environment:

```sh
python -m venv .venv
# Activate .venv using your shell's normal activation command.
python -m pip install -r requirements.txt
python Pullarr.py
```

Fresh source installs bind to `127.0.0.1:5656`. Existing configured bind addresses
are preserved. `python Pullarr.py --help` lists path and hosting options.
`Kapowarr.py`, `Kapowarr.db`, stable settings and browser-storage names are retained
compatibility identifiers; do not rename an existing database.

## Configuration

Add metadata providers in Settings before searching for volumes. Search Sources
are separate from metadata providers: Prowlarr or direct Torznab supply releases,
not canonical publication identity. Test configured sources and download clients.
Add Comics includes bounded lazy artwork and explicit continuation discovery;
see [metadata search](docs/metadata-search.md) for provider capabilities and limits.
NZBGet 21+ and qBittorrent 5.x / Web API 2.9.3+ are the supported added clients.
Create the desired qBittorrent category first. Managed clients permit one enabled
client per protocol. Manual Search uses the sole enabled compatible client, the
saved manual NZB default, or an explicit client choice. Automatic grabs use
GetComics only; NZB and torrent downloads always require manual selection.

Map remote client paths to **paths actually mounted inside Pullarr**. A remote
path mapping cannot create a Docker mount. For torrent hardlinks, both source and
library must be visible on the same underlying filesystem/device. Cross-device
imports use verified copies. Neither mode destructively moves seeding payloads.
Keep source data until import/review is complete and retention requirements permit
cleanup. See [deployment](docs/deployment.md) and [operating safely](docs/usage.md).

## Archives and limits

The public distribution uses **UnRAR 7.23**, not proprietary trial RAR 6.24.
Windows/Linux x64 reader binaries are separately licensed; see
[third-party notices](THIRD_PARTY_NOTICES.md). Other platforms need a compatible
`unrar` command on PATH. Legacy RAR creation requires a separately installed,
appropriately licensed `rar` command; it is not provided by UnRAR.

Local Library Import does not convert or repack archives. Historic journal records
remain for compatibility, but ordinary local imports use direct file registration
and exclusive moves or verified copies. See [workflow details](docs/reliability-workflows.md).
There is **no PDF conversion, page editor, arbitrary split/combine or image
recompression**. qBittorrent is the only supported torrent client.

## Development, releases and security

- [Contributing and validation](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Public release checklist](docs/public-release-checklist.md)
- [Release notes](CHANGELOG.md)

The completed implementation programmes are summarized in the changelog. Private
development diaries, runtime evidence and agent instructions are intentionally
not part of this public source candidate. Application version `1.4.0` adds
metadata search artwork and continuation discovery; see the release notes for details.
The official image reference is `ghcr.io/fenrisskoll/pullarr:latest`; verify
anonymous availability before installing or submitting the CA template.

## License and attribution

Pullarr's application code is GPL-3.0; see [LICENSE](LICENSE). This is a modified
Kapowarr-derived project, not an official Kapowarr release. Copyright notices
remain in source. CLU / Clu Comics was an implementation reference where licensed
compatibly. See [NOTICE](NOTICE) and [third-party notices](THIRD_PARTY_NOTICES.md).
No warranty is provided. Independent bundled utilities keep their own licenses.
No Pullarr donation, community or support URL is fabricated.
