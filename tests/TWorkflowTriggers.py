"""The source gate runs for main changes, not for release tag pushes."""

from fnmatch import fnmatchcase
from pathlib import Path
from unittest import TestCase

import yaml

ROOT = Path(__file__).resolve().parents[1]


class SourceGateTriggers(TestCase):
    def test_branch_push_pr_and_manual_only(self):
        workflow = yaml.load((ROOT / '.github/workflows/tests.yml').read_text(),
                             Loader=yaml.BaseLoader)
        events = workflow['on']
        self.assertEqual(set(events), {'push', 'pull_request', 'workflow_dispatch'})
        push = events['push']
        pull_request = events['pull_request']
        self.assertEqual(push['branches'], ['main'])
        self.assertEqual(pull_request['branches'], ['main'])
        self.assertNotIn('tags', push)
        self.assertTrue(fnmatchcase('main', push['branches'][0]))
        self.assertFalse(fnmatchcase('v1.5.8', push['branches'][0]))
        self.assertEqual(set(workflow['jobs']), {'static', 'windows', 'linux-and-image'})
        self.assertEqual(workflow['permissions']['contents'], 'read')
        release = yaml.load((ROOT / '.github/workflows/container-release.yml').read_text(),
                            Loader=yaml.BaseLoader)
        self.assertEqual(set(release['on']), {'workflow_dispatch'})
        self.assertEqual(release['jobs']['publish']['environment'], 'container-release')
