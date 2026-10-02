"""Quality identities survive real authority switches and domain display changes."""

from types import SimpleNamespace
from unittest import TestCase

import TProviderSwitchApply as switches
from fixtures.quality import comic

from backend.base.quality import default_policy
from backend.implementations.file_quality import analyze
from backend.internals.quality import QualityStore


class QualityDomainTests(TestCase):
    def test_switch_aba_display_repair_and_deleted_file_preserve_history(self):
        fixture=switches.SwitchApplyTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.source();db=fixture.db;store=QualityStore(db.cursor())
        comic(fixture.path,1200)
        db.execute('UPDATE files SET size=? WHERE id=1',(fixture.path.stat().st_size,))
        profile=store.save('Assigned',default_policy())
        store.assign('volume',1,profile['id'],None)
        assessment=store.assessment(1,analyze(str(fixture.path)))
        candidate=SimpleNamespace(raw_title='Original (Digital)',candidate_id='original',
            source=SimpleNamespace(key='fixture',kind=SimpleNamespace(value='newznab')))
        receipt=store.selected(1,candidate,reason='manual',decision={'score':5})
        db.execute("UPDATE acquisition_provenance SET state='imported',file_id=1,assessment_id=? WHERE id=?",(assessment,receipt));db.commit()
        original=store.detail(receipt)
        for provider,parent,first in [('metron','700',701),('comicvine','100',101)]:
            fixture.apply(fixture.review(switches.remote(provider,parent,first)))
            self.assertEqual(store.detail(receipt),original)
            self.assertEqual(store.effective([1])[1]['profile']['id'],profile['id'])
            self.assertEqual(store.issue_states([1])[0]['files'][0]['assessment_id'],assessment)
        db.execute("UPDATE volumes SET title='Repaired title',year=2029 WHERE id=1")
        db.execute("UPDATE issues SET date='2030-02-03' WHERE id=1")
        self.assertEqual(store.detail(receipt),original)
        store.save('Edited',default_policy(),identifier=profile['id'],revision=1)
        self.assertEqual(store.detail(receipt)['profile_snapshot']['name'],'Assigned')
        db.execute('DELETE FROM files WHERE id=1');db.commit()
        self.assertIsNone(store.detail(receipt)['file_id'])
        self.assertEqual(store.detail(receipt)['verified']['short_edge']['p10'],1200)
        self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
