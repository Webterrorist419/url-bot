"""Regression checks for the existing link, language and subscription behavior."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from bot import Database, TelegramBot, TelegramError, database_path, read_config


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'bot.sqlite3'
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())
        self.api = Mock()
        self.bot = TelegramBot(self.api, self.db, 1)

    def click(self, data, uid=1):
        self.bot.on_callback({'id': 'query', 'from': {'id': uid},
                              'message': {'chat': {'id': uid, 'type': 'private'}},
                              'data': data})

    def test_five_links_and_saved_settings_survive_restart(self):
        self.assertEqual(len([row for row in self.bot.main_keyboard('ru')['inline_keyboard'] if 'url' in row[0]]), 5)
        self.db.put('button1_url', 'https://example.org/custom')
        self.db.put('button1_label_en', 'Custom')
        self.db.put('button5_enabled', '0')
        self.db.set_language(42, 'en')
        self.db.record_start(42)
        self.db.close()
        self.db = Database(self.path)
        self.bot.db = self.db
        rows = self.bot.main_keyboard('en')['inline_keyboard']
        self.assertEqual(rows[0][0], {'text': 'Custom', 'url': 'https://example.org/custom'})
        self.assertEqual(len([row for row in rows if 'url' in row[0]]), 4)
        self.assertEqual(self.db.language(42), 'en')
        self.assertEqual(self.db.count(), 1)

    def test_non_admin_cannot_edit_or_reset(self):
        self.db.record_start(42)
        for data in ('edit_url_1', 'toggle_5', 'reset', 'broadcast_new'):
            self.click(data, 42)
        self.assertEqual(self.db.state(42), '')
        self.assertEqual(self.db.get('button5_enabled'), '1')
        self.assertEqual(self.db.count(), 1)

    def test_language_switch_and_link_edit(self):
        self.click('language', 42)
        self.assertEqual(self.db.language(42), 'en')
        self.assertEqual(self.api.call.call_args.kwargs['text'], 'Welcome! Choose a link.')
        self.click('edit_url_5')
        self.bot.on_message({'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'},
                             'text': 'https://example.org/fifth'})
        self.assertEqual(self.db.get('button5_url'), 'https://example.org/fifth')
        self.assertEqual(self.db.state(1), '')

    def test_subscription_membership_and_telegram_failure(self):
        self.db.put('subscription_enabled', '1')
        self.db.put('subscription_chat', '-1001234567890')
        self.db.put('subscription_url', 'https://t.me/example_channel')
        for status in ('creator', 'administrator', 'member'):
            self.api.call.return_value = {'status': status}
            self.assertTrue(self.bot.has_access(42))
        self.api.call.return_value = {'status': 'restricted', 'is_member': True}
        self.assertTrue(self.bot.has_access(42))
        self.api.call.return_value = {'status': 'left'}
        self.assertFalse(self.bot.has_access(42))
        def offline(method, **params):
            if method == 'getChatMember':
                raise TelegramError(503, 'offline')
        self.api.call.side_effect = offline
        self.assertFalse(self.bot.has_access(42))
        self.api.reset_mock()
        self.assertTrue(self.bot.has_access(1))
        self.api.call.assert_not_called()

    def test_config_values_are_literal_and_environment_wins(self):
        path = Path(self.temp.name) / 'config.env'
        path.write_text('BOT_TOKEN=$(no-shell)\nADMIN_ID=42\nDATABASE_PATH="data.sqlite3"\nUNUSED=ignored\n')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(read_config(path), {'BOT_TOKEN': '$(no-shell)', 'ADMIN_ID': '42', 'DATABASE_PATH': 'data.sqlite3'})
        with patch.dict(os.environ, {'ADMIN_ID': '7'}):
            self.assertEqual(read_config(path)['ADMIN_ID'], '7')
        self.assertEqual(database_path(str(self.path)), self.path)


if __name__ == '__main__':
    unittest.main()
