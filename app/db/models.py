from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    baidu_model: Mapped[str] = mapped_column(String(128))
    deep_search: Mapped[str] = mapped_column(String(8), default="0")
    think_mode: Mapped[str] = mapped_column(String(8), default="0")
    source: Mapped[str] = mapped_column(String(64), default="pc_csaitab")
    setype: Mapped[str] = mapped_column(String(64), default="csaitab")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), default="default")
    cookie: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_status: Mapped[str] = mapped_column(String(64), default="unknown")
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    use_count_total: Mapped[int] = mapped_column(Integer, default=0)
    use_count_today: Mapped[int] = mapped_column(Integer, default=0)
    use_count_date: Mapped[str] = mapped_column(String(10), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    key_preview: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    request_limit_total: Mapped[int] = mapped_column(Integer, default=0)
    request_limit_per_day: Mapped[int] = mapped_column(Integer, default=0)
    token_limit_total: Mapped[int] = mapped_column(Integer, default=0)
    token_limit_per_day: Mapped[int] = mapped_column(Integer, default=0)
    allowed_models: Mapped[str] = mapped_column(Text, default="*")
    ip_whitelist: Mapped[str] = mapped_column(Text, default="")
    ip_blacklist: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PromptConfig(Base):
    __tablename__ = "prompt_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(32), default="first")  # first | every
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    api_key_name: Mapped[str] = mapped_column(String(128), default="")
    source_ip: Mapped[str] = mapped_column(String(64), default="")
    endpoint: Mapped[str] = mapped_column(String(128), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    prompt_chars: Mapped[int] = mapped_column(Integer, default=0)
    completion_chars: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class SystemLog(Base):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    module: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class OperationAudit(Base):
    __tablename__ = "operation_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(128), default="admin")
    source_ip: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(128), default="")
    target: Mapped[str] = mapped_column(String(256), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class BaiduConversation(Base):
    __tablename__ = "baidu_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_conversation_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    api_key_name: Mapped[str] = mapped_column(String(128), default="")
    source_ip: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    requested_model: Mapped[str] = mapped_column(String(128), default="")
    baidu_model: Mapped[str] = mapped_column(String(128), default="")
    mode: Mapped[str] = mapped_column(String(32), default="bound")
    baidu_session_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    last_qid: Mapped[str] = mapped_column(String(128), default="")
    last_pkg_id: Mapped[str] = mapped_column(String(128), default="")
    credential_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credential_name: Mapped[str] = mapped_column(String(128), default="")
    cookie_snapshot: Mapped[str] = mapped_column(Text, default="")
    rank: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="active")
    title: Mapped[str] = mapped_column(String(256), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("baidu_conversations.id"), nullable=True, index=True)
    local_conversation_id: Mapped[str] = mapped_column(String(256), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=1)
    qid: Mapped[str] = mapped_column(String(128), default="")
    session_id: Mapped[str] = mapped_column(String(128), default="")
    pkg_id: Mapped[str] = mapped_column(String(160), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    requested_model: Mapped[str] = mapped_column(String(128), default="")
    baidu_model: Mapped[str] = mapped_column(String(128), default="")
    prompt_preview: Mapped[str] = mapped_column(Text, default="")
    response_preview: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="running")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
