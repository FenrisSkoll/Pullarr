# Contributing

Use Python 3.11+ and a repository-local virtual environment. Install
`requirements.txt` and `requirements-dev.txt`. Install Node.js for JavaScript
checks. `python -m playwright install chromium` installs the optional browser
acceptance dependency. Normal tests require no provider/client credentials.

```sh
python -m pip install -r requirements.txt -r requirements-dev.txt
python scripts/public_release_gate.py
python -m mypy --explicit-package-bases backend frontend tests scripts Kapowarr.py Pullarr.py
python -m isort --check-only backend frontend tests scripts Kapowarr.py Pullarr.py
python scripts/public_js_gate.py
python -m unittest discover -s tests -p '*.py'
```

For offline Linux acceptance, first build the test image using
`docker build --target test -t pullarr:test .`, then run
`docker run --rm --network none --tmpfs /tmp:rw,size=2g pullarr:test`. Disposable
tmpfs avoids slow overlay-filesystem journal flushes without changing SQLite
transaction/recovery tests. All fixtures are synthetic or
reviewed bounded metadata observations. Do not add real comic pages, downloads,
databases, tokens, personal logs or screenshots containing private information.

Preserve canonical/provider identity, server-owned admission, Quality Profiles,
acquisition provenance and OrganizationJob recovery. Never write shared torrent
inodes in place. Archive changes must preserve page bytes unless an explicitly
reviewed feature says otherwise. Tests must use disposable data, never a user's
library. Update public documentation when behavior or safety limits change.

PR CI provides fast/static checks plus Windows/Linux tests and Docker smoke.
Before a release, also run full-history, clean-clone/export, browser, dependency
and image-content checks from the [release checklist](docs/public-release-checklist.md).
Do not weaken tests or broad-allowlist a secret scanner merely to obtain PASS.
