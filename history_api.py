"""Authenticated AstrBot plugin-page endpoints; no standalone HTTP server."""

from __future__ import annotations

import asyncio
import base64
import functools
import io

from astrbot.api.web import json_response, request
from PIL import Image

from .policy import Fingerprint, fingerprint, rule_match


class HistoryAPI:
    def __init__(self, plugin):
        self.plugin = plugin
        self.library = plugin.history
        self.lock = asyncio.Lock()
        self.scan_task = None

    def register(self, context):
        for route, handler, methods in [
            ("images", self.images, ["GET"]),
            ("preview", self.preview, ["GET"]),
            ("detail", self.detail, ["GET"]),
            ("scan", self.scan, ["POST"]),
            ("action", self.action, ["POST"]),
            ("status", self.status, ["GET"]),
            ("group", self.group, ["POST"]),
            ("collection", self.collection, ["POST"]),
        ]:
            context.register_web_api(
                f"/astrbot_plugin_image_guard/{route}",
                self._protected(handler),
                methods,
                "图片历史管理器",
            )

    def _protected(self, handler):
        @functools.wraps(handler)
        async def call():
            if not request.username:
                return json_response(
                    {"success": False, "error": "请先登录 AstrBot 管理面板"}, status_code=401
                )
            try:
                return json_response({"success": True, **await handler()})
            except (ValueError, FileNotFoundError) as exc:
                return json_response({"success": False, "error": str(exc)}, status_code=400)
            except Exception as exc:
                self.plugin.logger.warning("[ImageHistory] 面板请求失败 (%s)", type(exc).__name__)
                return json_response(
                    {"success": False, "error": "操作失败，请查看 AstrBot 日志"}, status_code=500
                )

        return call

    async def status(self):
        return {
            "scan": dict(self.library.status),
            "guard_enabled": bool(self.plugin.config.get("enabled", True)),
            "blocked_count": self.plugin.guard.block_count,
            "history_enabled": bool(self.plugin.config.get("history_enabled", True)),
            "sqlite_history_supported": self.library.core_database is not None,
            "storage": await asyncio.to_thread(self.library.storage),
            "format_protection": bool(self.plugin.config.get("protect_format_variants", True)),
            "group_isolation": bool(self.plugin.config.get("group_isolation", True)),
            "collect_memes": bool(self.plugin.config.get("collect_memes", False)),
            "legacy_rules_without_visual": sum(
                not r.get("visual") for r in self.plugin.store.entries()
            ),
        }

    async def blocked(self):
        return {
            row["sha256"] for row in self.plugin.store.entries()
        } | await self.plugin.guard.external_hashes()

    async def images(self):
        query = request.query
        external = await self.plugin.guard.external_hashes()
        return await asyncio.to_thread(
            self.library.list_images,
            page=max(1, int(query.get("page", "1"))),
            size=max(1, min(100, int(query.get("size", "60")))),
            mode=query.get("mode", "all"),
            search=query.get("search", "")[:200],
            day=query.get("day", ""),
            blocked=await self.blocked(),
            rules=self.plugin.store.entries() + self.library.external_rules(external),
            variants=self.plugin.config.get("protect_format_variants", True),
            distance=int(self.plugin.config.get("similarity_distance", -1)),
            group=query.get("group", "")[:300],
            similar_to=query.get("similar_to", ""),
        )

    async def group(self):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("请求格式错误")
        async with self.lock:
            await asyncio.to_thread(
                self.library.rename_group, str(data.get("key", "")), data.get("name")
            )
        return {}

    async def collection(self):
        data = await request.json()
        if not isinstance(data, dict) or type(data.get("collect_memes")) is not bool:
            raise ValueError("收纳表情包开关必须为布尔值")
        value = data["collect_memes"]
        async with self.lock:
            old = self.plugin.config.get("collect_memes", False)
            try:
                self.plugin.config["collect_memes"] = value
                self.library.config["collect_memes"] = value
                if hasattr(self.plugin.config, "save_config_async"):
                    await self.plugin.config.save_config_async()
                elif hasattr(self.plugin.config, "save_config"):
                    await asyncio.to_thread(self.plugin.config.save_config)
            except Exception:
                self.plugin.config["collect_memes"] = old
                self.library.config["collect_memes"] = old
                raise
        self.start_scan()
        return {"collect_memes": value}

    async def acquire(self, image_id, force_archive=False):
        try:
            return await asyncio.to_thread(self.library.local_file, image_id)
        except FileNotFoundError:
            row = self.library.get(image_id)
            if row["removed"]:
                raise ValueError("请先恢复图片")
            for ref in self.library.sources(image_id):
                if ref.startswith(("http://", "https://")):
                    try:
                        data = await self.plugin.reader.read(ref)
                        item = await asyncio.to_thread(fingerprint, data)
                        if row["sha256"] and row["sha256"] != item.sha256:
                            continue
                        # Materialize a remote-reference placeholder without changing its ID.
                        with Image.open(io.BytesIO(data)) as image:
                            width, height, fmt = image.width, image.height, image.format
                        with self.library.lock, self.library.conn:
                            self.library.conn.execute(
                                "UPDATE images SET sha256=?,pixels=?,dhash=?,width=?,height=?,format=?,size=? WHERE id=?",
                                (
                                    item.sha256,
                                    item.pixels,
                                    item.dhash,
                                    width,
                                    height,
                                    fmt,
                                    len(data),
                                    image_id,
                                ),
                            )
                            self.library.conn.execute(
                                "UPDATE images SET visual=? WHERE id=?", (item.visual, image_id)
                            )
                        self.library.mark_meme_auto(
                            image_id, self.library.memes.reason(item.sha256, ref)
                        )
                        row = self.library.get(image_id)
                        if (
                            row["is_meme"]
                            and not self.plugin.config.get("collect_memes", False)
                            and not force_archive
                        ):
                            return None, data
                        await asyncio.to_thread(
                            self.library.ingest,
                            data,
                            ref,
                            archive=True,
                            seen=row["first_seen"],
                            force_archive=force_archive,
                        )
                        # Two remote URLs can materialize to the same file. Both
                        # catalogue IDs must be able to read the shared archive.
                        with self.library.lock, self.library.conn:
                            self.library._source(
                                image_id,
                                str(self.library.library / (item.sha256 + ".img")),
                                "archive",
                                row["first_seen"],
                            )
                        return await asyncio.to_thread(self.library.local_file, image_id)
                    except (OSError, ValueError) as exc:
                        if "容量" in str(exc):
                            raise
                        continue
            raise FileNotFoundError("原图已清理、远程地址已过期或文件无法安全读取") from None

    async def preview(self):
        image_id = request.query.get("id", "")
        _, raw = await self.acquire(image_id)
        data = await asyncio.to_thread(
            self.library.thumbnail, raw, request.query.get("large") == "1"
        )
        return {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()}

    async def detail(self):
        image_id = request.query.get("id", "")
        row = self.library.get(image_id)
        with self.library.lock:
            links = [
                dict(r)
                for r in self.library.conn.execute(
                    "SELECT table_name,session,seen_at FROM links WHERE image_id=? LIMIT 100",
                    (image_id,),
                )
            ]
        rules = self.plugin.store.entries() + self.library.external_rules(
            await self.plugin.guard.external_hashes()
        )
        matches = [
            {"sha256": r["sha256"], "external": bool(r.get("external")), "reason": reason}
            for r in rules
            if (
                reason := rule_match(
                    row,
                    r,
                    variants=self.plugin.config.get("protect_format_variants", True),
                    distance=int(self.plugin.config.get("similarity_distance", -1)),
                )
            )
        ]
        return {
            "item": row,
            "contexts": links,
            "blocked": row["sha256"] in await self.blocked() or bool(matches),
            "rules": matches,
            "confirmed_scopes": self.library.confirmed_scopes(image_id),
        }

    async def _scan(self):
        async with self.lock:
            try:
                result = await asyncio.to_thread(self.library.scan)
                await asyncio.to_thread(self.library.upgrade_catalogue)
                await asyncio.to_thread(self.library.enrich_rules, self.plugin.store)
                return result
            except Exception as exc:
                self.library.status["error"] = "扫描失败，请查看 AstrBot 日志"
                self.plugin.logger.warning("[ImageHistory] 扫描失败 (%s)", type(exc).__name__)

    def start_scan(self):
        if self.scan_task and not self.scan_task.done():
            return
        self.library.status["running"] = True
        self.scan_task = asyncio.create_task(self._scan())

    async def scan(self):
        self.start_scan()
        return {"scan": dict(self.library.status)}

    async def action(self):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("请求格式错误")
        ids = data.get("ids")
        operation = data.get("action")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise ValueError("请选中 1 至 100 张图片")
        if operation not in {
            "ban",
            "unban",
            "keep",
            "unkeep",
            "detach",
            "trash",
            "restore",
            "inspect",
            "purge",
            "unban_rule",
            "mark_meme",
            "mark_photo",
            "meme_auto",
        }:
            raise ValueError("未知操作")
        if operation in {"detach", "trash", "purge"} and data.get("confirm") is not True:
            raise ValueError("请在面板确认删除范围")
        if operation == "purge" and data.get("confirmation") != "彻底删除":
            raise ValueError("请输入“彻底删除”确认永久清理")
        if operation == "unban_rule" and data.get("confirm") is not True:
            raise ValueError("请确认解除来源规则，所有受其保护的转码图也将解除")
        result = []
        async with self.lock:
            for image_id in dict.fromkeys(ids):
                try:
                    row = self.library.get(image_id)
                    detail = {}
                    if operation in {"ban", "inspect"}:
                        _, raw = await self.acquire(image_id)
                        item = await asyncio.to_thread(fingerprint, raw)
                        if operation == "ban":
                            self.plugin.store.add(item)
                        else:
                            reason = self.plugin.store.match(
                                item,
                                int(self.plugin.config.get("similarity_distance", -1)),
                                variants=self.plugin.config.get("protect_format_variants", True),
                            )
                            external = await self.plugin.guard.external_hashes()
                            if (
                                item.sha256 in external
                                or self.plugin.config.get("protect_format_variants", True)
                                and any(
                                    rule_match(vars(item), r)
                                    for r in self.library.external_rules(external)
                                )
                            ):
                                reason = "表情包小偷黑名单命中"
                            detail = {
                                "verdict": reason or "允许发送",
                                "guard_enabled": bool(self.plugin.config.get("enabled", True)),
                            }
                            destination = data.get("destination", "")
                            if destination and self.plugin.config.get("group_isolation", True):
                                detail["scope_verdict"] = (
                                    await asyncio.to_thread(
                                        self.library.scope_verdict, vars(item), destination
                                    )
                                    or "允许在所选群聊发送"
                                )
                            else:
                                detail["scope_verdict"] = (
                                    "未选择群聊，本次仅测试禁发规则"
                                    if self.plugin.config.get("group_isolation", True)
                                    else "群聊隔离已关闭"
                                )
                    elif operation == "unban":
                        if any(r["sha256"] == row["sha256"] for r in self.plugin.store.entries()):
                            self.plugin.store.remove(row["sha256"])
                        if any(
                            rule_match(
                                row,
                                r,
                                variants=self.plugin.config.get("protect_format_variants", True),
                                distance=int(self.plugin.config.get("similarity_distance", -1)),
                            )
                            for r in self.plugin.store.entries()
                        ):
                            detail["notice"] = (
                                "此图片仍命中其他禁发原图的规则，请在详情中管理来源规则。"
                            )
                        if row["sha256"] in await self.plugin.guard.external_hashes():
                            detail["notice"] = "表情包小偷仍将此图拉黑，请到原插件解除。"
                    elif operation == "unban_rule":
                        rule_sha = str(data.get("rule_sha", ""))
                        rule = next(
                            (r for r in self.plugin.store.entries() if r["sha256"] == rule_sha),
                            None,
                        )
                        if not rule or not rule_match(
                            row,
                            rule,
                            variants=self.plugin.config.get("protect_format_variants", True),
                            distance=int(self.plugin.config.get("similarity_distance", -1)),
                        ):
                            raise ValueError("来源规则不存在或不匹配这张图片")
                        self.plugin.store.remove(rule_sha)
                        detail["notice"] = "已解除该来源规则；独立禁发规则和其他插件黑名单仍有效。"
                    elif operation in {"keep", "unkeep"}:
                        if operation == "keep":
                            await self.acquire(image_id, force_archive=True)
                        await asyncio.to_thread(
                            self.library.set_kept, image_id, operation == "keep"
                        )
                    elif operation in {"mark_meme", "mark_photo", "meme_auto"}:
                        await asyncio.to_thread(
                            self.library.set_meme_override,
                            image_id,
                            {"mark_meme": 1, "mark_photo": 0, "meme_auto": -1}[operation],
                        )
                    elif operation == "detach":
                        detail = await asyncio.to_thread(self.library.detach, image_id)
                    elif operation == "trash":
                        if row["kept"]:
                            raise ValueError("图片已保留，请先取消保留再删除")
                        # Refuse deletion until its fingerprint is known.
                        if not row["sha256"]:
                            await self.acquire(image_id)
                        if data.get("ban", True) is True:
                            row = self.library.get(image_id)
                            self.plugin.store.add(
                                Fingerprint(
                                    row["sha256"], row["pixels"], row["dhash"], row["visual"]
                                )
                            )
                        if data.get("detach", True) is True:
                            detail.update(await asyncio.to_thread(self.library.detach, image_id))
                        detail.update(await asyncio.to_thread(self.library.remove_files, image_id))
                    elif operation == "restore":
                        detail = await asyncio.to_thread(self.library.restore, image_id)
                    elif operation == "purge":
                        detail = await asyncio.to_thread(self.library.purge, image_id)
                    result.append({"id": image_id, "success": True, **detail})
                except (OSError, ValueError) as exc:
                    result.append({"id": image_id, "success": False, "error": str(exc)})
        return {"results": result}
