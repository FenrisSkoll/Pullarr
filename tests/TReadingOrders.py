"""Stable sequences, conservative identity and bounded CBL semantics."""

import sqlite3
from unittest import TestCase

from backend.base.reading_orders import (ReadingOrderError,
                                         export_cbl, parse_cbl)
from backend.internals.db import DB_SCHEMA
from backend.internals.reading_orders import ReadingOrderStore

CBL = b'''<?xml version="1.0" encoding="utf-8"?><ReadingList><Name>Fixture</Name><Books>
<Book Series="One" Number="1" Volume="2026"><Database Name="cv" Series="100" Issue="101"/></Book>
<Book Series="Two" Number="Annual"/><Book Series="One" Number="1" Volume="2026"><Database Name="cv" Series="100" Issue="101"/></Book>
<Book Series="Outside" Number="1.5"><Database Name="metron" Series="200" Issue="201"/></Book>
</Books></ReadingList>'''


class ReadingOrdersTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(DB_SCHEMA)
        self.db.execute("INSERT INTO root_folders VALUES(1,'fixture-root')")
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,year,root_folder,folder,monitored) VALUES(1,100,'One',2026,1,'fixture-folder',1)")
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,monitored) VALUES(1,1,101,'1',1)")
        self.db.commit()
        self.store = ReadingOrderStore(self.db.cursor())

    def imported(self):
        model = parse_cbl(CBL)
        return self.store.accept_model(model, self.store.match(model), dict(kind='cbl_import', version=1))

    def test_roundtrip_repeats_and_exact_external(self):
        order = self.imported()
        page = self.store.entries(order['id'])
        self.assertEqual([r['status'] for r in page['items']], ['missing', 'unresolved', 'missing', 'external'])
        self.assertNotEqual(page['items'][0]['id'], page['items'][2]['id'])
        exported = self.store.export(order['id'])
        self.assertEqual(exported, self.store.export(order['id']))
        self.assertEqual(parse_cbl(exported)['entries'], parse_cbl(CBL)['entries'])

    def test_parser_rejects_entities_depth_and_html(self):
        for raw in (b'<!DOCTYPE ReadingList [<!ENTITY x "bomb">]><ReadingList/>', b'<html>login</html>',
                    b'<ReadingList>' + b'<X>'*10 + b'</X>'*10 + b'</ReadingList>'):
            with self.subTest(raw=raw), self.assertRaises(ReadingOrderError):
                parse_cbl(raw)

    def test_unsupported_unqualified_ids_not_fabricated(self):
        model = parse_cbl(b'<ReadingList><Books><Book Series="X"><IssueID>123</IssueID></Book></Books></ReadingList>')
        self.assertFalse(model['entries'][0]['refs'])
        self.assertTrue(model['warnings'])

    def test_move_revision_and_delete_preserve_library(self):
        order = self.imported()
        ids = [r['id'] for r in self.store.entries(order['id'])['items']]
        order = self.store.move(order['id'], order['revision'], ids[0], 3)
        self.assertEqual([r['id'] for r in self.store.entries(order['id'])['items']], ids[1:]+ids[:1])
        with self.assertRaisesRegex(ReadingOrderError, 'revision_conflict'):
            self.store.move(order['id'], 0, ids[1], 0)
        self.store.delete(order['id'], order['revision'], True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM issues').fetchone()[0], 1)
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())

    def test_deletion_retains_external_sequence(self):
        order = self.imported()
        before = [r['id'] for r in self.store.entries(order['id'])['items']]
        self.db.execute('DELETE FROM volumes WHERE id=1')
        self.db.commit()
        page = self.store.entries(order['id'])
        self.assertEqual(before, [r['id'] for r in page['items']])
        self.assertEqual(page['items'][0]['status'], 'external')

    def test_subscription_pending_detach_and_repeated_id_stability(self):
        order = self.imported()
        order = self.store.attach(order['id'], order['revision'], 'cbl_url', 'https://example.com/list.cbl')
        source_id = order['source']['id']
        before = [r['id'] for r in self.store.entries(order['id'])['items']]
        model = parse_cbl(CBL)
        model['entries'] = list(reversed(model['entries']))
        self.store.observe(source_id, 0, dict(model=model, digest='a'*64))
        self.assertEqual(before, [r['id'] for r in self.store.entries(order['id'])['items']])
        pending = self.store.pending(source_id)
        self.store.decide_source(source_id, pending['revision'], order['revision'], pending['digest'], 'accept', True)
        self.assertEqual(set(before), {r['id'] for r in self.store.entries(order['id'])['items']})
        with self.assertRaisesRegex(ReadingOrderError, 'detach_required'):
            self.store.move(order['id'], self.store.get(order['id'])['revision'], before[0], 0)
        order = self.store.detach(order['id'], self.store.get(order['id'])['revision'], True)
        self.store.move(order['id'], order['revision'], before[0], 0)

    def test_text_candidate_requires_review_even_when_unique(self):
        model = parse_cbl(b'<ReadingList><Books><Book Series="One" Number="1" Volume="2026"/></Books></ReadingList>')
        item = self.store.match(model)[0]
        self.assertIsNone(item['issue_id'])
        self.assertEqual(item['match'], 'ambiguous')
        self.assertEqual(item['candidates'][0]['issue_id'], 1)

    def test_exact_reference_conflict_never_chooses_and_batched_page(self):
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder) VALUES(2,200,'Two',1,'other')")
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number) VALUES(2,2,201,'1')")
        self.db.execute("INSERT INTO issue_external_ids VALUES(2,'metron','501','fixture')")
        self.db.commit()
        raw=b'<ReadingList><Books><Book><Database Name="cv" Issue="101"/><Database Name="metron" Issue="501"/></Book></Books></ReadingList>'
        model=parse_cbl(raw); self.assertEqual(self.store.match(model)[0]['match'],'ambiguous')
        model=parse_cbl(CBL); model['entries']=[model['entries'][0]]*1000
        exported=export_cbl(model); self.assertEqual(len(parse_cbl(exported)['entries']),1000)
        order=self.store.accept_model(model,self.store.match(model),dict(kind='cbl_import'))
        selects=[]; self.db.set_trace_callback(lambda q: selects.append(q) if q.lstrip().upper().startswith(('SELECT','WITH')) else None)
        page=self.store.entries(order['id'],500,50)
        self.db.set_trace_callback(None)
        self.assertEqual(len(page['items']),50); self.assertEqual(page['total'],1000)
        self.assertLessEqual(len(selects),6)

    def test_actual_switch_aba_and_metadata_repair_keep_ids_order(self):
        import TProviderSwitchApply as switch
        fixture=switch.SwitchApplyTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups); fixture.source()
        store=ReadingOrderStore(fixture.cursor); order=store.create('Stable')
        order=store.add_local(order['id'],order['revision'],1); order=store.add_local(order['id'],order['revision'],2)
        before=fixture.db.execute('SELECT * FROM reading_order_entries').fetchall()
        fixture.apply(fixture.review(switch.remote('metron')))
        fixture.apply(fixture.review(switch.remote('comicvine',parent='100',first=101)))
        self.assertEqual(before,fixture.db.execute('SELECT * FROM reading_order_entries').fetchall())
        self.assertEqual([r['canonical_id'] for r in store.entries(order['id'])['items']],[1,2])
        import TMetadataRepairApply as repair
        other=repair.MetadataRepairApplyTests(); other.setUp(); self.addCleanup(other.doCleanups)
        store=ReadingOrderStore(other.cursor); order=store.create('Stable repair'); order=store.add_local(order['id'],0,1)
        before=other.db.execute('SELECT * FROM reading_order_entries').fetchall()
        other.apply(other.review())
        self.assertEqual(before,other.db.execute('SELECT * FROM reading_order_entries').fetchall())
        self.assertEqual(store.entries(order['id'])['items'][0]['series'],'Target comicvine')

    def test_date_change_c2_and_reading_edits_do_not_mutate_other_domains(self):
        from backend.base.content_claims import ClaimKind, PublicationRef
        from backend.internals.content_claims import (apply_coverage,
                                                      claim_preview,
                                                      confirm_claim,
                                                      coverage_preview)
        self.db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,monitored) VALUES(2,1,102,'2',1)")
        self.db.execute("INSERT INTO files(id,filepath,size) VALUES(1,'fixture-comic.cbz',1)")
        self.db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)'); self.db.commit()
        preview=claim_preview(self.db.cursor(),1,PublicationRef('comicvine','102'),ClaimKind.COMPLETE,manual=True)
        claim=confirm_claim(self.db.cursor(),1,PublicationRef('comicvine','102'),ClaimKind.COMPLETE,preview['preview_token'],manual=True)
        coverage=coverage_preview(self.db.cursor(),1,1,[claim]); apply_coverage(self.db.cursor(),1,1,[claim],coverage['preview_token'])
        order=self.store.create('Coverage'); order=self.store.add_local(order['id'],0,2)
        item=self.store.entries(order['id'])['items'][0]
        self.assertEqual(item['status'],'missing'); self.assertTrue(item['content_elsewhere']); self.assertFalse(item['wanted'])
        tables=('bibliographic_content_claims','file_content_coverage','collection_memberships','release_events','wanted_schedule','wanted_searches')
        before={t:self.db.execute('SELECT * FROM '+t).fetchall() for t in tables}
        self.db.execute("UPDATE issues SET date='2099-01-01' WHERE id=2"); self.db.commit()
        self.assertEqual(item['id'],self.store.entries(order['id'])['items'][0]['id'])
        self.store.remove(order['id'],order['revision'],item['id'])
        self.assertEqual(before,{t:self.db.execute('SELECT * FROM '+t).fetchall() for t in tables})

    def test_source_confirmation_detects_exact_match_change(self):
        order=self.store.create('Source'); order=self.store.attach(order['id'],0,'cbl_url','https://example.com/list.cbl')
        sid=order['source']['id']; model=parse_cbl(CBL)
        self.store.observe(sid,0,dict(model=model,digest='b'*64))
        preview=self.store.pending(sid)
        self.db.execute('DELETE FROM issues WHERE id=1'); self.db.commit()
        with self.assertRaisesRegex(ReadingOrderError,'revision_conflict'):
            self.store.decide_source(sid,preview['revision'],preview['order_revision'],preview['digest'],'accept',True)
        self.assertEqual(self.store.entries(order['id'])['total'],0)

    def test_complete_replacement_diff_pages_all_removed_occurrences(self):
        from backend.base.reading_orders import entry
        old = dict(title='Full replacement', description='', warnings=[], entries=[
            entry('External',str(i),refs=[dict(provider='comicvine',issue_id=str(10000+i))]) for i in range(2000)])
        order=self.store.accept_model(old,self.store.match(old),dict(kind='cbl_import'))
        order=self.store.attach(order['id'],order['revision'],'cbl_url','https://example.com/list.cbl')
        new=dict(old,entries=[entry('Different',str(i),refs=[dict(provider='metron',issue_id=str(20000+i))]) for i in range(2000)])
        self.store.observe(order['source']['id'],0,dict(model=new,digest='c'*64))
        page=self.store.pending(order['source']['id'],3950,50)
        self.assertEqual(page['total'],4000); self.assertEqual(len(page['items']),50)
        self.assertFalse(page['has_next']); self.assertTrue(all(r['change']=='removed' for r in page['items']))
