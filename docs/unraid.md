# Unraid deployment

Pullarr's Community Applications package is prepared locally, **not yet listed**.
After publication and CA approval, install Pullarr from Apps and review every
mapping before starting the container.

Until then, build the public source image normally; there is no special Unraid
fork. Linux/amd64 is the supported CA image architecture.

This repository contains the application, root `ca_profile.xml` and
`templates/pullarr.xml`; no additional template repository is required.

Use bridge networking, WebUI container/host port `5656` by default (the host port
is editable), and no privileged mode, extra capabilities, Docker socket, devices
or host networking.

Configure authentication on first launch before allowing untrusted LAN access.
Do not expose Pullarr directly to the Internet. Unlike the loopback-only Compose
example, Unraid's normally published WebUI port is reachable through the server's
LAN address.

## Persistence and ownership

| Host example | Container |
|---|---|
| `/mnt/user/appdata/pullarr/db` | `/app/db` |
| `/mnt/user/appdata/pullarr/logs` | `/app/logs` |
| `/mnt/user/appdata/pullarr/temp_downloads` | `/app/temp_downloads` |
| `/mnt/user/data` | `/data` |

These mappings retain the existing image contract and do not require a path or
schema migration purely for Unraid deployment.

`PUID=99` and `PGID=100` match the usual Unraid `nobody:users` identity. The
entrypoint initializes ownership of the three Pullarr appdata directories, then
uses `setpriv` to run the application as the configured UID/GID.

It does **not** recursively change ownership of your `/data` share. Grant the
configured identity appropriate access to the selected library and download
directories.

Do not use `chmod 777`.

### Library Import permission check

Library Import applies **Chmod Folder** and **Chown Group** when it creates a
managed destination. With mode `775` under a setgid parent, the expected
new-directory mode is usually `2775`, with effective `rwx` for the owning group.
Imported regular archives normally receive non-executable `664`-style access.
Pullarr checks the owning-group access ACL entry and mask; the parent's default
ACL is kept. A failed chmod, chgrp or ACL update stops the import row. The
preflight reports the *application worker's* UID/GID/groups and available umask;
`docker exec ... id` describes the exec shell, not necessarily that worker.

For manual acceptance, use only a copied synthetic archive in a disposable
folder under the configured library root. Preview and import it to a new
publication, then verify the final archive and exact issue link in Pullarr.
On the host, inspect the exact new directory and file with `stat` and
`getfacl -cp`; confirm the owning group has effective write access and that
the directory retained setgid. From the intended Windows SMB account, create
and delete a small test file in that exact destination. This SMB check is
separate from POSIX mode verification. If an empty canonical destination from
a failed import has wrong access, use the single-folder reviewed repair shown
in Library Import, then preview again. Do not run recursive chmod/chown over
the collection.

Unraid user shares may reject Linux's exclusive `renameat2` flag even when
both paths appear on the same device. Library Import then uses its journalled,
exclusive verified-copy fallback. A previously interrupted import showing
`unsupported_operation` should be opened in **Maintenance → History** for
**Review Recovery**. Confirm the original archive still exists, the intended
destination is absent, and the recorded publication and issue are correct
before **Continue Recovery**. An occupied or partial destination needs separate
inspection; do not manually overwrite it or start another import for the same
file. The exact filesystem cause requires a disposable user-share syscall test
or captured errno; the journal state alone cannot prove which FUSE operation
was rejected.

For a disposable acceptance run, copy one small CBR/CBZ into an isolated test
root mounted into a separate Pullarr test instance with a copied database.
Configure **Chmod Folder = 775** and **Chown Group = users** in that instance.
Before importing, identify the actual worker process with
`ps -eo pid,user,uid,gid,args` inside the container, then read that process's
`/proc/<pid>/status` (`Uid`, `Gid`, `Groups` and `Umask`), plus
`id -a <worker-user>` where an account name exists. A separate
`docker exec ... id` shell is not worker evidence. On the host, capture:

```sh
stat -c '%A %a %u:%g %n' /path/to/test-parent /path/to/test-destination
getfacl -cp /path/to/test-parent /path/to/test-destination
```

