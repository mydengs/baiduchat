import secrets

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password, sha256_token
from app.db.models import ApiKey, AppSetting, ModelConfig, PromptConfig
from app.db.session import Base, engine


DEFAULT_SETTINGS = {
    "admin_password_hash": ("", "后台管理员密码哈希值。请通过“修改后台密码”表单更新，不建议手动编辑。"),
    "log_request_body": ("false", "是否记录下游客户端请求正文。开发排查时可开启，可能包含敏感内容。"),
    "log_upstream_body": ("false", "是否记录百度上游请求或响应正文。仅建议排查接口问题时短期开启。"),
    "log_upstream_model": ("true", "是否记录每次请求百度 conversation 时使用的模型参数摘要，不包含 Cookie 和正文，建议开发排查时保持开启。"),
    "enable_request_logs": ("true", "是否开启请求日志与统计，包含来源 IP、模型、耗时、状态码和用量。"),
    "enable_system_logs": ("true", "是否开启系统运行日志，用于记录服务异常和关键流程。"),
    "baidu_base_url": ("https://chat.baidu.com", "百度文心网页端基础地址，通常保持默认值。"),
    "default_timeout": ("120", "请求百度上游接口的默认超时时间，单位为秒。"),
    "auto_direct_answer": ("false", "是否默认使用 ai_directans 直接回答模式，用于减少长文工作区或 Canvas 输出。"),
    "ip_whitelist": ("", "全局 IP 白名单，多个 IP 用英文逗号分隔；为空表示不限制。"),
    "ip_blacklist": ("", "全局 IP 黑名单，多个 IP 用英文逗号分隔。"),
    "rate_limit_per_minute": ("0", "全局每分钟请求上限，按 API Key 统计；0 表示不限制。"),
    "log_retention_days": ("30", "日志保留天数，日志清理功能会按此值删除旧记录。"),
    "output_reasoning": ("true", "是否向客户端输出模型思考过程 thinkingSteps。"),
    "output_image_urls": ("true", "是否向客户端输出生成图片链接。"),
    "long_text_strategy": ("stream_delta", "长文输出策略：stream_delta 表示输出工作区增量，summary 表示只输出普通总结。"),
    "proxy_file_downloads": ("false", "是否启用文件下载代理缓存；当前为预留开关。"),
    "mask_sensitive_logs": ("true", "是否在日志和后台展示中脱敏 Cookie、token、authorization 等敏感字段。"),
    "credential_mode": ("auto", "凭证调度模式：auto 自动模式优先凭证池、无可用凭证时匿名获取；pool 仅使用凭证池；anonymous 仅匿名从首页获取。"),
    "credential_failure_disable_threshold": ("3", "单个凭证连续失败达到该次数后自动停用；0 表示不自动停用。"),
    "conversation_mode": ("stateless", "会话模式：stateless 无状态；bound 绑定百度窗口；hybrid 有会话标识时绑定、否则无状态。"),
    "conversation_fallback_binding": ("false", "客户端没有传 conversation_id 时，是否用 API Key、来源 IP、模型和 user 自动生成绑定会话。"),
    "conversation_missing_id_strategy": ("smart", "客户端没有传 conversation_id 时的绑定策略：smart 使用首条用户消息派生；stable 使用 API Key+模型稳定绑定；strict 直接拒绝；ephemeral 每次新建；fallback 使用旧的 API Key+IP+模型。"),
    "conversation_ttl_hours": ("24", "绑定会话超过多少小时无请求后视为过期，后续自动新建百度窗口。"),
    "conversation_max_turns": ("50", "单个绑定会话最大轮次，超过后自动重置为新的百度窗口；0 表示不限制。"),
    "conversation_save_content": ("true", "是否保存会话请求和响应预览，用于后台排查；关闭后只保存 ID、状态和耗时。"),
    "conversation_message_strategy": ("smart", "绑定会话提交内容策略：smart 首轮完整、后续最新用户消息；latest_user_only 总是只发最新用户消息；full_messages 总是发送完整 messages。"),
    "conversation_response_mode": ("client", "客户端输出方式：client 跟随客户端；stream 强制流式；buffered_stream 流式请求内部缓冲后一次性返回；non_stream 强制非流式 JSON。"),
    "conversation_reset_on_model_change": ("true", "绑定/混合模式下同一本地会话切换模型时，是否重置百度窗口，避免复用旧窗口导致百度侧沿用旧模型。"),
    "conversation_max_query_chars": ("120000", "发给百度 conversation 接口的最大字符数；超过后保留最近上下文，避免百度返回 413。0 表示不限制。"),
    "conversation_max_query_scope": ("stateless_and_first_turn", "413 长度保护适用范围：all 全部请求；stateless_and_first_turn 无状态和绑定首轮；stateless_only 仅无状态；off 关闭。"),
    "baidu_empty_response_retry": ("true", "百度上游返回 200 但没有正文或工具调用时，绑定/混合模式下自动重置百度窗口并重试一次。"),
    "tool_call_mode": ("auto", "工具调用模式：auto 自动；force_buffer 请求带工具时强制缓冲；stream_compat 流式兼容；off 关闭工具调用解析。"),
    "tool_client_profile": ("auto", "客户端工具调用适配：auto、openai、cherry、cline、chatbox、openwebui、lobe、hermes。"),
    "tokeny_tool_result_compaction": ("true", "Tokeny 专用：工具结果返回后压缩为任务状态摘要，减少重复 schema 和历史工具结果导致的循环。仅 tool_client_profile=tokeny 时生效。"),
    "tokeny_compact_tool_schema_after_result": ("true", "Tokeny 专用：工具结果返回后只注入简化工具协议和工具名列表，不再每轮重复完整 tools schema。仅 tool_client_profile=tokeny 时生效。"),
    "document_output_strategy": ("auto", "文档/文件输出路由策略：auto 自动判断；client_tools 明确文件任务优先客户端工具；baidu_native 优先百度原生文档；text 普通文本输出。"),
    "baidu_native_document_policy": ("explicit_only", "百度原生文档能力策略：explicit_only 仅用户明确要求 Word/PDF/下载/百度文档时允许；allow 允许；deny 禁止。"),
    "tool_buffer_timeout_ms": ("60000", "工具调用流式缓冲超时时间，单位毫秒；当前主要用于配置展示和后续超时保护。"),
    "tool_max_buffer_chars": ("300000", "工具调用最大缓冲字符数，超过后根据失败兜底策略处理。"),
    "tool_parse_retries": ("1", "工具调用解析失败后的修复重试次数。"),
    "tool_parse_failure_strategy": ("clean_text", "工具调用解析失败兜底策略：clean_text 清理标签后按文本返回；error 返回错误；raw_text 原样返回。"),
    "tool_loop_protection": ("true", "工具结果回填后禁止同一轮继续触发工具调用，避免重复执行文件写入、命令等操作。"),
    "tool_force_final_after_result": ("true", "客户端返回 role=tool 工具结果后，强制要求模型生成最终自然语言回复。"),
    "tool_repeat_protection_enabled": ("false", "是否启用重复工具调用保护；Tokeny 等 Agent 客户端默认建议关闭，避免误伤批量文件操作。"),
    "tool_repeat_protection_scope": ("delete_move", "重复工具保护范围：delete_move 仅删除/移动/重命名；write 写入类；all 全部工具；off 关闭。"),
    "tool_repeat_match_mode": ("smart", "重复工具判定方式：smart 智能推进判断，允许分段读取/写入和批量不同目标；exact 完全相同参数；path 只看文件路径；none 不判定。"),
    "tool_arg_safety_enabled": ("true", "是否启用工具参数安全校验；用于阻止明显错误的文件路径或严重乱码内容。"),
    "tool_path_safety_enabled": ("true", "是否启用文件路径安全校验；仅检查换行、超长路径、路径中混入 write/append/markdown 标题等结构性异常。"),
    "tool_mojibake_safety_enabled": ("true", "是否启用工具内容乱码校验；仅对 modify_file.content 中严重疑似乱码的内容进行拦截。"),
    "tool_mojibake_markers": ("绗,绔,锛,涓,鍙,鎴,鍚,濂,鐨,杩,鏄,銆,撳,滄,蹭,犲,鐢,喉模,瘉缓,畸纡,撻唔,�", "工具内容乱码检测特征词，英文逗号或换行分隔；用于识别 UTF-8/GBK 误解码等 mojibake 内容。"),
}


