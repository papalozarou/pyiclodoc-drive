# ------------------------------------------------------------------------------
# This test module verifies Telegram command parsing behaviour for backup and
# authentication triggers.
#
# Notes:
# https://core.telegram.org/bots/api#update
# ------------------------------------------------------------------------------

import unittest

from tests._stubs import install_dependency_stubs

install_dependency_stubs()

from app.telegram_bot import parse_command


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


if __name__ == "__main__":
    unittest.main()
