# Unraid packaging consolidation

The main source candidate owns CA metadata and tooling without importing another
Git history. Runtime source, Dockerfile, database schema72 and storage semantics
are unchanged. The former template candidate remains local evidence only.

## Accepted file classification

| Former file | Main repository decision |
|---|---|
| `.gitattributes` | Equivalent rules; add PNG binary classification |
| `.github/workflows/validate.yml` | Merge checks into `.github/workflows/tests.yml` |
| `.gitignore` | Existing output/cache exclusions already cover requirements |
| `LICENSE` | Byte-identical GPL license; retain main license |
| `README.md` | Merge installation/publication guidance into README and Unraid guide |
| `ca_profile.xml` | Root profile; same-repository URLs |
| `docs/readiness.md` | Summarize contract and consolidation here and in Unraid guide |
| `icon.png` | `docs/assets/pullarr-ca.png`, accepted original 256×256 mark |
| `icon.svg` | Identical to existing `frontend/static/img/favicon.svg`; reuse |
| `scripts/repository_gate.py` | Existing public-release gate plus CA semantic check |
| `scripts/secret_gate.py` | Existing full-history/current-tree scanner |
| `scripts/template.py` | `scripts/unraid_template.py` |
| `templates/pullarr.xml` | Same location; same-repository URLs |
| `tests/test_template.py` | `tests/TUnraidTemplate.py`, expanded negative/determinism tests |

No Git database, generated outputs, local receipts, logs or caches were copied.

## Contract and gates

The previously audited [official CA starter](https://github.com/unraid/unraid-community-apps-starter)
uses a root profile and one Container v2 XML per app under `templates/`.
Keep MediaApp:Books, GPL-3.0, non-privileged bridge, port5656, PUID99/PGID100,
TZ Etc/UTC, three compatibility appdata paths and one common `/data` bind.
No plugin wrapper, extra capabilities, devices, Docker socket or host networking.

Local validation checks both placeholder and strict materialized modes, same-repo
raw `main` URLs, icon checksum/dimensions, XML safety and configuration invariants.
The Docker gate derives runtime values from the actual template, uses disposable
host paths, and tests hardlink/copy intake, seeded archive copy-on-write,
UID/GID writes, schema migration, restart/recreation and browser/layer safety.

This packaging-only change calls for focused tests, static/JS/public gates and
container acceptance, not repetition of the entire application test matrix.
Source artifacts include all CA files and are generated per commit; the ignored
`release-output/public/CURRENT` pointer identifies current output without deleting
older local evidence. No release, image or CA submission is performed by these gates.

## Consolidation acceptance

The focused Windows and network-disabled Linux runs pass 20 security/archive/packaging tests each, including
nine Unraid contract/materialization tests. Full mypy covers 529 source files;
isort, production JS syntax and all existing JS harnesses pass. Configured-secret
comparison is read-only; fixture credentials, vendor checksums, original icon,
brand/assets and whitespace checks pass.

The clean staged-source Linux/amd64 image passes actual template-derived UID99/
GID100 acceptance: same-device torrent hardlink, real cross-device verified copy,
seeded CBR conversion and CBZ repack with original source preserved, SABnzbd and
NZBGet completion fixtures, appdata ownership, health, restart/recreation and
schema71→72 migration/integrity. Browser checks report no unexpected errors or
external requests; the saved image layer audit checks 424 application files.
The image contains public-safe UnRAR, not the proprietary trial RAR writer.

The only changed image content is the reviewed asset inventory; application and
Docker runtime code remain unchanged. A clean committed-history scan and fresh
per-commit source exports are required after the consolidation commit. Older
outputs are deliberately preserved, not presented as current artifacts.

Two packaging assumptions were corrected: the default Docker gate previously
used equivalent hard-coded defaults when no XML was supplied; it now always reads
the main template. Source exports previously shared an output directory without
a current-commit pointer; per-commit output now prevents stale artifact ambiguity.
The broader public-image smoke also exposed a test-helper assumption: Docker can
reassign an ephemeral published port on restart. The helper now rereads the port
and fails explicitly if restarted readiness is not reached; runtime is unchanged.
