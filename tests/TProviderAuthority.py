"""Token guards tested independently; these are not production switch E2E."""

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from backend.base.switch_review import SwitchReviewError
from backend.internals.db import DB_SCHEMA
from backend.internals.provider_authority import (capture, guarded_stage,
                                                  require_current, serialized)


class ProviderAuthorityTests(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript(DB_SCHEMA)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute("INSERT INTO root_folders(id,folder) VALUES(1,'/fixture/')")
        self.db.execute("INSERT INTO volumes(id,comicvine_id,title,root_folder,folder) VALUES(1,100,'Source',1,'/fixture/volume')")
        self.db.commit()
        self.cursor = self.db.cursor()
        self.token = capture(self.cursor, (1,))[1]

    def test_capture_exact_missing_and_deduplicated(self):
        self.assertEqual((self.token.provider, self.token.provider_id, self.token.generation), ('comicvine', '100', 0))
        self.assertEqual(capture(self.cursor, (1, 1, 999)), {1: self.token})
        with self.assertRaises(SwitchReviewError):
            capture(self.cursor, (True,))
        with self.assertRaises(SwitchReviewError):
            capture(self.cursor, range(1, 10002))

    def test_guarded_metadata_refresh_does_not_advance_generation(self):
        with guarded_stage(self.cursor, (self.token,)):
            self.cursor.execute("UPDATE volumes SET title='Refreshed' WHERE id=1")
        self.assertEqual(capture(self.cursor, (1,))[1], self.token)
        self.assertFalse(self.db.in_transaction)

    def test_aba_rejects_ancient_provider_token(self):
        # Controlled fixture SQL only, not a supported switching entry point.
        self.db.execute("UPDATE volumes SET metadata_provider='metron',authority_generation=1 WHERE id=1")
        self.db.execute("UPDATE volumes SET metadata_provider='comicvine',authority_generation=2 WHERE id=1")
        self.db.commit()
        before = tuple(self.db.iterdump())
        with self.assertRaisesRegex(SwitchReviewError, 'stale_metadata_authority'):
            with guarded_stage(self.cursor, (self.token,)):
                self.fail('Stale worker entered its write stage')
        self.assertEqual(before, tuple(self.db.iterdump()))

    def test_id_change_or_deleted_selected_identity_rejected(self):
        self.db.execute('UPDATE volumes SET comicvine_id=101 WHERE id=1')
        self.db.commit()
        with self.assertRaises(SwitchReviewError):
            with guarded_stage(self.cursor, (self.token,)):
                self.fail('Changed identity accepted')

    def test_failure_rolls_back_all_stage_writes(self):
        before = tuple(self.db.iterdump())
        with self.assertRaisesRegex(ValueError, 'injected'):
            with guarded_stage(self.cursor, (self.token,)):
                self.cursor.execute("UPDATE volumes SET title='Bad',authority_generation=1 WHERE id=1")
                raise ValueError('injected')
        self.assertEqual(before, tuple(self.db.iterdump()))

    def test_pending_caller_transaction_not_committed_or_rolled_back(self):
        self.db.execute("UPDATE volumes SET title='Pending' WHERE id=1")
        with self.assertRaisesRegex(SwitchReviewError, 'transaction_boundary'):
            with serialized(self.cursor):
                self.fail('Nested transaction accepted')
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.db.execute('SELECT title FROM volumes').fetchone()[0], 'Pending')
        self.db.rollback()

    def test_duplicate_scope_rejected(self):
        with self.assertRaisesRegex(SwitchReviewError, 'duplicate_authority_scope'):
            require_current(self.cursor, (self.token, self.token))

    def test_batched_capture_has_no_issue_count_dependency(self):
        queries = []
        self.db.set_trace_callback(queries.append)
        capture(self.cursor, range(1, 1001))
        self.db.set_trace_callback(None)
        self.assertEqual(len(queries), 3)

    def test_write_serialization_blocks_competing_connection(self):
        with TemporaryDirectory(prefix='kapowarr-authority-') as folder:
            path = Path(folder) / 'fixture.sqlite'
            first = sqlite3.connect(path, timeout=0)
            second = sqlite3.connect(path, timeout=0)
            try:
                self.db.backup(first)
                with guarded_stage(first.cursor(), (self.token,)):
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                        second.execute('UPDATE volumes SET authority_generation=1 WHERE id=1')
                    second.rollback()
                second.execute('UPDATE volumes SET authority_generation=1 WHERE id=1')
                second.commit()
                with self.assertRaises(SwitchReviewError):
                    with guarded_stage(first.cursor(), (self.token,)):
                        self.fail('Old generation accepted')
                self.assertEqual(first.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(first.execute('PRAGMA foreign_key_check').fetchall(), [])
            finally:
                first.close()
                second.close()
