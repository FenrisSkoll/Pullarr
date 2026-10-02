"""All committed JavaScript harnesses and production syntax; local Node only."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in sorted((ROOT/'frontend/static/js').glob('*.js')):
    subprocess.run(['node', '--check', str(path)], check=True, cwd=ROOT)
for path in sorted((ROOT/'tests/js').glob('*.cjs')):
    subprocess.run(['node', str(path)], check=True, cwd=ROOT)
