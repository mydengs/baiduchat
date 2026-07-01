# 2026-06-27 Agent Multi-step Tool Fix
- Root cause: `tool_loop_protection=true` previously disabled tool-call parsing after a client returned `role=tool`. For agent clients such as Tokeny, Baidu may legitimately ask for another tool after reading a tool result; that DSML was leaked as normal text.
- Fixed Chat Completions and Responses logic: after `role=tool`, parsing remains enabled unless `tool_force_final_after_result=true`.
- `tool_loop_protection` now only controls whether the proxy appends an extra tool prompt after tool results; it no longer blocks parsing/conversion of model-emitted tool calls.
- Verified `read_file` DSML after a tool result can still be converted into standard OpenAI `tool_calls` when final-answer forcing is disabled.

# 2026-06-27 Tokeny / Response Mode Update
- Added `conversation_response_mode` setting in conversation management:
  - `client`: follow the client request.
  - `stream`: force SSE streaming.
  - `buffered_stream`: keep SSE protocol but internally buffer and emit once, useful for Tokeny/Cherry tool-call leakage.
  - `non_stream`: force normal JSON even when the client sends `stream=true`.
- Wired `conversation_response_mode` into `/v1/chat/completions` and `/v1/responses`.
- Added `force_buffer_all` support for streaming Chat Completions and Responses.
- Expanded DSML parser compatibility for Tokeny-style markup:
  - `<|DSML tool_calls>`
  - `<|DSML invoke ...>`
  - `<|DSML parameter ...>`
  - `</DSML parameter>`, `</DSML invoke>`, `</DSML tool_calls>`
- Verified Tokeny-style `modify_file` DSML can be parsed into standard OpenAI `tool_calls`.

# 项目完成状态

## 已完成

- OpenAI 兼容接口：
  - `GET /v1/models`
  - `POST /v1/chat/completions`
  - `POST /v1/chat/completions` with `stream=true`
  - `POST /v1/responses`
  - `POST /v1/responses` with `stream=true`
  - `POST /v1/images/generations`
  - `POST /v1/files`
  - Chat Completions 的思考过程已改为 `reasoning_content` 字段输出，避免污染普通正文。
  - `chat/completions` 已接入第一版客户端工具调用桥接：接收 `tools/tool_choice`，识别百度输出的 DSML/JSON 工具调用文本，并转换为 OpenAI `tool_calls`；工具执行结果 `role=tool` 会转成百度可理解的上下文。

- 百度网页端适配：
  - 首页 `aiTabFrameBaseData.token/lid` 解析。
  - `chat_token` 生成。
  - `conversation` SSE 调用。
  - 文本、思考、图片、去水印、长文工作区组件解析。
  - `canvas/search` 与 `download` 服务层方法。
  - `conversation` 已支持传入百度长期窗口 `sessionId` 和指定 `rank/re_rank`。
  - SSE 解析已提取 `lid`、`qid`、`sessionId`、`pkgId`、`seq_id`。

- 管理后台：
  - 系统配置编辑、导入、导出。
  - 限流、安全与输出策略配置。
  - 模型配置新增、编辑、启停。
  - 凭证池新增、编辑、启停、健康检测。
  - 凭证池支持粘贴浏览器 Cookie JSON 并自动转换为 Cookie 请求头格式。
  - 凭证池累计使用次数、今日使用次数、最近使用时间统计。
  - 凭证调度模式：自动模式、仅凭证池、仅匿名获取。
  - 凭证连续失败达到阈值后自动停用，阈值可配置。
  - API Key 创建、编辑、启停。
  - API Key 模型权限、请求次数额度、Token/字符额度、IP 白名单/黑名单。
  - 全局 IP 白名单、黑名单。
  - 全局按 API Key 每分钟限流。
  - 内置提示词开启、关闭与推送模式。
  - 请求日志、系统日志、操作审计。
  - 请求日志 CSV 导出与按保留天数清理。
  - 后台密码修改。
  - API Key、模型配置、凭证池页面已使用弹窗式新增/编辑。
  - 左侧导航已修复为中文。

- 会话管理：
  - 新增后台“会话管理”页面。
  - 支持三种模式：
    - 无状态模式：保持当前稳定逻辑，每次只依赖客户端 `messages[]`。
    - 绑定模式：维护第三方客户端会话与百度 `sessionId` 的映射。
    - 混合模式：客户端传入会话标识时绑定，否则无状态。
  - 支持会话过期时间、最大轮次、无会话标识时自动生成绑定会话、是否保存内容预览。
  - 客户端未传 `conversation_id` 时新增可配置策略：`smart` 使用首条用户消息 hash 派生本地会话，适配 Cherry Studio 新窗口；`strict` 要求客户端显式传入；`ephemeral` 每次新建；`fallback` 保留旧的 API Key+IP+模型逻辑。
  - 新增 `baidu_conversations` 表，记录本地会话 ID、百度 `sessionId`、最后 `qid`、模型、凭证、轮次、来源 IP、API Key、状态。
  - 新增 `conversation_turns` 表，记录每一轮请求的 `rank`、`qid`、`sessionId`、`pkgId`、耗时、状态、请求/响应预览。
  - 绑定模式下会固定凭证：如果首轮使用凭证池，会话会保存 `credential_id` 并在后续轮次继续使用同一 Cookie；如果首轮走匿名自动获取，会保存百度下发后的匿名 Cookie 快照，后续轮次继续复用，避免跨 Cookie 导致百度窗口失联。
  - 支持后台查看会话列表、会话详情、打开百度网页窗口、重置绑定、删除会话。
  - OpenAI `/v1/chat/completions` 和 `/v1/responses` 已接入会话绑定逻辑。
  - 流式 `/v1/chat/completions` 和 `/v1/responses` 已补充请求日志记录，绑定准备失败、上游失败等情况会写入后台请求日志。

