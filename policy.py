"""Local image fingerprints and a durable deny list. No model/API calls."""

from __future__ import annotations

import base64
import hashlib
import io
import sqlite3
import threading
import time
import warnings
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


@dataclass(frozen=True)
class Fingerprint:
    sha256: str
    pixels: str = ""
    dhash: str = ""
    visual: str = ""


def visual_match(left, right, *, loose=False):
    """Whole-image comparison only: geometry, edge hash and RGB thumbnail.

    Unlike dHash alone, the RGB check rejects same-layout pictures with very
    different colours. No face detection, embeddings or identity information.
    """
    a, b = left.get("visual", ""), right.get("visual", "")
    if not a or not b or not left.get("dhash") or not right.get("dhash"):
        return False
    try:
        ar, av = a.split(":", 1)
        br, bv = b.split(":", 1)
        if abs(int(ar) - int(br)) > max(int(ar), int(br)) * 0.025:
            return False
        if (int(left["dhash"], 16) ^ int(right["dhash"], 16)).bit_count() > (10 if loose else 8):
            return False
        av, bv = base64.b64decode(av, validate=True), base64.b64decode(bv, validate=True)
        if len(av) != 768 or len(bv) != 768:
            return False
        errors = [abs(x - y) for x, y in zip(av, bv)]
        return sum(errors) <= len(errors) * (18 if loose else 12)
    except (ValueError, TypeError):
        return False


def rule_match(item, rule, *, distance=-1, variants=True):
    if item.get("sha256") and item["sha256"] == rule["sha256"]:
        return "文件指纹命中"
    if item.get("pixels") and item["pixels"] == rule.get("pixels"):
        return "像素指纹命中"
    if variants and visual_match(item, rule):
        return "转码/缩放视觉指纹命中"
    if distance >= 0 and item.get("dhash") and rule.get("dhash"):
        if (int(item["dhash"], 16) ^ int(rule["dhash"], 16)).bit_count() <= min(4, distance):
            return "相似图指纹命中"
    return None


def fingerprint(data: bytes) -> Fingerprint:
    """Hash the original bytes plus oriented first-frame pixels and dHash.

    dHash only helps with visually similar re-encodings/resizes. It is not face
    recognition and must not be described as a reliable arbitrary-crop filter.
    """
    sha = hashlib.sha256(data).hexdigest()
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 40_000_000:
                raise ValueError("image pixel limit exceeded")
            source.seek(0)
            oriented = ImageOps.exif_transpose(source)
            # Composite transparent images consistently with normal chat previews.
            rgba = oriented.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            pixels = hashlib.sha256(
                f"{rgb.width}x{rgb.height}:".encode() + rgb.tobytes()
            ).hexdigest()
            small = rgb.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            values = small.tobytes()
            bits = 0
            for row in range(8):
                for col in range(8):
                    bits = (bits << 1) | (values[row * 9 + col] > values[row * 9 + col + 1])
            thumbnail = rgb.resize((16, 16), Image.Resampling.LANCZOS)
            visual = f"{round(rgb.width / rgb.height * 10000)}:" + base64.b64encode(
                thumbnail.tobytes()
            ).decode("ascii")
    return Fingerprint(sha, pixels, f"{bits:016x}", visual)


class DenyStore:
    def __init__(self, database: Path):
        database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(database, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS blocked_images ("
            "sha256 TEXT PRIMARY KEY, pixels TEXT NOT NULL, dhash TEXT NOT NULL, "
            "reason TEXT NOT NULL, created_at INTEGER NOT NULL)"
        )
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(blocked_images)")}
        if "visual" not in columns:
            self.conn.execute(
                "ALTER TABLE blocked_images ADD COLUMN visual TEXT NOT NULL DEFAULT ''"
            )
        self.conn.commit()

    def add(self, item: Fingerprint, reason: str = "管理员禁发") -> str:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO blocked_images(sha256,pixels,dhash,reason,created_at,visual) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sha256) DO UPDATE SET pixels=excluded.pixels, "
                "dhash=excluded.dhash, visual=excluded.visual, reason=excluded.reason",
                (item.sha256, item.pixels, item.dhash, reason[:200], int(time.time()), item.visual),
            )
        return item.sha256[:12]

    def entries(self) -> list[dict]:
        with self._lock:
            return [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM blocked_images ORDER BY created_at DESC, sha256"
                )
            ]

    def remove(self, identifier: str) -> bool:
        if len(identifier) < 8 or any(c not in "0123456789abcdef" for c in identifier.lower()):
            raise ValueError("请提供至少 8 位的禁发编号")
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT sha256 FROM blocked_images WHERE sha256 LIKE ?",
                (identifier.lower() + "%",),
            ).fetchall()
            if len(rows) != 1:
                raise ValueError("编号不存在或不唯一")
            self.conn.execute("DELETE FROM blocked_images WHERE sha256=?", (rows[0][0],))
        return True

    def match(self, item: Fingerprint, distance: int = -1, *, variants=False) -> str | None:
        for row in self.entries():
            reason = rule_match(vars(item), row, distance=distance, variants=variants)
            if reason:
                return reason
        return None

    def enrich(self, item: Fingerprint):
        """Upgrade an existing rule only, without creating a new ban."""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE blocked_images SET visual=?,dhash=? WHERE sha256=?",
                (item.visual, item.dhash, item.sha256),
            )

    def close(self):
        with self._lock:
            self.conn.close()


def read_stealer_blacklist(database: Path) -> set[str]:
    """Read, never modify, the optional Meme Stealer plugin database."""
    if not database.is_file():
        return set()
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        return {
            str(row[0]).strip().lower()
            for row in conn.execute("SELECT hash FROM blacklist")
            if len(str(row[0]).strip()) == 64
        }
