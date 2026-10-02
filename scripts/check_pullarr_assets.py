"""Deterministic current-product brand, local asset and contrast checks."""
import json
import re
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]


def contrast(a, b):
    def luminance(value):
        value = value.lstrip('#')
        if len(value) == 3:
            value = ''.join(c * 2 for c in value)
        channels = [int(value[i:i+2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, (.2126, .7152, .0722)))
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + .05) / (dark + .05)


def check():
    errors = []
    templates = ROOT / 'frontend/templates'
    for path in templates.glob('*.html'):
        text = path.read_text(encoding='utf-8')
        for line in text.splitlines():
            if 'Kapowarr' in line and not (path.name == 'status.html' and
                    ('derived from Kapowarr' in line or 'Kapowarr upstream source' in line)):
                errors.append(f'{path.name}: unexplained product branding')
        if re.search(r'<(?:script|link)[^>]+(?:src|href)=["\']https?://', text):
            errors.append(f'{path.name}: external executable/style asset')
        for asset in re.findall(r"filename=['\"]([^'\"]+)['\"]", text):
            if asset == 'img/':  # icon_button macro concatenates a server-owned name
                continue
            if not (ROOT / 'frontend/static' / asset).is_file():
                errors.append(f'{path.name}: missing {asset}')
    for name in ('favicon.svg', 'cover-placeholder.svg'):
        path = ROOT / 'frontend/static/img' / name
        tree = ElementTree.parse(path)
        assert tree.getroot().tag.endswith('svg')
        assert not re.search(r'<script|onload=|file:|Users[/\\]', path.read_text(), re.I)
    assert (ROOT / 'frontend/static/vendor/socket.io-LICENSE').is_file()
    assert (ROOT / 'LICENSE').is_file()
    for path in (ROOT / 'frontend/static/js').glob('*.js'):
        if 'Kapowarr' in path.read_text(encoding='utf-8'):
            errors.append(f'{path.name}: unexplained current product branding')
    css = (ROOT / 'frontend/static/css/pullarr.css').read_text()
    contrasts = {}
    for theme, selector in [('light', ':root'), ('dark', ':root.dark-mode')]:
        block = re.search(re.escape(selector) + r'\s*\{([^}]+)', css).group(1)
        colors = dict(re.findall(r'--([\w-]+):\s*(#[\da-fA-F]+)', block))
        for foreground, background in [('ink', 'surface'), ('muted', 'surface'), ('brand', 'canvas'),
                                       ('on-brand', 'brand'), ('negative', 'surface'), ('info', 'surface')]:
            ratio = contrast(colors[foreground], colors[background])
            assert ratio >= 4.5, (theme, foreground, background, ratio)
            contrasts[f'{theme}:{foreground}/{background}'] = round(ratio, 2)
        assert contrast(colors['focus'], colors['surface']) >= 3
    sizes = {kind: sum(p.stat().st_size for p in (ROOT / 'frontend/static' / folder).rglob('*') if p.is_file())
             for kind, folder in [('css_bytes', 'css'), ('js_bytes', 'js'), ('image_bytes', 'img'), ('vendor_bytes', 'vendor')]}
    assert not errors, errors
    return dict(brand='PASS', local_assets='PASS', contrasts=contrasts, **sizes)


if __name__ == '__main__':
    print(json.dumps(check()))