DEFAULT_MODELS = [
    ("deepseek-v4-pro", "DeepSeek-V4", "0", "1", "DeepSeek-V4 Pro with thinking"),
    ("deepseek-v4", "DeepSeek-V4", "0", "0", "DeepSeek-V4"),
    ("ernie-5.1", "ERINE-5.1", "0", "0", "ERNIE 5.1"),
    ("smart", "smartMode", "0", "0", "Smart mode"),
    ("miaotu", "smartMode", "0", "0", "Image generation route"),
]


def get_setting(db: Session, key: str, default: str = "") -> str:
    item = db.get(AppSetting, key)
    return item.value if item else default


def set_setting(db: Session, key: str, value: str, description: str = "") -> None:
    item = db.get(AppSetting, key)
    if item:
        item.value = value
        if description:
            item.description = description
    else:
        db.add(AppSetting(key=key, value=value, description=description))


def init_db(admin_password: str | None = None) -> str | None:
    Base.metadata.create_all(bind=engine)
    migrate_db()
    settings = get_settings()
    created_key: str | None = None

    with Session(engine) as db:
        for key, (value, description) in DEFAULT_SETTINGS.items():
            item = db.get(AppSetting, key)
            if item:
                item.description = description
            else:
                db.add(AppSetting(key=key, value=value, description=description))

        password_hash = get_setting(db, "admin_password_hash")
        if admin_password or not password_hash:
            set_setting(db, "admin_password_hash", hash_password(admin_password or settings.admin_password))

        for public_id, baidu_model, deep_search, think_mode, notes in DEFAULT_MODELS:
            exists = db.scalar(select(ModelConfig).where(ModelConfig.public_id == public_id))
            if not exists:
                db.add(
                    ModelConfig(
                        public_id=public_id,
                        baidu_model=baidu_model,
                        deep_search=deep_search,
                        think_mode=think_mode,
                        notes=notes,
                    )
                )

        if not db.scalar(select(PromptConfig)):
            db.add(PromptConfig(name="default", enabled=False, mode="first", content=""))

        if not db.scalar(select(ApiKey)):
            raw_key = "sk-baidu-" + secrets.token_urlsafe(32)
            created_key = raw_key
            db.add(
                ApiKey(
                    name="default",
                    key_hash=sha256_token(raw_key),
                    key_preview=raw_key[:12] + "...",
                    allowed_models="*",
                )
            )

        db.commit()
    return created_key


