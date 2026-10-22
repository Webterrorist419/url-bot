import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from bot import Database, TelegramBot, TelegramError


class BroadcastTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'db.sqlite'
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())
        self.api = Mock()
        self.bot = TelegramBot(self.api, self.db, 1)
        for uid in (1, 2, 3):
            self.db.record_start(uid)

    def click(self, data, uid=1):
        self.bot.on_callback({'id': 'query', 'from': {'id': uid}, 'message': {'chat': {'id': uid, 'type': 'private'}}, 'data': data})

    def prepare(self, lang='ru'):
        self.click(f'broadcast_new_{lang}')
        self.bot.on_message({'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'text': 'News', 'entities': [{'type': 'bold', 'offset': 0, 'length': 4}]})

    def test_preview_confirmation_reset_and_duplicate_click(self):
        self.prepare()
        self.assertEqual(self.db.connection.execute('SELECT status FROM broadcasts').fetchone()[0], 'draft')
        self.db.reset_count()
        self.click('broadcast_send_1', 2)
        self.assertEqual(self.db.connection.execute('SELECT status FROM broadcasts').fetchone()[0], 'draft')
        self.click('broadcast_send_1')
        self.click('broadcast_send_1')
        self.bot.broadcast_tick()
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM broadcast_recipients WHERE status='sent'").fetchone()[0], 3)
        self.assertEqual(self.db.connection.execute('SELECT status FROM broadcasts').fetchone()[0], 'done')

    def test_rate_limit_blocked_and_ambiguous_delivery(self):
        self.prepare()
        self.click('broadcast_send_1')
        self.api.call.side_effect = TelegramError(429, 'limit', 30)
        self.bot.broadcast_tick()
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM broadcast_recipients WHERE status='pending'").fetchone()[0], 3)
        with self.db.connection as conn:
            conn.execute('UPDATE broadcasts SET next_at=0')
        outcomes = iter([TelegramError(403, 'blocked'), TelegramError(503, 'timeout'), None])
        def deliver(method, **params):
            if params.get('_broadcast'):
                error = next(outcomes)
                if error:
                    raise error
        self.api.call.side_effect = deliver
        self.bot.broadcast_tick()
        self.assertEqual(dict(self.db.connection.execute('SELECT status,COUNT(*) FROM broadcast_recipients GROUP BY status')), {'failed': 1, 'unknown': 1, 'sent': 1})

    def test_restart_and_cancellation(self):
        self.prepare()
        self.click('broadcast_send_1')
        with self.db.connection as conn:
            conn.execute("UPDATE broadcast_recipients SET status='sending' WHERE user_id=1")
        self.db.close()
        self.db = Database(self.path)
        self.bot.db = self.db
        self.assertEqual(self.db.connection.execute('SELECT status FROM broadcast_recipients WHERE user_id=1').fetchone()[0], 'unknown')
        self.click('broadcast_cancel_1')
        self.api.reset_mock()
        self.bot.broadcast_tick()
        self.api.call.assert_not_called()

    def test_photo_and_album_rejection(self):
        self.click('broadcast_new_ru')
        message = {'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'photo': [{'file_id': 'small'}, {'file_id': 'large'}], 'caption': 'Caption'}
        self.bot.on_message(dict(message, media_group_id='album'))
        self.assertEqual(self.db.state(1), 'broadcast:compose:ru')
        self.bot.on_message(message)
        payload = json.loads(self.db.connection.execute('SELECT payload FROM broadcasts').fetchone()[0])
        self.assertEqual(payload['photo'], 'large')
        self.assertEqual(payload['caption'], 'Caption')

    def test_broadcast_recipients_follow_target_language_and_snapshot(self):
        self.db.set_language(3, 'en')
        self.db.set_language(4, 'en')
        self.bot.on_message({'from': {'id': 5}, 'chat': {'id': 5, 'type': 'private'}, 'text': '/start'})
        self.prepare('en')
        self.assertEqual(self.db.connection.execute('SELECT lang FROM broadcasts').fetchone()[0], 'en')
        preview = self.api.call.call_args.kwargs['text']
        self.assertIn('Язык рассылки: EN', preview)
        self.assertIn('Получателей сейчас: 2', preview)
        self.click('broadcast_send_1')
        recipients = {row[0] for row in self.db.connection.execute('SELECT user_id FROM broadcast_recipients WHERE job_id=1')}
        self.assertEqual(recipients, {3, 4})
        self.db.set_language(3, 'ru')
        self.db.set_language(6, 'en')
        self.api.reset_mock()
        self.bot.broadcast_tick()
        deliveries = [call.kwargs['chat_id'] for call in self.api.call.call_args_list if call.kwargs.get('_broadcast')]
        self.assertEqual(deliveries, [3, 4])
        self.prepare('ru')
        self.click('broadcast_send_2')
        self.assertEqual({row[0] for row in self.db.connection.execute('SELECT user_id FROM broadcast_recipients WHERE job_id=2')}, {1, 2, 3})

    def test_each_language_keeps_its_own_draft_and_history(self):
        self.prepare('ru')
        self.prepare('en')
        self.prepare('ru')
        self.assertEqual(list(self.db.connection.execute('SELECT lang,status FROM broadcasts ORDER BY id')),
                         [('ru', 'cancelled'), ('en', 'draft'), ('ru', 'draft')])
        self.click('broadcast_en')
        text = self.api.call.call_args.kwargs['text']
        self.assertIn('#2 [EN]', text)
        self.assertNotIn('#1', text)
        self.assertNotIn('#3', text)
        self.click('broadcast_new_en')
        self.assertEqual(self.api.call.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['callback_data'], 'broadcast_en')
        self.bot.on_message({'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'text': '/cancel'})
        self.assertEqual(self.db.state(1), '')
        self.assertIn('Рассылки EN', self.api.call.call_args.kwargs['text'])

    def test_legacy_new_broadcast_button_requires_language_selection(self):
        self.click('broadcast_new')
        self.assertEqual(self.db.state(1), '')
        rows = self.api.call.call_args.kwargs['reply_markup']['inline_keyboard']
        self.assertEqual([button['callback_data'] for button in rows[0]], ['broadcast_ru', 'broadcast_en'])

    def test_busy_broadcast_preserves_other_language_draft(self):
        self.db.set_language(3, 'en')
        self.prepare('ru')
        self.prepare('en')
        self.click('broadcast_send_1')
        self.api.reset_mock()
        self.click('broadcast_send_2')
        self.assertTrue(any('Другая рассылка' in call.kwargs.get('text', '') for call in self.api.call.call_args_list))
        self.assertEqual(self.db.connection.execute('SELECT status FROM broadcasts WHERE id=2').fetchone()[0], 'draft')
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM broadcast_recipients WHERE job_id=2').fetchone()[0], 0)
        self.bot.broadcast_tick()
        self.click('broadcast_send_2')
        self.assertEqual(list(self.db.connection.execute('SELECT user_id FROM broadcast_recipients WHERE job_id=2')), [(3,)])

    def test_language_draft_and_composer_survive_restart(self):
        self.prepare('en')
        self.click('broadcast_new_ru')
        self.db.close()
        self.db = Database(self.path)
        self.bot.db = self.db
        self.assertEqual(self.db.state(1), 'broadcast:compose:ru')
        self.assertEqual(self.db.connection.execute('SELECT lang,status FROM broadcasts').fetchone(), ('en', 'draft'))

    def test_empty_language_audience_completes_without_delivery(self):
        self.prepare('en')
        self.click('broadcast_send_1')
        self.api.reset_mock()
        self.bot.broadcast_tick()
        self.assertFalse(any(call.kwargs.get('_broadcast') for call in self.api.call.call_args_list))
        self.assertEqual(self.db.connection.execute('SELECT status FROM broadcasts').fetchone()[0], 'done')

    def test_upgrade_preserves_users_settings_and_running_recipient_snapshot(self):
        path = Path(self.temp.name) / 'legacy.sqlite3'
        legacy = sqlite3.connect(path)
        with legacy:
            legacy.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, lang TEXT NOT NULL DEFAULT 'ru', started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            legacy.executemany('INSERT INTO users(telegram_id,lang) VALUES(?,?)', [(1, 'ru'), (2, 'en')])
            legacy.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            legacy.execute("INSERT INTO settings VALUES('button1_url','https://example.org/legacy')")
            legacy.execute('CREATE TABLE admin_state (telegram_id INTEGER PRIMARY KEY, action TEXT NOT NULL)')
            legacy.execute("INSERT INTO admin_state VALUES(1,'broadcast:compose')")
            legacy.execute("CREATE TABLE broadcasts (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', next_at REAL NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
            payload = json.dumps({'method': 'sendMessage', 'text': 'Legacy'})
            legacy.executemany('INSERT INTO broadcasts(id,payload,status) VALUES(?,?,?)', [(1, payload, 'draft'), (2, payload, 'running')])
            legacy.execute("CREATE TABLE broadcast_recipients (job_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(job_id,user_id))")
            legacy.executemany('INSERT INTO broadcast_recipients VALUES(?,?,?)', [(2, 1, 'sending'), (2, 2, 'pending')])
        legacy.close()
        for _ in range(2):
            upgraded = Database(path)
            try:
                self.assertEqual((upgraded.count('ru'), upgraded.count('en')), (1, 1))
                self.assertEqual(upgraded.get('button1_url'), 'https://example.org/legacy')
                self.assertEqual(upgraded.get('button1_style'), '')
                self.assertEqual(upgraded.state(1), '')
                self.assertEqual(list(upgraded.connection.execute('SELECT lang,status FROM broadcasts ORDER BY id')), [('', 'cancelled'), ('', 'running')])
                self.assertEqual(list(upgraded.connection.execute('SELECT user_id,status FROM broadcast_recipients ORDER BY user_id')), [(1, 'unknown'), (2, 'pending')])
                legacy_bot = TelegramBot(self.api, upgraded, 1)
                legacy_bot.on_callback({'id': 'legacy', 'from': {'id': 1}, 'message': {'chat': {'id': 1, 'type': 'private'}}, 'data': 'broadcast_send_1'})
                self.assertEqual(upgraded.connection.execute('SELECT COUNT(*) FROM broadcast_recipients WHERE job_id=1').fetchone()[0], 0)
            finally:
                upgraded.close()


if __name__ == '__main__':
    unittest.main()
