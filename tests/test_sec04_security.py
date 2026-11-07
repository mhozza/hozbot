import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import os
from telegram import Update, Message, Chat, User, MessageEntity, constants
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import main


class TestSec04HandlerRegistration(unittest.TestCase):
    def setUp(self):
        self.app = Application.builder().token("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11").build()
        main.register_handlers(self.app)

    def test_register_handlers_adds_expected_handlers(self):
        """register_handlers must add CommandHandler('start') and MessageHandler."""
        handlers = self.app.handlers.get(0, [])
        self.assertEqual(len(handlers), 2, "Expected exactly 2 handlers registered in group 0")

        cmd_handlers = [h for h in handlers if isinstance(h, CommandHandler)]
        msg_handlers = [h for h in handlers if isinstance(h, MessageHandler)]

        self.assertEqual(len(cmd_handlers), 1, "Expected 1 CommandHandler")
        self.assertEqual(len(msg_handlers), 1, "Expected 1 MessageHandler")
        self.assertIn("start", cmd_handlers[0].commands)

    def test_command_handler_has_private_chat_filter(self):
        """CommandHandler for /start must enforce filters.ChatType.PRIVATE."""
        handlers = self.app.handlers.get(0, [])
        cmd_handler = next(h for h in handlers if isinstance(h, CommandHandler))
        self.assertIsNotNone(cmd_handler.filters, "CommandHandler must have filters configured")
        self.assertEqual(
            cmd_handler.filters,
            filters.ChatType.PRIVATE,
            "CommandHandler('start') must be filtered with filters.ChatType.PRIVATE",
        )

    def test_message_handler_has_private_chat_filter(self):
        """MessageHandler must enforce filters.ChatType.PRIVATE."""
        handlers = self.app.handlers.get(0, [])
        msg_handler = next(h for h in handlers if isinstance(h, MessageHandler))
        self.assertIsNotNone(msg_handler.filters, "MessageHandler must have filters configured")
        # Ensure filters.ChatType.PRIVATE is part of the filter representation
        filter_repr = repr(msg_handler.filters)
        self.assertIn(
            "filters.ChatType.PRIVATE",
            filter_repr,
            "MessageHandler filter chain must include filters.ChatType.PRIVATE",
        )


class TestSec04HandlerFilterCheckUpdate(unittest.TestCase):
    def setUp(self):
        self.app = Application.builder().token("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11").build()
        main.register_handlers(self.app)
        handlers = self.app.handlers.get(0, [])
        self.cmd_handler = next(h for h in handlers if isinstance(h, CommandHandler))
        self.msg_handler = next(h for h in handlers if isinstance(h, MessageHandler))

        self.bot = MagicMock()
        self.bot.username = "hozbot"

        self.user = User(id=100, is_bot=False, first_name="AuthorizedUser")

        self.chat_private = Chat(id=1, type=constants.ChatType.PRIVATE)
        self.chat_group = Chat(id=2, type=constants.ChatType.GROUP)
        self.chat_supergroup = Chat(id=3, type=constants.ChatType.SUPERGROUP)
        self.chat_channel = Chat(id=4, type=constants.ChatType.CHANNEL)

    def _create_command_update(self, chat: Chat, command_text: str = "/start") -> Update:
        entity = MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(command_text))
        message = Message(
            message_id=1,
            date=None,
            chat=chat,
            from_user=self.user,
            text=command_text,
            entities=[entity],
        )
        message.set_bot(self.bot)
        return Update(update_id=1, message=message)

    def _create_message_update(self, chat: Chat, text: str = "What's on tomorrow?") -> Update:
        message = Message(
            message_id=2,
            date=None,
            chat=chat,
            from_user=self.user,
            text=text,
        )
        message.set_bot(self.bot)
        return Update(update_id=2, message=message)

    def test_command_handler_accepts_private_chat(self):
        """CommandHandler accepts /start in private chat."""
        update = self._create_command_update(self.chat_private)
        self.assertTrue(bool(self.cmd_handler.check_update(update)))

    def test_command_handler_rejects_group_chats(self):
        """CommandHandler rejects /start in group and supergroup chats."""
        group_update = self._create_command_update(self.chat_group)
        self.assertFalse(bool(self.cmd_handler.check_update(group_update)))

        supergroup_update = self._create_command_update(self.chat_supergroup)
        self.assertFalse(bool(self.cmd_handler.check_update(supergroup_update)))

    def test_command_handler_rejects_channel_posts(self):
        """CommandHandler rejects /start in channel posts."""
        channel_update = self._create_command_update(self.chat_channel)
        self.assertFalse(bool(self.cmd_handler.check_update(channel_update)))

    def test_message_handler_accepts_private_chat(self):
        """MessageHandler accepts text messages in private chat."""
        update = self._create_message_update(self.chat_private)
        self.assertTrue(bool(self.msg_handler.check_update(update)))

    def test_message_handler_rejects_group_and_supergroup_chats(self):
        """MessageHandler rejects text messages in group and supergroup chats."""
        group_update = self._create_message_update(self.chat_group)
        self.assertFalse(bool(self.msg_handler.check_update(group_update)))

        supergroup_update = self._create_message_update(self.chat_supergroup)
        self.assertFalse(bool(self.msg_handler.check_update(supergroup_update)))

    def test_message_handler_rejects_channel_posts(self):
        """MessageHandler rejects channel posts."""
        channel_update = self._create_message_update(self.chat_channel)
        self.assertFalse(bool(self.msg_handler.check_update(channel_update)))


