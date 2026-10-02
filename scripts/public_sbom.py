"""Generate CycloneDX environment SBOM and add checksum-pinned source vendors.

The separate image SBOM should be generated with an established image scanner.
No repository files or runtime configuration are uploaded by this command.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    output = ROOT/'release-output'
    output.mkdir(exist_ok=True)
    target = output/'python-vendor-sbom.cdx.json'
    subprocess.run([sys.executable, '-m', 'cyclonedx_py', 'environment',
                    '--output-reproducible', '--of', 'JSON', '-o', str(target)], check=True)
    document = json.loads(target.read_text())
    for entry in json.loads((ROOT/'licenses/vendor-manifest.json').read_text())['files']:
        name = entry['path']
        license_name = entry['license']
        license_value = {'name': license_name} if license_name.startswith('LicenseRef-') else {'id': license_name}
        component = {'type':'file', 'bom-ref':'pullarr-vendor:'+name, 'name':name,
                     'hashes':[{'alg':'SHA-256','content':entry['sha256']}],
                     'licenses':[{'license':license_value}],
                     'properties':[{'name':'pullarr:provenance','value':entry['source']}]}
        if entry.get('version'):
            component['version'] = entry['version']
        document['components'].append(component)
    document['components'].append({'type':'data', 'bom-ref':'pullarr:gcd-fixtures',
        'name':'GCD adapted metadata fixtures', 'licenses':[{'license':{'id':'CC-BY-SA-4.0'}}],
        'externalReferences':[{'type':'website','url':'https://www.comics.org/'}]})
    target.write_text(json.dumps(document, indent=2)+'\n', encoding='utf-8')
    print('Path-free Python/vendor CycloneDX SBOM:', target.name)


if __name__ == '__main__':
    main()