def migrate_db() -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        if "api_keys" in tables:
            columns = {column["name"] for column in inspector.get_columns("api_keys")}
            migrations = {
                "request_limit_total": "ALTER TABLE api_keys ADD COLUMN request_limit_total INTEGER DEFAULT 0",
                "token_limit_total": "ALTER TABLE api_keys ADD COLUMN token_limit_total INTEGER DEFAULT 0",
                "token_limit_per_day": "ALTER TABLE api_keys ADD COLUMN token_limit_per_day INTEGER DEFAULT 0",
                "ip_whitelist": "ALTER TABLE api_keys ADD COLUMN ip_whitelist TEXT DEFAULT ''",
                "ip_blacklist": "ALTER TABLE api_keys ADD COLUMN ip_blacklist TEXT DEFAULT ''",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    conn.execute(text(statement))

        if "credentials" in tables:
            columns = {column["name"] for column in inspector.get_columns("credentials")}
            migrations = {
                "last_used_at": "ALTER TABLE credentials ADD COLUMN last_used_at DATETIME",
                "use_count_total": "ALTER TABLE credentials ADD COLUMN use_count_total INTEGER DEFAULT 0",
                "use_count_today": "ALTER TABLE credentials ADD COLUMN use_count_today INTEGER DEFAULT 0",
                "use_count_date": "ALTER TABLE credentials ADD COLUMN use_count_date VARCHAR(10) DEFAULT ''",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    conn.execute(text(statement))

        if "baidu_conversations" in tables:
            columns = {column["name"] for column in inspector.get_columns("baidu_conversations")}
            migrations = {
                "requested_model": "ALTER TABLE baidu_conversations ADD COLUMN requested_model VARCHAR(128) DEFAULT ''",
                "baidu_model": "ALTER TABLE baidu_conversations ADD COLUMN baidu_model VARCHAR(128) DEFAULT ''",
                "last_error": "ALTER TABLE baidu_conversations ADD COLUMN last_error TEXT DEFAULT ''",
                "cookie_snapshot": "ALTER TABLE baidu_conversations ADD COLUMN cookie_snapshot TEXT DEFAULT ''",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    conn.execute(text(statement))

        if "conversation_turns" in tables:
            columns = {column["name"] for column in inspector.get_columns("conversation_turns")}
            migrations = {
                "requested_model": "ALTER TABLE conversation_turns ADD COLUMN requested_model VARCHAR(128) DEFAULT ''",
                "baidu_model": "ALTER TABLE conversation_turns ADD COLUMN baidu_model VARCHAR(128) DEFAULT ''",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    conn.execute(text(statement))


if __name__ == "__main__":
    key = init_db()
    if key:
        print(f"Created default API key: {key}")
    else:
        print("Database initialized.")