class TestSec04StartCmdDefenseInDepth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_allowed = main.ALLOWED_USER_IDS
        main.ALLOWED_USER_IDS = [12345]
        self.context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    def tearDown(self):
        main.ALLOWED_USER_IDS = self.orig_allowed

    def _make_update(self, chat_type: str | constants.ChatType | None, user_id: int = 12345) -> Update:
        update = MagicMock(spec=Update)
        if chat_type is not None:
            chat = MagicMock(spec=Chat)
            chat.id = 999
            chat.type = chat_type
            update.effective_chat = chat
        else:
            update.effective_chat = None

        user = MagicMock(spec=User)
        user.id = user_id
        user.first_name = "Alice"
        update.effective_user = user

        message = AsyncMock(spec=Message)
        update.message = message
        return update

    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_start_cmd_allows_private_enum_chat(self, mock_safe_reply):
        """start_cmd accepts private chat (constants.ChatType.PRIVATE)."""
        update = self._make_update(constants.ChatType.PRIVATE, user_id=12345)
        await main.start_cmd(update, self.context)
        mock_safe_reply.assert_called_once()
        args, _ = mock_safe_reply.call_args
        self.assertIn("Family Office Chief of Staff", args[1])

    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_start_cmd_allows_private_string_chat(self, mock_safe_reply):
        """start_cmd accepts private chat with literal string 'private'."""
        update = self._make_update("private", user_id=12345)
        await main.start_cmd(update, self.context)
        mock_safe_reply.assert_called_once()

    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_start_cmd_rejects_group_chat(self, mock_safe_reply):
        """start_cmd drops group chat updates immediately."""
        update = self._make_update(constants.ChatType.GROUP, user_id=12345)
        await main.start_cmd(update, self.context)
        mock_safe_reply.assert_not_called()

    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_start_cmd_rejects_supergroup_chat(self, mock_safe_reply):
        """start_cmd drops supergroup chat updates immediately."""
        update = self._make_update(constants.ChatType.SUPERGROUP, user_id=12345)
        await main.start_cmd(update, self.context)
        mock_safe_reply.assert_not_called()

    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_start_cmd_rejects_channel_chat(self, mock_safe_reply):
        """start_cmd drops channel chat updates immediately."""
        update = self._make_update(constants.ChatType.CHANNEL, user_id=12345)
        await main.start_cmd(update, self.context)
        mock_safe_reply.assert_not_called()

    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_start_cmd_rejects_none_chat(self, mock_safe_reply):
        """start_cmd drops updates where effective_chat is None."""
        update = self._make_update(None, user_id=12345)
        await main.start_cmd(update, self.context)
        mock_safe_reply.assert_not_called()

    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_start_cmd_rejects_unauthorized_user_in_private_chat(self, mock_safe_reply):
        """start_cmd still rejects unauthorized user in private chat."""
        update = self._make_update(constants.ChatType.PRIVATE, user_id=99999)
        await main.start_cmd(update, self.context)
        mock_safe_reply.assert_not_called()


