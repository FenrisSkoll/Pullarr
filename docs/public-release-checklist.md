# Public release checklist

No script here pushes code, creates a public release or publishes an image.

1. Choose the exact branch and author identity to publish. Inspect all intended
   refs, remotes and author emails. Do not upload private development history.
2. Stage intended source and run `python scripts/public_release_gate.py`. Review
   every binary/vendor manifest change and fixture provenance. Reject private
   databases, logs, credentials, screenshots and oversized blobs.
3. Install an official checksum-verified Gitleaks binary. Run
   `python scripts/public_release_gate.py --history --gitleaks PATH_TO_GITLEAKS`.
   This scans **all local refs**, including deleted historical content, and the
   intended current files; a synthetic positive control tests the scanner first.
   The generic credential detector includes entropy thresholds. No broad secret
   allowlists are permitted. Repeat after every new public commit or added ref.
   The one reviewed inline suppression is the literal browser-storage namespace
   `kapowarr-maintenance-v1`; it is not a credential and preserves old tab storage.
   Reports are redacted under ignored `release-output/`; inspect before sharing.
   `python scripts/public_history_gate.py` bootstraps the official checksum-verified
   scanner on Windows/Linux x64 and runs the same check (network required).
4. Optionally add `--database PATH_TO_LOCAL_DB` for read-only comparison with your
   configured credentials. No private DB or credentials are required by CI.
5. Run full Windows and network-disabled Linux discovery, full mypy/isort, all JS
   harnesses/syntax and `python scripts/check_pullarr_assets.py` as documented in
   CONTRIBUTING. Run relevant browser/security/domain acceptance with fixtures.
6. Clone the exact candidate into a new directory, install only declared
   dependencies, and repeat required gates. Export with `git archive`, extract
   without `.git`, and prove test/build/start operation from that export.
7. Build Docker from the clean candidate; run `python scripts/public_docker_gate.py`.
   Inspect filesystem **and all saved image layers**, not just Dockerfile intent.
   No `.git`, development data, user DB, logs or credentials may be baked in.
8. Run current `pip-audit`, image vulnerability scanning and SBOM generation.
   `python scripts/public_sbom.py` records resolved Python packages and the
   checksum-pinned vendored components. Generate the image/system SBOM separately
   with Docker Scout or another established scanner.
   Review findings for applicability; don't equate an unverified match with an
   exploitable flaw. Unresolved applicable high/critical runtime findings block.
9. Review license notices (including UnRAR), dependency versions, synthetic fixture
   provenance and any screenshots. Preserve upstream/legal attribution.
10. Create local source ZIP/tar and SHA256SUMS; inspect/extract/scan each artifact.
    `python scripts/public_source_artifacts.py` exports committed HEAD and runs
    public-file, security/archive and entrypoint checks on both extracted formats.
    Intended upload artifacts are under `release-output/public/`; other files in
    `release-output/` are private audit/intermediate reports, not release assets.
    Keep artifacts/SBOM outside tracked source. Review release notes and backup/
    migration instructions. Version/tag selection is deliberate; schema is not
    a product version. No release tag is created automatically.
11. Recheck clean status, staged/working whitespace, case collisions, symlinks and
    executable bits. CI uses minimal permissions and no ordinary-PR secrets.
12. Only after approval, add the chosen public remote and push **only** the approved
    branch. Never use `--mirror` or push all local refs by default. Publishing a
    container is a separate authorized step and must provide matching source.

PR CI: static/public-file checks, Windows/Linux tests and Docker smoke. Full local
pre-upload: all steps above, including full-history scanning (shallow CI cannot
prove history safe), clean clone/export, browser and current vulnerability review.
