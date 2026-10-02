"""Refresh task/scheduler characterization without workers or live transport."""

import unittest

from fixtures.metadata_refresh import NOW, RefreshHarness

from backend.features.tasks import (TASK_INTERVALS, RefreshAndScanVolume,
                                    TaskHandler, UpdateAll)
from backend.internals.provider_authority import AuthorityToken


class RefreshTasks(RefreshHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.task_socket = self.start_patch(
            'backend.features.tasks.WebSocket').return_value

    def test_manual_task_executes_actual_refresh_and_scan(self):
        task = RefreshAndScanVolume(self.volume_id)
        task.run()
        self.scan.assert_called_once_with(self.volume_id, update_websocket=True,
            expected_authority=AuthorityToken(self.volume_id, 'comicvine', '2127', 0))
        self.assertEqual(task.message, 'Updating info on Example Hero')
        self.assertEqual(
            self.rows('volumes')[0]['last_cv_fetch'],
            NOW.timestamp())

    def test_update_all_default_is_forced_but_scheduler_explicitly_skips(self):
        self.assertFalse(UpdateAll().allow_skipping)
        refresh = self.start_patch('backend.features.tasks.refresh_and_scan')
        UpdateAll(True).run()
        refresh.assert_called_once_with(
            update_websocket=True, allow_skipping=True)
        self.assertEqual(TASK_INTERVALS['update_all'], '0 * * * *')

    def test_wrappers_swallow_invalid_key_without_changing_metadata(self):
        before = self.snapshot()
        for task in (RefreshAndScanVolume(self.volume_id), UpdateAll()):
            with self.subTest(task=type(task)):
                self.respond([], 100)
                task.run()
                self.assertEqual(self.snapshot(), before)

    def test_other_exceptions_are_not_swallowed_by_task_wrappers(self):
        self.start_patch(
            'backend.features.tasks.refresh_and_scan',
            side_effect=KeyError('raw'))
        for task in (RefreshAndScanVolume(self.volume_id), UpdateAll()):
            with self.subTest(task=type(task)):
                with self.assertRaises(KeyError):
                    task.run()

    def test_scheduler_queues_skipping_update_all_and_advances_stored_schedule(
            self):
        self.start_patch(
            'backend.features.tasks.get_db',
            side_effect=self.db.cursor)
        self.start_patch(
            'backend.features.tasks.time',
            return_value=NOW.timestamp())
        self.db.execute('INSERT INTO task_intervals VALUES (?,?,?)',
                        ('update_all', '0 * * * *', NOW.timestamp() - 1))
        next_run = self.start_patch(
            'backend.features.tasks.get_schedules_next_run',
            return_value=NOW.timestamp() + 3600)
        queued = self.start_patch('backend.features.tasks.TaskHandler.add')
        timers = self.start_patch(
            'backend.features.tasks.TaskHandler.handle_intervals')
        TaskHandler()._TaskHandler__check_intervals()
        task = queued.call_args.args[0]
        self.assertIsInstance(task, UpdateAll)
        self.assertTrue(task.allow_skipping)
        next_run.assert_called_once_with('0 * * * *')
        self.assertEqual(
            self.db.execute('SELECT next_run FROM task_intervals').fetchone(),
            (NOW.timestamp() + 3600,))
        timers.assert_called_once_with()