class TestSec04HandleMessageDefenseInDepth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_allowed = main.ALLOWED_USER_IDS
        main.ALLOWED_USER_IDS = [12345]
        self.context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    def tearDown(self):
        main.ALLOWED_USER_IDS = self.orig_allowed

    def _make_update(
        self,
        chat_type: str | constants.ChatType | None,
        user_id: int = 12345,
        text: str = "Summarize my emails",
    ) -> Update:
        update = MagicMock(spec=Update)
        if chat_type is not None:
            chat = MagicMock(spec=Chat)
            chat.id = 888
            chat.type = chat_type
            update.effective_chat = chat
        else:
            update.effective_chat = None

        user = MagicMock(spec=User)
        user.id = user_id
        user.first_name = "Bob"
        user.username = "bob123"
        user.last_name = "Smith"
        update.effective_user = user

        message = AsyncMock(spec=Message)
        message.text = text
        message.caption = None
        message.photo = None
        message.document = None
        update.message = message
        return update

    @patch("main.run_agent", new_callable=AsyncMock)
    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_handle_message_allows_private_enum_chat(self, mock_safe_reply, mock_run_agent):
        """handle_message accepts messages in private chat."""
        mock_run_agent.return_value = "Summary of emails"
        update = self._make_update(constants.ChatType.PRIVATE, user_id=12345)

        await main.handle_message(update, self.context)

        mock_run_agent.assert_called_once()
        mock_safe_reply.assert_called_once_with(update.message, "Summary of emails")

    @patch("main.run_agent", new_callable=AsyncMock)
    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_handle_message_allows_private_string_chat(self, mock_safe_reply, mock_run_agent):
        """handle_message accepts messages when chat.type is literal string 'private'."""
        mock_run_agent.return_value = "Response"
        update = self._make_update("private", user_id=12345)

        await main.handle_message(update, self.context)

        mock_run_agent.assert_called_once()
        mock_safe_reply.assert_called_once()

    @patch("main.run_agent", new_callable=AsyncMock)
    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_handle_message_rejects_group_chat(self, mock_safe_reply, mock_run_agent):
        """handle_message drops group chat updates without calling run_agent or replying."""
        update = self._make_update(constants.ChatType.GROUP, user_id=12345)

        await main.handle_message(update, self.context)

        mock_run_agent.assert_not_called()
        mock_safe_reply.assert_not_called()

    @patch("main.run_agent", new_callable=AsyncMock)
    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_handle_message_rejects_supergroup_chat(self, mock_safe_reply, mock_run_agent):
        """handle_message drops supergroup chat updates without calling run_agent or replying."""
        update = self._make_update(constants.ChatType.SUPERGROUP, user_id=12345)

        await main.handle_message(update, self.context)

        mock_run_agent.assert_not_called()
        mock_safe_reply.assert_not_called()

    @patch("main.run_agent", new_callable=AsyncMock)
    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_handle_message_rejects_channel_chat(self, mock_safe_reply, mock_run_agent):
        """handle_message drops channel chat updates without calling run_agent or replying."""
        update = self._make_update(constants.ChatType.CHANNEL, user_id=12345)

        await main.handle_message(update, self.context)

        mock_run_agent.assert_not_called()
        mock_safe_reply.assert_not_called()

    @patch("main.run_agent", new_callable=AsyncMock)
    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_handle_message_rejects_none_chat(self, mock_safe_reply, mock_run_agent):
        """handle_message drops updates where effective_chat is None."""
        update = self._make_update(None, user_id=12345)

        await main.handle_message(update, self.context)

        mock_run_agent.assert_not_called()
        mock_safe_reply.assert_not_called()

    @patch("main.run_agent", new_callable=AsyncMock)
    @patch("main.safe_reply", new_callable=AsyncMock)
    async def test_handle_message_rejects_unauthorized_user_in_private_chat(self, mock_safe_reply, mock_run_agent):
        """handle_message drops unauthorized users even in private chats."""
        update = self._make_update(constants.ChatType.PRIVATE, user_id=99999)

        await main.handle_message(update, self.context)

        mock_run_agent.assert_not_called()
        mock_safe_reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