Use the saved Library Import draft to select the exact publication and issue,
preview, then import the copied archive. Repeat `stat` and `getfacl -cp` for
the final directory and archive. Confirm the final file bytes and issue link in
Pullarr. From the intended Windows SMB account, create and delete a small file
in the new destination. POSIX group write is necessary for the configured
policy, but share permissions, ACL mapping and the SMB identity can still deny
access. Keep this test separate from the live Mimir collection.

`TZ` is supported through the process timezone and defaults to `Etc/UTC` in the
Unraid template. `UMASK` is not an implemented Pullarr image variable and is not
exposed by the template.

## One common data path

Configure a library path such as:

```text
/data/media/comics
```

`/comics` is only an example path and is not an internal requirement.

Put external download-client data beneath the same common tree, for example:

```text
/data/downloads/torrents
/data/downloads/usenet
```

Map one common host parent, such as:

```text
/mnt/user/data
```

to:

```text
/data
```

in Pullarr and, where practical, your download clients.

Do not create two writable aliases for the same library.

For example, with qBittorrent:

```text
Host:
/mnt/user/data/downloads/torrents

qBittorrent container:
/data/downloads/torrents

Pullarr host:
/mnt/user/data

Pullarr container:
/data
```

This gives both applications the same container-side path namespace.

Pullarr's client-scoped path admission/mapping can therefore use identical
prefixes. If a client reports a different path, remote-path mapping can translate
that path into the path visible inside Pullarr.

Remote-path mapping cannot make an unmounted host directory visible.

SABnzbd and NZBGet follow the same rule.

Pullarr's own:

```text
/app/temp_downloads
```

is its internal acquisition workspace and is **not** the external download
client's completed-data directory.

## Hardlinks and copy fallback

Hardlinks require both:

- compatible Docker path mappings; and
- compatible underlying filesystem/device placement.

A common `/data` bind gives Pullarr the correct container-side layout but cannot
guarantee hardlinks across every Unraid user-share, cache, pool or mover
configuration.

When hardlinking is unavailable, Pullarr uses its verified copy fallback and
preserves the original source payload.

That may consume additional storage.

Torrent retention remains governed by the configured seeding policy.

Archive normalization uses independent output when the source is shared with a
torrent and does not modify a seeded source inode in place.

## Backups and updates

Back up Pullarr appdata before upgrades.

For the 1.5.5 upgrade, record the actual host path mapped to `/app/db` in the
container settings (the supplied template defaults to
`/mnt/user/appdata/pullarr/db`). Stop Pullarr in the Unraid Docker UI, then copy
that **entire** database directory to a dated location outside appdata, including
`Kapowarr.db` and any `Kapowarr.db-wal`/`Kapowarr.db-shm` files. Verify the
backup is readable and keep the previously running image available. Version
1.5.5 keeps schema 73, which was introduced in 1.5.4. A rollback from an
older pre-schema-73 version requires stopping Pullarr, restoring its matching
pre-upgrade database directory, and starting that older image; switching image
tags alone cannot reverse the migration. Protect the backup because it contains
settings and credentials. Back up the media separately.

After making the backup, use the Unraid Docker UI to update Pullarr from the
stable `latest` image, then start it with the same paths and PUID/PGID. Inspect
startup logs, settings, Library Import drafts and a few existing library records
before using Import. The migration does not scan or alter comics. Already
authorised unfinished organisation or maintenance jobs can resume during normal
startup, so inspect their recovery state before the upgrade if no continuation
is desired. Do not run old and new instances against the same database.

Community Applications image updates will use deliberately maintained stable
`latest` releases, with immutable version and commit tags available for
reproducibility.

The official stable image reference is `ghcr.io/fenrisskoll/pullarr:latest`.
Verify anonymous pull before installation or Community Applications submission.

## Maintainer acceptance and publication

The Unraid Docker acceptance helper is:

```sh
python scripts/unraid_docker_gate.py \
  --image IMAGE \
  --template PATH_TO_PULLARR_XML \
  --browser
```

It creates only disposable resources and consumes the supplied XML's:

- port;
- variables;
- container mount targets.

Host directories are substituted with owned Linux-host fixture paths.

The gate verifies:

- `PUID=99` / `PGID=100` writes;
- non-privileged bridge networking;
- appdata ownership;
- common `/data` access;
- actual hardlink intake;
- cross-device verified copy fallback;
- archive copy-on-write;
- health checks;
- schema migration;
- restart and recreation;
- image-layer hygiene;
- browser startup behavior.

