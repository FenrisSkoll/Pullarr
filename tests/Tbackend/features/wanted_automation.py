"""Production automation → existing SAB observer → Phase 6, disposable only."""

import json
from dataclasses import asdict, replace
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from Tbackend.features import direct_downloads, intake_production
from Tbackend.features.release_search import CAPS, config, fake_http, item, rss
from Tbackend.features.sab_downloads import NZB, SABHarness, fake_sab

from backend.base.release_evaluation import WantedIssue
from backend.features.direct_downloads import load_target
from backend.features.intake_runtime import IntakeRuntime
from backend.features.wanted_automation import WantedAutomation
from backend.features.wanted_search import UnifiedReleaseSearch
from backend.internals.wanted_configuration import save_automation


class WantedProductionTests(SABHarness, TestCase):
    seed = intake_production.SABIntakeProduction.seed

    def flow(self, wrong=False, manual=False, crash=None, race=None, pack=False, partial=False, failure=None, canonical=False, abstain=False, force=False):
        self.seed()
        if canonical:
            from backend.internals.issue_facts import mapped_facts, write_facts
            for iid, label, day in self.store.db.execute('SELECT id,issue_number,date FROM issues').fetchall():
                write_facts(self.store.db.cursor(), iid, mapped_facts(label, day, 'legacy_mapped'))
        self.store.db.execute("UPDATE config SET value=58 WHERE key='database_version'")
        self.store.db.execute('UPDATE volumes SET monitored=1')
        self.store.db.execute('UPDATE issues SET monitored=(id=5)')
        if pack:
            self.store.db.execute('UPDATE issues SET monitored=1')
            self.target = replace(self.target, catalog=(*self.target.catalog, WantedIssue(6, '6', year=2016, owned=False)))
        actual = 6 if wrong else 5
        artifact = self.incoming / f'Batman {actual:03} (2016).cbz'
        with ZipFile(artifact, 'w') as archive:
            archive.writestr('page.jpg', b'disposable image')
            archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>Batman</Series><Number>{actual}</Number><Year>2016</Year></ComicInfo>')
        if pack:
            second = 7 if partial else 6
            with ZipFile(self.incoming / f'Batman {second:03} (2016).cbz', 'w') as archive:
                archive.writestr('page.jpg', b'disposable second image')
                archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>Batman</Series><Number>{second}</Number><Year>2016</Year></ComicInfo>')

        def response(path, query):
            return (200, NZB, {}) if path == '/nzb' else (
                200, CAPS if query.get('t') == ['caps'] else rss(item(title='Batman (2016).cbz' if force else 'Batman #5-6 (2016).cbz' if pack else 'Batman #5 (2016).cbz', url=source_url + '/nzb')), {})

        with fake_http(response) as (source_url, calls), fake_sab() as (url, remote):
            remote['drop'] = failure == 'ambiguous'
            client = self.client(url)
            private = asdict(client.config)
            private.pop('key')
            self.store.db.execute('INSERT INTO config VALUES(?,?)', ('sab_client_v1:sab', json.dumps(private)))
            save_automation(self.store.db, {'mode': 'grab', 'sab_client_id': None})
            self.store.db.execute('INSERT INTO acquisition_path_mappings VALUES(?,?,?,?,?,?,?,?)',
                ('map', 'sab', client.config.instance, '/complete', 'posix', str(self.incoming), 1, None))
            def current_target(volume, issue):
                with patch('backend.features.direct_downloads.get_db', side_effect=self.store.db.cursor), \
                        patch('backend.internals.identification.get_db', side_effect=self.store.db.cursor):
                    return load_target(volume, issue)
            searches = UnifiedReleaseSearch(target_loader=current_target,
                nzb_loader=lambda: (config(url=source_url + '/api'),), ddl_loader=lambda: {})
            self.addCleanup(searches.close_all)
            service = WantedAutomation(self.path, searches=searches, clock=lambda: 100.)
            self.addCleanup(service.close)
            def checkpoint(stage):
                if stage == crash:
                    raise RuntimeError('simulated process exit')
                if stage == 'selection_persisted' and race:
                    if race == 'unmonitor':
                        self.store.db.execute('UPDATE issues SET monitored=0 WHERE id=5')
                    elif race == 'owned':
                        self.store.db.execute("INSERT INTO files VALUES(1,'/disposable/owned.cbz',1)")
                        self.store.db.execute('INSERT INTO issues_files VALUES(1,5,0)')
                    elif race == 'policy':
                        save_automation(self.store.db, {'mode': 'off', 'sab_client_id': 'sab'})
                    elif race == 'block':
                        self.store.db.execute("INSERT INTO wanted_blocks SELECT source_kind,source_key,candidate_id,'operator',100 FROM wanted_decisions")
                if stage == 'nzb_resolved' and race == 'resolved_owned':
                    self.store.db.execute("INSERT INTO files VALUES(1,'/disposable/owned.cbz',1)")
                    self.store.db.execute('INSERT INTO issues_files VALUES(1,5,0)')
            service.checkpoint = checkpoint
            def explicit_selection():
                preview = service.search_manual(1, 5)
                result = preview['results'][0]
                self.assertTrue(result['operationally_available'], result)
                if force:
                    self.assertFalse(result['download_eligible'])
                    self.assertTrue(result['force_eligible'])
                session = searches.lookup(preview['search_id'], result['selection_id'])
                return service.grab(session, session.selections[result['selection_id']], automatic=False, force=force)
            if abstain:
                service.tick()
                self.assertEqual(len(remote['uploads']), 0)
                self.assertEqual(service.store.db.execute('SELECT outcome FROM wanted_searches').fetchone()[0], 'no_acceptable_getcomics_release')
                self.assertEqual(service.store.db.execute('SELECT COUNT(*) FROM wanted_decisions').fetchone()[0], 0)
                return
            if crash:
                with self.assertRaisesRegex(RuntimeError, 'simulated process exit'):
                    service.tick() if crash == 'search_claimed' else explicit_selection()
                service.store.recover_claims()
                expected = 1 if crash == 'acquisition_persisted' else 0
                self.assertEqual(len(remote['uploads']), expected)
                decision = service.store.db.execute('SELECT state FROM wanted_decisions').fetchone()
                if decision:
                    self.assertEqual(decision[0], 'abandoned' if crash == 'selection_persisted' else 'review')
                return
            if manual:
                from flask import Flask

                from frontend.api import api
                app = Flask(__name__)
                app.register_blueprint(api, url_prefix='/api')
                with patch('backend.features.wanted_automation.UNIFIED_SEARCH', searches), \
                        patch('backend.internals.db.DBConnection.default_file', self.path), \
                        patch('frontend.api.Settings') as settings, patch('frontend.api.StartTypeHandlers'), \
                        patch('frontend.api.Library') as library:
                    settings.return_value.sv.api_key = 'fixture-key'
                    library.get_issue.return_value.get_data.return_value.volume_id = 1
                    library.get_issue.return_value.get_volume_id.return_value = 1
                    http = app.test_client()
                    self.assertEqual(http.post('/api/issues/5/release-search').status_code, 401)
                    response = http.post('/api/issues/5/release-search?api_key=fixture-key')
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertEqual(len(remote['uploads']), 0)
                    result = response.json['result']
                    path = f'/api/release-search/{result["search_id"]}/{result["results"][0]["selection_id"]}?api_key=fixture-key'
                    self.assertEqual(http.get(path).status_code, 405)
                    self.assertEqual(http.post(path, json={'action': 'download', 'url': 'file:///bad'}).status_code, 400)
                    self.assertTrue(result['results'][0]['force_eligible'])
                    self.assertEqual(service.store.db.execute('SELECT COUNT(*) FROM wanted_decisions').fetchone()[0], 0)
                    self.assertEqual(http.post(path, json={'action': 'block'}).status_code, 200)
                    self.assertEqual(http.post(path, json={'action': 'download'}).status_code, 409)
                    self.assertEqual(http.post(path, json={'action': 'unblock'}).status_code, 200)
                    self.assertEqual(http.post(path, json={'action': 'download'}).status_code, 200)
                    self.assertEqual(http.post(path, json={'action': 'download'}).status_code, 200)
                    self.assertEqual(http.get('/api/wanted?api_key=fixture-key').status_code, 200)
                    self.assertEqual(http.get('/api/wanted/history?api_key=fixture-key').status_code, 200)
            else:
                with patch('backend.features.search_full.auto_search', side_effect=AssertionError('legacy search')):
                    if race and race != 'unmonitor' or failure == 'ambiguous':
                        from backend.base.download_job import DownloadFailure
                        from backend.implementations.direct_download_source import \
                            DDLError
                        from backend.internals.wanted import WantedConflict
                        with self.assertRaises((WantedConflict, DownloadFailure, DDLError)):
                            explicit_selection()
                    else:
                        explicit_selection()
            if race and race != 'unmonitor':
                self.assertEqual(len(remote['uploads']), 0)
                self.assertEqual(service.store.db.execute('SELECT state FROM wanted_decisions').fetchone()[0], 'review' if race == 'resolved_owned' else 'abandoned')
                self.assertEqual(service.store.db.execute('SELECT outcome FROM wanted_searches').fetchone()[0], 'manual_results')
                return
            self.assertEqual(len(remote['uploads']), 1)
            if failure == 'ambiguous':
                service.tick()
                self.assertEqual(service.store.db.execute('SELECT state FROM wanted_decisions').fetchone()[0], 'review')
                self.assertEqual(len(remote['uploads']), 1)
                self.assertEqual(self.store.db.execute('SELECT state FROM acquisition_downloads').fetchone()[0], 'ambiguous')
                return
            decision = service.store.db.execute('SELECT * FROM wanted_decisions').fetchone()
            acquisition = self.store.get(decision['id'])
            self.assertEqual(decision['authorization'], 'forced_manual' if force else 'manual')
            self.assertEqual(decision['state'], 'tracking')
            if pack:
                self.assertEqual(json.loads(decision['issue_ids']), [5, 6])
            request_count = len(calls)
            service.tick()
            self.assertEqual(len(calls), request_count)
            self.assertEqual(len(remote['uploads']), 1)
            remote['queue'].clear()
            if failure == 'remote':
                remote['history'][acquisition['nzo_id']] = dict(nzo_id=acquisition['nzo_id'], status='Failed')
                self.poll(client)
                service.tick()
                self.assertEqual(service.store.db.execute('SELECT state FROM wanted_decisions').fetchone()[0], 'review')
                self.assertEqual(len(remote['uploads']), 1)
                self.assertEqual(service.store.due(), ())
                return
            remote['history'][acquisition['nzo_id']] = dict(nzo_id=acquisition['nzo_id'], status='Completed',
                storage='/complete' if pack else '/complete/' + artifact.name, completed=1790000000)
            self.poll(client)
            runtime = IntakeRuntime(self.path, clock=lambda: 100.)
            runtime.tick()
            runtime.clock = lambda: 111.
            runtime.tick()
            runtime.tick()
            service.tick()
            state = service.store.db.execute('SELECT state FROM wanted_decisions').fetchone()[0]
            self.assertEqual(state, 'review' if wrong or partial else 'satisfied')
            self.assertEqual(artifact.exists(), wrong)
            self.assertEqual([r[0] for r in self.store.db.execute('SELECT issue_id FROM issues_files ORDER BY issue_id')], [] if wrong else [5, 6] if pack and not partial else [5])
            if partial:
                self.assertEqual([r[0] for r in service.store.db.execute('SELECT issue_id FROM wanted_reservations WHERE active=1')], [6])
                self.assertTrue((self.incoming / 'Batman 007 (2016).cbz').exists())
            self.assertEqual(service.store.due(), ())
            self.assertEqual(len(remote['uploads']), 1)

    def test_scheduler_sab_intake_organization_satisfaction(self):
        self.flow()

    def test_automatic_only_nzb_abstains_without_sab_submission(self):
        self.flow(abstain=True)

    def test_forced_manual_sab_preserves_exact_target_and_normal_intake(self):
        self.flow(force=True)

    def test_scheduler_wrong_artifact_keeps_reservation_and_does_not_repeat(self):
        self.flow(wrong=True)

    def test_canonical_facts_preserve_sab_happy_path(self):
        self.flow(canonical=True)

    def test_canonical_facts_preserve_wrong_artifact_review_hold(self):
        self.flow(wrong=True, canonical=True)

    def test_authenticated_interactive_search_has_no_side_effect_until_exact_selection(self):
        self.flow(manual=True)

    def test_crash_claim_no_acquisition(self):
        self.flow(crash='search_claimed')

    def test_crash_selection_never_replays_lost_source_context(self):
        self.flow(crash='selection_persisted')

    def test_crash_started_holds_uncertainty(self):
        self.flow(crash='grab_started')

    def test_crash_acquisition_already_exists_does_not_duplicate(self):
        self.flow(crash='acquisition_persisted')

    def test_ownership_race_stops_grab(self):
        self.flow(race='owned')

    def test_explicit_manual_selection_does_not_require_background_monitoring(self):
        self.flow(race='unmonitor')

    def test_policy_race_stops_grab(self):
        self.flow(race='policy')

    def test_blocklist_race_stops_grab_without_changing_score(self):
        self.flow(race='block')

    def test_ownership_change_during_nzb_resolution_stops_before_upload(self):
        self.flow(race='resolved_owned')

    def test_ambiguous_submission_holds_reservation(self):
        self.flow(failure='ambiguous')

    def test_definitive_remote_failure_requires_explicit_review_release(self):
        self.flow(failure='remote')

    def test_one_range_acquisition_reserves_and_organizes_both_proven_local_ids(self):
        self.flow(pack=True)

    def test_partial_pack_satisfies_only_real_ownership_and_holds_unresolved_member(self):
        self.flow(pack=True, partial=True)


class WantedDDLProductionTests(TestCase):
    def test_scheduler_ddl_worker_intake_and_ownership(self):
        direct_downloads.DDLWorkerAcceptance.unified_flow(self, automation=True)

    def test_scheduler_ddl_wrong_bytes_review_held(self):
        direct_downloads.DDLWorkerAcceptance.unified_flow(self, automation=True, wrong=True)

    def test_canonical_facts_preserve_ddl_happy_path(self):
        direct_downloads.DDLWorkerAcceptance.unified_flow(self, automation=True, canonical=True)

    def test_canonical_facts_preserve_ddl_wrong_artifact_hold(self):
        direct_downloads.DDLWorkerAcceptance.unified_flow(self, automation=True, wrong=True, canonical=True)

    def test_changed_ddl_page_does_not_grab_wrong_issue(self):
        direct_downloads.DDLWorkerAcceptance.unified_flow(self, automation=True, page_review='changed')

    def test_multiple_ddl_offerings_hold_for_operator_without_html_order_choice(self):
        direct_downloads.DDLWorkerAcceptance.unified_flow(self, automation=True, page_review='multiple')
