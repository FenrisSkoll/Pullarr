# Release notes

## Public source candidate — not a tagged release

Unraid Community Applications metadata, icon, validation and materialization
tooling are included alongside the application source. CA listing/approval is
still pending publication and official Validate/Scan.

The application retains its inherited version 1.3.2 and schema72. No release tag
has been selected. This snapshot includes the completed provider abstraction,
canonical identity, reviewed import/organization, monitoring, Collections,
Calendar, Reading Orders/CBL, Quality Profiles/upgrades/provenance, Discover,
Pullarr UI, expanded clients/indexers and archive-maintenance programmes.

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
