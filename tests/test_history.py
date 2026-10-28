import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_image_guard.history import MARKER, HistoryLibrary, image_refs, scrub


def picture(color="blue"):
    output = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(output, "PNG")
    return output.getvalue()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.temporary = self.root / "temp"
        self.temporary.mkdir()
        self.core = self.root / "core.db"
        self.image = self.temporary / "example.png"
        self.image.write_bytes(picture())
        self.other = self.temporary / "safe.png"
        self.other.write_bytes(picture("green"))
        saved_at = datetime(2026, 10, 27, 12, tzinfo=timezone.utc).timestamp()
        os.utime(self.image, (saved_at, saved_at))
        os.utime(self.other, (saved_at, saved_at))
        with closing(sqlite3.connect(self.core)) as conn, conn:
            conn.executescript("""
                CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY,content TEXT,
                  platform_id TEXT,user_id TEXT,created_at TEXT);
                CREATE TABLE platform_message_history(id INTEGER PRIMARY KEY,content TEXT,
                  platform_id TEXT,user_id TEXT,created_at TEXT);
            """)
            self.original = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "keep surrounding text"},
                        {"type": "image_url", "image_url": {"url": self.image.as_uri()}},
                        {"type": "image_url", "image_url": {"url": self.other.as_uri()}},
                    ],
                },
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "send_message_to_user",
                                "arguments": json.dumps(
                                    {"messages": [{"type": "image", "path": str(self.image)}]}
                                ),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "done"},
            ]
            conn.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?)",
                (
                    "conversation-1",
                    json.dumps(self.original),
                    "qq",
                    "session-1",
                    "2026-10-27T10:00:00",
                ),
            )
            platform = {
                "type": "user",
                "message": [
                    {"type": "Image", "file": str(self.image)},
                    {"type": "Plain", "text": "keep platform text"},
                ],
            }
            conn.execute(
                "INSERT INTO platform_message_history VALUES (?,?,?,?,?)",
                (1, json.dumps(platform), "qq", "session-1", "2026-10-27T10:00:00"),
            )
        self.config = {"max_image_mb": 16, "history_storage_mb": 8}
        self.library = HistoryLibrary(self.root / "plugin", self.core, self.temporary, self.config)
        self.library.scan()
        self.identifier = self.library.index_file(self.image)

    def tearDown(self):
        self.library.close()
        self.temp.cleanup()

    def conversation(self):
        with closing(sqlite3.connect(self.core)) as conn:
            return json.loads(conn.execute("SELECT content FROM conversations").fetchone()[0])

    def test_scan_deduplicates_files_uris_and_tool_arguments(self):
        result = self.library.list_images()
        self.assertEqual(result["total"], 2)
        row = next(row for row in result["items"] if row["id"] == self.identifier)
        self.assertEqual(row["context_count"], 3)
        self.assertEqual(row["day"], "2026-10-27")
        self.assertEqual(len(self.library.list_images(mode="context")["items"]), 2)

    def test_remove_only_selected_image_and_preserve_tool_schema(self):
        result = self.library.detach(self.identifier)
        self.assertEqual(result["changed_records"], 2)
        value = self.conversation()
        self.assertEqual(value[0]["content"][0]["text"], "keep surrounding text")
        self.assertEqual(value[0]["content"][1]["text"], MARKER)
        self.assertEqual(value[0]["content"][2], self.original[0]["content"][2])
        args = json.loads(value[1]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["messages"][0], {"type": "text", "text": MARKER})
        self.assertEqual(value[2], self.original[2])
        self.assertTrue(self.image.exists())

    def test_restore_exact_previous_history(self):
        self.library.detach(self.identifier)
        result = self.library.restore(self.identifier)
        self.assertEqual(result["conflicts"], 0)
        self.assertEqual(self.conversation(), self.original)

    def test_restore_does_not_overwrite_new_chat_messages(self):
        self.library.detach(self.identifier)
        value = self.conversation() + [{"role": "user", "content": "new message"}]
        with closing(sqlite3.connect(self.core)) as conn, conn:
            conn.execute("UPDATE conversations SET content=?", (json.dumps(value),))
        result = self.library.restore(self.identifier)
        self.assertGreater(result["conflicts"], 0)
        self.assertEqual(self.conversation(), value)

    def test_keep_protects_from_temp_cleanup_and_file_delete(self):
        self.library.set_kept(self.identifier, True)
        self.image.unlink()
        _, raw = self.library.local_file(self.identifier)
        self.assertEqual(raw, picture())
        with self.assertRaisesRegex(ValueError, "保留"):
            self.library.remove_files(self.identifier)
        self.assertEqual(self.library.list_images(mode="kept")["total"], 1)

    def test_trash_restore_then_permanent_delete(self):
        self.library.detach(self.identifier)
        result = self.library.remove_files(self.identifier)
        self.assertEqual(result["moved_files"], 1)
        self.assertFalse(self.image.exists())
        self.assertTrue(self.other.exists())
        self.assertEqual(self.library.list_images(mode="trash")["total"], 1)
        self.library.restore(self.identifier)
        self.assertTrue(self.image.exists())
        self.library.remove_files(self.identifier)
        result = self.library.purge(self.identifier)
        self.assertEqual(result["deleted_files"], 1)
        self.assertEqual(self.library.list_images(mode="trash")["total"], 0)
        self.assertFalse(list(self.library.trash.iterdir()))
        with self.assertRaisesRegex(ValueError, "彻底删除"):
            self.library.restore(self.identifier)
        self.assertTrue(self.other.exists())

    def test_external_image_can_be_indexed_but_not_moved(self):
        outside = self.root / "outside.png"
        outside.write_bytes(picture("red"))
        identifier = self.library.index_file(outside)
        result = self.library.remove_files(identifier)
        self.assertEqual(result["moved_files"], 0)
        self.assertEqual(result["external_files_untouched"], 1)
        self.assertTrue(outside.exists())

    def test_modified_file_is_not_previewed_or_deleted_as_original(self):
        self.image.write_bytes(picture("red"))
        with self.assertRaises(FileNotFoundError):
            self.library.local_file(self.identifier)
        self.library.remove_files(self.identifier)
        self.assertTrue(self.image.exists())

    def test_restore_does_not_replace_existing_file(self):
        self.library.remove_files(self.identifier)
        self.image.write_bytes(picture("red"))
        result = self.library.restore(self.identifier)
        self.assertGreater(result["conflicts"], 0)
        self.assertEqual(self.image.read_bytes(), picture("red"))

    def test_preview_is_resized_jpeg_not_arbitrary_file(self):
        preview = self.library.preview(self.identifier)
        with Image.open(io.BytesIO(preview)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertLessEqual(image.width, 320)
        with self.assertRaises(ValueError):
            self.library.preview("../../core.db")

    def test_scan_after_detach_does_not_restore_context_links(self):
        self.library.detach(self.identifier)
        self.library.scan()
        result = self.library.list_images(mode="context")
        self.assertNotIn(self.identifier, {r["id"] for r in result["items"]})
        self.assertIn(str(self.image), self.library.detached_references())

    def test_capacity_limit_never_deletes_existing_images(self):
        self.library.config["history_storage_mb"] = 0
        with self.assertRaisesRegex(ValueError, "容量"):
            self.library.set_kept(self.identifier, True)
        self.assertTrue(self.image.exists())
        self.assertEqual(self.library.get(self.identifier)["kept"], 0)

    def test_date_search_and_pagination(self):
        data = self.library.list_images(size=1)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(self.library.list_images(search="example")["total"], 1)
        self.assertEqual(self.library.list_images(day="2026-10-27")["total"], 2)
        self.assertEqual(self.library.list_images(day="2020-01-01")["total"], 0)

    def test_plain_path_and_json_argument_extraction(self):
        self.assertIn(str(self.image), set(image_refs("image at " + str(self.image))))
        self.assertIn(
            str(self.image), set(image_refs(json.dumps({"type": "image", "path": str(self.image)})))
        )

    def test_failed_core_schema_does_not_touch_unrelated_tables(self):
        self.library.detach(self.identifier)
        with closing(sqlite3.connect(self.core)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM conversations").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM platform_message_history").fetchone()[0], 1
            )

    def test_scrub_exact_reference_and_preserve_non_image_data(self):
        ref = self.image.as_uri()
        self.assertEqual(scrub({"text": "hi", "number": 7}, {ref}), {"text": "hi", "number": 7})
        value = {"type": "image_url", "image_url": {"url": ref}}
        self.assertEqual(scrub(value, {ref}), {"type": "text", "text": MARKER})


if __name__ == "__main__":
    unittest.main()
