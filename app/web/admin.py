import secrets
import csv
import io
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.adapters.baidu import BaiduAdapter
from app.core.security import create_admin_cookie, hash_password, sha256_token, verify_admin_cookie, verify_password
from app.db.init_db import get_setting, set_setting
from app.db.models import (
    ApiKey,
    AppSetting,
    BaiduConversation,
    ConversationTurn,
    Credential,
    ModelConfig,
    OperationAudit,
    PromptConfig,
    RequestLog,
    SystemLog,
)
from app.db.session import get_db

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


def require_admin(request: Request):
    cookie = request.cookies.get("admin_session", "")
    if not verify_admin_cookie(cookie):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


def parse_optional_datetime(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_optional_date_start(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_optional_date_end(value: str | None) -> datetime | None:
    start = parse_optional_date_start(value)
    if not start:
        return None
    return start + timedelta(days=1)


def api_key_preview(raw: str) -> str:
    return raw[:12] + "..." if len(raw) > 12 else raw


def parse_validity_days(preset: str | None, custom_days: int | None) -> int:
    raw = (preset or "0").strip()
    if raw == "custom":
        return max(0, custom_days or 0)
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def audit(db: Session, request: Request, action: str, target: str = "", detail: str = "") -> None:
    ip = request.client.host if request.client else ""
    db.add(OperationAudit(actor="admin", source_ip=ip, action=action, target=target, detail=detail))
    db.commit()


def normalize_cookie_input(raw_cookie: str) -> str:
    raw_cookie = (raw_cookie or "").strip()
    if not raw_cookie:
        return ""

    if raw_cookie.lower().startswith("cookie:"):
        return raw_cookie.split(":", 1)[1].strip()

    try:
        data = json.loads(raw_cookie)
    except json.JSONDecodeError:
        return raw_cookie

    pairs: list[str] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if name:
                pairs.append(f"{name}={value}")
    elif isinstance(data, dict):
        cookies = data.get("cookies")
        if isinstance(cookies, list):
            return normalize_cookie_input(json.dumps(cookies, ensure_ascii=False))
        for name, value in data.items():
            if isinstance(value, (str, int, float, bool)) and str(name).strip():
                pairs.append(f"{str(name).strip()}={value}")

    return "; ".join(pairs) if pairs else raw_cookie


def safe_conversations_return_url(raw_url: str | None) -> str:
    raw_url = (raw_url or "").strip()
    if raw_url.startswith("/admin/conversations"):
        return raw_url
    return "/admin/conversations"


def safe_logs_return_url(raw_url: str | None) -> str:
    raw_url = (raw_url or "").strip()
    if raw_url.startswith("/admin/logs"):
        return raw_url
    return "/admin/logs"


def page_param(request: Request, name: str) -> int:
    try:
        return max(1, int(request.query_params.get(name, "1") or "1"))
    except ValueError:
        return 1


def page_meta(total: int, page: int, per_page: int) -> dict[str, int | None]:
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "prev_page": page - 1 if page > 1 else None,
        "next_page": page + 1 if page < total_pages else None,
    }


@router.get("", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    per_page = 20
    req_page = page_param(request, "req_page")
    request_total = db.scalar(select(func.count()).select_from(RequestLog)) or 0
    req_meta = page_meta(request_total, req_page, per_page)
    stats = {
        "models": db.scalar(select(func.count()).select_from(ModelConfig)) or 0,
        "credentials": db.scalar(select(func.count()).select_from(Credential)) or 0,
        "api_keys": db.scalar(select(func.count()).select_from(ApiKey)) or 0,
        "requests": request_total,
    }
    recent = db.scalars(
        select(RequestLog)
        .order_by(desc(RequestLog.created_at))
        .offset((int(req_meta["page"]) - 1) * per_page)
        .limit(per_page)
    ).all()
    log_settings = {
        "enable_request_logs": get_setting(db, "enable_request_logs", "true"),
    }
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "stats": stats,
            "recent": recent,
            "req_meta": req_meta,
            "log_settings": log_settings,
        },
    )


