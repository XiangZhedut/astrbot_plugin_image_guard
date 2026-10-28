# Changelog

## 2.0.0

- Upgrade to Image History Manager with an authenticated, date-grouped gallery in AstrBot extensions.
- Index existing SQLite AI/platform history and local temporary images without sending messages.
- Add retained local copies, source/context association, filtering, pagination and batch actions.
- Add targeted history detachment, recovery journals, reversible file trash and confirmed permanent cleanup.
- Preserve updated chat records and changed files during recovery; keep the existing deny-list database and commands.
- Add a non-sending rule inspector and synthetic regression tests for history/file operations.

## 1.0.0

- Add image fingerprint checks before framework message delivery.
- Cover generic message tool, direct event sends, streaming and proactive sends.
- Add administrator-only local deny-list commands and optional Meme Stealer blacklist sync.
- Add bounded local/base64/remote media inspection, privacy-preserving logging and reversible wrappers.
# 2.1.0

- 增加来源群聊筛选、本地群名备注，合并 QQ 官方适配器同群不同成员会话；同图可关联多个群。
- 增加整图相似候选分组、详情相似图查找和按组当前页批量整理，保留每个版本的原始上下文。
- 已禁发相册展示继承规则的转码版本、待核对候选及来源规则；解除来源规则需确认影响范围。
- 默认开启比例、边缘与 RGB 指纹联合转码保护，补全可读取的旧规则；实际内容嗅探阻止修改附件扩展名跳过检查。
- 保留已有数据和文件，不自动删除或合并图片；增加转格式、缩放、透明度、方向、群聊归属及权限回归测试。

# 2.2.0

- 默认开启跨群/跨会话图片发送隔离，实际收到图片的会话才有发送权限；未知来源和未知目标拒发。
- 以平台用户图片组件或对话历史的用户图片块恢复接收来源，不以 AI 输出、工具参数、文字路径或相册显示群聊授予权限。
- 检查事件回复、主动发送、流式消息和工具指定目标；父级方法改发到另一会话时重新检查，避免嵌套包装跳过检查。
- 详情显示已确认接收会话；面板可按所选群聊离线测试隔离规则。保留跨群禁发规则与原有图片数据。

# 2.2.1

- 图片拦截返回明确的不可重试终止结果，不再把下层适配器拒发误报为工具发送成功。
- 本轮事件记录拒发状态，阻断后续发消息工具重试，普通/流式后续回复替换为固定反馈并抑制重复提示。
- 增加模型发送约束，替换本轮最终回复和最终助手历史中的重试建议；不修改已有旧历史，不影响新的正常事件。
- 说明本地 SHA-256、像素哈希、dHash、RGB 缩略颜色和群聊/日期归纳算法，无视觉模型调用。

# 2.3.0

- 增加默认关闭的「收纳表情包」开关，支持相册顶部即时保存及插件配置，历史扫描与新消息统一执行。
- 参考平台表情标记、商城 CDN 元信息，并只读复用本机表情包小偷已入库记录；不新增模型请求，不按尺寸或 GIF 格式武断过滤。
- 关闭时普通相册和相似整理隐藏表情，不自动新增其完整副本；保留最小安全指纹/来源，不删除已有图片，不削弱禁发和跨群隔离。
- 新增表情包分类、批量手动标记/纠正及恢复自动判定；保留/禁发/回收站始终可管理，明确保留仍可存副本。

