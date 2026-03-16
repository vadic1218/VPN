import tempfile
import unittest
from datetime import timedelta, tzinfo
from pathlib import Path

import bot


class FakeTimezone(tzinfo):
    key = "UTC"

    def utcoffset(self, dt):
        return timedelta(0)

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return self.key


class ReminderStoreTests(unittest.TestCase):
    def test_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = bot.ReminderStore(Path(tmpdir) / "reminders.db")

            store.set_state(123, bot.STATE_ADD_TEXT, {"time_text": "09:00"})

            self.assertEqual(
                store.get_state(123),
                (bot.STATE_ADD_TEXT, {"time_text": "09:00"}),
            )


class HandleMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []
        self.bot = self._build_fake_bot()

    def _build_fake_bot(self) -> object:
        outer = self

        class FakeBot:
            def send_message(self, chat_id: int, text: str) -> None:
                outer.sent_messages.append((chat_id, text))

            def send_inline_message(self, chat_id: int, text: str, inline_markup: str) -> None:
                outer.sent_messages.append((chat_id, text))

            def answer_callback_query(self, callback_query_id: str, text: str) -> None:
                return None

        return FakeBot()

    def test_cancel_clears_add_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = bot.ReminderStore(Path(tmpdir) / "reminders.db")
            store.set_state(123, bot.STATE_ADD_TIME, {})

            bot.handle_message(
                self.bot,
                store,
                FakeTimezone(),
                {"chat": {"id": 123}, "text": "/cancel"},
            )

            self.assertIsNone(store.get_state(123))
            self.assertIn("отменено", self.sent_messages[-1][1].lower())

    def test_help_is_available_during_add_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = bot.ReminderStore(Path(tmpdir) / "reminders.db")
            store.set_state(123, bot.STATE_ADD_TIME, {})

            bot.handle_message(
                self.bot,
                store,
                FakeTimezone(),
                {"chat": {"id": 123}, "text": "/help"},
            )

            self.assertEqual(store.get_state(123), (bot.STATE_ADD_TIME, {}))
            self.assertIn("бот-напоминалка", self.sent_messages[-1][1].lower())


if __name__ == "__main__":
    unittest.main()