@router.post("/request-logs/settings")
def save_index_request_log_settings(
    request: Request,
    enable_request_logs: str | None = Form(None),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    value = "true" if enable_request_logs == "on" else "false"
    set_setting(db, "enable_request_logs", value)
    db.commit()
    audit(db, request, "request_log_settings_update", "request_logs", f"enable_request_logs={value}")
    return RedirectResponse("/admin?updated=settings", status_code=303)


@router.post("/request-logs/bulk-delete")
async def bulk_delete_request_logs(
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    form = await request.form()
    ids: list[int] = []
    for raw in form.getlist("log_ids"):
        try:
            ids.append(int(str(raw)))
        except ValueError:
            continue
    ids = sorted(set(ids))
    if ids:
        deleted = db.query(RequestLog).filter(RequestLog.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        audit(db, request, "request_logs_bulk_delete", "request_logs", f"count={deleted}")
    return RedirectResponse(str(form.get("return_url") or "/admin"), status_code=303)


@router.post("/request-logs/clear")
def clear_request_logs(
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    deleted = db.query(RequestLog).delete(synchronize_session=False)
    db.commit()
    audit(db, request, "request_logs_clear", "request_logs", f"count={deleted}")
    return RedirectResponse(f"/admin?updated=clear&request_count={deleted}", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@router.post("/login")
def login(request: Request, password: str = Form(...), db: Session = Depends(get_db)):
    password_hash = get_setting(db, "admin_password_hash")
    if not password_hash or not verify_password(password, password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "密码错误"}, status_code=401)
    audit(db, request, "admin_login", "admin")
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("admin_session", create_admin_cookie(), httponly=True, samesite="lax")
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie("admin_session")
    return resp


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    items = db.scalars(select(AppSetting).order_by(AppSetting.key)).all()
    return templates.TemplateResponse("settings.html", {"request": request, "items": items})


@router.get("/limits", response_class=HTMLResponse)
def limits_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    values = {
        key: get_setting(db, key)
        for key in [
            "ip_whitelist",
            "ip_blacklist",
            "rate_limit_per_minute",
            "log_retention_days",
            "output_reasoning",
            "output_image_urls",
            "long_text_strategy",
            "proxy_file_downloads",
            "mask_sensitive_logs",
            "auto_direct_answer",
        ]
    }
    return templates.TemplateResponse("limits.html", {"request": request, "values": values})


@router.get("/tools", response_class=HTMLResponse)
def tools_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    values = {
        key: get_setting(db, key)
        for key in [
            "tool_call_mode",
            "tool_client_profile",
            "tokeny_tool_result_compaction",
            "tokeny_compact_tool_schema_after_result",
            "document_output_strategy",
            "baidu_native_document_policy",
            "tool_buffer_timeout_ms",
            "tool_max_buffer_chars",
            "tool_parse_retries",
            "tool_parse_failure_strategy",
            "tool_loop_protection",
            "tool_force_final_after_result",
            "tool_repeat_protection_enabled",
            "tool_repeat_protection_scope",
            "tool_repeat_match_mode",
            "tool_arg_safety_enabled",
            "tool_path_safety_enabled",
            "tool_mojibake_safety_enabled",
            "tool_mojibake_markers",
        ]
    }
    return templates.TemplateResponse("tools.html", {"request": request, "values": values})


@router.post("/tools")
def update_tools(
    request: Request,
    tool_call_mode: str = Form("auto"),
    tool_client_profile: str = Form("auto"),
    tokeny_tool_result_compaction: str | None = Form(None),
    tokeny_compact_tool_schema_after_result: str | None = Form(None),
    document_output_strategy: str = Form("auto"),
    baidu_native_document_policy: str = Form("explicit_only"),
    tool_buffer_timeout_ms: str = Form("60000"),
    tool_max_buffer_chars: str = Form("300000"),
    tool_parse_retries: str = Form("1"),
    tool_parse_failure_strategy: str = Form("clean_text"),
    tool_loop_protection: str | None = Form(None),
    tool_force_final_after_result: str | None = Form(None),
    tool_repeat_protection_enabled: str | None = Form(None),
    tool_repeat_protection_scope: str = Form("delete_move"),
    tool_repeat_match_mode: str = Form("smart"),
    tool_arg_safety_enabled: str | None = Form(None),
    tool_path_safety_enabled: str | None = Form(None),
    tool_mojibake_safety_enabled: str | None = Form(None),
    tool_mojibake_markers: str = Form(""),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    if tool_call_mode not in {"auto", "force_buffer", "stream_compat", "off"}:
        tool_call_mode = "auto"
    if tool_client_profile not in {"auto", "openai", "cherry", "cline", "chatbox", "openwebui", "lobe", "hermes", "tokeny"}:
        tool_client_profile = "auto"
    if document_output_strategy not in {"auto", "client_tools", "baidu_native", "text"}:
        document_output_strategy = "auto"
    if baidu_native_document_policy not in {"explicit_only", "allow", "deny"}:
        baidu_native_document_policy = "explicit_only"
    if tool_parse_failure_strategy not in {"clean_text", "error", "raw_text"}:
        tool_parse_failure_strategy = "clean_text"
    if tool_repeat_protection_scope not in {"off", "delete_move", "write", "all"}:
        tool_repeat_protection_scope = "delete_move"
    if tool_repeat_match_mode not in {"none", "smart", "exact", "path"}:
        tool_repeat_match_mode = "smart"
    try:
        tool_buffer_timeout_ms = str(max(1000, int(tool_buffer_timeout_ms or "60000")))
    except ValueError:
        tool_buffer_timeout_ms = "60000"
    try:
        tool_max_buffer_chars = str(max(1000, int(tool_max_buffer_chars or "300000")))
    except ValueError:
        tool_max_buffer_chars = "300000"
    try:
        tool_parse_retries = str(max(0, min(5, int(tool_parse_retries or "1"))))
    except ValueError:
        tool_parse_retries = "1"
    updates = {
        "tool_call_mode": tool_call_mode,
        "tool_client_profile": tool_client_profile,
        "tokeny_tool_result_compaction": "true" if tokeny_tool_result_compaction == "on" else "false",
        "tokeny_compact_tool_schema_after_result": "true" if tokeny_compact_tool_schema_after_result == "on" else "false",
        "document_output_strategy": document_output_strategy,
        "baidu_native_document_policy": baidu_native_document_policy,
        "tool_buffer_timeout_ms": tool_buffer_timeout_ms,
        "tool_max_buffer_chars": tool_max_buffer_chars,
        "tool_parse_retries": tool_parse_retries,
        "tool_parse_failure_strategy": tool_parse_failure_strategy,
        "tool_loop_protection": "true" if tool_loop_protection == "on" else "false",
        "tool_force_final_after_result": "true" if tool_force_final_after_result == "on" else "false",
        "tool_repeat_protection_enabled": "true" if tool_repeat_protection_enabled == "on" else "false",
        "tool_repeat_protection_scope": tool_repeat_protection_scope,
        "tool_repeat_match_mode": tool_repeat_match_mode,
        "tool_arg_safety_enabled": "true" if tool_arg_safety_enabled == "on" else "false",
        "tool_path_safety_enabled": "true" if tool_path_safety_enabled == "on" else "false",
        "tool_mojibake_safety_enabled": "true" if tool_mojibake_safety_enabled == "on" else "false",
        "tool_mojibake_markers": tool_mojibake_markers.strip(),
    }
    for key, value in updates.items():
        set_setting(db, key, value)
    db.commit()
    audit(db, request, "tools_update", "tools", json.dumps(updates, ensure_ascii=False))
    return RedirectResponse("/admin/tools?updated=1", status_code=303)


@router.post("/limits")
def update_limits(
    request: Request,
    ip_whitelist: str = Form(""),
    ip_blacklist: str = Form(""),
    rate_limit_per_minute: str = Form("0"),
    log_retention_days: str = Form("30"),
    output_reasoning: str | None = Form(None),
    output_image_urls: str | None = Form(None),
    proxy_file_downloads: str | None = Form(None),
    mask_sensitive_logs: str | None = Form(None),
    auto_direct_answer: str | None = Form(None),
    long_text_strategy: str = Form("stream_delta"),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    updates = {
        "ip_whitelist": ip_whitelist,
        "ip_blacklist": ip_blacklist,
        "rate_limit_per_minute": rate_limit_per_minute,
        "log_retention_days": log_retention_days,
        "output_reasoning": "true" if output_reasoning == "on" else "false",
        "output_image_urls": "true" if output_image_urls == "on" else "false",
        "proxy_file_downloads": "true" if proxy_file_downloads == "on" else "false",
        "mask_sensitive_logs": "true" if mask_sensitive_logs == "on" else "false",
        "auto_direct_answer": "true" if auto_direct_answer == "on" else "false",
        "long_text_strategy": long_text_strategy,
    }
    for key, value in updates.items():
        set_setting(db, key, value)
    db.commit()
    audit(db, request, "limits_update", "limits", json.dumps(updates, ensure_ascii=False))
    return RedirectResponse("/admin/limits?updated=1", status_code=303)


@router.post("/settings")
def update_settings(
    request: Request,
    key: list[str] = Form(default=[]),
    value: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    for k, v in zip(key, value):
        set_setting(db, k, v)
    db.commit()
    audit(db, request, "settings_update", "app_settings", ",".join(key))
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/settings/admin-password")
def change_admin_password(
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    if len(password) < 8:
        return RedirectResponse("/admin/settings?error=password_too_short", status_code=303)
    set_setting(db, "admin_password_hash", hash_password(password))
    db.commit()
    audit(db, request, "admin_password_update", "admin")
    return RedirectResponse("/admin/settings?updated=password", status_code=303)


@router.get("/settings/export")
def export_settings(db: Session = Depends(get_db), _: None = Depends(require_admin)):
    data = {
        "settings": [
            {"key": item.key, "value": item.value, "description": item.description}
            for item in db.scalars(select(AppSetting).order_by(AppSetting.key)).all()
            if item.key != "admin_password_hash"
        ],
        "models": [
            {
                "public_id": item.public_id,
                "baidu_model": item.baidu_model,
                "deep_search": item.deep_search,
                "think_mode": item.think_mode,
                "source": item.source,
                "setype": item.setype,
                "enabled": item.enabled,
                "notes": item.notes,
            }
            for item in db.scalars(select(ModelConfig).order_by(ModelConfig.public_id)).all()
        ],
        "prompts": [
            {"name": item.name, "enabled": item.enabled, "mode": item.mode, "content": item.content}
            for item in db.scalars(select(PromptConfig).order_by(PromptConfig.name)).all()
        ],
    }
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=baidu-openai-proxy-config.json"},
    )


@router.post("/settings/import")
async def import_settings(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    data = json.loads((await file.read()).decode("utf-8"))
    for item in data.get("settings", []):
        if item.get("key") and item.get("key") != "admin_password_hash":
            set_setting(db, item["key"], str(item.get("value", "")), str(item.get("description", "")))
    for item in data.get("models", []):
        public_id = item.get("public_id")
        if not public_id:
            continue
        model = db.scalar(select(ModelConfig).where(ModelConfig.public_id == public_id))
        if not model:
            model = ModelConfig(public_id=public_id, baidu_model=item.get("baidu_model", "smartMode"))
            db.add(model)
        model.baidu_model = item.get("baidu_model", model.baidu_model)
        model.deep_search = item.get("deep_search", model.deep_search)
        model.think_mode = item.get("think_mode", model.think_mode)
        model.source = item.get("source", model.source)
        model.setype = item.get("setype", model.setype)
        model.enabled = bool(item.get("enabled", model.enabled))
        model.notes = item.get("notes", model.notes)
    for item in data.get("prompts", []):
        name = item.get("name", "default")
        prompt = db.scalar(select(PromptConfig).where(PromptConfig.name == name))
        if not prompt:
            prompt = PromptConfig(name=name)
            db.add(prompt)
        prompt.enabled = bool(item.get("enabled", prompt.enabled))
        prompt.mode = item.get("mode", prompt.mode)
        prompt.content = item.get("content", prompt.content)
    db.commit()
    audit(db, request, "settings_import", "config", file.filename or "")
    return RedirectResponse("/admin/settings?updated=import", status_code=303)


@router.get("/models", response_class=HTMLResponse)
def models_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    items = db.scalars(select(ModelConfig).order_by(ModelConfig.public_id)).all()
    return templates.TemplateResponse("models.html", {"request": request, "items": items})


@router.post("/models")
def save_model(
    request: Request,
    public_id: str = Form(...),
    baidu_model: str = Form(...),
    deep_search: str = Form("0"),
    think_mode: str = Form("0"),
    source: str = Form("pc_csaitab"),
    setype: str = Form("csaitab"),
    enabled: str | None = Form(None),
    notes: str = Form(""),
    model_id: int | None = Form(None),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(ModelConfig, model_id) if model_id else None
    if not item:
        item = ModelConfig(public_id=public_id, baidu_model=baidu_model)
        db.add(item)
    item.public_id = public_id
    item.baidu_model = baidu_model
    item.deep_search = deep_search
    item.think_mode = think_mode
    item.source = source
    item.setype = setype
    item.enabled = enabled == "on"
    item.notes = notes
    db.commit()
    audit(db, request, "model_save", public_id)
    return RedirectResponse("/admin/models", status_code=303)


@router.get("/credentials", response_class=HTMLResponse)
def credentials_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    items = db.scalars(select(Credential).order_by(Credential.id)).all()
    credential_settings = {
        "credential_mode": get_setting(db, "credential_mode", "auto"),
        "credential_failure_disable_threshold": get_setting(db, "credential_failure_disable_threshold", "3"),
    }
    return templates.TemplateResponse(
        "credentials.html",
        {"request": request, "items": items, "credential_settings": credential_settings},
    )


@router.get("/conversations", response_class=HTMLResponse)
def conversations_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    try:
        page = max(1, int(request.query_params.get("page", "1") or "1"))
    except ValueError:
        page = 1
    per_page = 20
    q = (request.query_params.get("q", "") or "").strip()
    filters = []
    if q:
        like = f"%{q}%"
        filters.extend(
            [
                BaiduConversation.local_conversation_id.ilike(like),
                BaiduConversation.baidu_session_id.ilike(like),
                BaiduConversation.last_qid.ilike(like),
                BaiduConversation.last_pkg_id.ilike(like),
                BaiduConversation.model.ilike(like),
                BaiduConversation.requested_model.ilike(like),
                BaiduConversation.baidu_model.ilike(like),
                BaiduConversation.credential_name.ilike(like),
                BaiduConversation.cookie_snapshot.ilike(like),
                BaiduConversation.api_key_name.ilike(like),
                BaiduConversation.source_ip.ilike(like),
                BaiduConversation.title.ilike(like),
            ]
        )
        if q.isdigit():
            filters.append(BaiduConversation.id == int(q))
            filters.append(BaiduConversation.credential_id == int(q))
    where_clause = or_(*filters) if filters else None
    total_stmt = select(func.count()).select_from(BaiduConversation)
    list_stmt = select(BaiduConversation).order_by(desc(BaiduConversation.last_active_at))
    if where_clause is not None:
        total_stmt = total_stmt.where(where_clause)
        list_stmt = list_stmt.where(where_clause)
    total = db.scalar(total_stmt) or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    items = db.scalars(list_stmt.offset((page - 1) * per_page).limit(per_page)).all()
    values = {
        "conversation_mode": get_setting(db, "conversation_mode", "stateless"),
        "conversation_fallback_binding": get_setting(db, "conversation_fallback_binding", "false"),
        "conversation_missing_id_strategy": get_setting(db, "conversation_missing_id_strategy", "smart"),
        "conversation_ttl_hours": get_setting(db, "conversation_ttl_hours", "24"),
        "conversation_max_turns": get_setting(db, "conversation_max_turns", "50"),
        "conversation_save_content": get_setting(db, "conversation_save_content", "true"),
        "conversation_message_strategy": get_setting(db, "conversation_message_strategy", "smart"),
        "conversation_response_mode": get_setting(db, "conversation_response_mode", "client"),
        "conversation_max_query_chars": get_setting(db, "conversation_max_query_chars", "120000"),
        "conversation_max_query_scope": get_setting(db, "conversation_max_query_scope", "stateless_and_first_turn"),
    }
    return templates.TemplateResponse(
        "conversations.html",
        {
            "request": request,
            "items": items,
            "values": values,
            "q": q,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "prev_page": page - 1 if page > 1 else None,
            "next_page": page + 1 if page < total_pages else None,
        },
    )


@router.post("/conversations/settings")
def save_conversation_settings(
    request: Request,
    conversation_mode: str = Form("stateless"),
    conversation_fallback_binding: str | None = Form(None),
    conversation_missing_id_strategy: str = Form("smart"),
    conversation_ttl_hours: str = Form("24"),
    conversation_max_turns: str = Form("50"),
    conversation_message_strategy: str = Form("smart"),
    conversation_response_mode: str = Form("client"),
    conversation_max_query_chars: str = Form("120000"),
    conversation_max_query_scope: str = Form("stateless_and_first_turn"),
    conversation_save_content: str | None = Form(None),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    try:
        ttl_hours = str(max(0, int(conversation_ttl_hours or "24")))
    except ValueError:
        ttl_hours = "24"
    try:
        max_turns = str(max(0, int(conversation_max_turns or "50")))
    except ValueError:
        max_turns = "50"
    try:
        max_query_chars = str(max(0, int(conversation_max_query_chars or "120000")))
    except ValueError:
        max_query_chars = "120000"
    if conversation_mode not in {"stateless", "bound", "hybrid"}:
        conversation_mode = "stateless"
    if conversation_message_strategy not in {"smart", "latest_user_only", "full_messages"}:
        conversation_message_strategy = "smart"
    if conversation_missing_id_strategy not in {"smart", "stable", "strict", "ephemeral", "fallback"}:
        conversation_missing_id_strategy = "smart"
    if conversation_response_mode not in {"client", "stream", "buffered_stream", "non_stream"}:
        conversation_response_mode = "client"
    if conversation_max_query_scope not in {"all", "stateless_and_first_turn", "stateless_only", "off"}:
        conversation_max_query_scope = "stateless_and_first_turn"
    updates = {
        "conversation_mode": conversation_mode,
        "conversation_fallback_binding": "true" if conversation_fallback_binding == "on" else "false",
        "conversation_missing_id_strategy": conversation_missing_id_strategy,
        "conversation_ttl_hours": ttl_hours,
        "conversation_max_turns": max_turns,
        "conversation_message_strategy": conversation_message_strategy,
        "conversation_response_mode": conversation_response_mode,
        "conversation_max_query_chars": max_query_chars,
        "conversation_max_query_scope": conversation_max_query_scope,
        "conversation_save_content": "true" if conversation_save_content == "on" else "false",
    }
    for key, value in updates.items():
        set_setting(db, key, value)
    db.commit()
    audit(db, request, "conversation_settings_update", "conversations", json.dumps(updates, ensure_ascii=False))
    return RedirectResponse("/admin/conversations?updated=settings", status_code=303)


@router.post("/conversations/bulk-delete")
async def bulk_delete_conversations(
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    form = await request.form()
    return_url = safe_conversations_return_url(str(form.get("return_url") or ""))
    ids: list[int] = []
    for raw in form.getlist("conversation_ids"):
        try:
            ids.append(int(str(raw)))
        except ValueError:
            continue
    ids = sorted(set(ids))
    if ids:
        local_ids = db.scalars(select(BaiduConversation.local_conversation_id).where(BaiduConversation.id.in_(ids))).all()
        turn_filters = [ConversationTurn.conversation_id.in_(ids)]
        if local_ids:
            turn_filters.append(ConversationTurn.local_conversation_id.in_(local_ids))
        db.query(ConversationTurn).filter(or_(*turn_filters)).delete(synchronize_session=False)
        db.query(BaiduConversation).filter(BaiduConversation.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        audit(db, request, "conversation_bulk_delete", "conversations", f"count={len(ids)}")
    return RedirectResponse(return_url, status_code=303)


@router.post("/conversations/clear")
def clear_conversations(
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    turn_count = db.query(ConversationTurn).delete(synchronize_session=False)
    conversation_count = db.query(BaiduConversation).delete(synchronize_session=False)
    db.commit()
    audit(db, request, "conversation_clear_all", "conversations", f"conversations={conversation_count}, turns={turn_count}")
    return RedirectResponse("/admin/conversations", status_code=303)


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
def conversation_detail(
    request: Request,
    conversation_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(BaiduConversation, conversation_id)
    if not item:
        raise HTTPException(status_code=404, detail="Conversation not found")
    turns = db.scalars(
        select(ConversationTurn)
        .where(ConversationTurn.conversation_id == item.id)
        .order_by(ConversationTurn.created_at.desc())
        .limit(200)
    ).all()
    return templates.TemplateResponse("conversation_detail.html", {"request": request, "item": item, "turns": turns})


@router.post("/conversations/{conversation_id}/reset")
def reset_conversation(
    request: Request,
    conversation_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(BaiduConversation, conversation_id)
    if item:
        item.baidu_session_id = ""
        item.last_qid = ""
        item.last_pkg_id = ""
        item.cookie_snapshot = ""
        item.rank = 0
        item.status = "active"
        item.last_error = ""
        db.commit()
        audit(db, request, "conversation_reset", item.local_conversation_id)
    return RedirectResponse("/admin/conversations", status_code=303)


@router.post("/conversations/{conversation_id}/delete")
def delete_conversation(
    request: Request,
    conversation_id: int,
    return_url: str = Form("/admin/conversations"),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(BaiduConversation, conversation_id)
    if item:
        local_id = item.local_conversation_id
        db.query(ConversationTurn).filter(
            or_(
                ConversationTurn.conversation_id == item.id,
                ConversationTurn.local_conversation_id == local_id,
            )
        ).delete(synchronize_session=False)
        db.delete(item)
        db.commit()
        audit(db, request, "conversation_delete", local_id)
    return RedirectResponse(safe_conversations_return_url(return_url), status_code=303)


@router.post("/credentials/settings")
def save_credential_settings(
    request: Request,
    credential_mode: str = Form("auto"),
    credential_failure_disable_threshold: str = Form("3"),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    if credential_mode not in {"auto", "pool", "anonymous"}:
        credential_mode = "auto"
    try:
        threshold = str(max(0, int(credential_failure_disable_threshold)))
    except ValueError:
        threshold = "3"
    set_setting(db, "credential_mode", credential_mode)
    set_setting(db, "credential_failure_disable_threshold", threshold)
    db.commit()
    audit(db, request, "credential_settings_update", "credentials", f"mode={credential_mode}, threshold={threshold}")
    return RedirectResponse("/admin/credentials?updated=settings", status_code=303)


@router.post("/credentials")
def save_credential(
    request: Request,
    name: str = Form(...),
    cookie: str = Form(""),
    enabled: str | None = Form(None),
    notes: str = Form(""),
    credential_id: int | None = Form(None),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(Credential, credential_id) if credential_id else None
    if not item:
        item = Credential(name=name)
        db.add(item)
    item.name = name
    item.cookie = normalize_cookie_input(cookie)
    item.enabled = enabled == "on"
    item.notes = notes
    db.commit()
    audit(db, request, "credential_save", name)
    return RedirectResponse("/admin/credentials", status_code=303)


@router.get("/key-lookup", response_class=HTMLResponse)
def key_lookup_page(request: Request):
    return templates.TemplateResponse(
        "key_lookup.html",
        {
            "request": request,
            "raw_key": "",
            "item": None,
            "logs": [],
            "meta": None,
            "total_requests": 0,
            "today_requests": 0,
            "start_date": "",
            "end_date": "",
            "error": "",
            "now": datetime.utcnow(),
        },
    )


@router.post("/key-lookup", response_class=HTMLResponse)
async def key_lookup_search(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    raw_key = str(form.get("key_value") or "").strip()
    start_date = str(form.get("start_date") or "").strip()
    end_date = str(form.get("end_date") or "").strip()
    try:
        page = max(1, int(str(form.get("page") or "1")))
    except ValueError:
        page = 1

    context = {
        "request": request,
        "raw_key": raw_key,
        "item": None,
        "logs": [],
        "meta": None,
        "total_requests": 0,
        "today_requests": 0,
        "start_date": start_date,
        "end_date": end_date,
        "error": "",
        "now": datetime.utcnow(),
    }
    if not raw_key:
        context["error"] = "请输入 API Key。"
        return templates.TemplateResponse("key_lookup.html", context)

    item = db.scalar(select(ApiKey).where(ApiKey.key_hash == sha256_token(raw_key)))
    if not item:
        context["error"] = "没有找到这个 API Key，或 Key 输入不正确。"
        return templates.TemplateResponse("key_lookup.html", context)

    filters = [RequestLog.api_key_name == item.name]
    start_dt = parse_optional_date_start(start_date)
    end_dt = parse_optional_date_end(end_date)
    if start_dt:
        filters.append(RequestLog.created_at >= start_dt)
    if end_dt:
        filters.append(RequestLog.created_at < end_dt)

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    filtered_total = db.scalar(select(func.count()).select_from(RequestLog).where(*filters)) or 0
    today_requests = db.scalar(
        select(func.count())
        .select_from(RequestLog)
        .where(RequestLog.api_key_name == item.name, RequestLog.created_at >= today_start)
    ) or 0
    per_page = 20
    meta = page_meta(filtered_total, page, per_page)
    logs = db.scalars(
        select(RequestLog)
        .where(*filters)
        .order_by(desc(RequestLog.created_at))
        .offset((int(meta["page"]) - 1) * per_page)
        .limit(per_page)
    ).all()

    context.update(
        {
            "item": item,
            "logs": logs,
            "meta": meta,
            "total_requests": item.use_count_total or 0,
            "today_requests": today_requests,
        }
    )
    return templates.TemplateResponse("key_lookup.html", context)


@router.get("/api-keys", response_class=HTMLResponse)
def api_keys_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    items = db.scalars(select(ApiKey).order_by(ApiKey.id)).all()
    return templates.TemplateResponse(
        "api_keys.html",
        {"request": request, "items": items, "new_key": request.query_params.get("new_key"), "now": datetime.utcnow()},
    )


@router.post("/api-keys")
def create_api_key(
    request: Request,
    name: str = Form(...),
    key_value: str = Form(""),
    allowed_models: str = Form("*"),
    request_limit_total: int = Form(0),
    request_limit_per_day: int = Form(0),
    token_limit_total: int = Form(0),
    token_limit_per_day: int = Form(0),
    ip_whitelist: str = Form(""),
    ip_blacklist: str = Form(""),
    validity_preset: str = Form("0"),
    validity_days_custom: int = Form(0),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    raw = key_value.strip() or "sk-baidu-" + secrets.token_urlsafe(32)
    validity_days = parse_validity_days(validity_preset, validity_days_custom)
    db.add(
        ApiKey(
            name=name,
            key_hash=sha256_token(raw),
            key_preview=api_key_preview(raw),
            key_value=raw,
            allowed_models=allowed_models,
            request_limit_total=request_limit_total,
            request_limit_per_day=request_limit_per_day,
            token_limit_total=token_limit_total,
            token_limit_per_day=token_limit_per_day,
            ip_whitelist=ip_whitelist,
            ip_blacklist=ip_blacklist,
            validity_days=validity_days,
        )
    )
    db.commit()
    audit(db, request, "api_key_create", name)
    return RedirectResponse(f"/admin/api-keys?new_key={raw}", status_code=303)


@router.post("/api-keys/{key_id}/update")
def update_api_key(
    request: Request,
    key_id: int,
    name: str = Form(...),
    key_value: str = Form(""),
    allowed_models: str = Form("*"),
    request_limit_total: int = Form(0),
    request_limit_per_day: int = Form(0),
    token_limit_total: int = Form(0),
    token_limit_per_day: int = Form(0),
    ip_whitelist: str = Form(""),
    ip_blacklist: str = Form(""),
    validity_preset: str = Form("0"),
    validity_days_custom: int = Form(0),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(ApiKey, key_id)
    if item:
        previous_validity_days = item.validity_days or 0
        validity_days = parse_validity_days(validity_preset, validity_days_custom)
        item.name = name
        raw = key_value.strip()
        if raw:
            item.key_hash = sha256_token(raw)
            item.key_preview = api_key_preview(raw)
            item.key_value = raw
        item.allowed_models = allowed_models
        item.request_limit_total = request_limit_total
        item.request_limit_per_day = request_limit_per_day
        item.token_limit_total = token_limit_total
        item.token_limit_per_day = token_limit_per_day
        item.ip_whitelist = ip_whitelist
        item.ip_blacklist = ip_blacklist
        item.validity_days = validity_days
        if validity_days <= 0:
            item.activated_at = None
            item.expires_at = None
        elif validity_days != previous_validity_days:
            item.activated_at = None
            item.expires_at = None
        db.commit()
        audit(db, request, "api_key_update", item.name)
    return RedirectResponse("/admin/api-keys", status_code=303)


@router.post("/api-keys/{key_id}/reset")
def reset_api_key(
    request: Request,
    key_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(ApiKey, key_id)
    if not item:
        return RedirectResponse("/admin/api-keys", status_code=303)
    raw = "sk-baidu-" + secrets.token_urlsafe(32)
    item.key_hash = sha256_token(raw)
    item.key_preview = api_key_preview(raw)
    item.key_value = raw
    item.activated_at = None
    item.expires_at = None
    item.use_count_total = 0
    item.last_used_at = None
    db.commit()
    audit(db, request, "api_key_reset", item.name, f"id={key_id}")
    return RedirectResponse(f"/admin/api-keys?new_key={raw}", status_code=303)


@router.post("/api-keys/{key_id}/toggle")
def toggle_api_key(
    request: Request,
    key_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(ApiKey, key_id)
    if item:
        item.enabled = not item.enabled
        db.commit()
        audit(db, request, "api_key_toggle", item.name, f"enabled={item.enabled}")
    return RedirectResponse("/admin/api-keys", status_code=303)


@router.post("/api-keys/{key_id}/delete")
def delete_api_key(
    request: Request,
    key_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(ApiKey, key_id)
    if item:
        name = item.name
        preview = item.key_preview
        db.delete(item)
        db.commit()
        audit(db, request, "api_key_delete", name, f"id={key_id}, preview={preview}")
    return RedirectResponse("/admin/api-keys?deleted=1", status_code=303)


@router.post("/credentials/{credential_id}/toggle")
def toggle_credential(
    request: Request,
    credential_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(Credential, credential_id)
    if item:
        item.enabled = not item.enabled
        db.commit()
        audit(db, request, "credential_toggle", item.name, f"enabled={item.enabled}")
    return RedirectResponse("/admin/credentials", status_code=303)


@router.post("/credentials/{credential_id}/delete")
def delete_credential(
    request: Request,
    credential_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(Credential, credential_id)
    if not item:
        return RedirectResponse("/admin/credentials?error=credential_not_found", status_code=303)
    bound_count = db.scalar(
        select(func.count())
        .select_from(BaiduConversation)
        .where(BaiduConversation.credential_id == credential_id)
    ) or 0
    if bound_count:
        audit(
            db,
            request,
            "credential_delete_blocked",
            item.name,
            f"bound_conversations={bound_count}",
        )
        return RedirectResponse(
            f"/admin/credentials?error=credential_bound&bound_count={bound_count}",
            status_code=303,
        )
    name = item.name
    db.delete(item)
    db.commit()
    audit(db, request, "credential_delete", name, f"id={credential_id}")
    return RedirectResponse("/admin/credentials?deleted=1", status_code=303)


@router.post("/credentials/{credential_id}/check")
async def check_credential(
    request: Request,
    credential_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(Credential, credential_id)
    if item:
        try:
            item.cookie = normalize_cookie_input(item.cookie)
            context = await BaiduAdapter(db).init_context(cookie_override=item.cookie)
            item.last_status = f"ok token={context.token[:6]} lid={context.searchframe_lid}"
            item.failure_count = 0
            item.last_used_at = datetime.utcnow()
        except Exception as exc:
            item.last_status = f"failed: {str(exc)[:160]}"
            item.failure_count += 1
        db.commit()
        audit(db, request, "credential_check", item.name, item.last_status)
    return RedirectResponse("/admin/credentials", status_code=303)


@router.post("/models/{model_id}/toggle")
def toggle_model(
    request: Request,
    model_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.get(ModelConfig, model_id)
    if item:
        item.enabled = not item.enabled
        db.commit()
        audit(db, request, "model_toggle", item.public_id, f"enabled={item.enabled}")
    return RedirectResponse("/admin/models", status_code=303)


@router.get("/prompts", response_class=HTMLResponse)
def prompts_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    item = db.scalar(select(PromptConfig).where(PromptConfig.name == "default"))
    return templates.TemplateResponse("prompts.html", {"request": request, "item": item})


@router.post("/prompts")
def save_prompt(
    request: Request,
    enabled: str | None = Form(None),
    mode: str = Form("first"),
    content: str = Form(""),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.scalar(select(PromptConfig).where(PromptConfig.name == "default"))
    if not item:
        item = PromptConfig(name="default")
        db.add(item)
    item.enabled = enabled == "on"
    item.mode = mode
    item.content = content
    db.commit()
    audit(db, request, "prompt_save", item.name, f"enabled={item.enabled}, mode={item.mode}")
    return RedirectResponse("/admin/prompts", status_code=303)


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    q = (request.query_params.get("q", "") or "").strip()
    log_settings = {
        key: get_setting(db, key)
        for key in [
            "enable_request_logs",
            "enable_system_logs",
            "log_request_body",
            "log_upstream_body",
            "log_upstream_model",
            "mask_sensitive_logs",
            "log_retention_days",
        ]
    }
    per_page = 20
    req_page = page_param(request, "req_page")
    sys_page = page_param(request, "sys_page")
    audit_page = page_param(request, "audit_page")
    req_filters = []
    sys_filters = []
    audit_filters = []
    if q:
        like = f"%{q}%"
        req_filters.append(
            or_(
                RequestLog.request_id.ilike(like),
                RequestLog.api_key_name.ilike(like),
                RequestLog.source_ip.ilike(like),
                RequestLog.endpoint.ilike(like),
                RequestLog.model.ilike(like),
                RequestLog.error.ilike(like),
            )
        )
        sys_filters.append(
            or_(SystemLog.level.ilike(like), SystemLog.module.ilike(like), SystemLog.message.ilike(like))
        )
        audit_filters.append(
            or_(
                OperationAudit.actor.ilike(like),
                OperationAudit.source_ip.ilike(like),
                OperationAudit.action.ilike(like),
                OperationAudit.target.ilike(like),
                OperationAudit.detail.ilike(like),
            )
        )

    req_total_stmt = select(func.count()).select_from(RequestLog)
    sys_total_stmt = select(func.count()).select_from(SystemLog)
    audit_total_stmt = select(func.count()).select_from(OperationAudit)
    req_stmt = select(RequestLog).order_by(desc(RequestLog.created_at))
    sys_stmt = select(SystemLog).order_by(desc(SystemLog.created_at))
    audit_stmt = select(OperationAudit).order_by(desc(OperationAudit.created_at))
    if req_filters:
        req_total_stmt = req_total_stmt.where(*req_filters)
        req_stmt = req_stmt.where(*req_filters)
    if sys_filters:
        sys_total_stmt = sys_total_stmt.where(*sys_filters)
        sys_stmt = sys_stmt.where(*sys_filters)
    if audit_filters:
        audit_total_stmt = audit_total_stmt.where(*audit_filters)
        audit_stmt = audit_stmt.where(*audit_filters)

    req_meta = page_meta(db.scalar(req_total_stmt) or 0, req_page, per_page)
    sys_meta = page_meta(db.scalar(sys_total_stmt) or 0, sys_page, per_page)
    audit_meta = page_meta(db.scalar(audit_total_stmt) or 0, audit_page, per_page)
    req_logs = db.scalars(
        req_stmt.offset((int(req_meta["page"]) - 1) * per_page).limit(per_page)
    ).all()
    sys_logs = db.scalars(
        sys_stmt.offset((int(sys_meta["page"]) - 1) * per_page).limit(per_page)
    ).all()
    audits = db.scalars(
        audit_stmt.offset((int(audit_meta["page"]) - 1) * per_page).limit(per_page)
    ).all()
    stats = {
        "request_total": db.scalar(select(func.count()).select_from(RequestLog)) or 0,
        "request_failed": db.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.status_code >= 400)) or 0,
        "system_errors": db.scalar(select(func.count()).select_from(SystemLog).where(SystemLog.level.in_(["ERROR", "WARNING"]))) or 0,
        "audits": db.scalar(select(func.count()).select_from(OperationAudit)) or 0,
    }
    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "req_logs": req_logs,
            "sys_logs": sys_logs,
            "audits": audits,
            "q": q,
            "stats": stats,
            "log_settings": log_settings,
            "req_meta": req_meta,
            "sys_meta": sys_meta,
            "audit_meta": audit_meta,
        },
    )


@router.post("/logs/settings")
def save_log_settings(
    request: Request,
    enable_request_logs: str | None = Form(None),
    enable_system_logs: str | None = Form(None),
    log_request_body: str | None = Form(None),
    log_upstream_body: str | None = Form(None),
    log_upstream_model: str | None = Form(None),
    mask_sensitive_logs: str | None = Form(None),
    log_retention_days: str = Form("30"),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    try:
        retention_days = str(max(1, int(log_retention_days or "30")))
    except ValueError:
        retention_days = "30"
    updates = {
        "enable_request_logs": "true" if enable_request_logs == "on" else "false",
        "enable_system_logs": "true" if enable_system_logs == "on" else "false",
        "log_request_body": "true" if log_request_body == "on" else "false",
        "log_upstream_body": "true" if log_upstream_body == "on" else "false",
        "log_upstream_model": "true" if log_upstream_model == "on" else "false",
        "mask_sensitive_logs": "true" if mask_sensitive_logs == "on" else "false",
        "log_retention_days": retention_days,
    }
    for key, value in updates.items():
        set_setting(db, key, value)
    db.commit()
    audit(db, request, "log_settings_update", "logs", json.dumps(updates, ensure_ascii=False))
    return RedirectResponse("/admin/logs?updated=settings", status_code=303)


@router.get("/logs/export")
def export_request_logs(db: Session = Depends(get_db), _: None = Depends(require_admin)):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["created_at", "request_id", "api_key_name", "source_ip", "endpoint", "model", "status_code", "duration_ms", "prompt_chars", "completion_chars", "error"])
    for item in db.scalars(select(RequestLog).order_by(desc(RequestLog.created_at)).limit(10000)).all():
        writer.writerow([
            item.created_at,
            item.request_id,
            item.api_key_name,
            item.source_ip,
            item.endpoint,
            item.model,
            item.status_code,
            item.duration_ms,
            item.prompt_chars,
            item.completion_chars,
            item.error,
        ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=request-logs.csv"},
    )


@router.post("/logs/cleanup")
def cleanup_logs(
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    days = int(get_setting(db, "log_retention_days", "30") or "30")
    cutoff = datetime.utcnow() - timedelta(days=days)
    for model in (RequestLog, SystemLog, OperationAudit):
        db.query(model).filter(model.created_at < cutoff).delete()
    db.commit()
    audit(db, request, "logs_cleanup", "logs", f"retention_days={days}")
    return RedirectResponse("/admin/logs?updated=cleanup", status_code=303)


@router.post("/logs/bulk-delete")
async def bulk_delete_logs(
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    form = await request.form()
    log_type = str(form.get("log_type") or "").strip()
    return_url = safe_logs_return_url(str(form.get("return_url") or ""))
    ids: list[int] = []
    for raw in form.getlist("log_ids"):
        try:
            ids.append(int(str(raw)))
        except ValueError:
            continue
    ids = sorted(set(ids))
    model_map = {
        "request": RequestLog,
        "system": SystemLog,
        "audit": OperationAudit,
    }
    model = model_map.get(log_type)
    if model and ids:
        deleted = db.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        if log_type != "audit":
            audit(db, request, "logs_bulk_delete", f"{log_type}_logs", f"count={deleted}")
    return RedirectResponse(return_url, status_code=303)


@router.post("/logs/clear")
def clear_all_logs(
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    request_count = db.query(RequestLog).delete(synchronize_session=False)
    system_count = db.query(SystemLog).delete(synchronize_session=False)
    audit_count = db.query(OperationAudit).delete(synchronize_session=False)
    db.commit()
    return RedirectResponse(
        f"/admin/logs?updated=clear&request_count={request_count}&system_count={system_count}&audit_count={audit_count}",
        status_code=303,
    )
