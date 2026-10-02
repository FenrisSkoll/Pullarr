# Deployment and configuration

The Compose example builds local source and publishes only
`127.0.0.1:5658 → 5656/tcp`. It needs no privileged mode, Docker socket, host PID
namespace or credentials. Named volumes persist `/app/db`, `/app/logs`,
`/app/temp_downloads` and `/comics`. Do not use `down --volumes` to update.

The image supports PUID/PGID. Its compatibility default is root; for an existing
shared library choose the owning user's numeric IDs and grant that user the
necessary group access. Do not use `chmod 777`. The entrypoint adjusts only app
database/log/download directory ownership; it does not recursively chown comics.

## Shared download mounts

Replace the example comic/download volumes with appropriate host bind mounts
when integrating an external download client. A typical *example*, not an
existing host path, is one host `/srv/media` mounted at `/data` in both clients
and Pullarr, with `/data/downloads` and `/data/comics` underneath it. Avoid separate
filesystem boundaries if hardlinks are required. Confirm actual device behavior;
matching path strings are not proof of hardlink capability.

Configure client-scoped remote path mapping in Intake when a client reports a
different path. Mapping translates paths; it cannot make an unmounted directory
visible. Use Test and a disposable download before real acquisition. Provide your
own API keys/passwords through Settings, not committed Compose files.

## Security

Fresh authentication is disabled. A reachable unauthenticated instance lets its
caller obtain the generated application API key. Configure a login immediately
before widening access. API keys are bearer credentials, not a replacement for
network security. The database stores provider/client credentials; host backups
need the same access controls as the live database. Do not expose the application
directly to the Internet. No telemetry/CDN is required by the UI.

## Backups and upgrades

Stop Pullarr before copying its database directory, including any SQLite WAL/SHM
files, or use a proper SQLite online backup. Back up comics separately: a database
backup cannot restore deleted media. Preserve settings and all persistent mounts.
Test upgrades with copies first, especially when schema changes are announced.

Build the new image, stop the old container and recreate using the same volumes.
Startup applies historical migrations. Never run old and new applications against
one database concurrently. If rollback is necessary, stop the new application,
restore a matching verified pre-upgrade database backup and use the old image.
Do not simply downgrade the image while retaining a newer schema.

The compatibility database filename is `Kapowarr.db`; keep it unchanged. No
publication hardening migration is required (schema72).
