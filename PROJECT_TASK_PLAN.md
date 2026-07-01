# Baidu Chat OpenAI Gateway Task Plan

更新日期：2026-06-30

## 2026-06-30 本轮进展

- 已重新读取并索引 `百度文心对话系统分析记录.md`，确认后续开发应持续复用其中的抓包结论：
  - `conversation` 是文本、图片、去水印、长文、直接回答的统一入口。
  - `rank/re_rank/sessionId/qid/pkgId` 是会话绑定和排障的核心字段。
  - 图片 URL 来自 `image-generate.items[].originUrl/previewUrl` 或 `imageScroll`。
  - 长文内容来自 `editor-workspace-viewer.updateFile.content`，完整内容也可从 `canvas/search.data.fileMeta.content` 获取。
  - 文档导出链路为 `canvas/search` -> `download`。
  - 思考过程来自 `thinkingSteps`，`thinkMode:"1"` 只是必要条件，不保证每次返回思考内容。
- 已完成会话管理列表第一轮增强：
  - 默认每页 20 条。
  - 支持关键词搜索。
  - 支持单条删除。
  - 支持批量删除。
  - 支持一键清空全部会话和轮次记录。
  - 页面中文说明重写，移除原模板乱码。
  - 列表展示本地会话、百度窗口、模型、凭证、Cookie 绑定策略、轮次、状态、错误和最近活跃时间。
- 已修复项目 `.venv` 依赖损坏问题：
  - pip 包目录损坏，已通过 `ensurepip` 恢复。
  - `uvicorn`、`starlette` 等包文件缺失，已按 `requirements.txt` 强制重装。
  - 当前服务已可通过 `.venv` 启动，`/v1/health` 返回 `{"status":"ok"}`。
- 已验证：
  - `python -m compileall app` 通过。
  - `conversations.html` 模板加载通过。
  - `/admin/conversations` 测试请求返回 200。

## 2026-06-30 会话绑定稳定性增强

- 已加固绑定模式的并发保护：
  - 新增进程内会话锁，同一个本地会话 ID 同时只允许一条请求进入百度上游。
  - 锁覆盖本地绑定记录读取/创建、rank 计算、百度请求、会话状态更新这一整段流程。
  - 支持 `/v1/chat/completions` 非流式和流式。
  - 支持 `/v1/responses` 流式；非流式 Responses 复用 Chat Completions 路径。
- 已明确当前绑定复用策略：
  - 如果首轮使用凭证池，绑定会话保存 `credential_id`，后续轮次固定使用同一个凭证 Cookie。
  - 如果首轮走匿名自动获取，绑定会话保存百度首页下发后的匿名 Cookie 快照，后续轮次继续复用该 Cookie 快照。
  - 如果固定凭证被删除或停用，本轮会失败并写入会话错误和系统日志，不会静默换 Cookie 导致串窗口。
- 已增强排障日志：
  - 绑定准备失败写入 `conversation_binding` 系统日志。
  - 百度上游请求失败写入 `conversation_binding` 系统日志。
  - 绑定凭证缺失、停用、加载失败写入 `credential` 系统日志。
- 已验证：
  - `python -m compileall app` 通过。
  - 应用导入通过。
  - 本地服务重启成功，`/v1/health` 返回 `{"status":"ok"}`。

## 2026-06-30 日志与排障能力增强

- 已重写后台日志页 `/admin/logs`：
  - 修复原页面中文乱码。
  - 新增请求总数、失败请求、系统告警/错误、后台操作数量统计。
  - 支持关键词搜索：
    - `request_id`
    - trace 片段
    - API Key
    - 来源 IP
    - 接口路径
    - 模型
    - 错误内容
    - 系统日志模块/消息
    - 操作审计内容
  - 请求日志展示输入字符、输出字符、耗时、状态码和错误。
  - 系统日志展示模块、级别和详细消息。
  - 操作审计展示后台操作记录。
- 已新增 `traced_system_log`：
  - 系统日志可以带 `trace=<request_id>` 前缀。
  - 工具调用解析日志已关联当前请求 `request_id`。
  - 会话绑定上游失败日志已关联当前请求 `request_id`。
- 已保留 CSV 导出和按保留天数清理旧日志功能。
- 已验证：
  - `python -m compileall app` 通过。
  - `/admin/logs` 返回 200。
  - `/admin/logs?q=tool_call` 返回 200。
  - 本地服务健康检查返回 `{"status":"ok"}`。

