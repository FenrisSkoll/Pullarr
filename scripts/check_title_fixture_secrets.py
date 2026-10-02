"""Pipe pack output directly to gate inside the development container.

pack reads tracked working files AND index blobs; output is transport only, never
display it. gate reads allowlisted provider/application and private acquisition
secrets into memory and prints CLEAN or affected filenames, never matched text
or secrets.
"""

import base64
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path


def main():
    if len(sys.argv) == 1:
        affected = []
        root = Path(__file__).resolve().parents[1]
        for path in (root / 'tests/fixtures').rglob('*.json'):
            content = path.read_bytes()
            if (re.search(rb'(?i)"(?:authorization|cookie|cookies|api_key|token|account_id|settings)"\s*:', content)
                    or re.search(rb'(?i)(?:api_key=|Bearer\s+[A-Za-z0-9._-]{10})', content)):
                affected.append(path.relative_to(root).as_posix())
        print('\n'.join(affected) if affected else 'Fixture credential fields CLEAN')
        sys.exit(bool(affected))
    if sys.argv[1] == 'pack':
        paths = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
        payload = []
        for name in filter(None, paths):
            indexed = subprocess.check_output(['git', 'show', ':' + name])
            payload.append((name, base64.b64encode(indexed).decode()))
            path = Path(name)
            if path.is_file():
                payload.append((name, base64.b64encode(path.read_bytes()).decode()))
        print(json.dumps(payload))
        return
    if sys.argv[1] != 'gate':
        raise ValueError('Expected pack or gate')
    with sqlite3.connect('file:/app/db/Kapowarr.db?mode=ro', uri=True) as db:
        secrets = [str(row[0]).encode() for row in db.execute(
            'SELECT value FROM config WHERE key IN (?,?,?)',
            ('comicvine_api_key', 'metron_api_token', 'api_key')) if row[0]]
        for row in db.execute("SELECT value FROM config WHERE key GLOB 'release_source_v1:*' OR key GLOB 'sab_client_v1:*' OR key GLOB 'managed_client_v1:*'"):
            configured = json.loads(row[0])
            for name in ('api_key', 'password'):
                key = configured.get(name)
                if key:
                    secrets.append(key.encode())
    affected = set()
    for name, encoded in json.load(sys.stdin):
        content = base64.b64decode(encoded)
        if any(secret in content for secret in secrets):
            affected.add(name)
        if name.startswith('tests/fixtures/collected_editions/'):
            if (re.search(rb'(?i)"(?:authorization|cookie|cookies|api_key|token|account_id|settings)"\s*:', content)
                    or re.search(rb'(?i)(?:api_key=|Bearer\s+[A-Za-z0-9._-]{10})', content)):
                affected.add(name)
        if name.startswith('.devdata/') or name.endswith(('.db', '.sqlite', '.sqlite3')):
            affected.add(name)
    print('\n'.join(sorted(affected)) if affected else 'CLEAN')
    if affected:
        sys.exit(1)


if __name__ == '__main__':
    main()
