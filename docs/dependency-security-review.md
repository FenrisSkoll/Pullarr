# Dependency security review

Snapshot: 2026-10-02. This is a bounded applicability review, not a promise that
future releases or unknown vulnerabilities are safe. Repeat scans before release.

The runtime Python requirements resolve without known pip-audit advisories.
The image scan caught old cached/vendored build dependencies, so the public image
uses the current Python 3.13 Debian Trixie base, updates distribution packages,
requires urllib3 >=2.8.0 and msgpack >=1.2.1, and removes pip/setuptools/wheel
from the final runtime stage. The test stage retains its development tools.
The entrypoint uses distribution `setpriv` instead of an older Go-built gosu.

References for the runtime minimums:

- [urllib3 2.8.0 security fixes](https://github.com/urllib3/urllib3/releases/tag/2.8.0)
- [msgpack Unpacker advisory](https://github.com/msgpack/msgpack-python/security/advisories/GHSA-6v7p-g79w-8964)

The resulting Docker Scout scan reported 30 matches: 25 low, 2 medium, 3 high,
zero critical. No high-severity Python runtime match remained. The three high
distribution matches were reviewed as follows (do not globally suppress them):

| Match | Applicability to this image |
|---|---|
| CVE-2026-102010 | GCC/libstdc++ binary-heap `erase_if` template defect. Pullarr is Python; the native UnRAR reader does not expose this operation. Inspected official UnRAR source contains no `pb_ds`/`erase_if` use; the shipped reader has no such symbol. No reachable affected operation was identified. |
| CVE-2026-95619 | libstdc++ aligned-allocation overflow. The shipped reader has no `align_val_t` reference; inspected official UnRAR source does not use aligned `operator new`. Archive expansion remains independently bounded. No reachable affected operation was identified. |
| CVE-2026-85091 | Advisory describes zlib 1.3.1.2–1.3.2 nonblocking `gzwrite`/`gzprintf` behavior; the image contains Debian's 1.3.1 package, not that version range. Pullarr's archive writer uses Python ZIP/zlib APIs, not the affected nonblocking gzip-file sequence. Broad distribution matching is not evidence of this operation being vulnerable. |

These are reviewed applicability conclusions, not patched-version claims.
UnRAR source checks included the vendor's current 7.30 beta 1 source; shipped
binaries remain official stable 7.23. Reassess when changing reader/compiler or
adding native operations. Low/medium distribution matches remain recorded in
the release scan/SBOM; keep the base image current and avoid Internet exposure.

No scanner can prove absence of vulnerabilities. This project is not security
certified. See SECURITY.md for the deployment boundary and reporting policy.
