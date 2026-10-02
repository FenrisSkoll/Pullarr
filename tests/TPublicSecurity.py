"""Publication security regressions use synthetic values only."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from backend.base.archive_tools import archive_executable
from backend.base.custom_exceptions import InvalidKeyValue
from backend.base.logging import LOGGER
from backend.internals.settings import Settings, SettingsValues


class PublicSecurityTests(TestCase):
    def test_validation_responses_do_not_echo_credentials(self):
        secret = 'synthetic-validation-credential'
        for key in ('password', 'comicvine_api_key', 'metron_api_token', 'SID', 'authorization'):
            with self.assertLogs(LOGGER, level='WARNING') as captured:
                error = InvalidKeyValue(key, secret)
            self.assertNotIn(secret, str(error.api_response))
            self.assertNotIn(secret, '\n'.join(captured.output))

    def test_generated_key_not_logged(self):
        written = {}
        fake = SimpleNamespace(update=lambda values, **kwargs: written.update(values), clear_cache=lambda: None)
        with self.assertLogs(LOGGER, level='DEBUG') as captured:
            Settings.generate_api_key(fake)
        self.assertEqual(len(written['api_key']), 32)
        self.assertNotIn(written['api_key'], '\n'.join(captured.output))

    def test_credential_diagnostics_redacted(self):
        secret = 'synthetic-credential-never-real'
        with self.assertLogs(LOGGER, level='INFO') as captured:
            LOGGER.info('url https://user:%s@example.invalid/?api_key=%s password=%s SID=%s Bearer %s',
                        secret, secret, secret, secret, secret)
        self.assertNotIn(secret, '\n'.join(captured.output))

    def test_fresh_native_binding_is_loopback(self):
        self.assertEqual(SettingsValues().host, '127.0.0.1')

    def test_no_trial_writer_fallback(self):
        with patch('backend.base.archive_tools.which', return_value=None):
            with self.assertRaises(FileNotFoundError):
                archive_executable(write=True)
        self.assertIn('unrar', archive_executable().lower())
