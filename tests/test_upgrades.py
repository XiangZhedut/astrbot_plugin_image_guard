"""2.1 regression: chat provenance, visual candidates and encoding bypasses."""

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
from astrbot_plugin_image_guard.guard import ImageGuard, image_header
from astrbot_plugin_image_guard.history import HistoryLibrary, group_from_session
from astrbot_plugin_image_guard.media import MediaReader
from astrbot_plugin_image_guard.policy import DenyStore, fingerprint, visual_match


def drawing(background="#266ba4"):
    image = Image.new("RGB", (320, 240), background)
    draw = ImageDraw.Draw(image)
    draw.ellipse((30, 20, 170, 200), fill="#e1b536")
    draw.rectangle((190, 100, 290, 220), fill="#352341")
    draw.line((0, 230, 310, 10), fill="white", width=7)
    return image


def encoded(image, fmt="PNG", **options):
    output = io.BytesIO()
    image.save(output, fmt, **options)
    return output.getvalue()


class UpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library = HistoryLibrary(self.root / "plugin", None, self.root / "temp", {})
        self.store = DenyStore(self.root / "deny.db")
        self.reader = MediaReader()
        self.config = {}
        self.guard = ImageGuard(
            self.store,
            self.reader,
            self.config,
            self.root / "absent.db",
            logging.getLogger("upgrade"),
        )
        self.guard.history = self.library
        self.raw = encoded(drawing())
        self.image_id = self.library.ingest(self.raw, "original.png", archive=True)
        self.store.add(fingerprint(self.raw))

    async def asyncTearDown(self):
        self.library.close()
        self.store.close()
        await self.reader.close()
        self.tmp.cleanup()

    async def check(self, raw):
        return await self.guard.check(
            [{"type": "image", "file": "base64://" + base64.b64encode(raw).decode()}]
        )

    async def test_lossy_and_lossless_conversions_are_blocked_by_default(self):
        for fmt, options in [
            ("JPEG", {"quality": 35}),
            ("JPEG", {"quality": 85}),
            ("WEBP", {"quality": 40}),
            ("GIF", {}),
            ("BMP", {}),
            ("TIFF", {}),
            ("PNG", {}),
        ]:
            with self.subTest(format=fmt, options=options):
                raw = encoded(drawing(), fmt, **options)
                self.assertTrue(image_header(raw[:64]))
                self.assertIsNotNone(await self.check(raw))

    async def test_resize_plus_lossy_encoding_is_blocked(self):
        raw = encoded(drawing().resize((160, 120)), "JPEG", quality=50)
        self.assertIsNotNone(await self.check(raw))

    async def test_different_colours_and_layout_are_not_blocked(self):
        self.assertIsNone(await self.check(encoded(drawing("#a4266b"))))
        unrelated = Image.new("RGB", (320, 240), "green")
        self.assertIsNone(await self.check(encoded(unrelated)))
        # Bare dHash would treat these two featureless colours as identical.
        self.assertFalse(
            visual_match(
                vars(fingerprint(encoded(unrelated))),
                vars(fingerprint(encoded(Image.new("RGB", (320, 240), "blue")))),
            )
        )

    async def test_renamed_image_and_real_document_attachments(self):
        disguised = self.root / "report.bin"
        disguised.write_bytes(encoded(drawing(), "WEBP", quality=60))
        self.assertIsNotNone(
            await self.guard.check(
                [SimpleNamespace(type="File", file_=str(disguised), name="report.txt")]
            )
        )
        document = self.root / "ordinary.txt"
        document.write_text("just a text document")
        self.assertIsNone(
            await self.guard.check(
                [SimpleNamespace(type="File", file_=str(document), name="ordinary.txt")]
            )
        )

    async def test_additional_pillow_encoding_cannot_skip_attachment_inspection(self):
        raw = encoded(drawing(), "PPM")
        self.assertTrue(image_header(raw[:64]))
        disguised = self.root / "other.dat"
        disguised.write_bytes(raw)
        self.assertIsNotNone(
            await self.guard.check(
                [SimpleNamespace(type="File", file_=str(disguised), name="other.dat")]
            )
        )

    async def test_format_protection_can_be_disabled_without_disabling_exact_rules(self):
        self.config["protect_format_variants"] = False
        self.assertIsNone(await self.check(encoded(drawing(), "JPEG", quality=50)))
        self.assertIsNotNone(await self.check(self.raw))

    async def test_exif_orientation_is_normalized(self):
        rotated = drawing().transpose(Image.Transpose.ROTATE_90)
        exif = Image.Exif()
        exif[274] = 6
        self.assertIsNotNone(await self.check(encoded(rotated, "JPEG", quality=90, exif=exif)))

    async def test_transparency_and_white_background_conversion(self):
        transparent = drawing().convert("RGBA")
        transparent.putalpha(180)
        raw = encoded(transparent)
        self.store.add(fingerprint(raw))
        canvas = Image.new("RGB", transparent.size, "white")
        canvas.paste(transparent, mask=transparent.getchannel("A"))
        self.assertIsNotNone(await self.check(encoded(canvas, "JPEG", quality=70)))

    async def test_old_schema_and_rules_enriched_without_new_bans(self):
        old = self.root / "old.db"
        item = fingerprint(self.raw)
        with closing(sqlite3.connect(old)) as conn, conn:
            conn.execute(
                "CREATE TABLE blocked_images(sha256 TEXT PRIMARY KEY,pixels TEXT,dhash TEXT,reason TEXT,created_at INTEGER)"
            )
            conn.execute(
                "INSERT INTO blocked_images VALUES (?,?,?,?,?)",
                (item.sha256, item.pixels, item.dhash, "old rule", 123),
            )
        store = DenyStore(old)
        try:
            self.assertEqual(self.library.enrich_rules(store), 1)
            self.assertEqual(len(store.entries()), 1)
            self.assertEqual(store.entries()[0]["created_at"], 123)
            self.assertIsNotNone(
                store.match(fingerprint(encoded(drawing(), "JPEG")), variants=True)
            )
        finally:
            store.close()

    async def test_blocked_view_contains_inherited_variants_and_source(self):
        other_id = self.library.ingest(encoded(drawing(), "JPEG", quality=50), "variant.jpg")
        data = self.library.list_images(mode="blocked", rules=self.store.entries())
        variant = next(row for row in data["items"] if row["id"] == other_id)
        self.assertTrue(variant["blocked"])
        self.assertFalse(variant["direct_ban"])
        self.assertEqual(variant["blocked_by"][0]["sha256"], self.image_id)

    async def test_similar_album_does_not_change_bans_or_delete(self):
        self.library.ingest(encoded(drawing(), "JPEG", quality=50), "variant.jpg")
        self.library.ingest(encoded(Image.new("RGB", (320, 240), "red")), "unrelated.png")
        data = self.library.list_images(mode="similar")
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["similar_group_count"], 1)
        self.assertEqual(len(self.store.entries()), 1)
        self.assertTrue(self.library.local_file(self.image_id))

    async def test_group_filter_handles_multi_group_images_and_unknown(self):
        first, second = "qq:GroupMessage:100", "qq:GroupMessage:200"
        self.library.record_group(self.image_id, first, "roommates")
        self.library.record_group(self.image_id, second)
        unknown_id = self.library.ingest(encoded(drawing("red")), "unknown.png")
        self.assertEqual(self.library.list_images(group=first)["total"], 1)
        self.assertEqual(self.library.list_images(group=second)["total"], 1)
        self.assertEqual(self.library.list_images(group="unknown")["items"][0]["id"], unknown_id)
        self.library.rename_group(first, "new name")
        self.assertEqual(
            next(g for g in self.library.list_images()["groups"] if g["key"] == first)["name"],
            "new name",
        )
        with self.assertRaises(ValueError):
            self.library.rename_group("not-a-group", "bad")

    async def test_scan_merges_qq_official_members_into_one_group(self):
        self.assertEqual(
            group_from_session("qq", "qq:GroupMessage:" + "A" * 32 + "_" + "B" * 32),
            "qq:GroupMessage:" + "B" * 32,
        )
        core = self.root / "core.db"
        with closing(sqlite3.connect(core)) as conn, conn:
            conn.execute(
                "CREATE TABLE conversations(conversation_id TEXT,content TEXT,platform_id TEXT,user_id TEXT)"
            )
            for number, sender in enumerate(["A" * 32, "C" * 32]):
                conn.execute(
                    "INSERT INTO conversations VALUES (?,?,?,?)",
                    (
                        str(number),
                        json.dumps(
                            [
                                {
                                    "type": "image",
                                    "url": str(self.library.local_file(self.image_id)[0]),
                                }
                            ]
                        ),
                        "qq",
                        "qq:GroupMessage:" + sender + "_" + "B" * 32,
                    ),
                )
        self.library.core_database = core
        self.library.scan()
        data = self.library.list_images()
        self.assertEqual(len(data["groups"]), 1)
        self.assertEqual(self.library.list_images(group="qq:GroupMessage:" + "B" * 32)["total"], 1)

    async def test_external_blacklist_variants_remain_read_only(self):
        external = self.root / "external.db"
        with closing(sqlite3.connect(external)) as conn, conn:
            conn.execute("CREATE TABLE blacklist(hash TEXT)")
            conn.execute("INSERT INTO blacklist VALUES (?)", (self.image_id,))
        self.guard.stealer_db = external
        self.store.remove(self.image_id)
        self.assertIsNotNone(await self.check(encoded(drawing(), "JPEG", quality=70)))
        self.assertEqual(self.store.entries(), [])


if __name__ == "__main__":
    unittest.main()
