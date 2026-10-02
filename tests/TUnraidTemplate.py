import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from unraid_template import PNG, ROOT, SVG, materialize, validate

PENDING = '__PULLARR_IMAGE__' in (ROOT/'templates/pullarr.xml').read_text()


class TemplateTests(unittest.TestCase):
    def copy(self, destination):
        for name in ('ca_profile.xml','templates/pullarr.xml',PNG,SVG,'LICENSE','README.md'):
            target=destination/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/name,target)

    def test_source_state(self):
        pending='__PULLARR_IMAGE__' in (ROOT/'templates/pullarr.xml').read_text()
        validate(placeholders=pending)
        if pending:
            with self.assertRaises(AssertionError): validate()

    def test_materialization(self):
        if '__PULLARR_IMAGE__' not in (ROOT/'templates/pullarr.xml').read_text():
            return
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'generated'
            materialize('validation-only','source','ghcr.io/validation-only/pullarr:latest',output)
            validate(output)
            with self.assertRaises(AssertionError):
                materialize('validation-only','source','ghcr.io/validation-only/pullarr:latest',output)

    def test_reject_unsafe_and_inconsistent(self):
        changes=[('<Privileged>false','<Privileged>true'),('<Network>bridge','<Network>host'),
                 ('Target="5656"','Target="5658"'),('Target="/data"','Target="/"'),
                 ('Default="99"','Default="0"'),('MediaApp:Books','Invented:Category'),
                 ('GPL-3.0','GPLv2'),('templates/pullarr.xml','wrong.xml'),
                 ('</Container>','<ExtraParams>--privileged</ExtraParams></Container>'),
                 ('<?xml version="1.0" encoding="utf-8"?>','<!DOCTYPE bad>')]
        for old,new in changes:
            with self.subTest(old=old), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); self.copy(root)
                path=root/'templates/pullarr.xml'
                path.write_text(path.read_text().replace(old,new))
                with self.assertRaises(AssertionError): validate(root,placeholders=PENDING)

    def test_reject_owner_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            for owner in ('../owner','owner?token=secret','owner@host','owner;command'):
                with self.assertRaises(AssertionError):
                    materialize(owner,'source','ghcr.io/owner/pullarr:latest',Path(directory)/'generated')

    @unittest.skipUnless(PENDING, 'Materialization applies to the tokenized source template')
    def test_local_image_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'generated'
            with self.assertRaises(AssertionError):
                materialize('validation-only','source','pullarr:test',output)
            materialize('validation-only','source','pullarr:test',output,local_image=True)
            validate(output,local_image=True)
            with self.assertRaises(AssertionError):
                validate(output)

    @unittest.skipUnless(PENDING, 'Materialization applies to the tokenized source template')
    def test_deterministic_same_repository_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory)/'first', Path(directory)/'second'
            for output in (first,second):
                materialize('validation-only','source','ghcr.io/validation-only/pullarr:stable',output)
            for path in first.rglob('*'):
                if path.is_file():
                    self.assertEqual(path.read_bytes(),(second/path.relative_to(first)).read_bytes())
            xml = (first/'templates/pullarr.xml').read_text()
            self.assertIn('/validation-only/source/main/templates/pullarr.xml',xml)
            self.assertIn('/validation-only/source/main/README.md',xml)
            self.assertNotIn('__PULLARR_',xml)

    def test_missing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            for values in (('', 'source', 'pullarr:test'), ('owner', '', 'pullarr:test'), ('owner','source','')):
                with self.assertRaises(AssertionError):
                    materialize(*values,Path(directory)/'generated',local_image=True)


if __name__=='__main__': unittest.main()
