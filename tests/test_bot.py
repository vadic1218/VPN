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

    def test_delete_all_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = bot.ReminderStore(Path(tmpdir) / "reminders.db")
            store.add_reminder(123, "09:00", "Утро", "Выпить таблетку")
            store.add_reminder(123, "12:00", "День", "Выпить вторую таблетку")

            deleted_count = store.delete_all_reminders(123)

            self.assertEqual(deleted_count, 2)
            self.assertEqual(store.list_reminders(123), [])


class HandleMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []
        self.inline_messages: list[tuple[int, str, str]] = []
        self.callback_answers: list[tuple[str, str]] = []
        self.bot = self._build_fake_bot()

    def _build_fake_bot(self) -> object:
        outer = self

        class FakeBot:
            def send_message(self, chat_id: int, text: str, reply_markup: str | None = None) -> None:
                outer.sent_messages.append((chat_id, text))

            def send_inline_message(self, chat_id: int, text: str, inline_markup: str) -> None:
                outer.inline_messages.append((chat_id, text, inline_markup))

            def answer_callback_query(self, callback_query_id: str, text: str) -> None:
                outer.callback_answers.append((callback_query_id, text))

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

    def test_add_command_supports_title_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = bot.ReminderStore(Path(tmpdir) / "reminders.db")

            bot.handle_message(
                self.bot,
                store,
                FakeTimezone(),
                {"chat": {"id": 123}, "text": "/add 09:00 Утро | Выпить таблетку"},
            )

            rows = store.list_reminders(123)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], "Утро")
            self.assertEqual(rows[0]["message"], "Выпить таблетку")

    def test_delete_without_id_removes_all_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = bot.ReminderStore(Path(tmpdir) / "reminders.db")
            store.add_reminder(123, "09:00", "Утро", "Выпить таблетку")
            store.add_reminder(123, "12:00", "День", "Выпить вторую таблетку")

            bot.handle_message(
                self.bot,
                store,
                FakeTimezone(),
                {"chat": {"id": 123}, "text": "/delete"},
            )

            self.assertEqual(store.list_reminders(123), [])
            self.assertIn("удалил все", self.sent_messages[-1][1].lower())

    def test_list_sends_inline_delete_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = bot.ReminderStore(Path(tmpdir) / "reminders.db")
            store.add_reminder(123, "09:00", "Утро", "Выпить таблетку")

            bot.handle_message(
                self.bot,
                store,
                FakeTimezone(),
                {"chat": {"id": 123}, "text": "/list"},
            )

            self.assertEqual(len(self.inline_messages), 1)
            self.assertIn("название: утро", self.inline_messages[-1][1].lower())
            self.assertIn("delete:1", self.inline_messages[-1][2])

    def test_delete_callback_removes_one_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = bot.ReminderStore(Path(tmpdir) / "reminders.db")
            store.add_reminder(123, "09:00", "Утро", "Выпить таблетку")

            bot.handle_callback_query(
                self.bot,
                store,
                {
                    "id": "cb-1",
                    "data": "delete:1",
                    "message": {"chat": {"id": 123}},
                },
            )

            self.assertEqual(store.list_reminders(123), [])
            self.assertIn("удалено", self.callback_answers[-1][1].lower())


if __name__ == "__main__":
    unittest.main()
