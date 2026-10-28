"""Meme collection controls must not weaken deny rules or chat provenance."""

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_image_guard.history import HistoryLibrary
from astrbot_plugin_image_guard.memes import KnownMemes, event_meme_hint, metadata_hint
from astrbot_plugin_image_guard.policy import fingerprint


def picture(fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", (30, 20), "purple").save(out, fmt)
    return out.getvalue()


class MemeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {"collect_memes": False}
        self.library = HistoryLibrary(self.root / "plugin", None, self.root / "temp", self.config)
        self.raw = picture()

    def tearDown(self):
        self.library.close()
        self.tmp.cleanup()

    def test_metadata_detects_explicit_stickers_and_subtypes(self):
        for data in [
            {"sub_type": 1},
            {"subType": "1"},
            {"summary": "[动画表情]"},
            {"data": {"emoji_id": "123"}},
            {"type": "marketface"},
        ]:
            self.assertTrue(metadata_hint(data), data)

    def test_url_host_is_checked_not_untrusted_query_substring(self):
        self.assertTrue(metadata_hint({}, "https://gxh.vip.qq.com/a.png"))
        self.assertTrue(metadata_hint({}, "https://i.vip.qq.com/club/item/parcel/a.png"))
        self.assertFalse(metadata_hint({}, "https://example.com/?x=gxh.vip.qq.com"))
        self.assertFalse(metadata_hint({}, "https://gxh.vip.qq.com.evil.test/a.png"))

    def test_gif_small_dimensions_and_webp_do_not_automatically_mean_meme(self):
        self.assertFalse(metadata_hint({}, "https://example.com/small.gif"))
        self.assertFalse(metadata_hint({}, "small.webp"))
        image_id = self.library.ingest(picture("GIF"), "photo.gif")
        self.assertFalse(self.library.get(image_id)["is_meme"])

    def test_raw_platform_metadata_matches_correct_image(self):
        raw = {
            "message": [
                {"type": "image", "data": {"url": "normal", "sub_type": 0}},
                {"type": "image", "data": {"url": "sticker", "sub_type": 1}},
            ]
        }
        event = SimpleNamespace(message_obj=SimpleNamespace(raw_message=raw))
        self.assertFalse(event_meme_hint({}, "normal", event, index=0, total=2))
        self.assertTrue(event_meme_hint({}, "sticker", event, index=1, total=2))
        self.assertFalse(event_meme_hint({}, "unknown", event, index=0, total=3))

    def test_disabled_skips_copy_but_preserves_fingerprint_and_receipt(self):
        image_id = self.library.ingest(
            self.raw,
            "meme.png",
            archive=True,
            meme_hint="平台表情标记",
            received_scope="qq:GroupMessage:A",
        )
        row = self.library.get(image_id)
        self.assertTrue(row["is_meme"])
        self.assertEqual(row["sha256"], fingerprint(self.raw).sha256)
        self.assertEqual(list(self.library.library.iterdir()), [])
        self.assertIsNone(self.library.scope_verdict(row, "qq:GroupMessage:A"))
        self.assertIsNotNone(self.library.scope_verdict(row, "qq:GroupMessage:B"))
        self.assertEqual(self.library.list_images()["total"], 0)
        self.assertEqual(self.library.list_images(mode="memes")["total"], 1)

    def test_enabled_collects_copy_and_includes_in_normal_album(self):
        self.config["collect_memes"] = True
        image_id = self.library.ingest(self.raw, "meme.png", archive=True, meme_hint="平台表情标记")
        self.assertEqual(len(list(self.library.library.iterdir())), 1)
        self.assertEqual(self.library.list_images()["total"], 1)
        self.config["collect_memes"] = False
        self.assertEqual(self.library.list_images()["total"], 0)
        self.assertEqual(self.library.local_file(image_id)[1], self.raw)

    def test_manual_correction_survives_refresh_and_is_reversible(self):
        image_id = self.library.ingest(self.raw, "meme.png", meme_hint="平台表情标记")
        self.library.set_meme_override(image_id, 0)
        self.library.ingest(self.raw, "same.png", meme_hint="平台表情标记")
        self.library.refresh_memes()
        self.assertFalse(self.library.get(image_id)["is_meme"])
        self.assertEqual(self.library.list_images()["total"], 1)
        self.library.set_meme_override(image_id, -1)
        self.assertTrue(self.library.get(image_id)["is_meme"])
        self.library.set_meme_override(image_id, 1)
        self.assertEqual(self.library.get(image_id)["meme_override"], 1)

    def test_banned_kept_and_trash_management_are_not_hidden(self):
        self.config["collect_memes"] = True
        image_id = self.library.ingest(self.raw, "meme.png", archive=True, meme_hint="平台表情标记")
        self.library.set_kept(image_id, True)
        self.config["collect_memes"] = False
        self.assertEqual(self.library.list_images(mode="blocked", blocked={image_id})["total"], 1)
        self.assertEqual(self.library.list_images(mode="kept")["total"], 1)
        self.library.set_kept(image_id, False)
        self.library.remove_files(image_id)
        self.assertEqual(self.library.list_images(mode="trash")["total"], 1)

    def test_explicit_keep_can_archive_even_when_automatic_meme_collection_is_off(self):
        path = self.root / "source.png"
        path.write_bytes(self.raw)
        image_id = self.library.ingest(self.raw, str(path), meme_hint="平台表情标记")
        self.library.set_kept(image_id, True)
        self.assertEqual(self.library.local_file(image_id)[1], self.raw)
        self.assertEqual(len(list(self.library.library.iterdir())), 1)

    def test_approved_stealer_entries_used_read_only_pending_and_blacklist_ignored(self):
        database = self.root / "stealer.db"
        digest = fingerprint(self.raw).sha256
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute("CREATE TABLE emoji(hash TEXT,path TEXT,source_url TEXT,category TEXT)")
            conn.execute(
                "INSERT INTO emoji VALUES (?,?,?,?)",
                (digest, "meme.png", "https://example.com/meme", "happy"),
            )
            conn.execute("CREATE TABLE blacklist(hash TEXT)")
            conn.execute("INSERT INTO blacklist VALUES (?)", ("a" * 64,))
            conn.execute("CREATE TABLE emoji_pending(hash TEXT)")
            conn.execute("INSERT INTO emoji_pending VALUES (?)", ("b" * 64,))
        index = KnownMemes(database)
        self.assertTrue(index.reason(digest))
        self.assertFalse(index.reason("a" * 64))
        self.assertFalse(index.reason("b" * 64))
        self.library.memes = index
        image_id = self.library.ingest(self.raw, "source.png", archive=True)
        self.assertTrue(self.library.get(image_id)["is_meme"])
        self.assertEqual(list(self.library.library.iterdir()), [])
        with closing(sqlite3.connect(database)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM emoji").fetchone()[0], 1)

    def test_historical_base64_stickers_respect_collection_switch(self):
        import base64

        core = self.root / "core.db"
        reference = "base64://" + base64.b64encode(self.raw).decode()
        with closing(sqlite3.connect(core)) as conn, conn:
            conn.execute(
                "CREATE TABLE platform_message_history(id INTEGER,content TEXT,platform_id TEXT,user_id TEXT)"
            )
            conn.execute(
                "INSERT INTO platform_message_history VALUES (?,?,?,?)",
                (
                    1,
                    json.dumps(
                        {
                            "type": "user",
                            "message": [{"type": "image", "file": reference, "sub_type": 1}],
                        }
                    ),
                    "qq",
                    "qq:GroupMessage:A",
                ),
            )
        self.library.core_database = core
        self.library.scan()
        self.assertEqual(self.library.list_images()["total"], 0)
        self.assertEqual(self.library.list_images(mode="memes")["total"], 1)
        self.assertEqual(list(self.library.library.iterdir()), [])

    def test_missing_optional_plugin_is_ok_and_blacklisted_photo_stays_photo(self):
        self.assertFalse(self.library.memes.reason("a" * 64))
        image_id = self.library.ingest(self.raw, "photo.png")
        self.assertFalse(self.library.get(image_id)["is_meme"])

    def test_filename_does_not_trigger_file_io_property(self):
        class File:
            type = "File"
            file_ = "photo.png"

            @property
            def file(self):
                raise AssertionError("must not evaluate I/O property")

        self.assertFalse(metadata_hint(File(), "photo.png"))
