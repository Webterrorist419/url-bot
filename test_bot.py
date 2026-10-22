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
        self.db.set_language(1, 'ru')

    def click(self, data, uid=1):
        self.bot.on_callback({'id': 'query', 'from': {'id': uid},
                              'message': {'chat': {'id': uid, 'type': 'private'}},
                              'data': data})

    def message(self, text, uid=1):
        self.bot.on_message({'from': {'id': uid}, 'chat': {'id': uid, 'type': 'private'}, 'text': text})

    def last_markup(self):
        return self.api.call.call_args.kwargs['reply_markup']

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
        for data in ('edit_url_1', 'toggle_5', 'reset', 'broadcast_new', 'broadcast_new_en', 'color_1', 'set_color_1_danger', 'stats'):
            self.click(data, 42)
        self.assertEqual(self.db.state(42), '')
        self.assertEqual(self.db.get('button5_enabled'), '1')
        self.assertEqual(self.db.count(), 1)
        self.assertEqual(self.db.get('button1_style'), '')

    def test_language_switch_and_link_edit(self):
        self.click('language', 42)
        self.assertFalse(self.db.has_language(42))
        self.assertEqual(self.db.state(42), 'language:choose')
        self.click('language_en', 42)
        self.assertEqual(self.db.language(42), 'en')
        self.assertEqual(self.api.call.call_args.kwargs['text'], 'Welcome! Choose a link.')
        self.click('edit_url_5')
        self.bot.on_message({'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'},
                             'text': 'https://example.org/fifth'})
        self.assertEqual(self.db.get('button5_url'), 'https://example.org/fifth')
        self.assertEqual(self.db.state(1), '')
        self.assertEqual(self.last_markup(), self.bot.button_keyboard('ru', '5'))

    def test_start_requires_explicit_language_even_for_returning_user(self):
        self.message('/start referral', 42)
        self.assertFalse(self.db.has_language(42))
        self.assertEqual(self.db.count(), 0)
        choices = self.last_markup()
        self.assertEqual({button['callback_data'] for row in choices['inline_keyboard'] for button in row}, {'language_ru', 'language_en'})
        for data in ('home', 'check_subscription'):
            self.click(data, 42)
            self.assertEqual(self.last_markup(), choices)
        self.message('hello', 42)
        self.assertEqual(self.last_markup(), choices)
        self.bot.home(42)
        self.assertEqual(self.last_markup(), choices)
        self.click('language_en', 42)
        self.assertEqual(self.db.count('en'), 1)
        self.assertEqual(self.last_markup(), self.bot.main_keyboard('en'))

        self.message('/start', 42)
        self.click('home', 42)
        self.assertEqual(self.last_markup(), choices)
        self.assertEqual(self.db.count('en'), 1)
        self.click('language_ru', 42)
        self.click('language_ru', 42)
        self.assertEqual(self.db.count(), 1)
        self.assertEqual(self.db.count('ru'), 1)
        self.assertEqual(self.db.count('en'), 0)

    def test_pending_language_choice_survives_restart_and_gates_admin(self):
        self.message('/start')
        self.db.close()
        self.db = Database(self.path)
        self.bot.db = self.db
        self.click('edit_url_1')
        self.assertEqual(self.db.state(1), 'language:choose')
        self.click('language_en')
        self.click('edit_url_1')
        self.assertEqual(self.db.state(1), 'url:1')

    def test_language_choice_precedes_subscription_and_uses_chosen_language(self):
        self.db.put('subscription_enabled', '1')
        self.db.put('subscription_chat', '-1001234567890')
        self.db.put('subscription_url', 'https://t.me/example_channel')
        self.api.call.return_value = {'status': 'left'}
        self.message('/start', 42)
        self.assertFalse(any(call.args[0] == 'getChatMember' for call in self.api.call.call_args_list))
        self.click('language_en', 42)
        self.assertIn('Join the chat', self.api.call.call_args.kwargs['text'])
        self.assertEqual(self.db.count('en'), 1)
        self.api.call.return_value = {'status': 'member'}
        self.click('check_subscription', 42)
        self.assertEqual(self.last_markup(), self.bot.main_keyboard('en'))

    def test_button_edits_and_cancellation_return_to_same_button(self):
        for n in range(1, 6):
            for field, value in [('url', f'https://example.org/{n}'), ('label_ru', f'Текст {n}'), ('label_en', f'Label {n}')]:
                with self.subTest(button=n, field=field):
                    data = f'edit_url_{n}' if field == 'url' else f'edit_label_{n}_{field[-2:]}'
                    self.click(data)
                    self.assertEqual(self.last_markup()['inline_keyboard'][0][0]['callback_data'], f'button_{n}')
                    self.message(value)
                    self.assertEqual(self.db.get(f'button{n}_{field}'), value)
                    self.assertEqual(self.db.state(1), '')
                    self.assertEqual(self.last_markup(), self.bot.button_keyboard('ru', str(n)))
                    self.click(data)
                    self.message('/cancel')
                    self.assertEqual(self.db.state(1), '')
                    self.assertEqual(self.last_markup(), self.bot.button_keyboard('ru', str(n)))
                    self.click(data)
                    self.click(f'button_{n}')
                    self.assertEqual(self.db.state(1), '')
                    self.assertEqual(self.last_markup(), self.bot.button_keyboard('ru', str(n)))

    def test_invalid_button_edit_can_be_corrected_without_losing_context(self):
        for data, bad, good, state in [('edit_url_2', 'not a URL', 'https://example.org/new', 'url:2'),
                                       ('edit_label_2_en', 'x' * 65, 'New label', 'label:2:en')]:
            with self.subTest(data=data):
                self.click(data)
                self.message(bad)
                self.assertEqual(self.db.state(1), state)
                self.message(good)
                self.assertEqual(self.last_markup(), self.bot.button_keyboard('ru', '2'))

    def test_button_colors_apply_to_both_languages_and_survive_restart(self):
        self.click('color_3')
        rows = self.last_markup()['inline_keyboard']
        self.assertEqual({row[0]['callback_data'] for row in rows[:-1]},
                         {'set_color_3_default', 'set_color_3_primary', 'set_color_3_success', 'set_color_3_danger'})
        for style in ('primary', 'success', 'danger'):
            self.click(f'set_color_3_{style}')
            self.assertEqual(self.last_markup(), self.bot.button_keyboard('ru', '3'))
            for lang in ('ru', 'en'):
                self.assertEqual(self.bot.main_keyboard(lang)['inline_keyboard'][2][0]['style'], style)
        self.db.close()
        self.db = Database(self.path)
        self.bot.db = self.db
        self.assertEqual(self.bot.main_keyboard('en')['inline_keyboard'][2][0]['style'], 'danger')
        self.click('set_color_3_purple')
        self.assertEqual(self.db.get('button3_style'), 'danger')
        self.click('set_color_3_default')
        self.assertNotIn('style', self.bot.main_keyboard('ru')['inline_keyboard'][2][0])

    def test_statistics_split_reset_and_language_change(self):
        self.click('language_ru', 42)
        self.click('language_en', 43)
        self.click('language_en', 44)
        self.click('stats')
        text = self.api.call.call_args.kwargs['text']
        self.assertIn('Пользователей после сброса: 3\n🇷🇺 RU: 1\n🇬🇧 EN: 2', text)
        self.assertIn('Получатели рассылок: 4\n🇷🇺 RU: 2\n🇬🇧 EN: 2', text)
        self.click('language_ru', 43)
        self.assertEqual((self.db.count('ru'), self.db.count('en')), (2, 1))
        self.click('reset')
        self.assertEqual(self.db.count(), 0)
        self.assertEqual(self.db.audience_count(), 4)
        self.message('/start', 44)
        self.assertEqual(self.db.count(), 0)
        self.click('language_en', 44)
        self.assertEqual((self.db.count('ru'), self.db.count('en')), (0, 1))
        self.db.set_language(1, 'en')
        self.click('stats')
        self.assertIn('Users since reset: 1', self.api.call.call_args.kwargs['text'])

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
