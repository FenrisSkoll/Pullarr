"""Deterministic quality semantics; no provider/indexer/network dependency."""

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from PIL import Image

from backend.base.quality import (CLASSES, ClaimedQuality, QualityError,
                                  classify, compare, cutoff_satisfied,
                                  default_policy, validate_policy)
from backend.implementations.file_quality import aggregate, analyze


def policy():
    return validate_policy(dict(groups=[
        dict(name='Base', classes=[c for c in CLASSES if c != 'hd_digital'], allowed=True),
        dict(name='Preferred', classes=['hd_digital'], allowed=True)],
        cutoff=1, upgrades=True, minimum_p10=1000))


class QualityTests(unittest.TestCase):
    def test_claims(self):
        for label, expected in [('HD-Digital','hd_digital'), ('SD-Digital','sd_digital'),
                ('Digital','digital'), ('Scan','scan'), ('Upscaled','upscaled'), ('HD-Upscaled','hd_upscaled')]:
            for value in (f'Issue #1 ({label})', f'Ünicode [{label.lower().replace("-", " ")}]'):
                self.assertEqual(classify(value).quality_class, expected)
        for title in ('Digital Man #1', 'The Digital World #3', '(Retail)', '(Not Digital)', '<script>Digital</script>'):
            self.assertEqual(classify(title).quality_class, 'unknown')
        self.assertTrue(classify('(HD-Digital) (Scan)').conflict)
        self.assertEqual(classify('(Digital) (Digital)').quality_class, 'digital')
        with self.assertRaises(QualityError):
            classify('x'*1001)

    def test_default_no_upgrade(self):
        p = validate_policy(default_policy())
        self.assertFalse(p['upgrades'])
        self.assertEqual(compare(p, classify('(HD-Digital)'), current=ClaimedQuality())['result'], 'equal')

    def test_comparison_and_verification(self):
        p = policy()
        low, high = classify('(Digital)'), classify('(HD-Digital)')
        self.assertEqual(compare(p, high, current=low)['result'], 'provisional_upgrade')
        self.assertEqual(compare(p, low, current=low)['result'], 'equal')
        self.assertEqual(compare(p, low, current=high)['result'], 'downgrade')
        facts = dict(integrity='valid', short_edge=dict(p10=900))
        self.assertEqual(compare(p, high, current=low, verified=facts, post_import=True)['reason'], 'dimension_floor_failed')
        facts['short_edge']['p10'] = 1800
        self.assertEqual(compare(p, high, current=low, verified=facts, post_import=True)['result'], 'upgrade')
        self.assertTrue(cutoff_satisfied(p, high, facts))
        self.assertFalse(cutoff_satisfied(p, low, facts))
        self.assertFalse(cutoff_satisfied(p, high, None))
        self.assertEqual(compare(p, classify('(Scan)(Digital)'))['reason'], 'conflicting_claims')

    def test_policy_validation(self):
        p = default_policy()
        p['groups'][0]['classes'].append('digital')
        with self.assertRaises(QualityError):
            validate_policy(p)
        p = default_policy()
        p['cutoff'] = True
        with self.assertRaises(QualityError):
            validate_policy(p)

    def test_robust_orientation_independent_metric(self):
        self.assertEqual(aggregate([900]*24+[9000])['p10'], 900)
        self.assertEqual(aggregate([900]*24+[9000])['median'], 900)
        self.assertEqual(aggregate([10]+[900]*24)['p10'], 900)

    def test_archive_and_legacy_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'book.cbz'
            with ZipFile(path, 'w') as archive:
                for index, (width, height, codec) in enumerate([(1200,1800,'PNG'), (1800,1200,'JPEG'), (900,1300,'PNG')]):
                    output = BytesIO()
                    Image.new('RGB', (width,height), 'white').save(output, format=codec)
                    archive.writestr(f'{index}.png' if codec == 'PNG' else f'{index}.jpg', output.getvalue())
                archive.writestr('ComicInfo.xml', b'<ComicInfo><Title>Test</Title></ComicInfo>')
            before = path.read_bytes()
            facts = analyze(str(path))
            self.assertEqual(facts['pages'], 3)
            self.assertEqual(facts['short_edge']['p10'], 900)
            self.assertEqual(facts['spreads'], 1)
            self.assertEqual(facts['codecs'], {'JPEG': 1, 'PNG': 2})
            self.assertEqual(facts['metadata'], ['comicinfo.xml'])
            self.assertNotIn('source', facts)
            self.assertNotIn('dpi', facts)
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(analyze(str(path), verify_pixels=True)['integrity'], 'valid')

    def test_invalid_traversal_and_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'bad.cbz'
            for name in ('../page.png', 'page.png'):
                with ZipFile(path, 'w') as archive:
                    archive.writestr(name, b'not an image')
                with self.assertRaises(QualityError):
                    analyze(str(path))
            with self.assertRaisesRegex(QualityError, 'cancelled'):
                analyze(str(path), cancelled=lambda: True)


if __name__ == '__main__':
    unittest.main()
