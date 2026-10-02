# Security policy

Pullarr is an actively developed single-user application. No guaranteed response
time or formal security support lifetime is offered. Do not expose a fresh
unauthenticated installation to a LAN or the Internet. Use authentication,
network isolation and TLS at a trusted reverse proxy where remote access is
needed. The application database and its backups contain credentials: restrict
filesystem access and do not attach them to public issues.

For a suspected vulnerability, use the eventual repository host's private
vulnerability-reporting channel if the maintainer enables it. If no private
channel is available, open a minimal issue asking for a private reporting method
without exploit details, credentials, private URLs or user data. Do not publish
secrets in an issue. No reporting email address is currently designated.

Reports should describe affected versions and a synthetic reproduction. Redact
logs and inspect screenshots. If a secret was exposed, revoke/rotate it at its
issuer; deleting it from a new commit does not remove it from history.

Versions before the public hardening change could log generated application API
keys and setting values. Keep old logs private and rotate affected credentials
after upgrading. This warning is not a claim that a credential was published.
