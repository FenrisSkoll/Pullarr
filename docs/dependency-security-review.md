# Dependency security review

Snapshot: 2026-10-04. This is a bounded applicability review, not a promise that
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

The resulting Docker Scout scan reported 30 matches: 26 low, 2 medium, 2 high,
zero critical. No high-severity Python runtime match remained. The two high
distribution matches were reviewed as follows (do not globally suppress them):

| Match | Applicability to this image |
|---|---|
| CVE-2026-102010 | GCC/libstdc++ binary-heap `erase_if` template defect. Pullarr is Python; the native UnRAR reader does not expose this operation. Inspected official UnRAR source contains no `pb_ds`/`erase_if` use; the shipped reader has no such symbol. No reachable affected operation was identified. |
| CVE-2026-95619 | libstdc++ aligned-allocation overflow. The [upstream fix](https://github.com/gcc-mirror/gcc/commit/59d235ffa5a69231eb42e5290d52dc8c90d28b7a) excludes the POSIX allocation implementation used on Debian Linux. Inspection of 788 runtime ELF files found aligned-allocation symbols only in the libstdc++ provider, with no consumer or PBDS/binary-heap signature. No reachable affected operation was identified. |

These are reviewed applicability conclusions, not patched-version claims.
UnRAR source checks included the vendor's current 7.30 beta 1 source; shipped
binaries remain official stable 7.23. Reassess when changing reader/compiler or
adding native operations. Low/medium distribution matches remain recorded in
the release scan/SBOM; keep the base image current and avoid Internet exposure.
The medium tar finding concerns repeated extraction through a planted symlink;
Pullarr does not use GNU tar to extract user comic archives. The medium dash
finding requires hostile shell glob matching; member names are passed as process
arguments, not shell expressions. Archive admission no longer imposes capacity
ceilings; path containment and fixed streaming buffers remain separate safeguards.

No scanner can prove absence of vulnerabilities. This project is not security
certified. See SECURITY.md for the deployment boundary and reporting policy.