## 当前判断

项目已经具备第一版可运行能力：OpenAI 兼容接口、百度 Web 端适配、后台管理、凭证池、会话绑定、工具调用桥接、日志统计等都已经有基础实现。

接下来不应该继续零散补丁式修改，而应该按模块推进：每个模块先定位问题原因，再设计策略，再修改代码，最后用本地服务和第三方客户端做验证。

## 已完成基础能力

- 本地 FastAPI 服务可启动，健康检查 `/v1/health` 正常。
- 已支持 OpenAI 风格接口：
  - `GET /v1/models`
  - `POST /v1/chat/completions`
  - `POST /v1/chat/completions` stream
  - `POST /v1/responses`
  - `POST /v1/responses` stream
  - `POST /v1/images/generations`
  - `POST /v1/files`
- 已有后台管理：
  - 系统配置
  - 限流安全
  - 模型配置
  - 凭证池
  - 会话管理
  - API Keys
  - 内置提示词
  - 工具调用配置
  - 日志统计
- 已有凭证池能力：
  - Cookie 文本 / Cookie JSON 解析
  - 启用 / 停用
  - 健康检测
  - 使用次数统计
  - 失败次数和自动停用阈值
  - 自动模式 / 仅凭证池 / 仅匿名获取
- 已有会话绑定第一版：
  - 无状态模式
  - 绑定模式
  - 混合模式
  - 本地会话和百度 `sessionId` 映射
  - 绑定会话固定凭证或匿名 Cookie 快照
- 已有工具调用第一版：
  - 接收 OpenAI `tools/tool_choice`
  - 将百度输出的 DSML / JSON 工具调用转换为 OpenAI `tool_calls`
  - 支持工具结果 `role=tool` 回填
  - 支持 Tokeny 风格 DSML 变体解析
  - 支持工具调用缓冲和失败兜底配置

## 当前核心问题

### 1. 会话绑定稳定性仍需加强

现象：

- 绑定模式下第三方客户端有时仍表现为上下文丢失。
- Baidu 网页端可能显示“抱歉，内容已不可见”，导致即使复用同一 `sessionId`，百度侧也无法看到前文。
- Tokeny 一类 Agent 客户端可能发起多个内部子会话，导致一个客户端窗口对应多个后端请求链。

原因判断：

- 百度 Web 端自身对匿名历史和不可见内容有不稳定性。
- 当前项目主要依赖百度侧窗口记忆，没有完整的本地上下文兜底。
- 不同客户端传递会话标识的方式不同，当前 `conversation_id` 识别策略还不够丰富。
- 同一会话并发请求时，可能发生 rank/session 竞争。

需要完善：

- 增加本地上下文记忆策略：仅百度绑定、仅本地拼接、混合增强。
- 为绑定模式增加同一会话并发锁。
- 后台会话列表增加分页、搜索、批量删除、一键清空。
- 会话详情展示每轮请求的客户端来源、模型、凭证、百度 session、rank、qid、请求摘要和响应摘要。
- 支持客户端 Profile 对话识别规则，例如 Tokeny、Cherry Studio、Cline、Chatbox。

### 2. 工具调用兼容性仍需继续验证

现象：

- Tokeny 等 Agent 客户端中，百度有时把 DSML 工具调用作为普通正文输出。
- 长文本工具调用在流式输出时更容易泄漏半截标签。
- 工具调用后可能进入重复创建文件、重复读取文件等循环。

原因判断：

- 百度模型不是原生 OpenAI tool calling，工具调用依赖提示词和文本解析，天然不稳定。
- 不同客户端对 tool_calls、流式 tool_calls、reasoning、XML/DSML 的兼容要求不同。
- 当前工具循环保护只做了基础处理，还需要按客户端和工具类型细化。

需要完善：

- 工具调用链路增加更完整日志：原始片段、解析结果、失败原因、是否兜底。
- 后台工具配置增加客户端 Profile 细节配置。
- 对 Tokeny 做专项适配和回归测试。
- 增加工具循环保护规则：
  - 同一工具 + 同一参数重复次数限制。
  - 工具成功后是否强制最终总结。
  - 工具失败后是否允许模型重试。
- 支持请求带 tools 时强制内部非流式，但对客户端仍可返回标准流式 tool_calls。

### 3. 后台 UI 和管理体验需要系统性优化

