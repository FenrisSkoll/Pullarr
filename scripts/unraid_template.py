"""Local CA validation/materialization. No publication or remote control."""
import argparse
import hashlib
import re
import shutil
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKERS = {'__PULLARR_GITHUB_OWNER__', '__PULLARR_APP_REPO__', '__PULLARR_IMAGE__'}
PNG = 'docs/assets/pullarr-ca.png'
SVG = 'frontend/static/img/favicon.svg'
PNG_SHA256 = 'bddedc7ff4aa0eeaeba678b5e3b8c530b2eb9862130eaa0f56d521465c8b6a22'
PATHS = {'/app/db': '/mnt/user/appdata/pullarr/db', '/app/logs': '/mnt/user/appdata/pullarr/logs',
         '/app/temp_downloads': '/mnt/user/appdata/pullarr/temp_downloads', '/data': '/mnt/user/data'}


def parse(path):
    data = path.read_bytes()
    assert len(data) < 32768 and b'<!DOCTYPE' not in data.upper() and b'<!ENTITY' not in data.upper()
    return ET.fromstring(data)


def validate(root=ROOT, placeholders=False, local_image=False):
    app = parse(root/'templates/pullarr.xml')
    profile = parse(root/'ca_profile.xml')
    assert app.tag == 'Container' and app.attrib == {'version': '2'}
    assert profile.tag == 'CommunityApplications'
    assert sorted(x.tag for x in profile) == ['Icon','Profile','WebPage']
    tags = [x.tag for x in app if x.tag != 'Config']
    assert len(tags) == len(set(tags))
    fixed = {'Name':'Pullarr', 'Network':'bridge', 'Shell':'bash', 'Privileged':'false',
             'WebUI':'http://[IP]:[PORT:5656]', 'Category':'MediaApp:Books', 'License':'GPL-3.0'}
    for key, value in fixed.items():
        assert app.findtext(key) == value, key
    assert not any(app.find(x) is not None for x in ('ExtraParams', 'PostArgs', 'Support', 'Donate', 'Discord'))
    for key in ('Repository','Registry','Icon','Overview','Project','TemplateURL','ReadMe','Requires','ExtraSearchTerms'):
        assert app.findtext(key), key
    assert profile.findtext('Profile') and profile.findtext('WebPage')
    configs = app.findall('Config')
    assert len(configs) == 8
    assert len({c.get('Name') for c in configs}) == len(configs)
    assert len({c.get('Target') for c in configs}) == len(configs)
    for c in configs:
        target, kind = c.get('Target'), c.get('Type')
        assert c.get('Description') and c.get('Mask') == 'false'
        if kind == 'Port':
            assert target == c.get('Default') == '5656' and c.get('Mode') == 'tcp'
        elif kind == 'Path':
            assert PATHS.get(target) == c.get('Default') and c.get('Mode') == 'rw'
        else:
            assert kind == 'Variable' and {'PUID':'99','PGID':'100','TZ':'Etc/UTC'}.get(target) == c.get('Default')
            assert c.get('Display') == 'advanced'
    text = (root/'templates/pullarr.xml').read_text() + (root/'ca_profile.xml').read_text()
    markers = set(re.findall(r'__[A-Z_]+__', text))
    assert markers == MARKERS if placeholders else not markers, 'Unresolved/unknown placeholders'
    if placeholders:
        values = {'__PULLARR_GITHUB_OWNER__':'local-validation','__PULLARR_APP_REPO__':'app',
                  '__PULLARR_IMAGE__':'ghcr.io/local-validation/pullarr:latest'}
        def substituted(element):
            value = ET.tostring(element).decode()
            for key, replacement in values.items(): value = value.replace(key,replacement)
            return ET.fromstring(value)
        app, profile = substituted(app), substituted(profile)
    image = app.findtext('Repository')
    project = app.findtext('Project')
    assert re.fullmatch(r'https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', project)
    owner = project.split('/')[3]
    public_image = re.fullmatch(r'ghcr\.io/'+re.escape(owner.lower())+r'/pullarr:[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}', image)
    assert public_image or (local_image and re.fullmatch(r'pullarr:[a-z0-9][a-z0-9_.-]{0,127}',image)), 'Invalid image reference'
    raw = app.findtext('TemplateURL')
    assert re.fullmatch(r'https://raw\.githubusercontent\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/main/templates/pullarr\.xml', raw)
    prefix = raw.removesuffix('templates/pullarr.xml')
    assert prefix == project.replace('github.com','raw.githubusercontent.com')+'/main/'
    assert app.findtext('Icon') == prefix+PNG and app.findtext('ReadMe') == prefix+'README.md'
    assert profile.findtext('Icon') == prefix+PNG
    assert profile.findtext('WebPage') == project
    assert app.findtext('Registry') == project+'/pkgs/container/pullarr'
    assert not re.search(r'(?i)(?:password|api_key|token)\s*[=:]|https?://[^/\s]+@|docker\.sock|/mnt/user/[^<]*secret', text)
    png = (root/PNG).read_bytes()
    assert png[:8] == b'\x89PNG\r\n\x1a\n' and struct.unpack('>II',png[16:24]) == (256,256)
    assert hashlib.sha256(png).hexdigest() == PNG_SHA256
    assert len(png) < 100000 and parse(root/SVG).tag.endswith('svg')
    assert (root/'LICENSE').is_file() and (root/'README.md').is_file()
    assert 'GNU GENERAL PUBLIC LICENSE' in (root/'LICENSE').read_text()
    print('CA XML/semantic/security/icon gate PASS ('+('explicit placeholders' if placeholders else 'materialized')+')')
    return app


def materialize(owner, app, image, output, *, local_image=False):
    assert owner and app and image and output, 'Owner, app repository, image and output are required'
    assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*',owner)
    assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',app)
    assert image.startswith('ghcr.io/'+owner.lower()+'/pullarr:') or (local_image and re.fullmatch(r'pullarr:[a-z0-9][a-z0-9_.-]{0,127}',image))
    output = output.resolve()
    assert not output.exists(), 'Choose a new output directory; never overwrite source'
    values = dict(zip(('__PULLARR_GITHUB_OWNER__','__PULLARR_APP_REPO__','__PULLARR_IMAGE__'),(owner,app,image)))
    output.mkdir(parents=True)
    (output/'templates').mkdir()
    for name in ('ca_profile.xml','templates/pullarr.xml'):
        text = (ROOT/name).read_text()
        for key,value in values.items(): text = text.replace(key,value)
        (output/name).write_text(text,encoding='utf-8')
    for name in ('LICENSE','README.md',SVG,PNG):
        (output/name).parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/name,output/name)
    validate(output,local_image=local_image)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-placeholders',action='store_true')
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--materialize',type=Path)
    parser.add_argument('--owner')
    parser.add_argument('--app-repo')
    parser.add_argument('--image')
    parser.add_argument('--local-test',action='store_true',help='Allow an unpublished pullarr:TAG image for disposable acceptance only')
    args = parser.parse_args()
    if args.materialize:
        materialize(args.owner,args.app_repo,args.image,args.materialize,local_image=args.local_test)
    else:
        validate(args.root,args.allow_placeholders,local_image=args.local_test)