It is not a claim that every possible Unraid share, pool, cache or mover
configuration has been tested.

The manual GHCR workflow defaults to build/test only.

Publication requires:

- explicit publication confirmation;
- a version tag pointing at the checked-out commit;
- the protected `container-release` environment.

Configure required reviewers for that environment before enabling publication.

Pull requests cannot invoke the image publication job.

The planned image package is:

```text
ghcr.io/fenrisskoll/pullarr
```

No Docker Hub mirror is assumed.

The package must be public and anonymously pullable before Community Applications
submission.

## Template materialization

The Community Applications XML in this repository is materialized for the public
`FenrisSkoll/Pullarr` repository.

Current publication values are:

- GitHub owner: `FenrisSkoll`
- Application repository: `Pullarr`
- Container image: `ghcr.io/fenrisskoll/pullarr:latest`

Public raw URLs target the eventual `main` branch even though the local
publication candidate is maintained on `release/public-ready`.

Validate the committed template with:

```sh
python scripts/unraid_template.py --root .
```

The template tooling also retains support for placeholder-based generation for
future repository moves or release testing.

To reproduce the current materialized copy (a different destination requires
restoring the tooling's three placeholder tokens in the source XML first):

```sh
python scripts/unraid_template.py \
  --root . \
  --materialize release-output/ca-final \
  --owner FenrisSkoll \
  --app-repo Pullarr \
  --image ghcr.io/fenrisskoll/pullarr:latest

python scripts/unraid_template.py --root release-output/ca-final
```

Materialization always writes to a separate output directory; source files are
not overwritten.

Review generated files before copying:

```text
ca_profile.xml
templates/pullarr.xml
```

back to their matching repository paths.

The Community Applications icon lives at:

```text
docs/assets/pullarr-ca.png
```

and is `256×256`.

Its canonical vector source is:

```text
frontend/static/img/favicon.svg
```

Both are distributed under the application's licensing terms.

The container package is published through GHCR. No Docker Hub mirror, support
URL or donation URL is assumed.

`--allow-placeholders` exists only for validating an intentionally
unmaterialized development template.

`--local-test` permits an unpublished `pullarr:TAG` image only for disposable
acceptance testing and must never be used for Community Applications submission.

For Docker acceptance, pass a materialized `templates/pullarr.xml` using
`--template`.

Without that option, the gate reads this repository's committed XML.

The acceptance runner substitutes disposable host directories and an ephemeral
loopback host port. It does not change the template's container paths, container
port or UID/GID semantics.

## Publication and CA submission

Accept the source and choose a deliberate version/tag before publishing a
container image.

Publish the immutable GHCR release image only through an explicitly authorized
release, then deliberately update:

```text
ghcr.io/fenrisskoll/pullarr:latest
```

to that same accepted release.

Before public submission, rerun:

```sh
python scripts/unraid_template.py --root .
python scripts/public_release_gate.py
python scripts/public_history_gate.py
```

Push only the intended sanitized publication branch to public `main`.

Never publish all local branches or mirror the private development repository.

Verify anonymous access to:

```text
https://github.com/FenrisSkoll/Pullarr

https://raw.githubusercontent.com/FenrisSkoll/Pullarr/main/ca_profile.xml

https://raw.githubusercontent.com/FenrisSkoll/Pullarr/main/templates/pullarr.xml

https://raw.githubusercontent.com/FenrisSkoll/Pullarr/main/docs/assets/pullarr-ca.png

https://raw.githubusercontent.com/FenrisSkoll/Pullarr/main/README.md
```

Verify anonymous image pull:

```sh
docker pull ghcr.io/fenrisskoll/pullarr:latest
```

Then run the resulting Community Applications template configuration and confirm
normal startup.

After those checks:

1. Open Community Apps `/submit`.
2. Run **Validate**.
3. Run **Scan**.
4. Resolve any findings.
5. Submit Pullarr for review.

Until those steps complete, Pullarr is **not** listed in Community Applications.

Normal image updates do not require a new XML template while Community
Applications tracks the deliberately maintained stable `latest` tag.

Immutable release and commit tags remain available for reproducibility.
