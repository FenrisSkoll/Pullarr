# Unraid deployment

Pullarr's Community Applications package is prepared locally, **not yet listed**.
After publication/CA approval, install Pullarr from Apps and review every mapping.
Until then build the public source image normally; there is no special Unraid fork.
Linux/amd64 is the supported CA image architecture. This repository contains
the application, root `ca_profile.xml` and `templates/pullarr.xml`; no additional
repository is needed.

Use bridge networking, WebUI container/host port5656 (host port editable), and
no privileged mode, extra capabilities, Docker socket, devices or host networking.
Set authentication on first launch before allowing untrusted LAN access. Do not
expose directly to the Internet. Unlike the loopback-only Compose example,
Unraid's normal published port is reachable through the server's LAN address.

## Persistence and ownership

| Host example | Container |
|---|---|
| /mnt/user/appdata/pullarr/db | /app/db |
| /mnt/user/appdata/pullarr/logs | /app/logs |
| /mnt/user/appdata/pullarr/temp_downloads | /app/temp_downloads |
| /mnt/user/data | /data |

These retain the existing image contract: no path/schema migration for appearance.
PUID99 and PGID100 match the usual Unraid nobody/users identity. The entrypoint
initializes ownership of the three appdata directories, then `setpriv` runs the
application as that UID/GID. It does not recursively change your data share.
Grant this identity appropriate access to the selected data directories. Do not
use chmod777. TZ is supported through the process timezone (default Etc/UTC in
the template); UMASK is not an implemented image variable and is not exposed.

## One common data path

Configure `/data/media/comics` as the library root. `/comics` is an example, not
an internal requirement. Put external downloads under `/data/downloads/torrents`
or `/data/downloads/usenet`, visible to both Pullarr and your client.
Map one common host parent, such as `/mnt/user/data`, to `/data` in both containers.
Do not create two writable aliases for the same library.

For qBittorrent, `/mnt/user/data/downloads/torrents` → `/data/downloads/torrents`
and Pullarr `/mnt/user/data` → `/data` give matching reported/visible paths.
Pullarr's client-scoped path admission/mapping can use identical prefixes.
If client paths differ, remote-path mapping translates them; it cannot make an
unmounted directory visible. SABnzbd/NZBGet use the same rule. Pullarr's own
`/app/temp_downloads` is not the external client's completed-data directory.

Hardlinks require filesystem/device support as well as compatible Docker mounts.
Unraid user-share, cache/pool and mover configurations differ: a common bind is
necessary but not a universal guarantee. Safe verified copy fallback retains
source payloads; it may consume duplicate storage. Torrent retention stays under
the configured seeding policy. Archive normalization creates independent bytes
and does not modify a shared torrent inode in place.

## Backups and updates

Back up appdata before upgrades; stop Pullarr for filesystem copies or use the
supported application backup flow. Protect library media separately. Existing
databases migrate on normal startup; do not run two instances on one database.
CA image updates will use deliberate stable `latest` releases, with immutable
version/commit tags available for reproducibility. No image is published yet.

## Maintainer acceptance/publication

`python scripts/unraid_docker_gate.py --image IMAGE --template PATH_TO_PULLARR_XML --browser`
creates only disposable resources and consumes the XML's port, variables and
container mount targets. Host directories are substituted with owned Linux-host
fixture paths. It proves UID99 writes, actual hardlink/cross-device intake,
archive copy-on-write, bridge/health, migration, restart/recreation and image-layer
hygiene. It is not a claim of testing every Unraid pool/share configuration.

The manual GHCR workflow defaults to build/test only. Publication requires the
explicit confirmation input, a version tag pointing at the checked-out commit,
and the protected `container-release` environment. Configure required reviewers
there before enabling publication. No PR can invoke its publish job. The image
package is `ghcr.io/<lowercase-owner>/pullarr`; no Docker Hub mirror is assumed.
Make the package public and verify anonymous pull before CA submission.

## Template materialization

The local branch is `release/public-ready`; eventual public raw URLs target
`main`. Three deliberate tokens remain: `__PULLARR_GITHUB_OWNER__`,
`__PULLARR_APP_REPO__`, and `__PULLARR_IMAGE__`. Select real values before release.

```sh
python scripts/unraid_template.py --allow-placeholders
python scripts/unraid_template.py --owner OWNER --app-repo REPO --image ghcr.io/owner/pullarr:TAG --materialize release-output/ca-final
python scripts/unraid_template.py --root release-output/ca-final
```

The new output directory is required; source files are never overwritten.
Review and copy only its two XML files back to their matching repository paths.
The icon lives at `docs/assets/pullarr-ca.png` (256×256); its canonical vector
source is `frontend/static/img/favicon.svg`. Both use the application GPL license.
The registry package is GHCR Pullarr; no invented support or donation URL is used.
`--local-test` permits `pullarr:TAG` only for disposable testing, never submission.

For Docker acceptance, pass the materialized `templates/pullarr.xml` with
`--template`; without that option the gate reads this repository's actual XML.
It substitutes disposable host directories and an ephemeral loopback host port,
not the template's container paths, port or UID/GID.

## Publication and CA submission

Accept the source, choose a deliberate version/tag, and build/publish the GHCR
image only through an explicitly authorized release. Materialize the template
with that exact image reference and rerun public release/history/template gates.
Push only the intended public branch to `main`, never all local branches.
Verify anonymous access to the raw root profile, template, icon and README;
verify anonymous image pull and run the resulting template configuration.
Then open Community Apps `/submit`, run **Validate**, run **Scan**, fix findings,
and submit for review. Until these steps occur this is not a CA listing.

Normal image updates do not require a new XML file when using a deliberately
maintained stable tag. Immutable release tags remain available for reproducibility.
