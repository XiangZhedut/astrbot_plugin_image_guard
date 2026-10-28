"""Inspect message chains without depending on a specific platform adapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path

from PIL import Image

from .media import MediaReader, local_path
from .policy import DenyStore, Fingerprint, fingerprint, read_stealer_blacklist, rule_match

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif"}


def component_type(component):
    value = (
        component.get("type", "") if isinstance(component, dict) else getattr(component, "type", "")
    )
    return str(getattr(value, "value", value)).lower()


def get_field(component, field, default=None):
    if isinstance(component, dict):
        return component.get(field, component.get("data", {}).get(field, default))
    # File.file is an I/O property in AstrBot; use its actual stored field.
    if field == "file" and component_type(component) == "file":
        return getattr(component, "file_", default)
    return getattr(component, field, default)


def image_references(message, tool_only: bool = False, *, all_files=False):
    """Yield (component, source) including nested Node/Nodes/Reply chains."""
    seen = set()

    def walk(value, depth=0):
        if value is None:
            return
        if depth > 12 or len(seen) > 1000:
            raise ValueError("message nesting limit exceeded")
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, (list, tuple)):
            for item in value:
                yield from walk(item, depth + 1)
            return
        kind = component_type(value)
        if kind in {"image", "file"}:
            reference = next(
                (
                    get_field(value, field)
                    for field in ("path", "url", "file")
                    if get_field(value, field)
                ),
                "",
            )
            if tool_only and not get_field(value, "path"):
                # The generic tool turns URL inputs into Image components;
                # inspect and pin those at event/adapter send time instead.
                return
            if kind == "file":
                name = str(get_field(value, "name") or get_field(value, "text") or reference)
                if (
                    not all_files
                    and Path(name.split("?", 1)[0]).suffix.lower() not in IMAGE_SUFFIXES
                ):
                    return
            yield value, reference
            return
        for field in ("chain", "content", "nodes", "messages"):
            child = get_field(value, field)
            if isinstance(child, (list, tuple)):
                yield from walk(child, depth + 1)

    yield from walk(message)


class ImageGuard:
    def __init__(
        self, store: DenyStore, reader: MediaReader, config: dict, stealer_db: Path, logger
    ):
        self.store = store
        self.reader = reader
        self.config = config
        self.stealer_db = stealer_db
        self.logger = logger
        self.block_count = 0
        self.active = True

    async def external_hashes(self):
        if not self.config.get("sync_stealer_blacklist", True):
            return set()
        return await asyncio.to_thread(read_stealer_blacklist, self.stealer_db)

    async def check(self, message, *, tool_only=False, destination=None) -> str | None:
        if not self.active or not self.config.get("enabled", True):
            return None
        try:
            references = list(image_references(message, tool_only, all_files=True))
            if not references:
                return None
            external = await self.external_hashes()
            for component, source in references:
                item = None
                if component_type(component) == "file":
                    name = str(
                        get_field(component, "name") or get_field(component, "text") or source
                    )
                    if Path(name.split("?", 1)[0]).suffix.lower() not in IMAGE_SUFFIXES:
                        # Sniff the bytes, not the filename: a PNG renamed .txt is
                        # still an image. Ordinary local documents need no decode.
                        if not str(source).startswith(
                            ("http://", "https://", "base64://", "data:")
                        ):

                            def header():
                                with local_path(str(source)).open("rb") as stream:
                                    return stream.read(64)

                            if not image_header(await asyncio.to_thread(header)):
                                continue
                data = await self.reader.read(source)
                if component_type(component) == "file" and not image_header(data):
                    continue
                sha = hashlib.sha256(data).hexdigest()
                if sha in external:
                    reason = "表情包黑名单命中"
                else:
                    reason = self.store.match(Fingerprint(sha))
                    if reason is None and (self.store.entries() or external):
                        item = await asyncio.to_thread(fingerprint, data)
                        threshold = int(self.config.get("similarity_distance", -1))
                        reason = self.store.match(
                            item,
                            max(-1, min(4, threshold)),
                            variants=self.config.get("protect_format_variants", True),
                        )
                        history = getattr(self, "history", None)
                        if (
                            reason is None
                            and history
                            and self.config.get("protect_format_variants", True)
                        ):
                            if any(
                                rule_match(vars(item), rule)
                                for rule in history.external_rules(external)
                            ):
                                reason = "表情包黑名单转码指纹命中"
                history = getattr(self, "history", None)
                if (
                    reason is None
                    and destination is not None
                    and history
                    and self.config.get("group_isolation", True)
                ):
                    item = item or await asyncio.to_thread(fingerprint, data)
                    reason = await asyncio.to_thread(history.scope_verdict, vars(item), destination)
                if reason:
                    self.block_count += 1
                    self.logger.warning("[ImageGuard] 已拦截图片: %s, id=%s", reason, sha[:12])
                    return reason
                # Send exactly the remote bytes that were inspected. A changing
                # URL must not serve a different image on a second download.
                if not tool_only and str(source).startswith(("http://", "https://")):
                    pinned = "base64://" + base64.b64encode(data).decode("ascii")
                    if isinstance(component, dict) and component_type(component) == "image":
                        target = component.get("data", component)
                        for field in ("file", "url", "path"):
                            target.pop(field, None)
                        target["file"] = pinned
                    elif not isinstance(component, dict) and component_type(component) == "image":
                        component.file, component.url, component.path = pinned, "", ""
                    elif component_type(component) == "file":
                        # File components cannot consistently send base64 across
                        # adapters: refuse remote image attachments in this mode.
                        raise ValueError("remote image file attachment cannot be pinned")
            return None
        except Exception as exc:
            # Do not disclose paths, credentials, URLs or image data in logs.
            self.logger.warning("[ImageGuard] 图片检查失败 (%s)", type(exc).__name__)
            strict_scope = (
                destination is not None
                and getattr(self, "history", None) is not None
                and self.config.get("group_isolation", True)
            )
            return (
                "图片无法完成检查" if strict_scope or self.config.get("fail_closed", True) else None
            )


def image_header(data):
    known = (
        data.startswith(
            (
                b"\xff\xd8\xff",
                b"\x89PNG\r\n\x1a\n",
                b"GIF87a",
                b"GIF89a",
                b"BM",
                b"II*\x00",
                b"MM\x00*",
                b"\x00\x00\x01\x00",
            )
        )
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or (
            data[4:8] == b"ftyp"
            and any(brand in data[8:64] for brand in (b"avif", b"avis", b"heic", b"mif1"))
        )
    )
    if known:
        return True
    # Include installed Pillow decoders (e.g. JPEG2000, PPM, ICO) so changing
    # both the encoding and extension cannot skip the attachment check.
    Image.init()
    for _, accept in Image.OPEN.values():
        if accept:
            try:
                if accept(data) is True:
                    return True
            except (ValueError, IndexError, SyntaxError):
                continue
    return False
