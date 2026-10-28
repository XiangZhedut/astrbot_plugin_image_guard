"""Cross-chat isolation regressions. All sends stay in an in-memory recorder."""

import base64
import io
import json
import logging
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_image_guard.guard import ImageGuard
from astrbot_plugin_image_guard.history import HistoryLibrary
from astrbot_plugin_image_guard.intercept import SendInterceptor
from astrbot_plugin_image_guard.media import MediaReader
from astrbot_plugin_image_guard.policy import DenyStore
from astrbot_plugin_image_guard.scope import normalize_destination

A, B = "qq:GroupMessage:A", "qq:GroupMessage:B"


def picture(fmt="PNG", resize=False):
    im = Image.new("RGB", (240, 180), "#2679af")
    draw = ImageDraw.Draw(im)
    draw.ellipse((30, 10, 150, 150), fill="yellow")
    draw.line((0, 160, 230, 20), fill="red", width=9)
    if resize:
        im = im.resize((120, 90))
    output = io.BytesIO()
    im.save(output, fmt, quality=50)
    return output.getvalue()


class ScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library = HistoryLibrary(self.root / "plugin", None, self.root / "temp", {})
        self.store = DenyStore(self.root / "deny.db")
        self.reader = MediaReader()
        self.config = {"group_isolation": True}
        self.guard = ImageGuard(
            self.store,
            self.reader,
            self.config,
            self.root / "missing.db",
            logging.getLogger("scope"),
        )
        self.guard.history = self.library
        self.raw = picture()
        self.identifier = self.library.ingest(
            self.raw, "source.png", archive=True, group_key=A, received_scope=A
        )
        self.path = self.library.local_file(self.identifier)[0]
        self.message = [{"type": "image", "path": str(self.path)}]
        self.delivered = []
        recorder = self.delivered

        class Event:
            def __init__(self, scope):
                self.unified_msg_origin = scope

            async def send(self, message):
                recorder.append(message)

            async def send_streaming(self, generator, use_fallback=False):
                async for message in generator:
                    await self.send(message)

        class Platform:
            async def send_by_session(self, session, message_chain):
                recorder.append(message_chain)

        class Tool:
            async def call(self, context, **kwargs):
                recorder.append(kwargs["messages"])
                return "sent"

        self.Event, self.Platform, self.Tool = Event, Platform, Tool
        self.notice = [{"type": "plain", "text": "blocked"}]
        self.interceptor = SendInterceptor(self.guard, lambda: self.notice)
        self.interceptor.install(Event, Platform, Tool)

    async def asyncTearDown(self):
        self.interceptor.uninstall()
        await self.reader.close()
        self.store.close()
        self.library.close()
        self.tmp.cleanup()

    def context(self, scope):
        return SimpleNamespace(context=SimpleNamespace(event=self.Event(scope)))

    async def test_a_image_allowed_in_a_but_blocked_in_b(self):
        await self.Event(A).send(self.message)
        await self.Event(B).send(self.message)
        self.assertEqual(self.delivered, [self.message, self.notice])

    async def test_shared_group_members_normalize_to_same_group(self):
        first = "official:GroupMessage:" + "1" * 32 + "_" + "A" * 32
        second = "official:GroupMessage:" + "2" * 32 + "_" + "A" * 32
        self.library.record_received(self.identifier, first)
        self.assertEqual(normalize_destination(first), normalize_destination(second))
        await self.Event(second).send(self.message)
        self.assertEqual(self.delivered, [self.message])

    async def test_distinct_platforms_with_same_group_id_are_isolated(self):
        await self.Event("other:GroupMessage:A").send(self.message)
        self.assertEqual(self.delivered, [self.notice])

    async def test_explicit_tool_destination_is_checked_not_current_context(self):
        result = await self.Tool().call(self.context(A), messages=self.message, session=B)
        self.assertIn("ImageGuard blocked", result)
        self.assertEqual(self.delivered, [])

    async def test_tool_uses_current_group_when_destination_omitted(self):
        result = await self.Tool().call(self.context(B), messages=self.message)
        self.assertIn("ImageGuard blocked", result)
        self.assertEqual(self.delivered, [])
        await self.Tool().call(self.context(A), messages=self.message)
        self.assertEqual(self.delivered, [self.message])

    async def test_active_send_by_session_is_checked(self):
        await self.Platform().send_by_session(B, self.message)
        await self.Platform().send_by_session(A, self.message)
        self.assertEqual(self.delivered, [self.notice, self.message])

    async def test_streaming_cannot_bypass_scope_check(self):
        async def stream():
            yield self.message

        await self.Event(B).send_streaming(stream())
        self.assertEqual(self.delivered, [self.notice])

    async def test_file_uri_base64_and_encoding_changes_cannot_cross_groups(self):
        for source in [
            self.path.as_uri(),
            "base64://" + base64.b64encode(self.raw).decode(),
            "base64://" + base64.b64encode(picture("JPEG", resize=True)).decode(),
        ]:
            message = [{"type": "image", "file": source}]
            self.assertIsNotNone(await self.guard.check(message, destination=B))
            self.assertIsNone(await self.guard.check(message, destination=A))

    async def test_source_unknown_and_destination_unknown_both_fail_closed(self):
        output = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(output, "PNG")
        unknown = [
            {"type": "image", "file": "base64://" + base64.b64encode(output.getvalue()).decode()}
        ]
        self.assertIsNotNone(await self.guard.check(unknown, destination=A))
        self.assertIsNotNone(await self.guard.check(self.message, destination=""))
        self.assertIsNotNone(await self.guard.check(self.message, destination="malformed"))

    async def test_actual_receipt_in_b_grants_b_but_display_group_does_not(self):
        self.library.record_group(self.identifier, B, "UI only")
        self.assertIsNotNone(await self.guard.check(self.message, destination=B))
        self.library.ingest(self.raw, "another-file.png", received_scope=B)
        self.assertIsNone(await self.guard.check(self.message, destination=B))

    async def test_group_image_cannot_leak_to_private_chat(self):
        private = "qq:FriendMessage:owner"
        self.assertIsNotNone(await self.guard.check(self.message, destination=private))
        self.library.ingest(self.raw, "private-receipt.png", received_scope=private)
        self.assertIsNone(await self.guard.check(self.message, destination=private))

    async def test_scope_failures_cannot_be_relaxed_by_fail_closed_false(self):
        self.config["fail_closed"] = False
        self.assertIsNotNone(
            await self.guard.check(
                [{"type": "image", "path": str(self.root / "absent.png")}], destination=B
            )
        )

    async def test_plain_and_voice_messages_do_not_need_image_permissions(self):
        for kind in ("plain", "record"):
            msg = [{"type": kind, "text": "hello", "file": "voice.wav"}]
            self.assertIsNone(await self.guard.check(msg, destination=B))

    async def test_global_ban_still_overrides_valid_group_permission(self):
        from astrbot_plugin_image_guard.policy import fingerprint

        self.store.add(fingerprint(self.raw))
        self.assertIsNotNone(await self.guard.check(self.message, destination=A))

    async def test_scope_can_be_disabled_explicitly(self):
        self.config["group_isolation"] = False
        self.assertIsNone(await self.guard.check(self.message, destination=B))

    async def test_lookalike_in_b_does_not_override_exact_source_a(self):
        self.library.ingest(picture("JPEG"), "b-version.jpg", received_scope=B)
        self.assertIsNotNone(await self.guard.check(self.message, destination=B))
        self.assertIsNone(await self.guard.check(self.message, destination=A))

    async def test_history_bot_references_and_user_text_do_not_grant_permissions(self):
        core = self.root / "core.db"
        actual_image = {"type": "Image", "file": str(self.path)}
        with closing(sqlite3.connect(core)) as conn, conn:
            conn.execute(
                "CREATE TABLE platform_message_history(id INTEGER,content TEXT,platform_id TEXT,user_id TEXT)"
            )
            entries = [
                (1, {"type": "user", "message": [actual_image]}, A),
                (2, {"type": "bot", "message": [actual_image]}, B),
                (3, {"type": "user", "message": [{"type": "Plain", "text": str(self.path)}]}, B),
            ]
            for n, content, session in entries:
                conn.execute(
                    "INSERT INTO platform_message_history VALUES (?,?,?,?)",
                    (n, json.dumps(content), "qq", session),
                )
        self.library.core_database = core
        self.library.scan()
        self.assertIn(B, self.library.list_images()["items"][0]["groups"])
        self.assertEqual(self.library.confirmed_scopes(self.identifier), [A])
        self.assertIsNotNone(await self.guard.check(self.message, destination=B))

    async def test_parent_send_rerouting_to_another_group_is_rechecked(self):
        platform = self.Platform()

        class Rerouting(self.Event):
            async def send(self, message):
                await platform.send_by_session(B, message)

        self.interceptor.install(self.Event, self.Platform, self.Tool)
        await Rerouting(A).send(self.message)
        self.assertEqual(self.delivered, [self.notice])

    async def test_user_history_image_blocks_restore_permissions_but_bot_and_text_do_not(self):
        core = self.root / "fallback.db"
        im = {"type": "image_url", "image_url": {"url": self.path.as_uri()}}
        with closing(sqlite3.connect(core)) as conn, conn:
            conn.execute(
                "CREATE TABLE conversations(conversation_id TEXT,content TEXT,platform_id TEXT,user_id TEXT)"
            )
            conn.execute(
                "INSERT INTO conversations VALUES (?,?,?,?)",
                ("1", json.dumps([{"role": "user", "content": [im]}]), "qq", A),
            )
            conn.execute(
                "INSERT INTO conversations VALUES (?,?,?,?)",
                (
                    "2",
                    json.dumps(
                        [
                            {"role": "assistant", "content": [im]},
                            {"role": "user", "content": [{"type": "text", "text": str(self.path)}]},
                        ]
                    ),
                    "qq",
                    B,
                ),
            )
        with self.library.lock, self.library.conn:
            self.library.conn.execute("DELETE FROM received_scopes")
        self.library.core_database = core
        self.library.scan()
        self.assertEqual(self.library.confirmed_scopes(self.identifier), [A])
        self.assertIsNone(await self.guard.check(self.message, destination=A))
        self.assertIsNotNone(await self.guard.check(self.message, destination=B))


if __name__ == "__main__":
    unittest.main()
