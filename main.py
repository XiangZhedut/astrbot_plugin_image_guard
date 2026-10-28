from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.platform import Platform
from astrbot.core.tools.message_tools import SendMessageToUserTool

from .feedback import DEFAULT_NOTICE, RESPONSE_POLICY
from .guard import ImageGuard, image_references
from .history import HistoryLibrary, scrub
from .history_api import HistoryAPI
from .intercept import SendInterceptor
from .media import MediaReader
from .memes import event_meme_hint
from .policy import DenyStore, fingerprint
from .scope import event_destination


class ImageSendGuardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_image_guard"))
        self.store = DenyStore(self.data_dir / "denylist.db")
        self.reader = MediaReader(int(config.get("max_image_mb", 16)) * 1024 * 1024)
        self.guard = ImageGuard(
            self.store,
            self.reader,
            config,
            self.data_dir.parent / "astrbot_plugin_stealer" / "cache" / "emoji.db",
            self.logger,
        )
        self.interceptor = SendInterceptor(self.guard, self._notice)
        database = context.get_db() if hasattr(context, "get_db") else None
        core_path = getattr(database, "db_path", None)
        self.history = HistoryLibrary(
            self.data_dir,
            Path(core_path) if core_path else None,
            self.data_dir.parent.parent / "temp",
            config,
        )
        self.guard.history = self.history
        self.history_api = HistoryAPI(self)
        self.history_loop = None

    def _notice(self):
        text = str(self.config.get("block_notice", DEFAULT_NOTICE))
        return MessageChain(chain=[Plain(text)]) if text else None

    def _install(self):
        self.interceptor.install(AstrMessageEvent, Platform, SendMessageToUserTool)

    async def initialize(self):
        self._install()
        if hasattr(self.context, "register_web_api"):
            self.history_api.register(self.context)
            # Enrich rules before startup finishes, closing the legacy-rule gap.
            await asyncio.to_thread(self.history.enrich_rules, self.store)
            if self.config.get("history_enabled", True) or self.config.get("group_isolation", True):
                self.history_api.start_scan()
            self.history_loop = asyncio.create_task(self._history_watch())
        self.logger.info(
            "[ImageGuard] 图片禁发已启用；发送入口 %d 处", len(self.interceptor.patches)
        )

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        # Platform/event classes may be imported after plugin initialization.
        self._install()

    @filter.on_llm_request(priority=1000)
    async def before_llm(self, event, req):
        self._install()
        if self.config.get("enabled", True):
            prompt = getattr(req, "system_prompt", "") or ""
            if RESPONSE_POLICY not in prompt:
                req.system_prompt = prompt + "\n\n" + RESPONSE_POLICY
        removed = self.history.detached_references()
        if removed:
            req.contexts = scrub(req.contexts, removed)
            if getattr(req, "image_urls", None):
                req.image_urls = [ref for ref in req.image_urls if ref not in removed]

    @filter.on_llm_response(priority=-1000)
    async def refusal_response(self, event, response):
        state = self.interceptor.state(event)
        if not state or not state.blocked or response is None:
            return
        notice = self._notice()
        response.result_chain = notice or MessageChain(chain=[])
        response.completion_text = notice.get_plain_text() if notice else ""
        response.reasoning_content = None
        response.reasoning_signature = None
        response.tools_call_args = []
        response.tools_call_name = []
        response.tools_call_ids = []
        response.tools_call_extra_content = {}
        try:
            event.set_extra("_llm_reasoning_content", "")
        except AttributeError:
            pass

    @filter.on_agent_done(priority=-1000)
    async def refusal_history(self, event, run_context, response):
        state = self.interceptor.state(event)
        if not state or not state.blocked:
            return
        await self.refusal_response(event, response)
        messages = getattr(run_context, "messages", [])
        if messages and messages[-1].role == "assistant" and not messages[-1].tool_calls:
            notice = self._notice()
            messages[-1].content = [TextPart(text=notice.get_plain_text())] if notice else []

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def collect_images(self, event):
        references = (
            list(image_references(event.get_messages()))
            if self.config.get("history_enabled", True) or self.config.get("group_isolation", True)
            else []
        )
        group_id = event.get_group_id()
        group_key = f"{event.get_platform_id()}:GroupMessage:{group_id}" if group_id else ""
        group_name = getattr(getattr(event.message_obj, "group", None), "group_name", "") or ""
        for index, (component, reference) in enumerate(references):
            try:
                data = await self.reader.read(reference)
                await asyncio.to_thread(
                    self.history.ingest,
                    data,
                    reference,
                    archive=bool(self.config.get("history_enabled", True)),
                    group_key=group_key,
                    group_name=group_name,
                    received_scope=event_destination(event),
                    meme_hint=event_meme_hint(
                        component, reference, event, index=index, total=len(references)
                    ),
                )
            except Exception as exc:
                self.logger.debug("[ImageHistory] 图片收集跳过 (%s)", type(exc).__name__)

    async def _history_watch(self):
        try:
            while True:
                await asyncio.sleep(120)
                if self.config.get("history_enabled", True) or self.config.get(
                    "group_isolation", True
                ):
                    self.history_api.start_scan()
        except asyncio.CancelledError:
            pass

    @filter.on_decorating_result(priority=-1000)
    async def filter_result(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result:
            return
        state = self.interceptor.state(event)
        if not (state and state.blocked) and await self.guard.check(
            result, destination=event_destination(event)
        ):
            state = self.interceptor.reject(event)
        if state and state.blocked:
            notice = self._notice()
            result.chain = notice.chain if notice else []

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("禁图", alias={"imageguard"})
    async def manage(self, event: AstrMessageEvent):
        """禁图 状态/列表/添加/解除/检查。添加可带图、引用图或提供本机路径。"""
        # Defence in depth: the command is never exposed as an LLM tool.
        if not event.is_admin():
            yield event.plain_result("只有 AstrBot 管理员可以管理禁发图片。")
            return
        parts = event.message_str.strip().split(maxsplit=2)
        action = parts[1] if len(parts) > 1 else "状态"
        argument = parts[2].strip().strip('"') if len(parts) > 2 else ""
        try:
            if action in {"状态", "status"}:
                external = await self.guard.external_hashes()
                yield event.plain_result(
                    f"图片禁发：{'开启' if self.config.get('enabled', True) else '关闭'}\n"
                    f"本地禁发 {len(self.store.entries())} 张，表情黑名单 {len(external)} 张\n"
                    f"本次运行拦截 {self.guard.block_count} 次\n"
                    "管理：/禁图 添加（带图/引用图/文件路径）、/禁图 列表、/禁图 解除 编号"
                )
            elif action in {"列表", "list"}:
                entries = self.store.entries()
                lines = [f"{row['sha256'][:12]}：{row['reason']}" for row in entries[:50]]
                yield event.plain_result(
                    "\n".join(lines) or "本地禁发列表为空；表情包黑名单仍可自动生效。"
                )
            elif action in {"解除", "remove"}:
                self.store.remove(argument)
                yield event.plain_result(
                    "已解除本地禁发。若原表情包插件仍将其拉黑，发送仍会被拦截。"
                )
            elif action in {"添加", "add", "检查", "check"}:
                refs = list(image_references(event.get_messages()))
                source = argument or (refs[0][1] if refs else "")
                if not source:
                    yield event.plain_result("请带上/引用一张图片，或提供该图片在本机的文件路径。")
                    return
                data = await self.reader.read(source)
                item = await asyncio.to_thread(fingerprint, data)
                if action in {"添加", "add"}:
                    identifier = self.store.add(item)
                    yield event.plain_result(
                        f"已禁止发送图片，编号：{identifier}。原始文件未删除。"
                    )
                else:
                    reason = self.store.match(
                        item,
                        int(self.config.get("similarity_distance", -1)),
                        variants=self.config.get("protect_format_variants", True),
                    )
                    if item.sha256 in await self.guard.external_hashes():
                        reason = "表情包黑名单命中"
                    yield event.plain_result(f"检查结果：{reason or '未命中禁发规则'}")
            else:
                yield event.plain_result("用法：/禁图 状态、列表、添加、解除 编号、检查")
        except Exception as exc:
            self.logger.warning("[ImageGuard] 管理操作失败 (%s)", type(exc).__name__)
            yield event.plain_result("操作失败，请检查编号、图片格式、路径和大小；详细类型见日志。")

    async def terminate(self):
        self.interceptor.uninstall()
        self.guard.active = False
        if self.history_loop:
            self.history_loop.cancel()
            await self.history_loop
        if self.history_api.scan_task:
            await self.history_api.scan_task
        if hasattr(self.context, "registered_web_apis"):
            self.context.registered_web_apis[:] = [
                route
                for route in self.context.registered_web_apis
                if not route[0].startswith("/astrbot_plugin_image_guard/")
            ]
        await self.reader.close()
        self.history.close()
        self.store.close()
