"""Secret-safe local configuration; no real GCD requests."""

from unittest import TestCase
from unittest.mock import patch

from backend.base.definitions import Constants
from backend.implementations.metadata.gcd_client import GcdError
from backend.internals.settings import (PublicSettingsValues,
                                        Settings, SettingsValues)


class GcdCredentialTests(TestCase):
    def test_public_projection_repr_and_logs_mask_password(self):
        value = PublicSettingsValues(gcd_password='fixture-only-secret', gcd_username='fixture-user')
        self.assertNotIn('fixture-only-secret', repr(value))
        self.assertEqual(value.todict()['gcd_password'], Constants.CREDENTIAL_REPLACEMENT)
        self.assertEqual(value.todict()['gcd_username'], 'fixture-user')
        settings = object.__new__(Settings)
        with patch.object(settings, 'get_settings', return_value=SettingsValues()), \
                patch.object(settings, 'clear_cache'), patch.object(settings, '_Settings__validate_settings'), \
                patch('backend.internals.settings.get_db'), patch('backend.internals.settings.LOGGER') as log:
            settings.update({'gcd_password': 'fixture-only-secret'}, from_public=True)
            self.assertNotIn('fixture-only-secret', str(log.mock_calls))

    def test_blank_edit_preserves_and_invalid_credentials_never_echo(self):
        settings = object.__new__(Settings)
        with patch.object(settings, 'get_settings', return_value=SettingsValues(gcd_password='fixture-old')):
            convert = settings._Settings__format_value
            self.assertEqual(convert('gcd_password', '', True), 'fixture-old')
            self.assertEqual(convert('gcd_password', Constants.CREDENTIAL_REPLACEMENT, True), 'fixture-old')
            for value in ({'private': 'do-not-echo'}, 'do-not-echo\n'):
                with self.assertRaises(GcdError) as error:
                    convert('gcd_password', value, True)
                self.assertNotIn('do-not-echo', str(error.exception.api_response))

    def test_disabled_by_default_and_no_startup_network(self):
        from backend.implementations.metadata.gcd import GcdMetadataProvider
        from backend.implementations.metadata.registry import \
            get_search_provider
        self.assertFalse(PublicSettingsValues().gcd_enabled)
        with patch('backend.implementations.metadata.gcd.production_client', side_effect=AssertionError('startup I/O')):
            self.assertIsInstance(get_search_provider('gcd'), GcdMetadataProvider)
