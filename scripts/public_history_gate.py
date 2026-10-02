"""Checksum-verified official Gitleaks, all local refs, with scanner canary.

Requires network only to bootstrap the tool. Alternatively supply an already
verified binary to public_release_gate.py --history --gitleaks PATH.
"""
import hashlib
import platform
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import requests

VERSION = '8.24.3'
ROOT = Path(__file__).resolve().parents[1]


def main():
    system = 'windows' if sys.platform == 'win32' else 'linux'
    assert platform.machine().lower() in ('amd64', 'x86_64'), 'Supply a verified native scanner for this architecture'
    suffix = 'zip' if system == 'windows' else 'tar.gz'
    name = f'gitleaks_{VERSION}_{system}_x64.{suffix}'
    base = f'https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/'
    with requests.Session() as session, tempfile.TemporaryDirectory(prefix='pullarr-gitleaks-') as directory:
        session.trust_env = False
        checks = session.get(base + f'gitleaks_{VERSION}_checksums.txt', timeout=60)
        checks.raise_for_status()
        expected = next(line.split()[0] for line in checks.text.splitlines() if line.split()[-1] == name)
        response = session.get(base + name, timeout=120)
        response.raise_for_status()
        assert len(response.content) < 50 * 1024 * 1024
        assert hashlib.sha256(response.content).hexdigest() == expected
        archive = Path(directory) / name
        archive.write_bytes(response.content)
        executable = Path(directory) / ('gitleaks.exe' if system == 'windows' else 'gitleaks')
        if system == 'windows':
            with zipfile.ZipFile(archive) as source:
                executable.write_bytes(source.read('gitleaks.exe'))
        else:
            with tarfile.open(archive) as source:
                executable.write_bytes(source.extractfile('gitleaks').read())
            executable.chmod(0o755)
        subprocess.run([sys.executable, str(ROOT/'scripts/public_release_gate.py'), '--history',
                        '--gitleaks', str(executable)], cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
