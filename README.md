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
- Reviewed Maintenance, including CBR → CBZ, CBZ integrity verification and
  explicit deterministic repack. Page bytes and admitted metadata are preserved;
  replacements are journaled and never mutate shared seed bytes in place.

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
There is no published Pullarr image or project host URL assumed by these files.

Set authentication in **Settings → General → Security** before permitting any
other machine to connect. A fresh installation has no login password: anyone who
can reach it can obtain its API key. Copy/Show/Hide are explicit API-key controls;
Regenerate is separate. Do not expose Pullarr directly to the Internet.

Read [configuration and deployment](docs/deployment.md) for bind mounts,
download visibility, permissions, backups and upgrades. Do not run two Pullarr
processes against the same database.

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
NZBGet 21+ and qBittorrent 5.x / Web API 2.9.3+ are the supported added clients.
Create the desired qBittorrent category first. Managed clients permit one enabled
client per protocol; explicitly selected legacy SABnzbd retains Usenet precedence.

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

Maintenance is explicit: select files, scan, review, then confirm. Healthy CBZs
are not automatically rewritten. Supported page payloads are JPEG, PNG and WebP.
Encrypted/multipart or unsafe archives require review. Batches are bounded;
recovery protects interrupted operations, not unlimited undo after completion.
There is **no PDF conversion, page editor, arbitrary split/combine or image
recompression**. qBittorrent is the only supported torrent client.

## Development, releases and security

- [Contributing and validation](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Public release checklist](docs/public-release-checklist.md)
- [Release notes](CHANGELOG.md)

The completed implementation programmes are summarized in the changelog. Private
development diaries, runtime evidence and agent instructions are intentionally
not part of this public source candidate. Application version `1.3.3` adds
qBittorrent 5.2 compatibility; see the release notes for details.
The official image reference is `ghcr.io/fenrisskoll/pullarr:latest`; verify
anonymous availability before installing or submitting the CA template.

## License and attribution

Pullarr's application code is GPL-3.0; see [LICENSE](LICENSE). This is a modified
Kapowarr-derived project, not an official Kapowarr release. Copyright notices
remain in source. CLU / Clu Comics was an implementation reference where licensed
compatibly. See [NOTICE](NOTICE) and [third-party notices](THIRD_PARTY_NOTICES.md).
No warranty is provided. Independent bundled utilities keep their own licenses.
No Pullarr donation, community or support URL is fabricated.
