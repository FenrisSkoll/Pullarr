"""No-fallback dispatch, retained/deleted ownership, and frozen SQL parity."""

import sqlite3
from time import perf_counter
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, call, patch

from fixtures.comicvine_fetch import envelope, issue_response
from fixtures.comicvine_search import volume_response
from fixtures.legacy_refresh import LEGACY_REFRESH_SOURCE
from fixtures.metadata_refresh import RefreshHarness

from backend.base.definitions import RootFolder
from backend.base.switch_review import SwitchReviewError
from backend.features.library_import import import_library
from backend.implementations import volumes
from backend.implementations.metadata.legacy import (legacy_issue_metadata,
                                                     legacy_volume_identities,
                                                     legacy_volume_metadata)
from backend.implementations.metadata.registry import (
    PROVIDERS, get_bulk_volume_provider)
from backend.internals.provider_authority import AuthorityToken
from backend.internals.provider_identity import (ExternalIdentity,
                                                 MetadataIdentityError,
                                                 ProviderIdentityDB,
                                                 VolumeMetadataIdentity)


class RuntimeIdentityCutover(RefreshHarness, TestCase):
    def resolve(self):
        return ProviderIdentityDB.resolve_volume_metadata_identity(1, PROVIDERS.keys())

    def test_add_then_resolve_then_refresh_uses_storage_result(self):
        self.assertEqual(self.resolve(), VolumeMetadataIdentity(
            1, 'comicvine', '2127', 1234567890))
        real = get_bulk_volume_provider()
        real.fetch_volumes = AsyncMock(wraps=real.fetch_volumes)
        resolver = self.start_patch(
            'backend.implementations.volumes.get_bulk_volume_provider', return_value=real)
        lookup = self.start_patch(
            'backend.internals.provider_identity.ProviderIdentityDB.resolve_volume_metadata_identities',
            wraps=ProviderIdentityDB.resolve_volume_metadata_identities)
        self.prepare_refresh([volume_response(count_of_issues=3)],
                             [issue_response(), issue_response(id=302), issue_response(id=303)])
        self.refresh()
        lookup.assert_called_once_with(PROVIDERS.keys(), 1)
        resolver.assert_called_once_with('comicvine')
        real.fetch_volumes.assert_awaited_once_with(('2127',))
        self.assertEqual(ProviderIdentityDB.issue_identities(3)
                         [0].provider_id, '303')
        self.assertEqual(volumes.Issue(1).get_data().id, 1)
        self.assertEqual(volumes.Volume(1).get_public_data()
                         ['comicvine_id'], 2127)
        self.assertEqual(ProviderIdentityDB.audit(), [])

    def test_dispatch_rejects_resolver_value_that_no_longer_matches_authority(self):
        # A spy substitutes only the resolver result, not persisted invariants.
        self.start_patch(
            'backend.internals.provider_identity.ProviderIdentityDB.resolve_volume_metadata_identities',
            return_value=[VolumeMetadataIdentity(1, 'comicvine', 'resolver-value', 0)])
        provider = Mock()
        provider.fetch_volumes = AsyncMock(return_value=[])
        provider.fetch_issues = AsyncMock(return_value=[])
        self.start_patch(
            'backend.implementations.volumes.get_bulk_volume_provider', return_value=provider)
        with self.assertRaisesRegex(SwitchReviewError, 'stale_metadata_authority'):
            self.refresh()
        provider.fetch_volumes.assert_not_awaited()

    def test_missing_selected_identity_never_uses_other_reference(self):
        ProviderIdentityDB.put_volume_identity(
            ExternalIdentity(1, 'metron', '2127', 'test'))
        self.db.execute(
            "DELETE FROM volume_external_ids WHERE provider='comicvine'")
        before = self.snapshot()
        with self.assertRaisesRegex(MetadataIdentityError, 'Missing selected'):
            self.refresh()
        self.session.get.assert_not_awaited()
        self.assertEqual(self.snapshot(), before)

    def test_unregistered_selected_provider_fails_even_with_comicvine_reference(self):
        # Mock query rows; production CHECK remains untouched.
        cursor = Mock()
        cursor.execute.return_value = [(1, 'unregistered', '123', 0, 2127, 0)]
        with patch('backend.internals.provider_identity.get_db', return_value=cursor):
            with self.assertRaisesRegex(KeyError, 'Unregistered metadata provider'):
                self.resolve()
        self.session.get.assert_not_awaited()

    def test_registered_provider_without_bulk_capability_fails_explicitly(self):
        with patch.dict(PROVIDERS, {'comicvine': Mock}):
            with self.assertRaisesRegex(TypeError, 'does not support bulk'):
                self.refresh()
        self.session.get.assert_not_awaited()
        self.commit.assert_not_called()

    def test_missing_local_volume_resolver_is_explicit(self):
        with self.assertRaises(KeyError):
            ProviderIdentityDB.resolve_volume_metadata_identity(
                999, PROVIDERS.keys())

    def test_volume_mismatch_and_malformed_id_do_not_repair_or_fetch(self):
        for value in ('999', '02127', 'not-an-id'):
            with self.subTest(value=value):
                self.db.execute(
                    'UPDATE volume_external_ids SET provider_id=?', (value,))
                before = self.snapshot()
                with self.assertRaisesRegex(MetadataIdentityError, 'shadow conflict'):
                    self.refresh()
                self.assertEqual(self.snapshot(), before)
        self.session.get.assert_not_awaited()

    def test_timestamp_mismatch_not_silently_repaired(self):
        self.db.execute('UPDATE volume_external_ids SET last_fetch=42')
        with self.assertRaisesRegex(MetadataIdentityError, 'shadow conflict'):
            self.resolve()

    def test_missing_issue_identity_fails_without_legacy_fallback(self):
        self.db.execute('DELETE FROM issue_external_ids WHERE issue_id=1')
        self.db.commit()
        before = self.rows('issues')
        with self.assertRaisesRegex(MetadataIdentityError, 'issue identity shadow conflict'):
            self.refresh()
        self.assertEqual(self.rows('issues'), before)
        # Existing volume-first boundary.
        self.assertEqual(self.commit.call_count, 1)

    def test_mismatched_issue_identity_fails_without_overwriting_references(self):
        ProviderIdentityDB.put_issue_identity(
            ExternalIdentity(1, 'gcd', 'ref', 'test'))
        self.db.execute(
            "UPDATE issue_external_ids SET provider_id='999' WHERE issue_id=1 AND provider='comicvine'")
        self.db.commit()
        before = ProviderIdentityDB.issue_identities(1)
        with self.assertRaisesRegex(MetadataIdentityError, 'issue identity shadow conflict'):
            self.refresh()
        self.assertEqual(ProviderIdentityDB.issue_identities(1), before)

    def test_other_provider_same_string_does_not_match_authoritative_issue(self):
        ProviderIdentityDB.put_issue_identity(
            ExternalIdentity(1, 'metron', '303', 'test'))
        self.db.commit()
        self.prepare_refresh([volume_response(count_of_issues=3)],
                             [issue_response(), issue_response(id=302), issue_response(id=303)])
        self.refresh()
        self.assertEqual(ProviderIdentityDB.issue_identities(
            1, 'metron')[0].provider_id, '303')
        self.assertEqual(ProviderIdentityDB.issue_identities(
            3, 'comicvine')[0].provider_id, '303')
        self.assertEqual(volumes.Issue(1).get_data().comicvine_id, 301)

    def test_reconciliation_matches_external_identity_index_not_cv_conflict(self):
        statements = []
        self.db.set_trace_callback(statements.append)
        self.addCleanup(self.db.set_trace_callback, None)
        self.refresh()
        insert = next(s for s in statements if 'INSERT INTO issues(' in s)
        self.assertIn('SELECT e.issue_id FROM issue_external_ids', insert)
        self.assertIn('ON CONFLICT(id)', insert)
        self.assertNotIn('ON CONFLICT(comicvine_id)', insert)

    def test_thousand_volume_resolution_is_one_query(self):
        self.db.executemany('''INSERT INTO volumes(id,comicvine_id,title,root_folder,last_cv_fetch)
            VALUES (?,?,'Generated',1,0)''', [(i, 10000+i) for i in range(2, 1001)])
        statements = []
        self.db.set_trace_callback(statements.append)
        self.addCleanup(self.db.set_trace_callback, None)
        started = perf_counter()
        identities = ProviderIdentityDB.resolve_volume_metadata_identities(
            PROVIDERS.keys())
        elapsed = perf_counter() - started
        self.assertEqual(len(identities), 1000)
        self.assertEqual(len(statements), 1)
        print('\n1000-volume identity lookup: 1 SELECT, %.6fs' % elapsed)

    def test_library_import_add_naturally_creates_resolvable_identities(self):
        folder = self.root / 'imported'
        folder.mkdir()
        roots = self.start_patch(
            'backend.features.library_import.RootFolders').return_value
        roots.get_all.return_value = [RootFolder(1, str(self.root) + '/', None)]
        self.start_patch('backend.features.library_import.commit',
                         side_effect=self.db.commit)
        self.start_patch('backend.features.library_import.scan_files')
        self.prepare_fetch(volume_response(id=9001), [
                           issue_response(id=901, volume={'id': 9001})])
        import_library(
            [{'id': 9001, 'filepath': str(folder / 'synthetic.cbz')}])
        identity = ProviderIdentityDB.resolve_volume_metadata_identity(
            2, PROVIDERS.keys())
        self.assertEqual(
            (identity.provider, identity.provider_id), ('comicvine', '9001'))
        self.assertEqual(ProviderIdentityDB.audit(), [])

    def test_thousand_volume_http_and_query_parity(self):
        self.db.executemany('''INSERT INTO volumes(id,comicvine_id,title,root_folder,last_cv_fetch)
            VALUES (?,?,'Generated',1,0)''', [(i, 10000+i) for i in range(2, 1001)])
        self.db.executemany('INSERT INTO volumes_covers VALUES (?,NULL)', [
                            (i,) for i in range(2, 1001)])
        self.db.commit()
        checkpoint = sqlite3.connect(':memory:')
        self.addCleanup(checkpoint.close)
        self.db.backup(checkpoint)

        async def response(url, params):
            ids = params['filter'].split(':', 1)[1].split('|')
            if '/volumes/' in url:
                data = [volume_response(
                    id=i, count_of_issues=2 if i == '2127' else 0) for i in ids]
            else:
                data = [issue_response(), issue_response(id=302)
                        ] if '2127' in ids else []
            return Mock(json=AsyncMock(return_value=envelope(data, number_of_total_results=len(data))))

        self.session.get.side_effect = response
        namespace = dict(vars(volumes))
        namespace.update(legacy_issue_metadata=legacy_issue_metadata, legacy_volume_metadata=legacy_volume_metadata)
        namespace['legacy_volume_identities'] = legacy_volume_identities
        exec(LEGACY_REFRESH_SOURCE, namespace)
        results = []
        for fn in (namespace['refresh_and_scan'], volumes.refresh_and_scan):
            self.db.rollback()
            checkpoint.backup(self.db)
            self.session.get.reset_mock()
            self.session.get_content.reset_mock()
            statements = []
            self.db.set_trace_callback(statements.append)
            started = perf_counter()
            fn(allow_skipping=False)
            elapsed = perf_counter() - started
            self.db.set_trace_callback(None)
            selects = sum(s.lstrip().upper().startswith('SELECT')
                          for s in statements)
            requests = [(c.args[0], c.kwargs['params']['filter'])
                        for c in self.session.get.await_args_list]
            results.append(
                (requests, self.session.get_content.await_count, selects, elapsed))
        self.assertEqual(results[0][:2], results[1][:2])
        # 10 volume + 20 issue requests.
        self.assertEqual(len(results[1][0]), 30)
        # Identity cutover and 3D-A add two owner lookups. 2G-C captures
        # pre-fetch application revisions in three bounded 400-volume batches,
        # not one provenance read per volume. Provider request parity is exact.
        # Six batched generation checks (capture + five write stages), three
        # SELECTs each for 1,000 volumes. No per-volume authority reads.
        # Phase 8F adds one global reservation-index read before the final
        # orphan-pruning continuation, not another query per volume/file.
        self.assertEqual(results[1][2] - results[0][2], 24)
        print('\n1000-volume refresh old/new SELECTs and seconds:',
              results[0][2:], results[1][2:], '; metadata HTTP=30 each, covers=1000 each')

    def test_frozen_pre_cutover_persistence_requests_and_failures_match(self):
        self.add_second_volume()
        self.link_file(issue_ids=(1, 2))
        for issue_id in (1, 2):
            for provider in ('metron', 'gcd'):
                ProviderIdentityDB.put_issue_identity(
                    ExternalIdentity(issue_id, provider, 'ref:' + str(issue_id), 'test'))
        self.db.commit()
        checkpoint = sqlite3.connect(':memory:')
        self.addCleanup(checkpoint.close)
        self.db.backup(checkpoint)
        cases = [
            (1, [envelope([volume_response(count_of_issues=2)]),
                 envelope([issue_response(), issue_response(id=302)], number_of_total_results=2)]),
            (None, [envelope([volume_response(id=9001), volume_response(count_of_issues=3)]),
                    envelope([issue_response(id=901, volume={'id': 9001}), issue_response(),
                              issue_response(id=302), issue_response(id=303)], number_of_total_results=4)]),
            (1, [envelope([volume_response(count_of_issues=1)]),
                 envelope([issue_response()], number_of_total_results=1)]),
            (1, [envelope([volume_response(count_of_issues=0)]),
             envelope([], number_of_total_results=0)]),
            (1, [envelope([volume_response(count_of_issues=101)]),
                 envelope([issue_response()], number_of_total_results=101), envelope([], 107)]),
            (1, [envelope([volume_response(name='Committed')]), envelope([], 100)]),
            (None, [envelope([], 107)])]

        def capture(fn, identity, responses):
            self.response.json.side_effect = responses
            for mock in (self.session.get, self.session.get_content, self.commit,
                         self.scan, self.pool.starmap, self.status):
                mock.reset_mock()
            error = None
            try:
                fn(identity, allow_skipping=False)
            except Exception as exc:
                error = (type(exc), exc.args)
            # New evidence tables have their own preservation/parity tests;
            # the frozen pre-3D-A function cannot maintain them.
            legacy_state = {k: v for k, v in self.snapshot().items()
                            if k not in ('issue_number_facts', 'issue_date_facts',
                                         'classification_state', 'classification_provenance',
                                         'classification_evidence_receipts', 'classification_control_state')}
            scans, pools = [], []
            for invocation in self.scan.call_args_list:
                keywords = dict(invocation.kwargs)
                token = keywords.pop('expected_authority', None)
                if fn is volumes.refresh_and_scan:
                    self.assertIsInstance(token, AuthorityToken)
                    self.assertEqual(token.generation, 0)
                scans.append(call(*invocation.args, **keywords))
            for invocation in self.pool.starmap.call_args_list:
                function, arguments = invocation.args
                if fn is volumes.refresh_and_scan:
                    for argument in arguments:
                        self.assertIsInstance(argument[4], AuthorityToken)
                        self.assertEqual(argument[4].generation, 0)
                    arguments = [argument[:4] for argument in arguments]
                pools.append(call(function, arguments))
            return (legacy_state, error, self.db.in_transaction,
                    self.session.get.await_args_list[:], self.session.get_content.await_args_list[:],
                    self.commit.call_count, scans, pools, self.status.mock_calls[:])

        namespace = dict(vars(volumes))
        namespace.update(legacy_issue_metadata=legacy_issue_metadata, legacy_volume_metadata=legacy_volume_metadata)
        namespace['legacy_volume_identities'] = legacy_volume_identities
        exec(LEGACY_REFRESH_SOURCE, namespace)
        for identity, responses in cases:
            with self.subTest(identity=identity, responses=responses):
                self.db.rollback()
                checkpoint.backup(self.db)
                old = capture(
                    namespace['refresh_and_scan'], identity, responses)
                self.db.rollback()
                checkpoint.backup(self.db)
                new = capture(volumes.refresh_and_scan, identity, responses)
                self.assertEqual(new, old)
