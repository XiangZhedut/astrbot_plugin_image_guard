import base64
import io
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
from astrbot_plugin_image_guard.guard import ImageGuard, image_references
from astrbot_plugin_image_guard.intercept import SendInterceptor
from astrbot_plugin_image_guard.media import (
    InspectionError,
    MediaReader,
    local_path,
    validate_remote,
)
from astrbot_plugin_image_guard.policy import DenyStore, fingerprint, read_stealer_blacklist


def sample_image(color="blue", fmt="PNG", size=(80, 60)):
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 5, 35, 45), fill="yellow")
    draw.line((0, 50, 70, 10), fill="red", width=4)
    output = io.BytesIO()
    image.save(output, format=fmt)
    return output.getvalue()


def image_chain(source):
    return SimpleNamespace(chain=[SimpleNamespace(type="Image", file=source, url="", path="")])


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = DenyStore(self.root / "guard.db")
        self.reader = MediaReader()
        self.config = {"enabled": True, "sync_stealer_blacklist": True}
        self.stealer = self.root / "stealer.db"
        self.guard = ImageGuard(
            self.store, self.reader, self.config, self.stealer, logging.getLogger("test")
        )
        self.data = sample_image()
        self.source = self.root / "original.png"
        self.source.write_bytes(self.data)

    async def asyncTearDown(self):
        await self.reader.close()
        self.store.close()
        self.temp.cleanup()

    async def test_block_copy_at_different_path_and_file_uri(self):
        self.store.add(fingerprint(self.data))
        copy = self.root / "copy.png"
        copy.write_bytes(self.data)
        self.assertIsNotNone(await self.guard.check(image_chain(copy.as_uri())))

    async def test_allow_other_image_and_plain_voice_messages(self):
        self.store.add(fingerprint(self.data))
        safe = "base64://" + base64.b64encode(sample_image("green")).decode()
        self.assertIsNone(await self.guard.check(image_chain(safe)))
        self.assertIsNone(
            await self.guard.check(
                SimpleNamespace(chain=[SimpleNamespace(type="Record", file="voice.wav")])
            )
        )
        self.assertIsNone(
            await self.guard.check(
                SimpleNamespace(chain=[SimpleNamespace(type="Plain", text="hi")])
            )
        )

    async def test_base64_exact_and_lossless_reencoding(self):
        self.store.add(fingerprint(self.data))
        encoded = "base64://" + base64.b64encode(self.data).decode()
        self.assertIsNotNone(await self.guard.check(image_chain(encoded)))
        alternate = io.BytesIO()
        with Image.open(io.BytesIO(self.data)) as image:
            image.save(alternate, format="BMP")
        reference = "base64://" + base64.b64encode(alternate.getvalue()).decode()
        self.assertEqual(await self.guard.check(image_chain(reference)), "像素指纹命中")

    async def test_stealer_hash_blocks_without_local_entry(self):
        digest = fingerprint(self.data).sha256
        with closing(sqlite3.connect(self.stealer)) as conn, conn:
            conn.execute("CREATE TABLE blacklist(hash TEXT PRIMARY KEY, created_at INTEGER)")
            conn.execute("INSERT INTO blacklist VALUES (?, 1)", (digest,))
        self.assertEqual(await self.guard.check(image_chain(str(self.source))), "表情包黑名单命中")
        with closing(sqlite3.connect(self.stealer)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM blacklist").fetchone()[0], 1)

    async def test_nested_forward_and_image_file_attachment(self):
        self.store.add(fingerprint(self.data))
        file = SimpleNamespace(type="File", name="original.png", file_=str(self.source), url="")
        nested = SimpleNamespace(
            chain=[
                SimpleNamespace(type="Nodes", nodes=[SimpleNamespace(type="Node", content=[file])])
            ]
        )
        self.assertIsNotNone(await self.guard.check(nested))

    async def test_tool_arguments_inspected_before_delivery(self):
        self.store.add(fingerprint(self.data))
        messages = [{"type": "image", "path": str(self.source)}]
        self.assertIsNotNone(await self.guard.check(messages, tool_only=True))

    async def test_missing_image_fails_closed_but_configurable(self):
        chain = image_chain(str(self.root / "missing.jpg"))
        self.assertIsNotNone(await self.guard.check(chain))
        self.config["fail_closed"] = False
        self.assertIsNone(await self.guard.check(chain))

    async def test_remote_bytes_pinned_after_successful_inspection(self):
        async def fake_read(url):
            return sample_image("green")

        self.reader.read = fake_read
        chain = image_chain("https://example.com/image.png")
        self.assertIsNone(await self.guard.check(chain))
        self.assertTrue(chain.chain[0].file.startswith("base64://"))
        self.assertEqual(chain.chain[0].url, "")

    async def test_disable_guard(self):
        self.store.add(fingerprint(self.data))
        self.config["enabled"] = False
        self.assertIsNone(await self.guard.check(image_chain(str(self.source))))

    async def test_similarity_opt_in(self):
        original = fingerprint(self.data)
        self.store.add(original)
        modified = fingerprint(sample_image(fmt="JPEG"))
        self.assertIsNone(self.store.match(modified))
        self.assertIsNotNone(self.store.match(modified, 4))

    async def test_persistent_store_and_unban(self):
        identifier = self.store.add(fingerprint(self.data))
        other = DenyStore(self.root / "guard.db")
        try:
            self.assertEqual(len(other.entries()), 1)
            self.assertTrue(other.remove(identifier))
            self.assertEqual(self.store.entries(), [])
        finally:
            other.close()

    async def test_event_platform_tool_stream_and_unload(self):
        self.store.add(fingerprint(self.data))
        delivered = []

        class Event:
            async def send(self, message):
                delivered.append(message)

            async def send_streaming(self, generator, use_fallback=False):
                async for chunk in generator:
                    await self.send(chunk)

        class ConcreteEvent(Event):
            async def send(self, message):
                await super().send(message)

        class Platform:
            async def send_by_session(self, session, message_chain):
                delivered.append(message_chain)

        class Tool:
            async def call(self, context, **kwargs):
                delivered.append(kwargs)
                return "sent"

        original = ConcreteEvent.send
        notice = SimpleNamespace(chain=[SimpleNamespace(type="Plain", text="blocked")])
        wrapper = SendInterceptor(self.guard, lambda: notice)
        wrapper.install(Event, Platform, Tool)
        wrapper.install(Event, Platform, Tool)
        try:
            await ConcreteEvent().send(image_chain(str(self.source)))
            self.assertEqual(delivered, [notice])
            await Platform().send_by_session("test", image_chain(str(self.source)))
            self.assertEqual(delivered, [notice, notice])
            result = await Tool().call(None, messages=[{"type": "image", "path": str(self.source)}])
            self.assertIn("blocked", result)
            self.assertEqual(len(delivered), 2)

            async def stream():
                yield image_chain(str(self.source))

            await ConcreteEvent().send_streaming(stream())
            self.assertEqual(len(delivered), 3)
            self.assertIs(delivered[-1], notice)
        finally:
            wrapper.uninstall()
        self.assertIs(ConcreteEvent.send, original)
        await ConcreteEvent().send(image_chain(str(self.source)))
        self.assertEqual(len(delivered), 4)

    async def test_silent_block_and_cooperative_unload(self):
        self.store.add(fingerprint(self.data))
        delivered = []

        class Event:
            async def send(self, message):
                delivered.append(message)

        class Platform:
            pass

        class Tool:
            pass

        interceptor = SendInterceptor(self.guard, lambda: None)
        interceptor.install(Event, Platform, Tool)
        await Event().send(image_chain(str(self.source)))
        self.assertEqual(delivered, [])
        ours = Event.send

        async def another_plugin(instance, message):
            await ours(instance, message)

        Event.send = another_plugin
        interceptor.uninstall()
        self.assertIs(Event.send, another_plugin)
        await Event().send(image_chain(str(self.source)))
        self.assertEqual(len(delivered), 1)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_network_restrictions(self):
        for url in (
            "http://127.0.0.1/a",
            "http://[::1]/a",
            "https://192.168.1.2/a",
            "http://169.254.169.254/a",
            "http://localhost/a",
            "https://user:secret@example.com/a",
            "file:///tmp/a",
        ):
            with self.assertRaises(InspectionError):
                validate_remote(url)
        validate_remote("https://example.com/image.png")

    async def test_encoded_file_uri_and_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a b%20.png"
            path.write_bytes(b"12345")
            self.assertEqual(local_path(path.as_uri()), path.resolve())
            reader = MediaReader(max_bytes=4)
            with self.assertRaises(InspectionError):
                await reader.read(str(path))
            with self.assertRaises(InspectionError):
                await reader.read("base64://MTIzNDU=")
            with self.assertRaises(InspectionError):
                await reader.read("base64://!!!")
            await reader.close()

    async def test_missing_optional_database(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(read_stealer_blacklist(Path(directory) / "absent.db"), set())

    async def test_forward_traversal_does_not_resolve_file_property(self):
        class File:
            type = "File"
            file_ = "image.jpg"
            name = "image.jpg"
            url = ""

            @property
            def file(self):
                raise AssertionError("file property must not be evaluated")

        self.assertEqual(list(image_references([File()]))[0][1], "image.jpg")


if __name__ == "__main__":
    unittest.main()
