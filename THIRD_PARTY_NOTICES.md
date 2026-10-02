# Third-party software and asset notices

Pullarr is GPL-3.0 application code derived from Kapowarr; see LICENSE and NOTICE.
The following independent components keep their own terms. Their presence does
not mean GPL licenses a proprietary utility. Checksums and source locations are
recorded in `licenses/vendor-manifest.json`.

## UnRAR 7.23 — separate command-line reader

Copyright © 1993–2026 Alexander Roshal. Windows/Linux x64 readers are the unmodified
UnRAR components from the official RARLAB 7.23 packages. The exact UnRAR license
is included at `backend/lib/UnRAR-LICENSE.txt` and is copied into Docker with the
utility. It explicitly permits distributing UnRAR inside other software packages;
it prohibits using its source to recreate RAR compression. Pullarr invokes this
independent executable through its command-line interface and does not link it
into the GPL application or implement the RAR compression algorithm.

The old trial **RAR 6.24** executables are NOT redistributed. Its EULA restricts
separate package parts and bundling without written permission; historical local
presence was not proof of publication permission. Legacy RAR creation requires
the user's separately installed, appropriately licensed `rar` command. UnRAR
cannot create archives. Other platforms may provide their own compatible UnRAR.

References: https://www.rarlab.com/rar_add.htm and https://www.rarlab.com/license.htm.
Exact binary packages/checksums are in the manifest. The 7.23 package EULA
explicitly exempts UnRAR components from its separate-parts restriction. The
included UnRAR license also matches the current official `unrarsrc-7.3.1`
(7.30 beta 1) source license in its full terms (only whitespace normalized); this
does not claim that the newer source archive produced the shipped 7.23 binaries.

## Socket.IO client 4.7.5

Copyright © 2014 Guillermo Rauch. MIT; full license is at
`frontend/static/vendor/socket.io-LICENSE` and the test-vendor copy. The local
distribution matches official `socket.io-client/4.7.5/dist/socket.io.min.js`
after LF normalization. No CDN is required. Retain both the banner and license.

## Artwork and fixtures

Pullarr mark/placeholder and public UI geometric icons are original project SVGs,
GPL-3.0. Older unclear-provenance icons and unused third-party branding images
are excluded. No screenshots, comic pages or downloaded covers are distributed.
RAR test fixtures contain original generated white PNGs and minimal XML only;
their manifest hashes and creation provenance accompany them. Metadata fixtures
retain factual identity/type observations but use original synthetic prose.
The small GCD characterization subset, including retained notes/credits, is
attributed to the Grand Comics Database and contributors under **CC BY-SA 4.0**,
not GPL; per-file source URLs and modifications are recorded. See
`tests/fixtures/gcd/README.md` and `licenses/GCD-CC-BY-SA-4.0.txt`.

## Installed Python and container dependencies

Declared Python dependencies are installed through requirements.txt; development
tools are separate in requirements-dev.txt. Wheel metadata/license notices remain
in installed distributions. These include Python/PSF; Requests/Apache-2.0;
BeautifulSoup/MIT; Flask/BSD-3-Clause; Waitress/ZPL-2.1; cryptography/Apache-2.0 OR
BSD-3-Clause; aiohttp/Apache-2.0 AND MIT; Flask-SocketIO/MIT; websocket-client/
Apache-2.0; rarfile/ISC; Pillow/MIT-CMU; and their separately identified dependencies.
The release SBOM and license inventory describe the resolved versions, not just
these direct dependencies. Refer to each installed distribution's license file.

Docker uses official Python/Debian images and distribution packages, including
util-linux/setpriv. `/usr/share/doc/*/copyright` and Python distribution license metadata are
retained. Release image scans/SBOM cover system packages as well as Python.
When distributing an image, provide matching application source and preserve
component notices and any corresponding-source obligations of included packages.
