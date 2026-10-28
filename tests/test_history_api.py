"""Optional integration tests when run in an installed AstrBot environment."""

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

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_image_guard.guard import ImageGuard
from astrbot_plugin_image_guard.history import HistoryLibrary
from astrbot_plugin_image_guard.media import MediaReader
from astrbot_plugin_image_guard.policy import DenyStore

try:
    from astrbot.api.web import PluginRequest, bind_request_context
    from astrbot_plugin_image_guard.history_api import HistoryAPI
    from starlette.requests import Request
except ImportError:
    HistoryAPI = None


@unittest.skipIf(HistoryAPI is None, "Run with an installed AstrBot for page API integration tests")
class HistoryAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.image = self.root / "temp" / "sample.png"
        self.image.parent.mkdir()
        output = io.BytesIO()
        Image.new("RGB", (20, 30), "blue").save(output, "PNG")
        self.image.write_bytes(output.getvalue())
        self.core = self.root / "core.db"
        with closing(sqlite3.connect(self.core)) as conn, conn:
            conn.executescript(
                "CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY,content TEXT);"
            )
            conn.execute(
                "INSERT INTO conversations VALUES (?,?)",
                (
                    "1",
                    json.dumps(
                        [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "image_url", "image_url": {"url": self.image.as_uri()}}
                                ],
                            }
                        ]
                    ),
                ),
            )
        self.library = HistoryLibrary(self.root / "plugin", self.core, self.image.parent, {})
        self.image_id = self.library.index_file(self.image)
        self.store = DenyStore(self.root / "denylist.db")
        self.reader = MediaReader()
        logger = logging.getLogger("api-test")
        guard = ImageGuard(self.store, self.reader, {}, self.root / "missing.db", logger)
        self.api = HistoryAPI(
            SimpleNamespace(
                history=self.library,
                store=self.store,
                reader=self.reader,
                config={},
                guard=guard,
                logger=logger,
            )
        )

    async def asyncTearDown(self):
        if self.api.scan_task:
            await self.api.scan_task
        await self.reader.close()
        self.library.close()
        self.store.close()
        self.temp.cleanup()

    async def call(self, handler, body=None, query="", username="admin"):
        raw = json.dumps(body or {}).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST" if body else "GET",
                "path": "/test",
                "query_string": query.encode(),
                "headers": [],
                "scheme": "http",
                "server": ("localhost", 80),
                "client": ("localhost", 1),
            },
            receive,
        )
        with bind_request_context(PluginRequest(request, username=username)):
            response = await self.api._protected(handler)()
        return response.status_code, json.loads(response.body)

    async def action(self, operation, **options):
        return await self.call(
            self.api.action, {"action": operation, "ids": [self.image_id], **options}
        )

    async def test_unauthenticated_requests_rejected(self):
        code, _ = await self.call(self.api.images, username=None)
        self.assertEqual(code, 401)
        code, _ = await self.call(self.api.preview, query="id=" + self.image_id, username=None)
        self.assertEqual(code, 401)

    async def test_rule_inspector_does_not_send_messages(self):
        _, result = await self.action("inspect")
        self.assertEqual(result["results"][0]["verdict"], "允许发送")
        await self.action("ban")
        _, result = await self.action("inspect")
        self.assertIn("指纹命中", result["results"][0]["verdict"])
        await self.action("unban")
        self.assertEqual(self.store.entries(), [])

    async def test_delete_requires_explicit_confirmation(self):
        code, _ = await self.action("trash")
        self.assertEqual(code, 400)
        self.assertTrue(self.image.exists())
        code, _ = await self.action("purge", confirm=True)
        self.assertEqual(code, 400)

    async def test_keep_trash_restore_and_purge_lifecycle(self):
        await self.action("keep")
        _, result = await self.action("trash", confirm=True)
        self.assertFalse(result["results"][0]["success"])
        self.assertTrue(self.image.exists())
        await self.action("unkeep")
        _, result = await self.action("trash", confirm=True)
        self.assertTrue(result["results"][0]["success"])
        self.assertFalse(self.image.exists())
        self.assertEqual(len(self.store.entries()), 1)
        await self.action("restore")
        self.assertTrue(self.image.exists())
        self.assertEqual(len(self.store.entries()), 1)
        await self.action("trash", confirm=True)
        _, result = await self.action("purge", confirm=True, confirmation="彻底删除")
        self.assertTrue(result["results"][0]["success"])
        self.assertEqual(self.library.list_images(mode="trash")["total"], 0)

    async def test_preview_and_details_are_catalogue_id_bound(self):
        code, data = await self.call(self.api.preview, query="id=" + self.image_id)
        self.assertEqual(code, 200)
        self.assertTrue(data["url"].startswith("data:image/jpeg;base64,"))
        code, _ = await self.call(self.api.preview, query="id=../core.db")
        self.assertEqual(code, 400)

    async def test_async_scan_and_registered_routes(self):
        routes = []
        self.api.register(SimpleNamespace(register_web_api=lambda *args: routes.append(args)))
        self.assertEqual(len(routes), 8)
        self.assertTrue(all(row[0].startswith("/astrbot_plugin_image_guard/") for row in routes))
        await self.call(self.api.scan, {})
        await self.api.scan_task
        self.assertFalse(self.library.status["running"])
        _, result = await self.call(self.api.images)
        self.assertEqual(result["total"], 1)

    async def test_two_remote_references_can_share_one_archive(self):
        async def read_synthetic(_reference):
            return self.image.read_bytes()

        self.reader.read = read_synthetic
        ids = [self.library.placeholder(f"https://example.com/{n}.png", 100) for n in range(2)]
        for identifier in ids:
            _, raw = await self.api.acquire(identifier)
            self.assertEqual(raw, self.image.read_bytes())
        self.assertEqual(len(list(self.library.library.iterdir())), 1)

    async def test_remote_archive_capacity_error_is_explained(self):
        async def read_synthetic(_reference):
            return self.image.read_bytes()

        self.reader.read = read_synthetic
        self.library.config["history_storage_mb"] = 0
        identifier = self.library.placeholder("https://example.com/synthetic.png", 100)
        with self.assertRaisesRegex(ValueError, "容量"):
            await self.api.acquire(identifier)

    async def test_group_filter_and_local_alias_require_login(self):
        key = "qq:GroupMessage:123"
        self.library.record_group(self.image_id, key)
        code, _ = await self.call(self.api.group, {"key": key, "name": "Roommates"}, username=None)
        self.assertEqual(code, 401)
        code, _ = await self.call(self.api.group, {"key": key, "name": "Roommates"})
        self.assertEqual(code, 200)
        _, data = await self.call(self.api.images, query="group=qq:GroupMessage:123")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["groups"][0]["name"], "Roommates")
        _, data = await self.call(self.api.images, query="group=unknown")
        self.assertEqual(data["total"], 0)

    async def test_inherited_rule_removal_requires_explicit_scope_confirmation(self):
        await self.action("ban")
        output = io.BytesIO()
        Image.new("RGB", (20, 30), "blue").save(output, "JPEG", quality=60)
        variant_id = self.library.ingest(output.getvalue(), "variant.jpg", archive=True)
        _, listing = await self.call(self.api.images, query="mode=blocked")
        self.assertIn(variant_id, {row["id"] for row in listing["items"]})
        _, detail = await self.call(self.api.detail, query="id=" + variant_id)
        source = detail["rules"][0]["sha256"]
        code, _ = await self.call(
            self.api.action, {"action": "unban_rule", "ids": [variant_id], "rule_sha": source}
        )
        self.assertEqual(code, 400)
        _, data = await self.call(
            self.api.action,
            {"action": "unban_rule", "ids": [variant_id], "rule_sha": source, "confirm": True},
        )
        self.assertTrue(data["results"][0]["success"])
        self.assertEqual(self.store.entries(), [])

    async def test_invalid_similarity_anchor_is_rejected(self):
        code, _ = await self.call(self.api.images, query="similar_to=../../core.db")
        self.assertEqual(code, 400)

    async def test_rule_inspector_reports_selected_group_isolation_without_sending(self):
        self.library.record_received(self.image_id, "qq:GroupMessage:A")
        _, first = await self.action("inspect", destination="qq:GroupMessage:A")
        self.assertEqual(first["results"][0]["scope_verdict"], "允许在所选群聊发送")
        _, second = await self.action("inspect", destination="qq:GroupMessage:B")
        self.assertIn("隔离已拦截", second["results"][0]["scope_verdict"])

    async def test_meme_collection_toggle_requires_login_and_boolean(self):
        code, _ = await self.call(self.api.collection, {"collect_memes": True}, username=None)
        self.assertEqual(code, 401)
        code, _ = await self.call(self.api.collection, {"collect_memes": "false"})
        self.assertEqual(code, 400)
        code, result = await self.call(self.api.collection, {"collect_memes": True})
        self.assertEqual(code, 200)
        self.assertTrue(result["collect_memes"])
        self.assertTrue(self.library.config["collect_memes"])
        self.assertTrue(self.api.plugin.config["collect_memes"])

    async def test_manual_meme_correction_does_not_change_bans_or_sources(self):
        await self.action("ban")
        self.library.record_received(self.image_id, "qq:GroupMessage:A")
        _, result = await self.action("mark_meme")
        self.assertTrue(result["results"][0]["success"])
        _, listing = await self.call(self.api.images)
        self.assertEqual(listing["total"], 0)
        _, listing = await self.call(self.api.images, query="mode=memes")
        self.assertEqual(listing["total"], 1)
        self.assertEqual(len(self.store.entries()), 1)
        self.assertEqual(self.library.confirmed_scopes(self.image_id), ["qq:GroupMessage:A"])
        await self.action("mark_photo")
        self.assertFalse(self.library.get(self.image_id)["is_meme"])

    async def test_ignored_remote_meme_preview_does_not_archive_but_explicit_keep_does(self):
        async def synthetic(_reference):
            return self.image.read_bytes()

        self.reader.read = synthetic
        identifier = self.library.placeholder("https://gxh.vip.qq.com/parcel/test.png", 100)
        code, result = await self.call(self.api.preview, query="id=" + identifier)
        self.assertEqual(code, 200)
        self.assertTrue(result["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(list(self.library.library.iterdir()), [])
        _, result = await self.call(self.api.action, {"action": "keep", "ids": [identifier]})
        self.assertTrue(result["results"][0]["success"])
        self.assertEqual(len(list(self.library.library.iterdir())), 1)
        self.assertEqual(self.library.get(identifier)["kept"], 1)

    async def test_meme_toggle_save_failure_rolls_back(self):
        class BrokenConfig(dict):
            async def save_config_async(self):
                raise OSError("synthetic save failure")

        self.api.plugin.config = BrokenConfig(collect_memes=False)
        code, _ = await self.call(self.api.collection, {"collect_memes": True})
        self.assertEqual(code, 500)
        self.assertFalse(self.api.plugin.config["collect_memes"])
        self.assertFalse(self.library.config["collect_memes"])
