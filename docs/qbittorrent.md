# qBittorrent compatibility

Pullarr supports qBittorrent 5.x with WebAPI 2.9.3 or newer, including
qBittorrent 5.2.4 / WebAPI 2.15.1. The configured category must already exist.

The compatibility fixtures follow upstream
[5.2.4 authentication](https://github.com/qbittorrent/qBittorrent/blob/release-5.2.4/src/webui/api/authcontroller.cpp),
[HTTP/session handling](https://github.com/qbittorrent/qBittorrent/blob/release-5.2.4/src/webui/webapplication.cpp)
and [torrent endpoints](https://github.com/qbittorrent/qBittorrent/blob/release-5.2.4/src/webui/api/torrentscontroller.cpp).

- Login accepts legacy 200 / `Ok.` or current 204 with no body. Only `SID` or
  `QBT_SID_<port>` session cookies are admitted. The suffix is qBittorrent's
  configured WebUI port, which can differ from a reverse proxy's external port.
- Version, WebAPI version, categories, torrent info, properties and files retain
  their 200 response contracts and existing validation.
- Add accepts legacy `Ok.` or validated 200/202 JSON receipts. An explicitly
  pending receipt permits three tag lookups, separated by 250 ms, each subject
  to the existing network bounds. Success still requires exact protocol hashes;
  title matching is never used. Unresolved submissions remain ambiguous.
- Delete accepts 200 or 204 and retains exact torrent targeting, reviewed import
  and retention gates. No generic mutation retry or redirect following is added.

Managed-client errors show only known Pullarr failure codes. Neither remote
response text nor credentials are displayed.