现象：

- 部分页面布局仍显粗糙。
- 部分配置说明不够中文化。
- 有些列表操作还不够直观，例如会话批量管理、模型编辑、API Key 限额设置等。

需要完善：

- 统一后台视觉和表单交互。
- 列表页使用分页、搜索、筛选、批量操作。
- 新增 / 编辑统一使用弹窗或抽屉。
- 配置项说明全部改成清晰中文。
- 高风险操作增加确认提示。
- 表格中敏感信息脱敏展示。

### 4. 日志和排障能力需要增强

现象：

- 有些失败没有明显出现在后台请求日志。
- 上游百度错误、工具解析错误、会话绑定错误、凭证调度错误之间还没有完全分层。

需要完善：

- 新增上游请求日志表或调试日志模块。
- 每次请求生成统一 trace_id。
- 请求日志关联：
  - API Key
  - 来源 IP
  - 客户端 Profile
  - 本地会话 ID
  - 百度 sessionId
  - 凭证 ID
  - 工具调用 ID
- 后台支持按 trace_id 搜索。
- 日志脱敏策略继续完善。

### 5. 文件和图片能力需要产品化

现状：

- 已经能解析图片 URL 和文档下载 URL。
- 但还没有完整本地代理、缓存、过期清理、文件列表管理。

需要完善：

- 图片 / 文档代理下载接口。
- 本地缓存目录和自动清理策略。
- 后台文件列表和手动清理。
- 可选对象存储接口：S3 / MinIO。
- 图片返回策略：
  - 直接返回百度链接
  - 返回本项目代理链接
  - 返回 Markdown 图片
  - 返回 OpenAI images 格式

### 6. 部署和运维还需要补齐

需要完善：

- Linux 一键部署脚本继续验证。
- systemd 自启动脚本验证。
- Docker / docker-compose。
- 数据库备份和恢复。
- 配置导入导出。
- 更新脚本。

## 优先级计划

### P0：先修稳定性和可观测

1. 会话管理列表补全：
   - 默认 20 条分页
   - 关键词搜索
   - 单条删除
   - 批量删除
   - 一键清空
   - UI 优化

2. 会话绑定稳定性：
   - 绑定模式固定凭证复用再检查
   - 匿名 Cookie 快照复用再检查
   - 同一会话并发锁
   - 会话详情中展示凭证和 Cookie 策略

3. 日志增强：
   - 请求失败必须落库
   - 上游异常必须可见
   - 工具解析失败必须可见
   - 会话绑定准备失败必须可见

### P1：工具调用专项适配

1. Tokeny 客户端专项：
   - 确认请求中的 tools 格式
   - 确认流式和非流式返回要求
   - 测试 list/read/modify 文件链路

2. 工具调用配置增强：
   - 客户端 Profile 独立配置
   - 重复工具调用限制
   - 工具结果后策略
   - 解析失败兜底策略细化

3. OpenAI tool_calls 兼容：
   - Chat Completions 非流式
   - Chat Completions 流式
   - Responses 非流式
   - Responses 流式

### P2：后台 UI 系统优化

1. 会话管理页面重做。
2. 日志统计页面重做。
3. 系统配置页面中文说明补全。
4. 模型配置编辑体验优化。
5. API Key 限额和权限编辑体验优化。

### P3：文件、图片、部署增强

1. 图片和文件本地代理缓存。
2. 文件管理后台。
3. Docker / docker-compose。
4. 部署脚本二次验证。

## 接下来需要确认的问题

这些问题不阻塞 P0，但会影响后续设计：

1. Tokeny 客户端是否会在请求里传稳定的 conversation/session/thread 标识？
2. Tokeny 的 tools 字段是否完全兼容 OpenAI 格式，还是有自己的变体？
3. 用户希望本地保存完整对话内容吗？如果保存，需要默认脱敏还是完整保存？
4. 绑定模式下，当百度侧内容不可见时，是否允许项目自动把本地历史摘要拼回给百度？
5. 工具调用是否只由客户端执行，还是未来希望项目服务端也能执行部分工具？

## 后续执行规则

每次开始改代码前按以下顺序执行：

1. 先说明要解决的问题和判断原因。
2. 标明会改哪些文件。
3. 修改代码。
4. 运行最小验证。
5. 更新本文档或 `PROJECT_STATUS.md`。
6. 告知用户结果、风险和下一步。
## 2026-06-30 Tokeny 工具调用适配进度

