"""Terminal refusal behaviour, without any platform or model requests."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_image_guard.feedback import DEFAULT_NOTICE
from astrbot_plugin_image_guard.intercept import SendInterceptor


class RefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.delivered = []
        self.checks = []
        recorder, checks = self.delivered, self.checks

        class Guard:
            async def check(self, message, **options):
                checks.append(options)
                if any(part.get("type") == "image" for part in message or []):
                    return "blocked"
                return None

        class Event:
            unified_msg_origin = "qq:GroupMessage:A"

            async def send(self, message):
                recorder.append(message)

            async def send_streaming(self, generator, use_fallback=False):
                async for chunk in generator:
                    await self.send(chunk)

        class Platform:
            async def send_by_session(self, session, message_chain):
                recorder.append(message_chain)

        class Tool:
            async def call(self, context, **kwargs):
                if kwargs.get("via_adapter"):
                    await Platform().send_by_session(
                        context.context.event.unified_msg_origin, kwargs["messages"]
                    )
                else:
                    recorder.append(kwargs["messages"])
                return "Message sent successfully"

        self.Event, self.Tool, self.Platform = Event, Tool, Platform
        self.guard = Guard()
        self.notice = [{"type": "plain", "text": DEFAULT_NOTICE}]
        self.wrapper = SendInterceptor(self.guard, lambda: self.notice)
        self.wrapper.install(Event, Platform, Tool)
        self.event = Event()
        self.context = SimpleNamespace(context=SimpleNamespace(event=self.event))
        self.image = [{"type": "image", "path": "denied.png"}]
        self.retry = [{"type": "plain", "text": "换格式重新发一次试试"}]

    async def asyncTearDown(self):
        self.wrapper.uninstall()

    async def deny(self):
        return await self.Tool().call(self.context, messages=self.image)

    async def test_tool_reports_non_retryable_terminal_result(self):
        text = await self.deny()
        payload = json.loads(text[text.index("{") :])
        self.assertFalse(payload["retryable"])
        self.assertTrue(payload["request_terminated"])
        self.assertEqual(payload["user_message"], DEFAULT_NOTICE)
        self.assertIn("不得建议", payload["instruction"])
        self.assertEqual(self.delivered, [])

    async def test_follow_up_text_is_replaced_once_and_duplicates_suppressed(self):
        await self.deny()
        await self.event.send(self.retry)
        await self.event.send(self.retry)
        self.assertEqual(self.delivered, [self.notice])

    async def test_retry_tool_call_stops_without_reinspecting_or_sending(self):
        await self.deny()
        before = len(self.checks)
        result = await self.Tool().call(self.context, messages=self.retry)
        self.assertIn('"retryable": false', result)
        self.assertEqual(len(self.checks), before)
        self.assertEqual(self.delivered, [])

    async def test_lower_adapter_refusal_cannot_return_sent_success(self):
        original = self.guard.check

        async def adapter_only(message, **options):
            return None if options.get("tool_only") else await original(message, **options)

        self.guard.check = adapter_only
        result = await self.Tool().call(self.context, messages=self.image, via_adapter=True)
        self.assertIn("ImageGuard blocked", result)
        self.assertNotIn("successfully", result)
        await self.event.send(self.retry)
        self.assertEqual(self.delivered, [self.notice])

    async def test_new_incoming_event_is_not_stuck_in_old_refusal(self):
        await self.deny()
        ordinary = [{"type": "plain", "text": "正常的新话题"}]
        await self.Event().send(ordinary)
        self.assertEqual(self.delivered, [ordinary])

    async def test_parallel_unrelated_event_is_not_affected(self):
        normal = [{"type": "plain", "text": "hello"}]
        await asyncio.gather(self.deny(), self.Event().send(normal))
        self.assertEqual(self.delivered, [normal])

    async def test_stream_stops_at_blocked_image_without_retry_tail(self):
        async def stream():
            yield self.image
            yield self.retry

        await self.event.send_streaming(stream())
        self.assertEqual(self.delivered, [self.notice])

    async def test_stream_after_tool_refusal_contains_only_fixed_notice(self):
        await self.deny()

        async def stream():
            yield self.retry
            yield self.retry

        await self.event.send_streaming(stream())
        self.assertEqual(self.delivered, [self.notice])

    async def test_silent_refusal_suppresses_generated_text_too(self):
        self.wrapper.notice_factory = lambda: None
        await self.deny()
        await self.event.send(self.retry)
        self.assertEqual(self.delivered, [])

    async def test_unblocked_text_and_tool_success_are_unchanged(self):
        text = [{"type": "plain", "text": "hello"}]
        result = await self.Tool().call(self.context, messages=text)
        self.assertEqual(result, "Message sent successfully")
        self.assertEqual(self.delivered, [text])
