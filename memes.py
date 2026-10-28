"""Local, metadata-based meme hints; no model calls and no stealer writes."""

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit


def field(value, name, default=None):
    if name == "file" and not isinstance(value, dict):
        kind = getattr(value, "type", "")
        if str(getattr(kind, "value", kind)).lower() == "file":
            return getattr(value, "file_", default)
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def metadata_hint(value, reference=""):
    if value is None:
        value = {}
    for name in ("sub_type", "subType"):
        if str(field(value, name, "")) == "1":
            return "平台表情标记"
    kind = str(field(value, "type", "")).lower()
    if kind in {"mface", "marketface", "sticker", "emoji", "meme"}:
        return "平台贴纸类型"
    summary = str(field(value, "summary", "") or "").lower()
    if "表情" in summary or "emoji" in summary or "sticker" in summary:
        return "平台表情摘要"
    if field(value, "emoji_id") or field(value, "emoji_package_id"):
        return "平台商城表情编号"
    urls = [reference] + [field(value, key, "") for key in ("url", "file", "path")]
    for url in urls:
        if not isinstance(url, str):
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if (
            host == "gxh.vip.qq.com"
            or host.endswith(".gxh.vip.qq.com")
            or (
                (host == "vip.qq.com" or host.endswith(".vip.qq.com"))
                and parsed.path.lower().startswith("/club/item/parcel")
            )
        ):
            return "QQ 表情商城 CDN"
    nested = field(value, "data")
    if isinstance(nested, dict) and nested is not value:
        return metadata_hint(nested, reference)
    return ""


def event_meme_hint(component, reference, event=None, *, index=0, total=1):
    reason = metadata_hint(component, reference)
    if reason:
        return reason
    raw = field(field(event, "message_obj"), "raw_message")
    segments = field(raw, "message", [])
    if not isinstance(segments, list):
        return ""
    images = [
        s
        for s in segments
        if str(field(s, "type", "")).lower() in {"image", "mface", "marketface", "sticker"}
    ]
    for segment in images:
        data = field(segment, "data", segment)
        if reference in [field(data, key) for key in ("file", "url", "path")]:
            return metadata_hint(segment, reference)
    # Some adapters replace the original URL with a downloaded local path.
    # Positional fallback is safe only when the image counts agree.
    if len(images) == total and 0 <= index < total:
        return metadata_hint(images[index], reference)
    return ""


class KnownMemes:
    def __init__(self, database: Path):
        self.database = database
        self.hashes = set()
        self.references = set()
        self.refreshed = None

    def refresh(self, force=False):
        now = time.monotonic()
        if not force and self.refreshed is not None and now - self.refreshed < 30:
            return
        hashes, references = set(), set()
        if self.database.is_file():
            try:
                with closing(
                    sqlite3.connect(
                        self.database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1
                    )
                ) as conn:
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(emoji)")}
                    if "hash" in columns:
                        selected = [
                            name
                            for name in ("hash", "path", "source_url", "category")
                            if name in columns
                        ]
                        conn.row_factory = sqlite3.Row
                        for row in conn.execute("SELECT " + ",".join(selected) + " FROM emoji"):
                            values = dict(row)
                            if str(values.get("category", "")).lower() in {
                                "raw",
                                "not_emoji",
                                "filtered",
                            }:
                                continue
                            digest = str(values.get("hash", "")).lower()
                            if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                                hashes.add(digest)
                            references.update(
                                str(values[k]) for k in ("path", "source_url") if values.get(k)
                            )
            except sqlite3.Error:
                # Missing/locked/incompatible optional plugin is not a photo ban.
                self.refreshed = now
                return
        self.hashes, self.references, self.refreshed = hashes, references, now

    def reason(self, digest="", reference=""):
        self.refresh()
        if digest and digest in self.hashes or reference and reference in self.references:
            return "本机表情包小偷已入库"
        return metadata_hint({}, reference)