本轮根据 Tokeny 测试现象做了专项修复：

- 已确认 Tokeny 请求会进入项目，项目日志中已有多条 `tool_call` 解析记录。
- 新增 `tokeny` 客户端 profile，并把当前后台配置切换为 `tool_client_profile=tokeny`。
- 工具提示词增强：Tokeny 场景下要求模型严格使用客户端提供的工具名和参数名；文件操作默认使用相对路径；文件正文必须完整放入 `content` 参数内，不允许把 Markdown 泄露到 DSML 标签外。
- 修复工具结果后的协议提示逻辑：此前 `tool_loop_protection=true` 时，工具结果回来后不会继续追加工具协议提示，容易导致模型把 DSML 当作普通正文输出。现在改为仍追加工具协议，同时提示不要重复同一个成功工具调用。
- 增强工具调用日志：解析成功时记录工具名和参数摘要；解析失败时记录原始片段摘要，并写入会话回合预览，方便后台排查。
- 修复 Chat Completions 非流式路径的返回缩进问题，避免工具调用解析成功后没有正常返回标准 OpenAI 响应结构。
- 当前推荐 Tokeny 测试配置：`tool_call_mode=auto`，`tool_client_profile=tokeny`，`conversation_response_mode=buffered_stream`，`tool_force_final_after_result=false`，`tool_loop_protection=true`。

待继续观察：

- Tokeny 是否在请求体中稳定传递 OpenAI `tools` 字段，以及每个工具的参数 schema 是否固定。
- Tokeny 执行工具失败时回传的 `role=tool` 内容格式是否标准。
- Tokeny 的子智能体/记忆助手是否会传递可识别的会话 ID；如果没有，需要继续优化本项目的本地会话归属策略，避免多个内部 Agent 请求被错误合并或错误拆分。
- 若再次出现 DSML 正文泄露，需要查看后台 `tool_call` 日志中的 raw 片段，判断是未闭合标签、参数名不匹配，还是客户端未携带 tools。

## 2026-07-01 Tokeny 工具结果回传修复

本轮根据 Tokeny 读取目录后仍错误判断“章纲目录已有 10 个文件”的问题做了修复：

- 根因：绑定模式下 `conversation_message_strategy=smart` 会在已有百度 sessionId 时只提交最新用户消息，导致 `role=tool` 的真实工具结果被丢弃。百度侧只看到用户追问和旧上下文，没有看到 Tokeny 实际返回的 `0 directories, 4 files`。
- 修复：只要 OpenAI 请求最后一条消息是 `role=tool`，项目都会构造“工具结果上下文”提交给百度，明确提示工具结果是本轮最高优先级事实。
- 工具结果上下文包含：原始用户请求、上一轮工具调用 JSON、工具名称/ID、工具执行结果、是否允许继续下一步工具调用。
- 该修复不强制最终回答；当 `tool_force_final_after_result=false` 时，Tokeny 仍可以继续多轮 Agent 工具调用。
- 会话详情中的 `prompt_preview` 现在会刷新为最终发给百度的 query，方便后台确认工具结果是否真的传给百度。

验证方式：

- 在 Tokeny 中触发 `list_files path=章纲` 后，查看后台会话详情。
- 下一轮 prompt 应包含类似 `工具执行结果：... 0 directories, 4 files`。
- 百度回答应以工具结果为准，而不是继续引用旧计划中的 10 个章纲。

## 2026-07-01 Tokeny 工具参数类型与空响应兜底修复

本轮针对 Tokeny 测试中出现的 `read_file 当前不可用: Received tool input did not match expected schema`、`tasks.todos Expected array, received string` 和“服务端返回空响应，正在重试”问题做了专项修复：

- 修复 DSML 转 OpenAI `tool_calls` 时的参数类型问题：项目会根据客户端传入的 `tools[].function.parameters` schema，把 `"1"` 转成数字 `1`，把 `"true"/"false"` 转成布尔值，把 JSON 字符串数组/对象转成真正的数组/对象。
- 该修复覆盖 `read_file.startLine/endLine`、`tasks.todos` 等 Tokeny 常见工具参数，避免客户端因为参数类型不匹配而拒绝执行工具。
- 新增 `baidu_empty_response_retry=true` 默认设置：当百度上游返回 HTTP 200 但没有正文、图片或工具调用时，绑定/混合模式会自动清空当前百度 sessionId，并用同一请求重试一次。
- 如果重试后仍然为空，项目会返回明确的中文提示，而不是继续给客户端返回空 SSE/空内容，避免 Tokeny 进入连续 1/5 到 5/5 的空响应重试循环。
- 修复范围覆盖 `/v1/chat/completions` 非流式、`/v1/chat/completions` 流式、`/v1/responses` 流式；`/v1/responses` 非流式复用 Chat Completions 路径。

