"""Publication defaults cannot silently become privileged or auto-publishing."""
import sys
from pathlib import Path
from unittest import TestCase

import yaml

ROOT=Path(__file__).resolve().parents[1]


class UnraidPackaging(TestCase):
    def test_runtime_contract(self):
        sys.path.insert(0,str(ROOT/'scripts'))
        try:
            from unraid_docker_gate import contract
            paths,variables=contract(None)
            self.assertEqual(set(paths),{'/app/db','/app/logs','/app/temp_downloads','/data'})
            self.assertEqual(variables,{'PUID':'99','PGID':'100','TZ':'Etc/UTC'})
        finally:
            sys.path.remove(str(ROOT/'scripts'))

    def test_manual_publication_defaults(self):
        workflow=yaml.load((ROOT/'.github/workflows/container-release.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']),{'workflow_dispatch'})
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['publish']['default'],'false')
        self.assertEqual(workflow['permissions'],{'contents':'read'})
        job=workflow['jobs']['publish']
        self.assertEqual(job['if'],'${{ inputs.publish }}')
        self.assertEqual(job['environment'],'container-release')
        self.assertEqual(job['needs'],'validate')
        commands='\n'.join(s.get('run','') for s in job['steps'])
        self.assertIn('linux/amd64',commands)
        self.assertIn('org.opencontainers.image.revision=$GITHUB_SHA',commands)
        self.assertIn('python scripts/unraid_docker_gate.py',commands)
        self.assertIn('if [ "$STABLE" = true ]',commands)
        self.assertIn('refusing overwrite',commands)
