import json
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

    def prepare(self):
        self.click('broadcast_new')
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
        self.click('broadcast_new')
        message = {'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'photo': [{'file_id': 'small'}, {'file_id': 'large'}], 'caption': 'Caption'}
        self.bot.on_message(dict(message, media_group_id='album'))
        self.assertEqual(self.db.state(1), 'broadcast:compose')
        self.bot.on_message(message)
        payload = json.loads(self.db.connection.execute('SELECT payload FROM broadcasts').fetchone()[0])
        self.assertEqual(payload['photo'], 'large')
        self.assertEqual(payload['caption'], 'Caption')


if __name__ == '__main__':
    unittest.main()