建议继续观察：

- Tokeny 是否还会把某些工具参数以非标准字段名传入，例如 `path` vs `filePath`。如果出现，需要继续增加字段别名映射。
- 百度侧空响应如果频繁出现，需要在会话详情里查看是否集中发生在同一个百度窗口、同一个凭证或同一个长上下文任务。
- 如果重置百度窗口后上下文丢失，需要结合本地会话摘要回填策略继续增强，而不是完全依赖百度网页端历史。

## 2026-07-01 Tokeny UTF-8 写文件与乱码拦截增强

本轮针对 Tokeny 写入 Markdown 文件后出现 `绗�5绔...` 这类中文乱码的问题做了专项增强：

- 在 Tokeny profile 的工具提示词中加入写文件编码要求：调用 `modify_file` 时，`content` 必须是正常 UTF-8 中文文本，禁止输出 `绗/鍙/涓/鎴/锛/銆/�` 等疑似转码乱码。
- 增加项目侧乱码检测：当 `modify_file.content` 疑似 mojibake/中文转码乱码时，项目不会把该工具调用下发给客户端执行，避免坏内容落盘。
- 增加工具 fallback 保护：如果模型返回的工具调用文本本身带大量乱码特征，项目返回“工具调用已被项目拦截”的明确提示，而不是把乱码正文透传给客户端。
- 增加工具日志：当工具输出因乱码检测被拦截时，系统日志会记录 `blocked mojibake tool output`，便于后台定位。
- 已验证：模拟乱码内容会被拦截；正常中文 `modify_file` 内容不会被误拦截。

同时再次核实无状态模式代码路径：

- `/v1/chat/completions` 会先把客户端传入的 `body.messages` 转为 `messages`。
- 无状态模式下 `binding=None`，`_build_query_for_request()` 会执行 `adapter.build_query(messages)`。
- 因此无状态模式提交给百度的是“客户端本次请求传来的完整 messages”，不是只提交最新一条。
- 如果百度侧仍然表现为丢上下文，主要原因应检查客户端本次是否真的传了完整历史、是否压缩/裁剪了工具结果，或项目是否需要新增请求体调试日志来证明客户端实际传入内容。

## 2026-07-01 Tokeny 工具循环根因修复

本轮根据 Tokeny 截图和后台日志定位到工具循环的两个直接根因：

- 百度模型在无状态 Agent 长任务中反复输出相同 `read_file` 工具调用，例如连续读取 `章纲/第05章_身份疑云.md`。Tokeny 能检测到重复调用并弹窗，但项目此前没有提前拦截。
- 百度返回的 DSML 偶尔参数结构不规范，出现 `modify_file.filePath` 粘连 `write# 第5章...正文` 的情况，项目此前解析后可能把异常工具调用下发给客户端。

已完成修复：

- 新增工具调用指纹：从客户端本次请求的历史 `assistant.tool_calls` 中提取最近工具调用指纹。
- 当百度本轮再次输出相同工具名和相同参数时，项目会拦截该 tool_call，返回“检测到重复工具调用”的自然语言提示，不再交给 Tokeny 执行。
- 新增 `modify_file.filePath` 结构校验：如果路径包含换行、大段正文、`.mdwrite`、标题正文等异常特征，项目会阻止该工具调用。
- 修复覆盖 Chat Completions 流式/非流式与 Responses 流式；Responses 非流式复用 Chat Completions 路径。
- 已验证：重复 `read_file` 会被过滤；坏 `modify_file.filePath` 会被拦截；正常 `modify_file` 不受影响。

后续观察：

- 如果 Tokeny 没有把上一轮 `assistant.tool_calls` 带回请求，项目无法识别“重复工具调用”。这种情况需要开启请求体调试日志，确认客户端实际传入内容。
- 如果模型需要重新读取同一文件的不同范围，应使用不同参数如 `startLine/endLine`，不会被判定为完全重复。
