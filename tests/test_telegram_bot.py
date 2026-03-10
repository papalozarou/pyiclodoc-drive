# ------------------------------------------------------------------------------
# This test module verifies Telegram command parsing behaviour for backup and
# authentication triggers.
#
# Notes:
# https://core.telegram.org/bots/api#update
# ------------------------------------------------------------------------------

import unittest
from unittest.mock import Mock, patch

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app import telegram_bot
from app.telegram_bot import TelegramConfig, get_endpoint, parse_command


# ------------------------------------------------------------------------------
# These tests cover username-prefixed command parsing and filtering rules.
# ------------------------------------------------------------------------------
class TestTelegramCommandParsing(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms a correctly formatted backup command is parsed
# for the configured chat.
# --------------------------------------------------------------------------
    def test_parse_backup_command(self) -> None:
        UPDATE = {
            "update_id": 101,
            "message": {
                "chat": {"id": 12345},
                "text": "alice backup",
            },
        }

        EVENT = parse_command(UPDATE, "alice", "12345")

        self.assertIsNotNone(EVENT)
        self.assertEqual(EVENT.command, "backup")
        self.assertEqual(EVENT.args, "")

# --------------------------------------------------------------------------
# This test confirms auth commands preserve argument payload, such as
# MFA codes.
# --------------------------------------------------------------------------
    def test_parse_auth_command_with_arg(self) -> None:
        UPDATE = {
            "update_id": 102,
            "message": {
                "chat": {"id": 12345},
                "text": "alice auth 123456",
            },
        }

        EVENT = parse_command(UPDATE, "alice", "12345")

        self.assertIsNotNone(EVENT)
        self.assertEqual(EVENT.command, "auth")
        self.assertEqual(EVENT.args, "123456")

# --------------------------------------------------------------------------
# This test confirms messages from unexpected chats are ignored for safety.
# --------------------------------------------------------------------------
    def test_ignore_wrong_chat(self) -> None:
        UPDATE = {
            "update_id": 103,
            "message": {
                "chat": {"id": 67890},
                "text": "alice backup",
            },
        }

        EVENT = parse_command(UPDATE, "alice", "12345")

        self.assertIsNone(EVENT)

# --------------------------------------------------------------------------
# This test confirms messages without the username prefix are not
# treated as commands.
# --------------------------------------------------------------------------
    def test_ignore_without_prefix(self) -> None:
        UPDATE = {
            "update_id": 104,
            "message": {
                "chat": {"id": 12345},
                "text": "backup",
            },
        }

        EVENT = parse_command(UPDATE, "alice", "12345")

        self.assertIsNone(EVENT)


# ------------------------------------------------------------------------------
# These tests cover endpoint and API request helper behaviour.
# ------------------------------------------------------------------------------
class TestTelegramApiHelpers(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms endpoint generation follows Bot API URL format.
# --------------------------------------------------------------------------
    def test_get_endpoint(self) -> None:
        ENDPOINT = get_endpoint("abc123", "sendMessage")
        self.assertEqual(ENDPOINT, "https://api.telegram.org/botabc123/sendMessage")

# --------------------------------------------------------------------------
# This test confirms send_message returns False without token or chat ID.
# --------------------------------------------------------------------------
    def test_send_message_requires_token_and_chat_id(self) -> None:
        self.assertFalse(telegram_bot.send_message(TelegramConfig("", "12345"), "hello"))
        self.assertFalse(telegram_bot.send_message(TelegramConfig("token", ""), "hello"))

# --------------------------------------------------------------------------
# This test confirms successful send_message returns True.
# --------------------------------------------------------------------------
    def test_send_message_success(self) -> None:
        CONFIG = TelegramConfig("token", "12345")
        RESPONSE = Mock(ok=True)

        with patch("app.telegram_bot.requests.post", return_value=RESPONSE) as POST:
            RESULT = telegram_bot.send_message(CONFIG, "hello", TIMEOUT=10)

        self.assertTrue(RESULT)
        POST.assert_called_once_with(
            "https://api.telegram.org/bottoken/sendMessage",
            json={"chat_id": "12345", "text": "hello"},
            timeout=10,
        )

# --------------------------------------------------------------------------
# This test confirms request exceptions in send_message return False.
# --------------------------------------------------------------------------
    def test_send_message_request_exception(self) -> None:
        CONFIG = TelegramConfig("token", "12345")

        with patch(
            "app.telegram_bot.requests.post",
            side_effect=telegram_bot.requests.RequestException("boom"),
        ):
            RESULT = telegram_bot.send_message(CONFIG, "hello")

        self.assertFalse(RESULT)

# --------------------------------------------------------------------------
# This test confirms fetch_updates returns empty when token is missing.
# --------------------------------------------------------------------------
    def test_fetch_updates_requires_token(self) -> None:
        RESULT = telegram_bot.fetch_updates(TelegramConfig("", "12345"), OFFSET=None)
        self.assertEqual(RESULT, [])

# --------------------------------------------------------------------------
# This test confirms successful fetch_updates returns Telegram result list.
# --------------------------------------------------------------------------
    def test_fetch_updates_success(self) -> None:
        CONFIG = TelegramConfig("token", "12345")
        RESPONSE = Mock(ok=True)
        RESPONSE.json.return_value = {"ok": True, "result": [{"update_id": 1}]}

        with patch("app.telegram_bot.requests.get", return_value=RESPONSE) as GET:
            RESULT = telegram_bot.fetch_updates(CONFIG, OFFSET=11, TIMEOUT=7)

        self.assertEqual(RESULT, [{"update_id": 1}])
        GET.assert_called_once_with(
            "https://api.telegram.org/bottoken/getUpdates",
            params={"timeout": 7, "offset": 11},
            timeout=12,
        )

# --------------------------------------------------------------------------
# This test confirms fetch_updates handles API and payload failure states.
# --------------------------------------------------------------------------
    def test_fetch_updates_handles_non_ok_and_bad_payload(self) -> None:
        CONFIG = TelegramConfig("token", "12345")
        RESPONSE = Mock(ok=False)

        with patch("app.telegram_bot.requests.get", return_value=RESPONSE):
            self.assertEqual(telegram_bot.fetch_updates(CONFIG, OFFSET=None), [])

        RESPONSE.ok = True
        RESPONSE.json.return_value = {"ok": False}

        with patch("app.telegram_bot.requests.get", return_value=RESPONSE):
            self.assertEqual(telegram_bot.fetch_updates(CONFIG, OFFSET=None), [])

        RESPONSE.json.return_value = {"ok": True, "result": {"bad": "shape"}}

        with patch("app.telegram_bot.requests.get", return_value=RESPONSE):
            self.assertEqual(telegram_bot.fetch_updates(CONFIG, OFFSET=None), [])

# --------------------------------------------------------------------------
# This test confirms request exceptions in fetch_updates return empty list.
# --------------------------------------------------------------------------
    def test_fetch_updates_request_exception(self) -> None:
        CONFIG = TelegramConfig("token", "12345")

        with patch(
            "app.telegram_bot.requests.get",
            side_effect=telegram_bot.requests.RequestException("boom"),
        ):
            RESULT = telegram_bot.fetch_updates(CONFIG, OFFSET=None)

        self.assertEqual(RESULT, [])


# ------------------------------------------------------------------------------
# These tests cover extra command parsing edge conditions.
# ------------------------------------------------------------------------------
class TestTelegramParseCommandEdges(unittest.TestCase):
# --------------------------------------------------------------------------
# This test confirms updates without message payload are ignored.
# --------------------------------------------------------------------------
    def test_parse_command_ignores_non_message_update(self) -> None:
        EVENT = parse_command({"update_id": 5}, "alice", "12345")
        self.assertIsNone(EVENT)

# --------------------------------------------------------------------------
# This test confirms unknown commands are ignored.
# --------------------------------------------------------------------------
    def test_parse_command_ignores_unknown_command(self) -> None:
        UPDATE = {
            "update_id": 6,
            "message": {"chat": {"id": 12345}, "text": "alice ping"},
        }
        EVENT = parse_command(UPDATE, "alice", "12345")
        self.assertIsNone(EVENT)

# --------------------------------------------------------------------------
# This test confirms command matching is case-insensitive on username.
# --------------------------------------------------------------------------
    def test_parse_command_allows_case_insensitive_prefix(self) -> None:
        UPDATE = {
            "update_id": 7,
            "message": {"chat": {"id": 12345}, "text": "Alice ReAuth 123456"},
        }
        EVENT = parse_command(UPDATE, "alice", "12345")
        self.assertIsNotNone(EVENT)
        self.assertEqual(EVENT.command, "reauth")
        self.assertEqual(EVENT.args, "123456")


if __name__ == "__main__":
    unittest.main()
