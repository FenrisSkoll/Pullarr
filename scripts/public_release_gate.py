"""Public-file/upload gate. Reports locations/categories, never secret values."""
import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from unraid_template import validate as validate_unraid

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 50 * 1024 * 1024


def tracked(root=ROOT):
    if (root / '.git').exists():
        raw = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root)
        return [p for p in raw.decode().split('\0') if p]
    excluded = {'.venv', '.git', '.devdata', '__pycache__', 'release-output', '.mypy_cache'}
    return sorted(p.relative_to(root).as_posix() for p in root.rglob('*')
                  if p.is_file() and not excluded.intersection(p.relative_to(root).parts))


def check(root=ROOT, database=None):
    template_text = (root/'templates/pullarr.xml').read_text()
    validate_unraid(root, placeholders='__PULLARR_IMAGE__' in template_text)
    names = tracked(root)
    assert names, 'No intended public files (stage candidate files first)'
    errors = []
    manifest = json.loads((root / 'licenses/vendor-manifest.json').read_text())
    admitted = {r['path']: r for r in manifest['files']}
    folded = set()
    configured = []
    if database:
        with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
            for key, value in db.execute('SELECT key,value FROM config'):
                if not value:
                    continue
                if any(word in key.lower() for word in ('password', 'api_key', 'api_token')):
                    configured.append(str(value).encode())
                if key.startswith(('release_source_v1:', 'sab_client_v1:', 'managed_client_v1:')):
                    for k, v in json.loads(value).items():
                        if v and k in ('password', 'api_key'):
                            configured.append(str(v).encode())
    for name in names:
        path = root / name
        if path.is_symlink():
            errors.append((name, 'symlink not admitted'))
            continue
        if name.casefold() in folded:
            errors.append((name, 'case collision'))
        folded.add(name.casefold())
        if (re.search(r'(?i)(?:^|/)(?:\.devdata|\.env(?:\..+)?|AGENTS\.md|release-output|node_modules)(?:/|$)', name)
                or name.startswith('docs/development/')
                or re.search(r'(?i)\.(?:db(?:-wal|-shm|-journal)?|sqlite3?(?:-wal|-shm|-journal)?|log(?:\.\d+)?|dmp|bak|torrent|nzb|key)$', name)):
            errors.append((name, 'private/runtime artifact'))
        if not path.is_file():
            errors.append((name, 'missing intended file'))
            continue
        content = path.read_bytes()
        if len(content) > LIMIT:
            errors.append((name, 'file exceeds 50MiB public limit'))
        if any(value in content for value in configured):
            errors.append((name, 'configured credential'))
        media_or_binary = path.suffix.lower() in {'.exe', '.dll', '.so', '.cbr', '.cbz', '.rar', '.zip', '.7z', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico'}
        if (b'\0' in content or media_or_binary) and name not in admitted:
            errors.append((name, 'unreviewed binary'))
        if name in admitted:
            record = admitted[name]
            normalized = content.replace(b'\r\n', b'\n') if record.get('text_lf') else content
            if hashlib.sha256(normalized).hexdigest() != record['sha256']:
                errors.append((name, 'vendor/fixture checksum changed'))
        if re.search(rb'(?i)[A-Z]:[\\/]+Users[\\/]+[^\s\\/]+', content):
            errors.append((name, 'personal home path'))
        if re.search(rb'Pullarr[-]unraid|__PULLARR[_]UNRAID[_]REPO__', content):
            errors.append((name, 'obsolete separate template repository reference'))
    for name in ('README.md', 'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md', 'SECURITY.md',
                 'CONTRIBUTING.md', 'docs/public-release-checklist.md', 'backend/lib/UnRAR-LICENSE.txt'):
        if name not in names:
            errors.append((name, 'required public document missing'))
    if any('rar_' in n and n.startswith('backend/lib/') and not Path(n).name.startswith('unrar_') for n in names):
        errors.append(('backend/lib', 'trial RAR binary forbidden'))
    compose = (root / 'docker-compose.pullarr.yml').read_text()
    if '127.0.0.1:5658:5656' not in compose or re.search(r'privileged:|docker.sock|pid:\s*host', compose):
        errors.append(('docker-compose.pullarr.yml', 'unsafe public example'))
    if (root / '.git').exists():
        stage = subprocess.check_output(['git', 'ls-files', '--stage'], cwd=root, text=True)
        if not re.search(r'^100755 [^\n]+\tentrypoint.sh$', stage, re.M):
            errors.append(('entrypoint.sh', 'executable bit missing'))
    assert not errors, json.dumps(errors)
    print(json.dumps(dict(public_files=len(names), bytes=sum((root/n).stat().st_size for n in names),
                          private_artifacts='PASS', binaries_licenses='PASS', configured_secrets='PASS' if database else 'not supplied; run history scanner too')))


def history(scanner):
    assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT), 'History gate requires a clean committed candidate'
    output = ROOT / 'release-output'
    output.mkdir(exist_ok=True)
    # Positive control: generated invalid/synthetic PAT shape, never a real token.
    with tempfile.TemporaryDirectory(prefix='pullarr-scanner-control-') as temporary:
        p = Path(temporary) / 'control.txt'
        p.write_text('token = "' + 'gh' + 'p_' + ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(36)) + '"')
        result = subprocess.run([scanner, 'dir', '--redact=100', '--no-banner', temporary],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert result.returncode == 1, 'Secret scanner positive control failed'
    result = subprocess.run([scanner, 'git', '--redact=100', '--no-banner', '--log-opts=--all',
                             '--report-format=json', '--report-path=' + str(output/'history-secrets.json'), str(ROOT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert result.returncode == 0, 'History secret findings: inspect redacted report privately'
    with tempfile.TemporaryDirectory(prefix='pullarr-public-tree-') as temporary:
        for name in tracked():
            p = Path(temporary) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes((ROOT/name).read_bytes())
        result = subprocess.run([scanner, 'dir', '--redact=100', '--no-banner', '--report-format=json',
                                 '--report-path=' + str(output/'tree-secrets.json'), temporary],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert result.returncode == 0, 'Current-tree secret findings: inspect redacted report privately'
    objects = subprocess.check_output(['git', 'rev-list', '--objects', '--all'], cwd=ROOT)
    sizes = subprocess.check_output(['git', 'cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)'],
                                    input=b'\n'.join(line.split(b' ', 1)[0] for line in objects.splitlines())+b'\n', cwd=ROOT)
    assert all(int(row.split()[2]) <= LIMIT for row in sizes.splitlines() if row.split()[1] == b'blob')
    print('Full-history and current-tree Gitleaks entropy/rule scan PASS; positive control PASS; historical size PASS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--gitleaks', default='gitleaks')
    parser.add_argument('--database', help='Optional read-only local configured-secret comparison; never needed by CI')
    args = parser.parse_args()
    check(database=args.database)
    if args.history:
        history(args.gitleaks)
