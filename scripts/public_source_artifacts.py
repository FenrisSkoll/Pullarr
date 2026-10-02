"""Create and validate local source artifacts from committed HEAD; never upload."""
import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT), 'Commit final source first'
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    output = ROOT / 'release-output' / 'public' / revision[:12]
    output.mkdir(parents=True, exist_ok=True)
    checksums = []
    for extension, format_name in (('zip', 'zip'), ('tar.gz', 'tar.gz')):
        archive = output / f'pullarr-source-{revision[:12]}.{extension}'
        subprocess.run(['git', 'archive', '--format='+format_name, '-o', str(archive), 'HEAD'], cwd=ROOT, check=True)
        with tempfile.TemporaryDirectory(prefix='pullarr-export-') as directory:
            destination = Path(directory)
            if extension == 'zip':
                with zipfile.ZipFile(archive) as source:
                    source.extractall(destination)
                if sys.platform != 'win32':
                    (destination/'backend/lib/unrar_linux_64').chmod(0o755)
            else:
                with tarfile.open(archive) as source:
                    source.extractall(destination, filter='data')
            assert not (destination/'.git').exists()
            subprocess.run([sys.executable, 'scripts/public_release_gate.py'], cwd=destination, check=True)
            environment = dict(os.environ, PYTHONPATH=str(destination/'tests'))
            subprocess.run([sys.executable, '-m', 'unittest', 'TPublicSecurity',
                            'TArchiveMaintenance.ArchiveEngineTests', 'TUnraidTemplate',
                            'TUnraidPackaging'], cwd=destination, env=environment, check=True)
            subprocess.run([sys.executable, 'Pullarr.py', '--help'], cwd=destination, env=environment,
                           stdout=subprocess.DEVNULL, check=True)
        checksums.append(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name)
    notes = output/'RELEASE-NOTES.md'
    notes.write_text(f'# Pullarr source candidate\n\nCommit: {revision}\n\n'
                     'Includes the Unraid CA template. Listing remains pending public URLs/image '
                     'and official Validate/Scan; nothing is published by this script.\n', encoding='utf-8')
    checksums.append(hashlib.sha256(notes.read_bytes()).hexdigest()+'  '+notes.name)
    (output/'SHA256SUMS').write_text('\n'.join(checksums)+'\n', encoding='ascii')
    (output.parent/'CURRENT').write_text(revision[:12]+'\n', encoding='ascii')
    print('\n'.join(checksums))


if __name__ == '__main__':
    main()
