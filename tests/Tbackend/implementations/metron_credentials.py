"""Secret-safe settings validation without a real credential or database."""

from unittest import TestCase
from unittest.mock import patch

from backend.base.definitions import Constants
from backend.implementations.metadata.metron_client import MetronError
from backend.internals.settings import (PublicSettingsValues,
                                        Settings, SettingsValues)


class MetronCredentials(TestCase):
    def test_public_serialization_and_repr_do_not_disclose_token(self):
        value = PublicSettingsValues(metron_api_token='unit-not-a-credential')
        self.assertNotIn('unit-not-a-credential', repr(value))
        self.assertEqual(value.todict()['metron_api_token'], Constants.CREDENTIAL_REPLACEMENT)
        self.assertEqual(PublicSettingsValues().todict()['metron_api_token'], '')

    def test_mask_retention_empty_removal_and_validation(self):
        settings = object.__new__(Settings)
        with patch.object(settings, 'get_settings', return_value=SettingsValues(
                metron_api_token='old-unit-value')), \
                patch('backend.implementations.metadata.metron_client.MetronClient') as client:
            format_value = settings._Settings__format_value
            self.assertEqual(format_value('metron_api_token', Constants.CREDENTIAL_REPLACEMENT, True), 'old-unit-value')
            self.assertEqual(format_value('metron_api_token', '', True), '')
            client.assert_not_called()
            self.assertEqual(format_value('metron_api_token', ' unit-value ', True), 'unit-value')
            client.assert_called_once_with('unit-value')
            client.return_value.get.assert_called_once()
            with self.assertRaises(MetronError) as error:
                format_value('metron_api_token', {'secret': 'never-echo'}, True)
            self.assertNotIn('never-echo', str(error.exception.api_response))

    def test_setting_change_log_is_redacted(self):
        settings = object.__new__(Settings)
        with patch.object(settings, 'get_settings', return_value=SettingsValues()), \
                patch.object(settings, 'clear_cache'), \
                patch.object(settings, '_Settings__validate_settings'), \
                patch('backend.implementations.metadata.metron_client.MetronClient'), \
                patch('backend.internals.settings.get_db'), \
                patch('backend.internals.settings.LOGGER') as log:
            settings.update({'metron_api_token': 'unit-not-a-credential'}, from_public=True)
            self.assertNotIn('unit-not-a-credential', str(log.mock_calls))
            self.assertIn('metron_api_token', str(log.mock_calls))
            self.assertIn('Settings changed', str(log.mock_calls))
