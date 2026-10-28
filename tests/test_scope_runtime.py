"""Optional checks against installed AstrBot's real event/tool classes."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
try:
    from astrbot.api.star import StarTools
    from astrbot.core.message.components import Image as ImageComponent
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )
    from astrbot.core.tools.message_tools import SendMessageToUserTool
    from astrbot_plugin_image_guard.main import ImageSendGuardPlugin
except ImportError:
    ImageSendGuardPlugin = None


@unittest.skipIf(ImageSendGuardPlugin is None, "Requires installed AstrBot")
class RuntimeScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_refusal_overrides_model_retry_text_tools_and_saved_reply(self):
        from astrbot.api.provider import LLMResponse, ProviderRequest
        from astrbot.core.agent.message import Message, TextPart
        from astrbot_plugin_image_guard.feedback import DEFAULT_NOTICE, RESPONSE_POLICY

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(StarTools, "get_data_dir", return_value=Path(directory)):
                plugin = ImageSendGuardPlugin(SimpleNamespace(), {})
                try:
                    event = SimpleNamespace()
                    req = ProviderRequest(system_prompt="keep persona")
                    await plugin.before_llm(event, req)
                    await plugin.before_llm(event, req)
                    self.assertEqual(req.system_prompt.count(RESPONSE_POLICY), 1)
                    self.assertIn("keep persona", req.system_prompt)
                    response = LLMResponse(
                        role="assistant",
                        completion_text="换格式再发一次吧",
                        tools_call_name=["send_message_to_user"],
                        tools_call_ids=["retry"],
                        tools_call_args=[{}],
                        reasoning_content="建议重试",
                    )
                    plugin.interceptor.reject(event)
                    await plugin.refusal_response(event, response)
                    self.assertEqual(response.completion_text, DEFAULT_NOTICE)
                    self.assertEqual(response.tools_call_name, [])
                    self.assertIsNone(response.reasoning_content)
                    user = Message(role="user", content=[TextPart(text="keep user message")])
                    saved = Message(role="assistant", content=[TextPart(text="换路径试试")])
                    run = SimpleNamespace(messages=[user, saved])
                    await plugin.refusal_history(event, run, response)
                    self.assertEqual(run.messages[1].content[0].text, DEFAULT_NOTICE)
                    self.assertEqual(user.content[0].text, "keep user message")
                    unrelated = LLMResponse(role="assistant", completion_text="new topic")
                    await plugin.refusal_response(SimpleNamespace(), unrelated)
                    self.assertEqual(unrelated.completion_text, "new topic")
                finally:
                    await plugin.terminate()

    async def test_real_event_collection_and_real_tool_do_not_cross_chats(self):
        captured = []

        async def recorder(_event, message):
            captured.append(message)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = io.BytesIO()
            Image.new("RGB", (48, 64), "orange").save(data, "PNG")
            image = root / "received.png"
            image.write_bytes(data.getvalue())
            with (
                patch.object(StarTools, "get_data_dir", return_value=root / "plugin"),
                patch.object(QQOfficialMessageEvent, "send", recorder),
            ):
                plugin = ImageSendGuardPlugin(
                    SimpleNamespace(), {"group_isolation": True, "history_enabled": False}
                )
                await plugin.initialize()
                try:

                    def event(group):
                        value = object.__new__(QQOfficialMessageEvent)
                        value.unified_msg_origin = f"qq:GroupMessage:{group}"
                        value.platform_meta = SimpleNamespace(id="qq")
                        value.message_obj = SimpleNamespace(
                            group_id=group,
                            group=None,
                            message=[ImageComponent.fromFileSystem(image)],
                        )
                        return value

                    first, second = event("A"), event("B")
                    await plugin.collect_images(first)
                    chain = MessageChain(chain=[ImageComponent.fromFileSystem(image)])
                    await first.send(chain)
                    await second.send(chain)
                    self.assertEqual(len(captured), 2)
                    self.assertIsInstance(captured[0].chain[0], ImageComponent)
                    self.assertTrue(all(isinstance(part, Plain) for part in captured[1].chain))
                    context = SimpleNamespace(context=SimpleNamespace(event=first))
                    result = await SendMessageToUserTool().call(
                        context,
                        messages=[{"type": "image", "path": str(image)}],
                        session="qq:GroupMessage:B",
                    )
                    self.assertIn("ImageGuard blocked", result)
                    self.assertEqual(len(captured), 2)
                    self.assertEqual(len(list(plugin.history.library.iterdir())), 0)
                finally:
                    await plugin.terminate()
