"""Local photo catalogue and targeted AstrBot SQLite history editing.

No message is sent by this module. File operations only touch individual,
hash-checked files below temp/ or this plugin's own library/ and trash/.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

from .guard import IMAGE_SUFFIXES, image_references
from .media import local_path
from .memes import KnownMemes, event_meme_hint
from .policy import fingerprint, rule_match, visual_match
from .scope import group_from_session, normalize_destination

TABLES = {"conversations": "conversation_id", "platform_message_history": "id"}
MARKER = "[图片已由管理员从上下文移除]"
PATH_PATTERN = re.compile(
    r"(?:file:///|[A-Za-z]:[\\/]|(?<![A-Za-z0-9:/])/)[^\n\r\"'<>|?*]{1,1024}?\.(?:jpe?g|png|gif|webp|bmp|tiff?|avif)(?=$|[\s\"'<>),\]])",
    re.I,
)


def image_refs(value, depth=0):
    """Read standard components, OpenAI image blocks and JSON tool arguments."""
    if depth > 20:
        return
    if isinstance(value, dict):
        kind = str(value.get("type", "")).lower()
        data = value.get("data") if isinstance(value.get("data"), dict) else value
        if kind in {"image", "image_url", "input_image"}:
            block = data.get("image_url", data)
            if isinstance(block, str):
                yield block
            elif isinstance(block, dict):
                ref = next((block.get(k) for k in ("path", "url", "file") if block.get(k)), None)
                if isinstance(ref, str):
                    yield ref
        for child in value.values():
            yield from image_refs(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from image_refs(child, depth + 1)
    elif isinstance(value, str):
        if value.startswith(("http://", "https://", "data:", "base64://")):
            return
        if value[:1] in {"{", "["}:
            try:
                yield from image_refs(json.loads(value), depth + 1)
                return
            except (ValueError, RecursionError):
                pass
        for match in PATH_PATTERN.finditer(value):
            yield match.group(0)


def scrub(value, references: set[str], depth=0):
    """Remove exact image references, preserving surrounding text/tool structure."""
    if depth > 30:
        return value
    if isinstance(value, dict):
        kind = str(value.get("type", "")).lower()
        if kind in {"image", "image_url", "input_image"}:
            if any(ref in references for ref in image_refs(value)):
                return {"type": "text", "text": MARKER}
        return {key: scrub(child, references, depth + 1) for key, child in value.items()}
    if isinstance(value, list):
        return [scrub(child, references, depth + 1) for child in value]
    if isinstance(value, str):
        if value[:1] in {"{", "["}:
            try:
                decoded = json.loads(value)
                updated = scrub(decoded, references, depth + 1)
                if decoded != updated:
                    return json.dumps(updated, ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        for ref in sorted(references, key=len, reverse=True):
            if ref:
                value = value.replace(ref, MARKER)
        return value
    return value


def timestamp(value, fallback=None):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError):
        return float(fallback or time.time())


class HistoryLibrary:
    def __init__(self, directory: Path, core_database: Path | None, temp_dir: Path, config: dict):
        self.directory = directory.resolve()
        self.core_database = core_database.resolve() if core_database else None
        self.temp_dir = temp_dir.resolve()
        self.config = config
        self.memes = KnownMemes(
            self.directory.parent / "astrbot_plugin_stealer" / "cache" / "emoji.db"
        )
        self.lock = threading.RLock()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.library = self.directory / "library"
        self.trash = self.directory / "trash"
        self.library.mkdir(exist_ok=True)
        self.trash.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(self.directory / "history.db", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS images (
                id TEXT PRIMARY KEY, sha256 TEXT NOT NULL DEFAULT '',
                pixels TEXT NOT NULL DEFAULT '', dhash TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
                format TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0,
                first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                kept INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0,
                detached INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sources (
                image_id TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
                seen_at REAL NOT NULL, PRIMARY KEY(image_id, source));
            CREATE TABLE IF NOT EXISTS links (
                image_id TEXT NOT NULL, table_name TEXT NOT NULL, row_key TEXT NOT NULL,
                session TEXT NOT NULL, reference TEXT NOT NULL, seen_at REAL NOT NULL,
                PRIMARY KEY(image_id, table_name, row_key, reference));
            CREATE TABLE IF NOT EXISTS changes (
                id TEXT PRIMARY KEY, image_id TEXT NOT NULL, created_at REAL NOT NULL,
                payload TEXT NOT NULL, restored INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS file_cache (
                path TEXT PRIMARY KEY, mtime INTEGER NOT NULL, size INTEGER NOT NULL,
                image_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS image_groups (
                image_id TEXT NOT NULL, group_key TEXT NOT NULL, seen_at REAL NOT NULL,
                PRIMARY KEY(image_id,group_key));
            CREATE TABLE IF NOT EXISTS chat_groups (
                group_key TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS image_groups_key ON image_groups(group_key,image_id);
            CREATE TABLE IF NOT EXISTS received_scopes (
                image_id TEXT NOT NULL, scope TEXT NOT NULL, seen_at REAL NOT NULL,
                PRIMARY KEY(image_id,scope));
        """)
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(images)")}
        if "purged" not in columns:
            self.conn.execute("ALTER TABLE images ADD COLUMN purged INTEGER NOT NULL DEFAULT 0")
        if "file_date" not in columns:
            self.conn.execute("ALTER TABLE images ADD COLUMN file_date REAL")
        if "visual" not in columns:
            self.conn.execute("ALTER TABLE images ADD COLUMN visual TEXT NOT NULL DEFAULT ''")
        for name, default in (("meme_auto", 0), ("meme_override", -1)):
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE images ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}"
                )
        if "meme_reason" not in columns:
            self.conn.execute("ALTER TABLE images ADD COLUMN meme_reason TEXT NOT NULL DEFAULT ''")
        self.conn.commit()
        self.status = {"running": False, "last_scan": None, "errors": 0, "indexed": 0}

    def _source(self, image_id, reference, kind, seen):
        self.conn.execute(
            "INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(image_id,source) "
            "DO UPDATE SET seen_at=max(seen_at,excluded.seen_at)",
            (image_id, reference, kind, seen),
        )

    def ingest(
        self,
        data: bytes,
        reference: str,
        *,
        seen=None,
        archive=False,
        group_key="",
        group_name="",
        received_scope="",
        meme_hint="",
        force_archive=False,
    ):
        item = fingerprint(data)
        seen = timestamp(seen)
        with Image.open(io.BytesIO(data)) as source:
            width, height = source.size
            fmt = str(source.format or "image")
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT id,removed,detached FROM images WHERE sha256=?", (item.sha256,)
            ).fetchone()
            image_id = row["id"] if row else item.sha256
            self.conn.execute(
                "INSERT INTO images(id,sha256,pixels,dhash,width,height,format,size,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "first_seen=min(first_seen,excluded.first_seen),last_seen=max(last_seen,excluded.last_seen),"
                "pixels=excluded.pixels,dhash=excluded.dhash",
                (
                    image_id,
                    item.sha256,
                    item.pixels,
                    item.dhash,
                    width,
                    height,
                    fmt,
                    len(data),
                    seen,
                    seen,
                ),
            )
            self.conn.execute("UPDATE images SET visual=? WHERE id=?", (item.visual, image_id))
            self.mark_meme_auto(image_id, meme_hint or self.memes.reason(item.sha256, reference))
            if group_key:
                self.record_group(image_id, group_key, group_name, seen)
            if received_scope:
                self.record_received(image_id, received_scope, seen)
            # Do not put large base64 blobs in the catalogue or duplicate them in history.db.
            if reference and not reference.startswith(("base64://", "data:")):
                self._source(image_id, reference, "observed", seen)
            include = self.config.get("collect_memes", False) or not self.is_meme(
                self.get(image_id)
            )
            if archive and (force_archive or include) and not (row and row["removed"]):
                target = self.library / (item.sha256 + ".img")
                if not target.exists():
                    used = sum(p.stat().st_size for p in self.library.iterdir() if p.is_file())
                    budget = int(self.config.get("history_storage_mb", 512)) * 1024 * 1024
                    if used + len(data) > budget:
                        raise ValueError("图片库已达容量上限，请在面板清理后继续收集")
                    target.write_bytes(data)
                self._source(image_id, str(target), "archive", seen)
            return image_id

    def placeholder(self, reference, seen):
        image_id = hashlib.sha256(("ref:" + reference).encode()).hexdigest()
        with self.lock, self.conn:
            found = self.conn.execute(
                "SELECT image_id FROM sources WHERE source=? LIMIT 1", (reference,)
            ).fetchone()
            if found:
                self.mark_meme_auto(found[0], self.memes.reason(reference=reference))
                return found[0]
            self.conn.execute(
                "INSERT OR IGNORE INTO images(id,first_seen,last_seen) VALUES (?,?,?)",
                (image_id, seen, seen),
            )
            self._source(image_id, reference, "reference", seen)
            self.mark_meme_auto(image_id, self.memes.reason(reference=reference))
        return image_id

    def index_file(self, path: Path, seen=None):
        resolved = path.resolve()
        info = resolved.stat()
        with self.lock:
            cached = self.conn.execute(
                "SELECT * FROM file_cache WHERE path=?", (str(resolved),)
            ).fetchone()
            enriched = cached and self.get(cached["image_id"]).get("visual")
            if enriched and cached["mtime"] == info.st_mtime_ns and cached["size"] == info.st_size:
                with self.conn:
                    self.conn.execute(
                        "UPDATE images SET file_date=min(coalesce(file_date,?),?) WHERE id=?",
                        (info.st_mtime, info.st_mtime, cached["image_id"]),
                    )
                return cached["image_id"]
        if info.st_size > int(self.config.get("max_image_mb", 16)) * 1024 * 1024:
            raise ValueError("image too large")
        image_id = self.ingest(resolved.read_bytes(), str(resolved), seen=seen or info.st_mtime)
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO file_cache VALUES (?,?,?,?)",
                (str(resolved), info.st_mtime_ns, info.st_size, image_id),
            )
            self.conn.execute(
                "UPDATE images SET file_date=min(coalesce(file_date,?),?) WHERE id=?",
                (info.st_mtime, info.st_mtime, image_id),
            )
        return image_id

    def scan(self):
        self.status.update(running=True, errors=0, indexed=0)
        try:
            self.refresh_memes()
            records = self.history_rows()
            links = []
            limit = max(100, min(50000, int(self.config.get("history_scan_limit", 5000))))
            for table, row in records:
                try:
                    value = json.loads(row["content"])
                except (ValueError, TypeError):
                    continue
                seen = timestamp(row.get("created_at") or row.get("updated_at"))
                # Bot outputs and plain-text paths cannot grant send permissions.
                trusted_refs = set()
                meme_hints = {}
                if (
                    table == "platform_message_history"
                    and isinstance(value, dict)
                    and value.get("type") == "user"
                ):
                    trusted_refs = {
                        str(ref) for _, ref in image_references(value.get("message", []))
                    }
                    received = list(image_references(value.get("message", [])))
                    for idx, (component, reference) in enumerate(received):
                        hint = event_meme_hint(component, reference, index=idx, total=len(received))
                        if hint:
                            meme_hints[reference] = hint
                elif table == "conversations" and isinstance(value, list):
                    # Some adapters persist image components only in user-role
                    # LLM history. Accept explicit image blocks, never text paths.
                    for message in value:
                        if not isinstance(message, dict) or message.get("role") != "user":
                            continue
                        content = message.get("content")
                        if not isinstance(content, list):
                            continue
                        for block in content:
                            if isinstance(block, dict) and str(block.get("type", "")).lower() in {
                                "image",
                                "image_url",
                                "input_image",
                            }:
                                trusted_refs.update(image_refs(block))
                for reference in set(image_refs(value)):
                    try:
                        if reference.startswith(("http://", "https://", "base64://", "data:")):
                            image_id = (
                                self.placeholder(reference, seen)
                                if not reference.startswith(("base64://", "data:"))
                                else None
                            )
                            if image_id is None:
                                import base64

                                raw = (
                                    reference.split(",", 1)[1]
                                    if reference.startswith("data:")
                                    else reference[9:]
                                )
                                if len(raw) > int(self.config.get("max_image_mb", 16)) * 1400000:
                                    raise ValueError("image too large")
                                image_id = self.ingest(
                                    base64.b64decode(raw, validate=True),
                                    reference,
                                    seen=seen,
                                    archive=bool(self.config.get("history_enabled", True)),
                                    meme_hint=meme_hints.get(reference, ""),
                                )
                        else:
                            path = local_path(reference)
                            image_id = (
                                self.index_file(path, seen)
                                if path.is_file()
                                else self.placeholder(reference, seen)
                            )
                        links.append(
                            (
                                image_id,
                                table,
                                str(row[TABLES[table]]),
                                str(row.get("user_id", "")),
                                reference,
                                seen,
                            )
                        )
                        group_key = group_from_session(
                            str(row.get("platform_id", "")),
                            str(row.get("user_id", "")),
                            str(row.get("sender_id", "")),
                        )
                        if group_key:
                            self.record_group(image_id, group_key, seen=seen)
                        if reference in trusted_refs:
                            scope = group_key or normalize_destination(str(row.get("user_id", "")))
                            self.record_received(image_id, scope, seen)
                        self.mark_meme_auto(
                            image_id,
                            meme_hints.get(reference, "") or self.memes.reason(reference=reference),
                        )
                    except (OSError, ValueError):
                        self.status["errors"] += 1
            with self.lock, self.conn:
                self.conn.execute("DELETE FROM links")
                self.conn.executemany("INSERT OR IGNORE INTO links VALUES (?,?,?,?,?,?)", links)
            if self.temp_dir.exists():
                for count, path in enumerate(sorted(self.temp_dir.iterdir(), key=lambda p: p.name)):
                    if count >= limit:
                        break
                    if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                        try:
                            self.index_file(path)
                            self.status["indexed"] += 1
                        except (OSError, ValueError):
                            self.status["errors"] += 1
            self.status["last_scan"] = time.time()
        finally:
            self.status["running"] = False
        return dict(self.status)

    @staticmethod
    def is_meme(row):
        override = row.get("meme_override", -1)
        return bool(override if override >= 0 else row.get("meme_auto", 0))

    def mark_meme_auto(self, image_id, reason):
        if reason:
            with self.lock, self.conn:
                self.conn.execute(
                    "UPDATE images SET meme_auto=1,meme_reason=? WHERE id=?",
                    (reason[:100], image_id),
                )

    def set_meme_override(self, image_id, value):
        self.get(image_id)
        if value not in {-1, 0, 1}:
            raise ValueError("无效的图片类型")
        with self.lock, self.conn:
            self.conn.execute("UPDATE images SET meme_override=? WHERE id=?", (value, image_id))

    def refresh_memes(self):
        self.memes.refresh(force=True)
        with self.lock:
            rows = [dict(row) for row in self.conn.execute("SELECT id,sha256 FROM images")]
            sources = [
                (r[0], r[1])
                for r in self.conn.execute(
                    "SELECT image_id,source FROM sources WHERE kind!='archive'"
                )
            ]
        for row in rows:
            self.mark_meme_auto(row["id"], self.memes.reason(row["sha256"]))
        for image_id, source in sources:
            self.mark_meme_auto(image_id, self.memes.reason(reference=source))

    def record_group(self, image_id, group_key, name="", seen=None):
        if not group_key or ":GroupMessage:" not in group_key:
            return
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO chat_groups(group_key,name) VALUES (?,?)",
                (group_key, str(name)[:100]),
            )
            self.conn.execute(
                "INSERT INTO image_groups VALUES (?,?,?) ON CONFLICT(image_id,group_key) DO UPDATE SET seen_at=max(seen_at,excluded.seen_at)",
                (image_id, group_key, timestamp(seen)),
            )

    def record_received(self, image_id, scope, seen=None):
        scope = normalize_destination(scope)
        if not scope:
            return
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO received_scopes VALUES (?,?,?) ON CONFLICT(image_id,scope) DO UPDATE SET seen_at=max(seen_at,excluded.seen_at)",
                (image_id, scope, timestamp(seen)),
            )

    def scope_verdict(self, item, destination):
        """Deny unless an actual received-image record confirms this chat."""
        destination = normalize_destination(destination)
        if not destination:
            return "无法确认图片接收会话，已阻止发送"
        with self.lock:
            rows = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT i.*,s.scope FROM images i JOIN received_scopes s ON s.image_id=i.id",
                )
            ]
        # An exact source wins over lookalikes in another chat. If a new
        # encoding visually matches several chats, do not guess permission.
        exact = {row["scope"] for row in rows if rule_match(item, row, variants=False)}
        if exact:
            return None if destination in exact else "图片属于其他会话，跨群/跨会话隔离已拦截"
        visual_scopes = {row["scope"] for row in rows if visual_match(item, row)}
        if visual_scopes == {destination}:
            return None
        return "图片未在当前会话接收，跨群/跨会话隔离已拦截"

    def confirmed_scopes(self, image_id):
        with self.lock:
            return [
                r[0]
                for r in self.conn.execute(
                    "SELECT scope FROM received_scopes WHERE image_id=? ORDER BY scope", (image_id,)
                )
            ]

    def rename_group(self, group_key, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("群聊备注需为 1 至 100 个字符")
        with self.lock, self.conn:
            changed = self.conn.execute(
                "UPDATE chat_groups SET name=? WHERE group_key=?", (name.strip(), group_key)
            ).rowcount
            if not changed:
                raise ValueError("群聊不存在")

    def enrich_rules(self, store):
        """Upgrade old fingerprints from known local files; never create bans."""
        upgraded = 0
        for rule in store.entries():
            if rule.get("visual"):
                continue
            with self.lock:
                rows = [
                    r[0]
                    for r in self.conn.execute(
                        "SELECT id FROM images WHERE sha256=?", (rule["sha256"],)
                    )
                ]
            for image_id in rows:
                try:
                    _, raw = self.local_file(image_id, allow_removed=True)
                    store.enrich(fingerprint(raw))
                    upgraded += 1
                    break
                except (OSError, ValueError):
                    continue
        return upgraded

    def upgrade_catalogue(self):
        with self.lock:
            ids = [
                r[0]
                for r in self.conn.execute("SELECT id FROM images WHERE visual='' AND purged=0")
            ]
        for image_id in ids:
            try:
                _, raw = self.local_file(image_id, allow_removed=True)
                item = fingerprint(raw)
                with self.lock, self.conn:
                    self.conn.execute(
                        "UPDATE images SET pixels=?,dhash=?,visual=? WHERE id=?",
                        (item.pixels, item.dhash, item.visual, image_id),
                    )
            except (OSError, ValueError):
                continue

    def history_rows(self):
        if not self.core_database or not self.core_database.is_file():
            return []
        result = []
        with closing(sqlite3.connect(self.core_database.as_uri() + "?mode=ro", uri=True)) as core:
            core.row_factory = sqlite3.Row
            for table, key in TABLES.items():
                # Explicit schema support: do not scan arbitrary plugin databases.
                columns = {r[1] for r in core.execute(f"PRAGMA table_info({table})")}
                if not {key, "content"}.issubset(columns):
                    continue
                result.extend((table, dict(row)) for row in core.execute(f"SELECT * FROM {table}"))
        return result

    def get(self, image_id):
        if not re.fullmatch(r"[0-9a-f]{64}", str(image_id)):
            raise ValueError("无效图片编号")
        with self.lock:
            row = self.conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
            if not row:
                raise ValueError("图片记录不存在")
            result = dict(row)
            result["is_meme"] = self.is_meme(result)
            return result

    def references(self, image_id):
        with self.lock:
            result = {
                r[0]
                for r in self.conn.execute(
                    "SELECT source FROM sources WHERE image_id=?", (image_id,)
                )
            }
            result.update(
                r[0]
                for r in self.conn.execute(
                    "SELECT reference FROM links WHERE image_id=?", (image_id,)
                )
            )
        for ref in list(result):
            if not ref.startswith(("http://", "https://", "data:", "base64://")):
                try:
                    path = local_path(ref)
                    result.update({str(path), path.as_uri(), path.as_posix()})
                except (OSError, ValueError):
                    pass
        return result

    def sources(self, image_id):
        with self.lock:
            return [
                r[0]
                for r in self.conn.execute(
                    "SELECT source FROM sources WHERE image_id=? ORDER BY kind='archive' DESC",
                    (image_id,),
                )
            ]

    def local_file(self, image_id, *, allow_removed=False):
        row = self.get(image_id)
        if row["removed"] and not allow_removed:
            raise ValueError("图片在回收站中")
        for reference in self.sources(image_id):
            if reference.startswith(("http://", "https://", "base64://", "data:")):
                continue
            try:
                path = local_path(reference)
                limit = int(self.config.get("max_image_mb", 16)) * 1024 * 1024
                if path.is_file() and path.stat().st_size <= limit:
                    data = path.read_bytes()
                    if not row["sha256"] or hashlib.sha256(data).hexdigest() == row["sha256"]:
                        return path, data
            except OSError:
                continue
        raise FileNotFoundError("原图已清理、地址过期或文件已改变")

    def preview(self, image_id, original=False):
        _, data = self.local_file(image_id)
        return self.thumbnail(data, original)

    @staticmethod
    def thumbnail(data, original=False):
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 40_000_000:
                raise ValueError("image pixel limit exceeded")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1600, 1600) if original else (320, 320))
            output = io.BytesIO()
            image.save(output, "JPEG", quality=88 if original else 75)
        return output.getvalue()

    def set_kept(self, image_id, kept):
        row = self.get(image_id)
        if row["removed"]:
            raise ValueError("请先从回收站恢复图片")
        if kept:
            _, data = self.local_file(image_id)
            self.ingest(data, "", archive=True, seen=row["first_seen"], force_archive=True)
        with self.lock, self.conn:
            self.conn.execute("UPDATE images SET kept=? WHERE id=?", (int(kept), image_id))

    def detach(self, image_id):
        self.get(image_id)
        references = self.references(image_id)
        edits = []
        if not self.core_database:
            raise ValueError("当前数据库不支持上下文编辑")
        with closing(sqlite3.connect(self.core_database, timeout=10)) as core:
            core.execute("BEGIN IMMEDIATE")
            try:
                for table, key in TABLES.items():
                    columns = {r[1] for r in core.execute(f"PRAGMA table_info({table})")}
                    if not {key, "content"}.issubset(columns):
                        continue
                    for row_key, before in core.execute(
                        f"SELECT {key},content FROM {table}"
                    ).fetchall():
                        try:
                            value = json.loads(before)
                        except (ValueError, TypeError):
                            continue
                        changed = scrub(value, references)
                        if changed != value:
                            after = json.dumps(changed, ensure_ascii=False)
                            edits.append(
                                {"table": table, "key": row_key, "before": before, "after": after}
                            )
                # Persist a local recovery journal before committing any core changes.
                change_id = uuid.uuid4().hex
                with self.lock, self.conn:
                    self.conn.execute(
                        "INSERT INTO changes VALUES (?,?,?,?,0)",
                        (change_id, image_id, time.time(), json.dumps(edits, ensure_ascii=False)),
                    )
                for edit in edits:
                    table, key = edit["table"], TABLES[edit["table"]]
                    core.execute(
                        f"UPDATE {table} SET content=? WHERE {key}=? AND content=?",
                        (edit["after"], edit["key"], edit["before"]),
                    )
                core.commit()
            except BaseException:
                core.rollback()
                raise
        with self.lock, self.conn:
            self.conn.execute("UPDATE images SET detached=1 WHERE id=?", (image_id,))
            self.conn.execute("DELETE FROM links WHERE image_id=?", (image_id,))
        return {"changed_records": len(edits), "change_id": change_id}

    def detached_references(self):
        with self.lock:
            ids = [r[0] for r in self.conn.execute("SELECT id FROM images WHERE detached=1")]
        return set().union(*(self.references(image_id) for image_id in ids)) if ids else set()

    def _managed(self, path):
        resolved = path.resolve()
        if path.is_symlink():
            return False
        return any(resolved.is_relative_to(root) for root in (self.temp_dir, self.library))

    def remove_files(self, image_id):
        row = self.get(image_id)
        if row["kept"]:
            raise ValueError("图片已标记保留，请先取消保留")
        moved, skipped = [], 0
        seen = set()
        for reference in self.sources(image_id):
            if reference.startswith(("http://", "https://", "base64://", "data:")):
                continue
            path = local_path(reference)
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            if not self._managed(path):
                skipped += 1
                continue
            if path.stat().st_size > int(self.config.get("max_image_mb", 16)) * 1024 * 1024:
                skipped += 1
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                skipped += 1
                continue
            target = self.trash / (uuid.uuid4().hex + ".img")
            # Journal each individual move first, so interrupted operations can be restored.
            entry = {"original": str(path), "trash": str(target)}
            with self.lock, self.conn:
                self.conn.execute(
                    "INSERT INTO changes VALUES (?,?,?,?,0)",
                    (uuid.uuid4().hex, image_id, time.time(), json.dumps({"file": entry})),
                )
            path.rename(target)
            moved.append(entry)
        with self.lock, self.conn:
            self.conn.execute("UPDATE images SET removed=1 WHERE id=?", (image_id,))
            self.conn.execute("DELETE FROM file_cache WHERE image_id=?", (image_id,))
        return {"moved_files": len(moved), "external_files_untouched": skipped}

    def restore(self, image_id):
        if self.get(image_id)["purged"]:
            raise ValueError("图片已彻底删除，无法恢复")
        restored, conflicts = 0, 0
        with self.lock:
            changes = self.conn.execute(
                "SELECT * FROM changes WHERE image_id=? AND restored=0 ORDER BY created_at DESC",
                (image_id,),
            ).fetchall()
        for change in changes:
            payload = json.loads(change["payload"])
            conflict = False
            if isinstance(payload, dict) and "file" in payload:
                paths = payload["file"]
                source, target = Path(paths["trash"]), Path(paths["original"])
                if not source.resolve().is_relative_to(self.trash) or not self._managed(target):
                    conflict = True
                elif source.is_file():
                    if target.exists():
                        conflict = True
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source.rename(target)
                        restored += 1
            elif isinstance(payload, list) and self.core_database:
                with closing(sqlite3.connect(self.core_database, timeout=10)) as core:
                    with core:
                        for edit in payload:
                            if edit["table"] not in TABLES:
                                continue
                            table, key = edit["table"], TABLES[edit["table"]]
                            cursor = core.execute(
                                f"UPDATE {table} SET content=? WHERE {key}=? AND content=?",
                                (edit["before"], edit["key"], edit["after"]),
                            )
                            if cursor.rowcount:
                                restored += 1
                            else:
                                conflict = True
            if conflict:
                conflicts += 1
            else:
                with self.lock, self.conn:
                    self.conn.execute("UPDATE changes SET restored=1 WHERE id=?", (change["id"],))
        with self.lock, self.conn:
            self.conn.execute("UPDATE images SET removed=0,detached=0 WHERE id=?", (image_id,))
        return {
            "restored": restored,
            "conflicts": conflicts,
            "notice": "恢复不会解除禁发；有更新的聊天记录不会被覆盖。",
        }

    def purge(self, image_id):
        row = self.get(image_id)
        if not row["removed"] or row["kept"]:
            raise ValueError("请先取消保留并将图片移入回收站")
        # A purge also detaches references when the earlier trash operation did not.
        self.detach(image_id)
        with self.lock:
            records = self.conn.execute(
                "SELECT payload FROM changes WHERE image_id=?", (image_id,)
            ).fetchall()
        deleted = 0
        for record in records:
            payload = json.loads(record[0])
            if not isinstance(payload, dict) or "file" not in payload:
                continue
            path = Path(payload["file"]["trash"])
            if not path.resolve().is_relative_to(self.trash) or path.is_symlink():
                raise ValueError("回收站路径异常，已停止操作")
            if path.is_file():
                if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                    raise ValueError("回收站文件已改变，已停止操作")
                path.unlink()
                deleted += 1
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM changes WHERE image_id=?", (image_id,))
            self.conn.execute("DELETE FROM links WHERE image_id=?", (image_id,))
            self.conn.execute("UPDATE images SET purged=1,detached=1 WHERE id=?", (image_id,))
        return {
            "deleted_files": deleted,
            "notice": "回收站副本和本插件恢复记录已删除，禁发指纹保留；外部文件及其他服务的记录不受影响。",
        }

    def external_rules(self, hashes):
        with self.lock:
            rows = [
                dict(r)
                for r in self.conn.execute("SELECT * FROM images WHERE sha256!='' AND visual!=''")
            ]
        return [{**row, "external": True} for row in rows if row["sha256"] in hashes]

    @staticmethod
    def similar_groups(rows):
        """Bounded band index followed by whole-image checks against an anchor.

        16 four-bit bands ensure <=10 differing hash bits share a band.
        No transitive chaining: every member must match its own anchor.
        """
        anchors, buckets, groups = {}, {}, {}
        for row in rows:
            if not row.get("visual") or not row.get("dhash"):
                continue
            value = int(row["dhash"], 16)
            bands = [(n, (value >> (n * 4)) & 15) for n in range(16)]
            candidates = set().union(*(buckets.get(band, set()) for band in bands))
            anchor = next(
                (key for key in sorted(candidates) if visual_match(row, anchors[key], loose=True)),
                None,
            )
            if anchor is None:
                anchor = row["id"]
                anchors[anchor] = row
                groups[anchor] = []
                for band in bands:
                    buckets.setdefault(band, set()).add(anchor)
            groups[anchor].append(row)
        return {key: members for key, members in groups.items() if len(members) > 1}

    def list_images(
        self,
        *,
        page=1,
        size=60,
        mode="all",
        search="",
        day="",
        blocked=None,
        rules=None,
        variants=True,
        distance=-1,
        group="",
        similar_to="",
    ):
        blocked = blocked or set()
        rules = rules or []
        with self.lock:
            rows = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT i.*, (SELECT count(*) FROM links l WHERE l.image_id=i.id) AS context_count "
                    "FROM images i WHERE purged=0 ORDER BY coalesce(file_date,first_seen) DESC,id"
                )
            ]
            names = {
                r["image_id"]: r["source"]
                for r in self.conn.execute(
                    "SELECT image_id,source FROM sources WHERE kind!='archive'"
                )
            }
            associations = {}
            for r in self.conn.execute("SELECT * FROM image_groups"):
                associations.setdefault(r["image_id"], set()).add(r["group_key"])
            group_names = {
                r["group_key"]: r["name"] for r in self.conn.execute("SELECT * FROM chat_groups")
            }
        group_counts = {}
        scoped = []
        for row in rows:
            row["is_meme"] = self.is_meme(row)
            row["groups"] = sorted(associations.get(row["id"], set()))
            for key in row["groups"]:
                group_counts[key] = group_counts.get(key, 0) + 1
            if (
                group == "unknown"
                and row["groups"]
                or group
                and group != "unknown"
                and group not in row["groups"]
            ):
                continue
            row["blocked_by"] = []
            row["related_blocked"] = False
            for rule in rules:
                reason = rule_match(row, rule, distance=distance, variants=variants)
                if reason:
                    row["blocked_by"].append(
                        {
                            "sha256": rule["sha256"],
                            "reason": reason,
                            "external": bool(rule.get("external")),
                        }
                    )
                elif visual_match(row, rule, loose=True):
                    row["related_blocked"] = True
            row["blocked"] = row["sha256"] in blocked or bool(row["blocked_by"])
            row["direct_ban"] = row["sha256"] in blocked
            row["day"] = (
                datetime.fromtimestamp(row["file_date"] or row["first_seen"])
                .astimezone()
                .strftime("%Y-%m-%d")
            )
            row["name"] = Path(names.get(row["id"], "").split("?", 1)[0]).name or row["id"][:12]
            row["similar_group"] = ""
            row["similar_count"] = 0
            scoped.append(row)
        grouped = (
            self.similar_groups(
                [
                    r
                    for r in scoped
                    if not r["removed"]
                    and (self.config.get("collect_memes", False) or not r["is_meme"])
                ]
            )
            if mode == "similar"
            else {}
        )
        for key, members in grouped.items():
            for row in members:
                row["similar_group"], row["similar_count"] = key, len(members)
        target = self.get(similar_to) if similar_to else None
        result = []
        for row in scoped:
            if mode == "memes" and not row["is_meme"]:
                continue
            if (
                mode in {"all", "context", "similar"}
                and row["is_meme"]
                and not self.config.get("collect_memes", False)
            ):
                continue
            if mode == "trash" and not row["removed"] or mode != "trash" and row["removed"]:
                continue
            if (
                mode == "kept"
                and not row["kept"]
                or mode == "blocked"
                and not (row["blocked"] or row["related_blocked"])
            ):
                continue
            if mode == "similar" and not row["similar_group"]:
                continue
            if (
                target
                and row["id"] != target["id"]
                and not (
                    row["sha256"]
                    and row["sha256"] == target["sha256"]
                    or visual_match(row, target, loose=True)
                )
            ):
                continue
            if mode == "context" and not row["context_count"] or day and row["day"] != day:
                continue
            if search and search.casefold() not in (row["name"] + row["id"]).casefold():
                continue
            result.append(row)
        counts = {
            "total": sum(
                self.config.get("collect_memes", False) or not r["is_meme"] for r in scoped
            ),
            "kept": sum(r["kept"] for r in scoped),
            "blocked": sum(r["blocked"] or r["related_blocked"] for r in scoped),
            "trash": sum(r["removed"] for r in scoped),
            "memes": sum(r["is_meme"] and not r["removed"] for r in scoped),
        }
        if mode == "similar":
            result.sort(key=lambda r: (r["similar_group"], -r["first_seen"], r["id"]))
        days = sorted({r["day"] for r in result}, reverse=True)
        start = (max(1, page) - 1) * size
        return {
            "items": result[start : start + size],
            "total": len(result),
            "counts": counts,
            "days": days,
            "page": page,
            "size": size,
            "scan": dict(self.status),
            "groups": [
                {
                    "key": key,
                    "name": group_names.get(key) or key.split(":GroupMessage:", 1)[-1],
                    "platform": key.split(":", 1)[0],
                    "count": count,
                }
                for key, count in sorted(group_counts.items())
            ],
            "similar_group_count": len(grouped),
            "similar_to": similar_to,
            "collect_memes": bool(self.config.get("collect_memes", False)),
        }

    def close(self):
        with self.lock:
            self.conn.close()

    def storage(self):
        return {
            "library_bytes": sum(p.stat().st_size for p in self.library.iterdir() if p.is_file()),
            "trash_bytes": sum(p.stat().st_size for p in self.trash.iterdir() if p.is_file()),
            "limit_mb": int(self.config.get("history_storage_mb", 512)),
        }