- 部署与运行：
  - `.env.example`
  - `README.md`
  - Linux `scripts/install.sh`
  - Windows 开发脚本 `scripts/run_dev.ps1`
  - systemd 自启动脚本生成。

## 部分完成

- 多凭证池：
  - 已支持多凭证维护、启停、检测、使用统计和基础自动选择。
  - 已支持失败跳过、自动停用和匿名回退。
  - 尚未实现并发锁、失败熔断窗口、权重分配。

- Token 额度：
  - 当前按 `prompt_chars + completion_chars` 作为 Token/字符额度近似统计。
  - 尚未接入真实 tokenizer 或百度真实 token usage。

- 文件与图片：
  - 已能解析图片 URL 和文档导出 URL。
  - 尚未实现本地代理缓存、缓存清理、S3/MinIO。
- 工具调用：
  - 已实现 Chat Completions 的基础工具调用转换，第三方客户端可根据 `tool_calls` 自行执行工具。
  - 已补充 DSML 兜底识别：即使客户端没有按 OpenAI `tools` 字段传工具，只要百度输出中出现 DSML `tool_calls/invoke` 标记，也会尝试转换为 `tool_calls`，避免原样污染正文。
  - 已兼容 DSML 闭合标签变体，例如 `</DSML| parameter>`、`</DSML| invoke>`，避免百度模型输出不标准标签时解析失败。
  - 已提前拦截流式输出中的半截 DSML 标记：一旦响应疑似以 `<|DSML| tool_calls>` 开始，就先缓冲到完整响应结束后再转换，避免半截工具调用泄漏到正文。
  - 已补充工具结果回填保护：当客户端返回 `role=tool` 结果后，本轮不再附加工具提示，也不再把模型输出转换成新的工具调用，避免创建文件等操作陷入重复执行循环。
  - Responses API 的非流式工具调用已做基础映射；流式 Responses 的完整 function_call 事件仍需继续增强。
  - 当前不在服务端直接执行命令或本地工具，默认遵循 OpenAI 客户端工具调用模式：模型提出调用，客户端执行，再把 `role=tool` 结果发回。

- 会话管理：
  - 已实现第一版可用的会话绑定与后台查看。
  - 后续需要继续增强客户端识别规则、会话搜索筛选、按客户端类型适配、并发请求保护、跨凭证会话保护。

- 日志：
  - 已有请求日志、系统日志、操作审计。
  - 尚未拆分独立上游请求/响应调试日志表。

- 后台 UI：
  - 已重做基础视觉样式和部分核心页面。
  - 仍需继续统一概览、限流安全、内置提示词、日志统计等页面的交互和视觉质量。

## 待继续完善

- DeepSeek-V4 Flash / DeepSeek-R1 等模型真实 `modelName` 抓包补充。
- `download.token` 的完整生成来源继续确认。
- docx/pdf 文件 URL 的实际响应头验证。
- Docker / docker-compose。
- PostgreSQL 支持。
- S3/MinIO 对象存储支持。
- 后台多管理员账号与权限分级。
- 会话管理增加搜索、筛选、批量清理、导出、按 API Key/IP/模型聚合统计。
- 客户端会话 ID 适配策略：Cherry Studio、Open WebUI、LobeChat 等分别验证。
- 长期绑定模式下的并发锁，避免同一会话同时发多条导致 rank/session 竞争。
- 单张图片编辑、下载、再次去水印独立接口继续抓包。
# 2026-06-26 Tool Calling Update
- Added admin route and page: `/admin/tools`.
- Added configurable tool call strategy:
  - `tool_call_mode`: `auto`, `force_buffer`, `stream_compat`, `off`.
  - `tool_client_profile`: `auto`, `openai`, `cherry`, `cline`, `chatbox`, `openwebui`, `lobe`, `hermes`.
  - `tool_buffer_timeout_ms`, `tool_max_buffer_chars`, `tool_parse_retries`.
  - `tool_parse_failure_strategy`: `clean_text`, `error`, `raw_text`.
  - `tool_loop_protection`, `tool_force_final_after_result`.
- Tool calls are now buffered whenever a request includes `tools`, or when the stream looks like DSML/tool markup, so partial DSML is no longer leaked as normal text.
- Added DSML normalization for variants such as `</DSML| parameter>` and `</DSML| invoke>`.
- Added parse retry path and clean fallback for malformed tool markup.
- Added loop protection for `role=tool` follow-up messages: after the client executes a tool, the proxy asks Baidu for a final natural-language answer instead of triggering the same tool again.
- Extended the same buffering/parsing behavior to `/v1/responses` streaming output.
- Added tool-call parse success/failure system logs under module `tool_call`.
