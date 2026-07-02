import asyncio
import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.baidu import BaiduAdapter
from app.db.init_db import get_setting
from app.db.models import ApiKey, BaiduConversation, ConversationTurn, ModelConfig
from app.db.session import SessionLocal, get_db
from app.schemas.openai import ChatCompletionRequest, ChatMessage, ImageGenerationRequest, ResponsesRequest
from app.services.auth_service import enforce_access_policy, get_api_key
from app.services.logging_service import request_timer, system_log, traced_system_log

router = APIRouter()


@dataclass
class ConversationBinding:
    conversation_id: int
    turn_id: int
    local_conversation_id: str
    baidu_session_id: str
    rank: int
    credential_id: int | None = None
    cookie_snapshot: str = ""


_CONVERSATION_LOCKS: dict[str, asyncio.Lock] = {}
_CONVERSATION_LOCKS_GUARD = threading.Lock()
_TOOL_RESULT_CACHE: dict[str, dict[str, tuple[float, str]]] = {}
_TOOL_RESULT_CACHE_GUARD = threading.Lock()
_TOOL_RESULT_CACHE_TTL_SECONDS = 6 * 60 * 60


def _get_conversation_lock(local_id: str) -> asyncio.Lock:
    with _CONVERSATION_LOCKS_GUARD:
        lock = _CONVERSATION_LOCKS.get(local_id)
        if lock is None:
            lock = asyncio.Lock()
            _CONVERSATION_LOCKS[local_id] = lock
        return lock


def _release_conversation_lock(lock: asyncio.Lock | None) -> None:
    if lock and lock.locked():
        lock.release()


def now_ts() -> int:
    return int(time.time())


def sse_line(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def sse_event(event: str, payload: dict[str, Any]) -> str:
    return (
        f"event: {event}\n"
        + "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )


def message_dicts(messages: list[Any]) -> list[dict[str, Any]]:
    return [m.model_dump() if hasattr(m, "model_dump") else dict(m) for m in messages]


def latest_user_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in reversed(messages):
        if item.get("role") == "user":
            return [item]
    return messages[-1:] if messages else []


def first_user_text(messages: list[dict[str, Any]]) -> str:
    for item in messages:
        if item.get("role") != "user":
            continue
        content = item.get("content", "")
        if isinstance(content, list):
            return "\n".join(str(part.get("text", part)) for part in content)
        return str(content)
    return ""


def _message_content_text(content: Any) -> str:
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or part))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content or "")


def has_recent_tool_result(messages: list[dict[str, Any]]) -> bool:
    return bool(messages and messages[-1].get("role") == "tool")


def _looks_like_cached_tool_result(text: str) -> bool:
    lowered = text.lower()
    return "[cached]" in lowered or "\u8be5\u6587\u4ef6\u5185\u5bb9\u672a\u53d8\u5316" in text or "\u5185\u5bb9\u672a\u53d8\u5316" in text


def _looks_like_truncated_tool_result(text: str) -> bool:
    return "\u7ed3\u679c\u5df2\u622a\u65ad" in text or ("\u663e\u793a\u524d" in text and "\u5171" in text and "\u5b57\u7b26" in text)


def _tool_result_cache_get(cache_key: str, fingerprint: str) -> str:
    if not cache_key or not fingerprint:
        return ""
    now = time.time()
    with _TOOL_RESULT_CACHE_GUARD:
        bucket = _TOOL_RESULT_CACHE.get(cache_key)
        if not bucket:
            return ""
        stale = [key for key, (ts, _) in bucket.items() if now - ts > _TOOL_RESULT_CACHE_TTL_SECONDS]
        for key in stale:
            bucket.pop(key, None)
        item = bucket.get(fingerprint)
        return item[1] if item else ""


def _tool_result_cache_set(cache_key: str, fingerprint: str, content: str) -> None:
    if not cache_key or not fingerprint or not content or _looks_like_cached_tool_result(content):
        return
    with _TOOL_RESULT_CACHE_GUARD:
        bucket = _TOOL_RESULT_CACHE.setdefault(cache_key, {})
        bucket[fingerprint] = (time.time(), content[-24000:])
        if len(bucket) > 80:
            for key, _ in sorted(bucket.items(), key=lambda item: item[1][0])[:20]:
                bucket.pop(key, None)


def _store_tool_results_from_messages(messages: list[dict[str, Any]], cache_key: str) -> None:
    if not cache_key:
        return
    call_map: dict[str, str] = {}
    for item in messages:
        if item.get("role") == "assistant":
            for call in item.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                fingerprint = _tool_call_fingerprint(call)
                if call_id and fingerprint and not fingerprint.startswith(":"):
                    call_map[call_id] = fingerprint
        if item.get("role") == "tool":
            raw = _message_content_text(item.get("content", ""))
            if not raw or _looks_like_cached_tool_result(raw):
                continue
            call_id = str(item.get("tool_call_id") or "")
            fingerprint = call_map.get(call_id)
            if not fingerprint:
                name = str(item.get("name") or "")
                fingerprint = f"{name}:tool_result" if name else ""
            _tool_result_cache_set(cache_key, fingerprint, raw)


def _latest_assistant_call_fingerprint(messages: list[dict[str, Any]]) -> str:
    assistant_call = next((item for item in reversed(messages[:-1]) if item.get("role") == "assistant" and item.get("tool_calls")), None)
    for call in (assistant_call or {}).get("tool_calls") or []:
        if isinstance(call, dict):
            fingerprint = _tool_call_fingerprint(call)
            if fingerprint and not fingerprint.startswith(":"):
                return fingerprint
    return ""


def _recent_tool_result_context(
    messages: list[dict[str, Any]],
    cache_key: str = "",
    latest_fingerprint: str = "",
    limit: int = 4,
    max_chars: int = 24000,
) -> str:
    snippets: list[str] = []
    total = 0
    cached = _tool_result_cache_get(cache_key, latest_fingerprint)
    if cached:
        clipped = cached[-8000:]
        snippets.append(f"Project cache for the same tool arguments:\n{clipped}")
        total += len(clipped)
    for item in reversed(messages[:-1]):
        if item.get("role") != "tool":
            continue
        raw = _message_content_text(item.get("content", ""))
        if not raw or _looks_like_cached_tool_result(raw):
            continue
        tool_name = item.get("name") or item.get("tool_call_id") or "tool"
        snippet_limit = max(1000, min(8000, max_chars - total))
        snippet = raw[:snippet_limit]
        if len(raw) > snippet_limit:
            snippet += f"\n...[tool result clipped by proxy, original length={len(raw)} chars]"
        entry = f"Earlier real result from tool {tool_name}:\n{snippet}"
        snippets.append(entry)
        total += len(entry)
        if len(snippets) >= limit or total >= max_chars:
            break
    snippets.reverse()
    return "\n\n".join(snippets)


def _short_tool_args(call: dict[str, Any]) -> str:
    args = _tool_call_arguments(call)
    if not args:
        return "{}"
    return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:500]


def _recent_tool_call_summaries(messages: list[dict[str, Any]], limit: int = 8) -> list[str]:
    summaries: list[str] = []
    for item in reversed(messages):
        if item.get("role") != "assistant" or not item.get("tool_calls"):
            continue
        for call in item.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = _tool_call_name(call) or "tool"
            summaries.append(f"- {name} args={_short_tool_args(call)}")
            if len(summaries) >= limit:
                return list(reversed(summaries))
    return list(reversed(summaries))


def _latest_assistant_tool_call(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    assistant_call = next((item for item in reversed(messages[:-1]) if item.get("role") == "assistant" and item.get("tool_calls")), None)
    calls = (assistant_call or {}).get("tool_calls") or []
    return calls[-1] if calls and isinstance(calls[-1], dict) else None


def _tool_names_for_prompt(tools: list[dict[str, Any]] | None) -> str:
    names = sorted(_tool_function_names(tools))
    return ", ".join(names[:30])


def _schema_has_enum(schema: Any) -> bool:
    if isinstance(schema, dict):
        if "enum" in schema:
            return True
        return any(_schema_has_enum(value) for value in schema.values())
    if isinstance(schema, list):
        return any(_schema_has_enum(item) for item in schema)
    return False


def _compact_schema_for_prompt(schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    compact: dict[str, Any] = {}
    if schema.get("required"):
        compact["required"] = schema.get("required")
    if isinstance(properties, dict):
        compact_props: dict[str, Any] = {}
        for key, prop in properties.items():
            if not isinstance(prop, dict):
                continue
            item: dict[str, Any] = {}
            for field in ("type", "enum", "items", "anyOf", "oneOf"):
                if field in prop:
                    item[field] = prop[field]
            compact_props[key] = item or {"type": _schema_type(prop) or "string"}
        if compact_props:
            compact["properties"] = compact_props
    return compact


def _compact_enum_tool_schema_lines(tools: list[dict[str, Any]] | None) -> list[str]:
    lines: list[str] = []
    for tool in tools or []:
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "")
        params = fn.get("parameters") or {}
        if not name or not isinstance(params, dict):
            continue
        if "memory" not in name.lower() and not _schema_has_enum(params):
            continue
        compact = _compact_schema_for_prompt(params)
        if compact:
            lines.append(f"- {name}: parameters={json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}")
    return lines


def _looks_like_file_write_task(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "保存",
        "写入",
        "创建文档",
        "创建文件",
        "生成文档",
        "补齐",
        "正文",
        ".md",
        "write",
        "save",
        "file",
    ]
    return any(marker in lowered or marker in text for marker in markers)


def _compact_tool_result_messages(
    messages: list[dict[str, Any]],
    force_final: bool = False,
    cache_key: str = "",
    tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not has_recent_tool_result(messages):
        return messages
    _store_tool_results_from_messages(messages, cache_key)
    result = messages[-1]
    previous_user = latest_user_message(messages[:-1])
    latest_call = _latest_assistant_tool_call(messages)
    latest_fingerprint = _latest_assistant_call_fingerprint(messages)
    tool_name = result.get("name") or result.get("tool_call_id") or (latest_call and _tool_call_name(latest_call)) or "tool"
    result_text = _message_content_text(result.get("content", ""))
    prior_context = ""
    if _looks_like_cached_tool_result(result_text):
        prior_context = _recent_tool_result_context(messages, cache_key=cache_key, latest_fingerprint=latest_fingerprint, limit=2, max_chars=10000)

    visible_result = result_text
    if len(visible_result) > 12000:
        visible_result = visible_result[:12000] + f"\n...[tool result clipped by proxy, original length={len(result_text)} chars]"

    completed = _recent_tool_call_summaries(messages[:-1], limit=8)
    user_text = _message_content_text(previous_user[0].get("content", "")) if previous_user else ""
    available_tools = _tool_names_for_prompt(tools)
    write_task = _looks_like_file_write_task(user_text)
    latest_call_text = ""
    if latest_call:
        latest_call_text = f"{_tool_call_name(latest_call)} args={_short_tool_args(latest_call)}"

    content = [
        "Tokeny tool result update (compressed by proxy).",
        "Treat the tool result as factual state. Do not restart planning from scratch.",
    ]
    if user_text:
        content.append(f"Original user request:\n{user_text}")
    if completed:
        content.append("Completed tool calls in recent context:\n" + "\n".join(completed))
    if latest_call_text:
        content.append(f"Latest tool call:\n{latest_call_text}")
    content.append(f"Latest tool result from {tool_name}:\n{visible_result}")
    if prior_context:
        content.append(
            "The latest tool result is a client cache notice. Use this earlier real result instead of repeating the exact same read:\n"
            + prior_context
        )
    if _looks_like_truncated_tool_result(result_text):
        content.append(
            "The result was truncated but still useful. Continue from the visible content. "
            "Only read another startLine/endLine range if the hidden part is required."
        )

    next_rules = [
        "Next-step rules:",
        "- Do not repeat a successful list_files/read_file/glob/grep call with identical arguments.",
        "- Reading different files or a different line range is allowed when it is genuinely required.",
        "- If enough information is already available, continue the task instead of inspecting directories again.",
    ]
    if write_task and not force_final:
        next_rules.extend(
            [
                "- This is a client workspace file/document task. If the target content and path are clear, your next response MUST call modify_file/write_file.",
                "- Do not output the document body as normal prose when the user asked to save/create a local file.",
                "- For long files, call modify_file once with operation=write for the first segment, then append different later segments in later turns.",
            ]
        )
    if force_final:
        next_rules.append("- Now provide the final natural-language answer based on the tool result; do not call another tool unless the user explicitly asks.")
    if available_tools:
        next_rules.append(f"Available tool names: {available_tools}. Use exact names and parameter names from prior schema.")
    next_rules.append(
        'If you call a tool, output only DSML: <|DSML| tool_calls><|DSML| invoke name="tool_name"><|DSML| parameter name="parameter_name">parameter_value</|DSML| parameter></|DSML| invoke></|DSML| tool_calls>'
    )
    content.append("\n".join(next_rules))
    return [{"role": "user", "content": "\n\n".join(content)}]


def tool_result_messages(
    messages: list[dict[str, Any]],
    force_final: bool = False,
    cache_key: str = "",
    profile: str = "auto",
    compact_tokeny: bool = False,
    tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not has_recent_tool_result(messages):
        return messages
    if profile == "tokeny" and compact_tokeny:
        return _compact_tool_result_messages(messages, force_final=force_final, cache_key=cache_key, tools=tools)
    _store_tool_results_from_messages(messages, cache_key)
    result = messages[-1]
    previous_user = latest_user_message(messages[:-1])
    assistant_call = next((item for item in reversed(messages[:-1]) if item.get("role") == "assistant" and item.get("tool_calls")), None)
    latest_fingerprint = _latest_assistant_call_fingerprint(messages)
    content = [
        "The client has just executed a tool call. The following tool result is the highest-priority fact for this turn.",
        "If the tool result conflicts with your plan, memory, or guess, trust the tool result.",
    ]
    if previous_user:
        content.append(f"Original user request:\n{_message_content_text(previous_user[0].get('content', ''))}")
    if assistant_call:
        content.append(f"Previous assistant tool call:\n{json.dumps(assistant_call.get('tool_calls'), ensure_ascii=False)}")
    tool_name = result.get("name") or result.get("tool_call_id") or "tool"
    result_text = _message_content_text(result.get("content", ""))
    content.append(f"Tool name/id: {tool_name}")
    content.append(f"Tool execution result:\n{result_text}")
    if _looks_like_cached_tool_result(result_text):
        prior_context = _recent_tool_result_context(messages, cache_key=cache_key, latest_fingerprint=latest_fingerprint)
        content.append(
            "Important: the current tool result is a client cache notice, not an empty file and not a failed read. "
            "Use the earlier real tool result excerpt below. If it is missing or insufficient, read a different range "
            "with startLine/endLine instead of repeating the exact same tool arguments."
        )
        if prior_context:
            content.append(prior_context)
    if _looks_like_truncated_tool_result(result_text):
        content.append(
            "Important: a truncated tool result is still useful. Continue from the visible content first. "
            "If the hidden tail is required, request the remaining range with startLine/endLine."
        )
    if force_final:
        content.append("Now produce the final natural-language answer based on the tool result. Do not call the same tool again unless the user explicitly asks.")
    else:
        content.append("Continue the task based on the tool result. Call another tool only if it is a necessary different next step; do not repeat the same successful tool call.")
    return [{"role": "user", "content": "\n\n".join(content)}]


DSML_INVOKE_RE = re.compile(
    r"<\s*\|?\s*DSML\s*\|?\s*invoke\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)<\s*/\s*\|?\s*DSML\s*\|?\s*invoke\s*>",
    re.I | re.S,
)
DSML_PARAM_RE = re.compile(
    r"<\s*\|?\s*DSML\s*\|?\s*parameter\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)<\s*/\s*\|?\s*DSML\s*\|?\s*parameter\s*>",
    re.I | re.S,
)


def _tool_function_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        if tool.get("type") == "function":
            fn = tool.get("function") or {}
            if fn.get("name"):
                names.add(str(fn["name"]))
        elif tool.get("name"):
            names.add(str(tool["name"]))
    return names


def _tool_prompt(
    tools: list[dict[str, Any]] | None,
    tool_choice: Any = None,
    profile: str = "auto",
    after_tool_result: bool = False,
    loop_protection: bool = True,
    route_instruction: str = "",
    compact_after_tool: bool = False,
) -> str:
    if not tools or tool_choice == "none":
        return ""
    use_compact_schema = profile == "tokeny" and after_tool_result and compact_after_tool
    if use_compact_schema:
        lines = [
            "Compact tool calling protocol: tools are still available.",
            "When a tool is needed, output only one DSML tool-call structure and no prose.",
            '<|DSML| tool_calls><|DSML| invoke name="tool_name"><|DSML| parameter name="parameter_name">parameter_value</|DSML| parameter></|DSML| invoke></|DSML| tool_calls>',
            f"Available tool names: {_tool_names_for_prompt(tools)}.",
            "Use the exact parameter names from earlier tool schemas and the latest tool result context.",
        ]
        enum_schema_lines = _compact_enum_tool_schema_lines(tools)
        if enum_schema_lines:
            lines.append("Critical compact schemas for enum/action tools:")
            lines.extend(enum_schema_lines[:12])
    else:
        lines = [
            "Tool calling protocol: you may use the tools provided by the client.",
            "When a tool is needed, do not explain in natural language and do not output the tool call as normal prose.",
            "If you call a tool, the entire assistant response must contain only one tool-call structure, with no text before or after it.",
            "Strictly output this DSML format; the server will convert it into standard OpenAI tool_calls for the client:",
            '<|DSML| tool_calls><|DSML| invoke name="tool_name"><|DSML| parameter name="parameter_name">parameter_value</|DSML| parameter></|DSML| invoke></|DSML| tool_calls>',
            "Available tools:",
        ]
    if profile == "tokeny":
        lines.extend(
            [
                "Tokeny agent compatibility rules:",
                "- Use the exact function names and exact parameter names from the available tools below.",
                "- For file operations, use relative file paths unless the user explicitly provides an absolute path.",
                "- If the user asks to create a document and no directory is given, write to the current workspace using only the filename.",
                "- Put the whole file body inside the content parameter; do not leak markdown outside the DSML parameter.",
                "- If several tool steps are required, call exactly one tool now and wait for the tool result before calling the next one.",
                "- When writing files with modify_file, the content parameter must be normal UTF-8 Chinese text. Do not output mojibake/transcoded text such as 绗, 鍙, 涓, 鎴, 锛, 銆 or �.",
                "- If you notice file content contains mojibake, read the source again and regenerate clean UTF-8 Chinese before calling modify_file.",
            ]
        )
    if route_instruction:
        lines.extend(["Output routing rules:", route_instruction])
    if after_tool_result and loop_protection:
        lines.extend(
            [
                "A client tool result has already been returned in this turn.",
                "Do not repeat the same successful tool call with the same arguments.",
                "Only call another tool if it is a different necessary next step; otherwise answer the user in natural language.",
            ]
        )
    if not use_compact_schema:
        for tool in tools:
            fn = tool.get("function") if tool.get("type") == "function" else tool
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not name:
                continue
            desc = fn.get("description") or ""
            params = fn.get("parameters") or {}
            lines.append(f"- {name}: {desc}; parameters={json.dumps(params, ensure_ascii=False)}")
    return "\n".join(lines)


def _append_tool_prompt(
    query: str,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any = None,
    profile: str = "auto",
    after_tool_result: bool = False,
    loop_protection: bool = True,
    route_instruction: str = "",
    compact_after_tool: bool = False,
) -> str:
    prompt = _tool_prompt(tools, tool_choice, profile, after_tool_result, loop_protection, route_instruction, compact_after_tool)
    if not prompt:
        return query
    return f"{query}\n\n{prompt}".strip()


def _strip_dsml(text: str) -> str:
    return re.sub(r"<\s*/?\s*\|?\s*DSML\s*\|?[^>]*>", "", text, flags=re.I).strip()


def _make_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "call_" + uuid.uuid4().hex[:24],
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _tool_call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    fn = call.get("function") if isinstance(call, dict) else None
    if not isinstance(fn, dict):
        return {}
    arguments = fn.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            loaded = json.loads(arguments)
            return loaded if isinstance(loaded, dict) else {"input": loaded}
        except Exception:
            return {"input": arguments}
    return arguments if isinstance(arguments, dict) else {"input": arguments}


def _tool_call_name(call: dict[str, Any]) -> str:
    fn = call.get("function") if isinstance(call, dict) else None
    if isinstance(fn, dict):
        return str(fn.get("name") or "").strip()
    return str(call.get("name") or "").strip() if isinstance(call, dict) else ""


def _tool_call_fingerprint(call: dict[str, Any]) -> str:
    name = _tool_call_name(call)
    args = _tool_call_arguments(call)
    return f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"


def _tool_path_value(arguments: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("filePath", "path", "source", "target", "from", "to", "src", "dst"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return "->".join(values)


def _stable_tool_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if value is None:
        return ""
    return str(value).strip()


def _tool_content_hash(value: Any) -> str:
    text = _stable_tool_value(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""


def _path_list_value(arguments: dict[str, Any]) -> str:
    paths: list[str] = []
    file_paths = arguments.get("filePaths")
    if isinstance(file_paths, list):
        paths.extend(str(item).strip() for item in file_paths if str(item).strip())
    for key in ("filePath", "path", "source", "destination", "target", "from", "to", "src", "dst"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    return "|".join(sorted(paths))


def _tool_smart_repeat_signature(call: dict[str, Any]) -> str:
    name = _tool_call_name(call).strip()
    if not name:
        return ""
    normalized_name = name.lower()
    args = _tool_call_arguments(call)

    if normalized_name in {"read_file", "read_text", "read"}:
        parts = [
            name,
            _stable_tool_value(args.get("filePath") or args.get("path")),
            _stable_tool_value(args.get("startLine")),
            _stable_tool_value(args.get("endLine")),
            _stable_tool_value(args.get("offset")),
            _stable_tool_value(args.get("limit")),
        ]
        return "smart:read:" + ":".join(parts)

    if normalized_name in {"list_files", "ls", "glob", "grep", "search"}:
        important = {
            key: args.get(key)
            for key in (
                "path",
                "directory",
                "pattern",
                "fileGlob",
                "recursive",
                "all",
                "long",
                "contextLines",
                "maxResults",
                "excludePatterns",
            )
            if key in args
        }
        return f"smart:{name}:{json.dumps(important, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"

    if normalized_name in {"modify_file", "write_file", "create_file", "edit_file"}:
        parts = [
            name,
            _stable_tool_value(args.get("filePath") or args.get("path")),
            _stable_tool_value(args.get("operation") or "write"),
            _stable_tool_value(args.get("startLine")),
            _stable_tool_value(args.get("endLine")),
            _stable_tool_value(args.get("afterLine")),
            _tool_content_hash(args.get("content")),
            _tool_content_hash(args.get("searchText")),
        ]
        return "smart:write:" + ":".join(parts)

    if normalized_name in {"delete_file", "remove_file", "move_file", "rename_file", "copy_file"}:
        parts = [
            name,
            _path_list_value(args),
            _stable_tool_value(args.get("operation")),
        ]
        return "smart:fileop:" + ":".join(parts)

    return "smart:" + _tool_call_fingerprint(call)


def _tool_repeat_signature(call: dict[str, Any], match_mode: str = "exact") -> str:
    if match_mode == "none":
        return ""
    if match_mode == "smart":
        return _tool_smart_repeat_signature(call)
    if match_mode == "path":
        name = _tool_call_name(call)
        path_value = _tool_path_value(_tool_call_arguments(call))
        return f"{name}:path:{path_value}" if name and path_value else ""
    return _tool_call_fingerprint(call)


def _recent_tool_fingerprints(messages: list[dict[str, Any]], limit: int = 12, match_mode: str = "exact") -> set[str]:
    fingerprints: set[str] = set()
    if match_mode == "none":
        return fingerprints
    for item in reversed(messages):
        if item.get("role") != "assistant" or not item.get("tool_calls"):
            continue
        for call in item.get("tool_calls") or []:
            if isinstance(call, dict):
                fingerprint = _tool_repeat_signature(call, match_mode)
                if fingerprint and not fingerprint.startswith(":"):
                    fingerprints.add(fingerprint)
        if len(fingerprints) >= limit:
            break
    return fingerprints


def _tool_repeat_protection_enabled(db: Session) -> bool:
    return get_setting(db, "tool_repeat_protection_enabled", "false").lower() == "true"


def _tool_repeat_match_mode(db: Session) -> str:
    mode = get_setting(db, "tool_repeat_match_mode", "smart").lower()
    return mode if mode in {"none", "smart", "exact", "path"} else "smart"


def _tool_repeat_scope(db: Session) -> str:
    scope = get_setting(db, "tool_repeat_protection_scope", "delete_move").lower()
    return scope if scope in {"off", "delete_move", "write", "all"} else "delete_move"


def _tool_in_repeat_scope(call: dict[str, Any], scope: str) -> bool:
    if scope == "off":
        return False
    name = _tool_call_name(call).lower()
    write_tools = {"modify_file", "write_file"}
    delete_move_tools = {"delete_file", "remove_file", "move_file", "rename_file"}
    if scope == "all":
        return True
    if scope == "write":
        return name in write_tools
    if scope == "delete_move":
        return name in delete_move_tools
    return False


def _filter_repeated_tool_calls(
    calls: list[dict[str, Any]],
    recent_fingerprints: set[str],
    db: Session,
    trace_id: str,
    binding: ConversationBinding | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not calls or not recent_fingerprints:
        return calls, []
    protection_enabled = _tool_repeat_protection_enabled(db)
    match_mode = _tool_repeat_match_mode(db)
    scope = _tool_repeat_scope(db)
    if match_mode == "none" or scope == "off":
        protection_enabled = False
    kept: list[dict[str, Any]] = []
    blocked: list[str] = []
    for call in calls:
        fingerprint = _tool_repeat_signature(call, match_mode)
        if fingerprint in recent_fingerprints:
            fn = call.get("function") or {}
            repeated = f"{fn.get('name') or 'tool'} args={fn.get('arguments') or '{}'}"
            if protection_enabled and _tool_in_repeat_scope(call, scope):
                blocked.append(repeated)
                continue
            _log_tool_event(db, "INFO", f"observed repeated tool call, not blocked: {repeated}", trace_id)
        kept.append(call)
    if blocked:
        summary = "; ".join(blocked[:3])
        _log_tool_event(db, "WARNING", f"blocked repeated tool call(s): {summary}", trace_id)
        _note_conversation_turn(db, binding, f"blocked repeated tool call(s): {summary}")
    return kept, blocked


def _repeated_tool_block_text(blocked: list[str]) -> str:
    summary = "；".join(blocked[:3])
    return (
        "检测到模型正在重复执行已经执行过的相同写入/删除类工具和参数，项目已拦截本轮高风险工具调用，避免重复改写文件。"
        "请基于客户端已经返回的工具结果继续完成任务；如果确实需要重新写入，请调整目标路径或内容后再调用。"
        f" 被拦截：{summary}"
    )


def _tool_schema(tools: list[dict[str, Any]] | None, name: str) -> dict[str, Any]:
    for tool in tools or []:
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if isinstance(fn, dict) and fn.get("name") == name:
            params = fn.get("parameters") or {}
            return params if isinstance(params, dict) else {}
    return {}


def _schema_type(schema: dict[str, Any]) -> str:
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        return raw_type
    if isinstance(raw_type, list):
        for item in raw_type:
            if item != "null":
                return str(item)
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict):
                    variant_type = _schema_type(variant)
                    if variant_type:
                        return variant_type
    return ""


def _jsonish(value: str) -> Any:
    stripped = value.strip()
    if not stripped:
        return value
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    if re.fullmatch(r"-?\d+", stripped):
        try:
            return int(stripped)
        except ValueError:
            return value
    if re.fullmatch(r"-?\d+\.\d+", stripped):
        try:
            return float(stripped)
        except ValueError:
            return value
    if stripped[0] in "[{":
        try:
            return json.loads(stripped)
        except Exception:
            return value
    return value


def _coerce_value_for_schema(value: Any, schema: dict[str, Any]) -> Any:
    expected = _schema_type(schema)
    if isinstance(value, str):
        parsed = _jsonish(value)
    else:
        parsed = value
    if expected in {"integer", "number"} and isinstance(parsed, str):
        try:
            return int(parsed) if expected == "integer" else float(parsed)
        except ValueError:
            return parsed
    if expected == "boolean" and isinstance(parsed, str):
        lowered = parsed.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    if expected == "array" and isinstance(parsed, str):
        try:
            loaded = json.loads(parsed)
            return loaded if isinstance(loaded, list) else parsed
        except Exception:
            return parsed
    if expected == "object" and isinstance(parsed, str):
        try:
            loaded = json.loads(parsed)
            return loaded if isinstance(loaded, dict) else parsed
        except Exception:
            return parsed
    if expected == "array" and not isinstance(parsed, list):
        return parsed
    if expected == "object" and not isinstance(parsed, dict):
        return parsed
    return parsed


def _coerce_tool_arguments(name: str, arguments: dict[str, Any], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    schema = _tool_schema(tools, name)
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        properties = {}
    coerced: dict[str, Any] = {}
    for key, value in arguments.items():
        prop_schema = properties.get(key)
        coerced[key] = _coerce_value_for_schema(value, prop_schema if isinstance(prop_schema, dict) else {})
    return coerced


MOJIBAKE_MARKERS = (
    "绗", "绔", "锛", "涓", "鍙", "鎴", "鍚", "濂", "鐨", "杩", "鏄", "銆", "撳", "滄", "蹭", "犲", "鐢",
    "喉模", "瘉缓", "畸纡", "撻唔",
    "�",
)


def _split_mojibake_markers(raw: str) -> tuple[str, ...]:
    markers = [item.strip() for item in re.split(r"[,，\r\n]+", raw or "") if item.strip() and item.strip() != "?"]
    return tuple(dict.fromkeys(markers))


def _mojibake_markers(db: Session | None = None) -> tuple[str, ...]:
    if db is None:
        return MOJIBAKE_MARKERS
    raw = get_setting(db, "tool_mojibake_markers", "")
    return _split_mojibake_markers(raw) or MOJIBAKE_MARKERS


def _looks_like_mojibake(text: str, markers: tuple[str, ...] = MOJIBAKE_MARKERS) -> bool:
    if not text:
        return False
    marker_hits = sum(text.count(marker) for marker in markers)
    replacement_hits = text.count("�")
    if replacement_hits >= 1 and marker_hits >= 2:
        return True
    if replacement_hits >= 2:
        return True
    if marker_hits >= 5:
        return True
    sample = text[:2000]
    sample_hits = sum(sample.count(marker) for marker in markers)
    return sample_hits >= 5 and sample_hits / max(len(sample), 1) > 0.003


def _looks_like_severe_mojibake(text: str, markers: tuple[str, ...] = MOJIBAKE_MARKERS) -> bool:
    if not text:
        return False
    marker_hits = sum(text.count(marker) for marker in markers)
    replacement_hits = text.count("�")
    if replacement_hits >= 10:
        return True
    if marker_hits >= 25:
        return True
    sample = text[:4000]
    sample_hits = sum(sample.count(marker) for marker in markers)
    return sample_hits >= 12 and sample_hits / max(len(sample), 1) > 0.008


def _tool_safety_config(db: Session | None = None) -> dict[str, Any]:
    if db is None:
        return {"arg": True, "path": True, "mojibake": True, "markers": MOJIBAKE_MARKERS}
    return {
        "arg": get_setting(db, "tool_arg_safety_enabled", "true").lower() == "true",
        "path": get_setting(db, "tool_path_safety_enabled", "true").lower() == "true",
        "mojibake": get_setting(db, "tool_mojibake_safety_enabled", "true").lower() == "true",
        "markers": _mojibake_markers(db),
    }


def _tool_args_block_reason(name: str, arguments: dict[str, Any], safety: dict[str, Any] | None = None) -> str:
    safety = safety or _tool_safety_config()
    if not safety.get("arg", True):
        return ""
    if name != "modify_file":
        return ""
    file_path = arguments.get("filePath") or arguments.get("path") or ""
    if safety.get("path", True) and isinstance(file_path, str):
        normalized_path = file_path.strip()
        if (
            "\n" in normalized_path
            or "\r" in normalized_path
            or len(normalized_path) > 260
            or re.search(r"\.md\s*(write|append|#|##)", normalized_path, re.I)
        ):
            return "modify_file.filePath looks malformed and appears to contain operation/content"
    content = arguments.get("content")
    markers = safety.get("markers", MOJIBAKE_MARKERS)
    if not isinstance(markers, tuple):
        markers = MOJIBAKE_MARKERS
    if safety.get("mojibake", True) and isinstance(content, str) and _looks_like_severe_mojibake(content, markers):
        return "modify_file.content has severe mojibake/transcoded text"
    return ""



def _blocked_tool_text(reason: str) -> str:
    return (
        "Tool call was blocked by the proxy because the generated arguments look unsafe or malformed. "
        "Please regenerate a valid tool call with a clean file path and normal UTF-8 content. "
        f"Reason: {reason}"
    )


def _looks_like_malformed_modify_file_text(text: str) -> bool:
    if "modify_file" not in text:
        return False
    file_path_match = re.search(
        r"<\s*\|?\s*DSML\s*\|?\s*parameter\s+name=[\"'](?:filePath|path)[\"'][^>]*>(.*?)<\s*/\s*\|?\s*DSML\s*\|?\s*parameter\s*>",
        text,
        re.I | re.S,
    )
    if not file_path_match:
        return False
    file_path = _strip_dsml(file_path_match.group(1)).strip()
    return bool(
        "\n" in file_path
        or "\r" in file_path
        or len(file_path) > 260
        or re.search(r"\.md\s*(write|append|#|##)", file_path, re.I)
    )


def _parse_tool_calls(
    text: str,
    tools: list[dict[str, Any]] | None = None,
    safety: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    if not text:
        return []
    if _maybe_tool_call_markup(text):
        text = _normalize_tool_markup(text)
    allowed = _tool_function_names(tools)
    calls: list[dict[str, Any]] = []
    for match in DSML_INVOKE_RE.finditer(text):
        name = match.group(1).strip()
        if allowed and name not in allowed:
            continue
        body = match.group(2)
        args: dict[str, Any] = {}
        for param in DSML_PARAM_RE.finditer(body):
            key = param.group(1).strip()
            value = _strip_dsml(param.group(2))
            args[key] = value
        args = _coerce_tool_arguments(name, args, tools)
        if _tool_args_block_reason(name, args, safety):
            continue
        calls.append(_make_tool_call(name, args))
    if calls:
        return calls
    try:
        data = json.loads(text.strip())
    except Exception:
        return []
    raw_calls = data.get("tool_calls") if isinstance(data, dict) else None
    if not raw_calls and isinstance(data, dict) and data.get("tool_call"):
        raw_calls = [data["tool_call"]]
    for raw in raw_calls or []:
        fn = raw.get("function") if isinstance(raw, dict) and raw.get("function") else raw
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name or (allowed and name not in allowed):
            continue
        arguments = fn.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                arguments = {"input": arguments}
        args = arguments if isinstance(arguments, dict) else {"input": arguments}
        args = _coerce_tool_arguments(name, args, tools)
        if _tool_args_block_reason(name, args, safety):
            continue
        calls.append(_make_tool_call(name, args))
    return calls


def _normalize_tool_markup(text: str) -> str:
    normalized = text.strip()
    normalized = re.sub(r"<\s*DSML\s*\|", "<|DSML|", normalized, flags=re.I)
    normalized = re.sub(r"<\s*DSML\s+", "<|DSML| ", normalized, flags=re.I)
    normalized = normalized.replace("</DSML|", "</|DSML|")
    normalized = normalized.replace("</DSML ", "</|DSML| ")
    normalized = normalized.replace("< /DSML|", "</|DSML|")
    normalized = re.sub(r"</\s*DSML\s*\|", "</|DSML|", normalized, flags=re.I)
    normalized = re.sub(r"</\s*DSML\s+", "</|DSML| ", normalized, flags=re.I)
    normalized = normalized.replace("<| DSML |", "<|DSML|")
    normalized = normalized.replace("<|DSML ", "<|DSML| ")
    normalized = re.sub(
        r"<\s*/\s*\|?\s*DSML\s*\|?\s*parameter\s+name=([\"'][^\"']+[\"'])\s*>",
        r"</|DSML| parameter><|DSML| parameter name=\1>",
        normalized,
        flags=re.I,
    )
    has_invoke_open = bool(re.search(r"<\s*\|?\s*DSML\s*\|?\s*invoke\b", normalized, flags=re.I))
    has_invoke_close = bool(re.search(r"<\s*/\s*\|?\s*DSML\s*\|?\s*invoke\s*>", normalized, flags=re.I))
    if has_invoke_open and not has_invoke_close:
        normalized = re.sub(
            r"(<\s*/\s*\|?\s*DSML\s*\|?\s*tool_calls\s*>)",
            r"</|DSML| invoke>\1",
            normalized,
            count=1,
            flags=re.I,
        )
    return normalized


def _tool_parse_retries(db: Session) -> int:
    try:
        return max(0, min(5, int(get_setting(db, "tool_parse_retries", "1") or "1")))
    except ValueError:
        return 1


def _parse_tool_calls_with_retries(text: str, tools: list[dict[str, Any]] | None, db: Session) -> list[dict[str, Any]]:
    safety = _tool_safety_config(db)
    calls = _parse_tool_calls(text, tools, safety)
    if calls:
        return calls
    current = text
    for _ in range(_tool_parse_retries(db)):
        normalized = _normalize_tool_markup(current)
        if normalized == current:
            break
        calls = _parse_tool_calls(normalized, tools, safety)
        if calls:
            return calls
        current = normalized
    markers = safety.get("markers", MOJIBAKE_MARKERS)
    if not isinstance(markers, tuple):
        markers = MOJIBAKE_MARKERS
    if _looks_like_mojibake(text, markers):
        _log_tool_event(db, "WARNING", "suspected mojibake in tool-like output; parser could not recover a valid tool call")
    if _looks_like_malformed_modify_file_text(text):
        _log_tool_event(db, "WARNING", "blocked malformed modify_file output; model must regenerate valid tool parameters")
    return []


def _tool_call_summary(calls: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for call in calls[:5]:
        fn = call.get("function") or {}
        name = str(fn.get("name") or "")
        args = str(fn.get("arguments") or "")
        if len(args) > 500:
            args = args[:500] + "..."
        parts.append(f"{name} args={args}")
    if len(calls) > 5:
        parts.append(f"...+{len(calls) - 5} more")
    return "; ".join(parts)


def _has_tool_call_markup(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return "dsml" in lowered and "tool_calls" in lowered and "invoke" in lowered


def _maybe_tool_call_markup(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    stripped = lowered.lstrip()
    return "dsml" in lowered or "tool_calls" in lowered or stripped.startswith("<")


def _tool_mode(db: Session) -> str:
    mode = get_setting(db, "tool_call_mode", "auto").lower()
    return mode if mode in {"auto", "force_buffer", "stream_compat", "off"} else "auto"


def _tool_client_profile(db: Session) -> str:
    profile = get_setting(db, "tool_client_profile", "auto").lower()
    allowed = {"auto", "openai", "cherry", "cline", "chatbox", "openwebui", "lobe", "hermes", "tokeny"}
    return profile if profile in allowed else "auto"


def _tokeny_tool_result_compaction(db: Session) -> bool:
    return get_setting(db, "tokeny_tool_result_compaction", "true").lower() == "true"


def _tokeny_compact_tool_schema_after_result(db: Session) -> bool:
    return get_setting(db, "tokeny_compact_tool_schema_after_result", "true").lower() == "true"


def _document_output_strategy(db: Session) -> str:
    strategy = get_setting(db, "document_output_strategy", "auto").lower()
    return strategy if strategy in {"auto", "client_tools", "baidu_native", "text"} else "auto"


def _baidu_native_document_policy(db: Session) -> str:
    policy = get_setting(db, "baidu_native_document_policy", "explicit_only").lower()
    return policy if policy in {"explicit_only", "allow", "deny"} else "explicit_only"


def _has_tool(tools: list[dict[str, Any]] | None, names: set[str]) -> bool:
    return bool(_tool_function_names(tools) & names)


def _looks_like_image_task(query: str) -> bool:
    lowered = query.lower()
    keywords = ("画图", "图片", "图像", "插画", "海报", "生图", "生成图", "image", "photo", "poster")
    return any(keyword in lowered for keyword in keywords)


def _explicit_baidu_document_intent(query: str) -> bool:
    lowered = query.lower()
    keywords = ("word", "pdf", "下载", "可下载", "百度文档", "文档卡片", "工作区文档", "docx", "ppt")
    return any(keyword in lowered for keyword in keywords)


def _client_file_write_intent(query: str) -> bool:
    lowered = query.lower()
    if _looks_like_image_task(query):
        return False
    write_keywords = (
        "保存到", "写入", "创建文件", "创建文档", "生成文件", "新建文件", "本地文件", "客户端", "项目目录",
        "当前项目", "对应目录", "目录文档", "md文件", ".md", ".txt", "modify_file", "write_file",
    )
    return any(keyword in lowered for keyword in write_keywords)


def _output_route_instruction(db: Session, query: str, tools: list[dict[str, Any]] | None) -> str:
    if not tools:
        return ""
    strategy = _document_output_strategy(db)
    native_policy = _baidu_native_document_policy(db)
    has_file_writer = _has_tool(tools, {"modify_file", "write_file", "create_file", "edit_file"})
    if not has_file_writer or _looks_like_image_task(query):
        return ""

    explicit_native = _explicit_baidu_document_intent(query)
    client_write_intent = _client_file_write_intent(query)
    force_client = strategy == "client_tools" or (strategy == "auto" and client_write_intent and not explicit_native)
    prefer_native = strategy == "baidu_native" or (native_policy == "allow" and explicit_native)

    if strategy == "text":
        return (
            "- The admin output route is text-only. Do not force client file tools unless the user explicitly asks to call a tool."
        )
    if prefer_native and native_policy != "deny":
        return (
            "- The user/admin route allows Baidu native document output for this request. "
            "You may use native document/workspace/download style output when it matches the user request."
        )
    if native_policy == "deny" and explicit_native:
        return (
            "- Baidu native document/workspace/download output is disabled by admin policy. "
            "If a file must be created and a client file-writing tool is available, use that tool instead."
        )
    if force_client:
        return (
            "- The user is asking to create/save/write a document or text file in the client project/workspace. "
            "You MUST complete this by calling the client file-writing tool such as modify_file/write_file. "
            "Do not only output the document body as normal prose. Do not create a Baidu native workspace document/card for this task. "
            "Do not wait for another confirmation when the target path or filename is already clear from the request/context. "
            "The assistant response must contain only the DSML tool-call structure."
        )
    if native_policy == "explicit_only":
        return (
            "- Use Baidu native document/workspace/download output only when the user explicitly asks for Word/PDF/download/Baidu document. "
            "If the user asks to save into the client project or a local path, use the client file-writing tool."
        )
    return ""


def _tool_failure_strategy(db: Session) -> str:
    strategy = get_setting(db, "tool_parse_failure_strategy", "clean_text").lower()
    return strategy if strategy in {"clean_text", "error", "raw_text"} else "clean_text"


def _tool_max_buffer_chars(db: Session) -> int:
    try:
        return max(1000, int(get_setting(db, "tool_max_buffer_chars", "300000") or "300000"))
    except ValueError:
        return 300000


def _tool_loop_protection(db: Session) -> bool:
    return get_setting(db, "tool_loop_protection", "true").lower() == "true"


def _force_final_after_tool_result(db: Session) -> bool:
    return get_setting(db, "tool_force_final_after_result", "true").lower() == "true"


def _should_parse_tools(db: Session, allow_tool_calls: bool) -> bool:
    return allow_tool_calls and _tool_mode(db) != "off"


def _should_buffer_tool_output(db: Session, tools: list[dict[str, Any]] | None, content_preview: str, current_buffer: bool) -> bool:
    mode = _tool_mode(db)
    profile = _tool_client_profile(db)
    if mode == "off":
        return False
    if current_buffer:
        return True
    if tools and profile in {"cline", "hermes", "tokeny"}:
        return True
    if mode == "force_buffer" and tools:
        return True
    if tools and mode in {"auto", "stream_compat"}:
        return True
    return _maybe_tool_call_markup(content_preview)


def _fallback_tool_text(text: str, strategy: str) -> str:
    if _looks_like_severe_mojibake(text):
        return _blocked_tool_text("tool-call text contains severe mojibake markers")
    if _looks_like_malformed_modify_file_text(text):
        return _blocked_tool_text("modify_file parameters are malformed; filePath appears to contain operation/content")
    if strategy == "raw_text":
        return text
    if strategy == "error":
        return "Tool call parse failed: the model returned incomplete tool-call markup, so raw tags were blocked."
    return _strip_dsml(text)


def _log_tool_event(db: Session, level: str, message: str, trace_id: str = "") -> None:
    try:
        traced_system_log(db, level, "tool_call", trace_id, message[:1000])
    except Exception:
        pass


def _note_conversation_turn(db: Session, binding: ConversationBinding | None, note: str) -> None:
    if not binding:
        return
    try:
        turn = db.get(ConversationTurn, binding.turn_id)
        if not turn:
            return
        marker = f"[tool] {note[:1500]}"
        if turn.response_preview:
            turn.response_preview = (turn.response_preview + "\n" + marker)[-3000:]
        else:
            turn.response_preview = marker
        db.commit()
    except Exception:
        db.rollback()


def _update_turn_prompt_preview(db: Session, binding: ConversationBinding | None, query: str) -> None:
    if not binding or get_setting(db, "conversation_save_content", "true").lower() != "true":
        return
    try:
        turn = db.get(ConversationTurn, binding.turn_id)
        if turn:
            turn.prompt_preview = query[:3000]
            db.commit()
    except Exception:
        db.rollback()


def _conversation_mode(db: Session) -> str:
    mode = get_setting(db, "conversation_mode", "stateless").lower()
    return mode if mode in {"stateless", "bound", "hybrid"} else "stateless"


def _conversation_response_mode(db: Session) -> str:
    mode = get_setting(db, "conversation_response_mode", "client").lower()
    allowed = {"client", "stream", "buffered_stream", "non_stream"}
    return mode if mode in allowed else "client"


def _empty_response_retry_enabled(db: Session) -> bool:
    return get_setting(db, "baidu_empty_response_retry", "true").lower() == "true"


def _empty_response_message() -> str:
    return "本轮返回空内容，项目已尝试重置窗口并重试。请稍后重试，或在清空该会话后继续。"


def _reset_binding_for_empty_retry(db: Session, binding: ConversationBinding | None, trace_id: str, endpoint: str) -> bool:
    if not binding or not _empty_response_retry_enabled(db):
        return False
    try:
        conversation = db.get(BaiduConversation, binding.conversation_id)
        turn = db.get(ConversationTurn, binding.turn_id)
        if not conversation:
            return False
        previous_session = conversation.baidu_session_id or binding.baidu_session_id
        conversation.baidu_session_id = ""
        conversation.last_qid = ""
        conversation.last_pkg_id = ""
        conversation.rank = 0
        conversation.last_error = "empty upstream response; retrying with a new Baidu window"
        if turn:
            turn.response_preview = ((turn.response_preview or "") + "\n[retry] empty upstream response; reset Baidu session and retry once")[-3000:]
        binding.baidu_session_id = ""
        binding.rank = 1
        db.commit()
        traced_system_log(
            db,
            "WARNING",
            "conversation_binding",
            trace_id,
            f"{endpoint} empty upstream response; reset local={binding.local_conversation_id} previous_session={previous_session or '-'} and retry once",
        )
        return True
    except Exception as exc:
        db.rollback()
        traced_system_log(
            db,
            "ERROR",
            "conversation_binding",
            trace_id,
            f"{endpoint} failed to reset empty response binding local={binding.local_conversation_id if binding else '-'}: {str(exc)[:500]}",
        )
        return False


def _should_prepare_binding(body: ChatCompletionRequest | ResponsesRequest, request: Request, api_key: ApiKey, db: Session) -> bool:
    mode = _conversation_mode(db)
    if mode == "stateless":
        return False
    if mode == "bound":
        return True
    return bool(_conversation_key(body, request, api_key, db))


def _build_query_for_request(
    adapter: BaiduAdapter,
    db: Session,
    messages: list[dict[str, Any]],
    binding: ConversationBinding | None,
    cache_key: str = "",
    tools: list[dict[str, Any]] | None = None,
) -> str:
    if has_recent_tool_result(messages):
        return adapter.build_query(
            tool_result_messages(
                messages,
                force_final=_force_final_after_tool_result(db),
                cache_key=cache_key,
                profile=_tool_client_profile(db),
                compact_tokeny=_tokeny_tool_result_compaction(db),
                tools=tools,
            ),
            force_prompt=False,
        )
    if not binding:
        return adapter.build_query(messages)
    strategy = get_setting(db, "conversation_message_strategy", "smart").lower()
    if strategy not in {"smart", "latest_user_only", "full_messages"}:
        strategy = "smart"
    if strategy == "full_messages":
        return adapter.build_query(messages)
    if strategy == "latest_user_only":
        return adapter.build_query(latest_user_message(messages), force_prompt=False)
    if binding.baidu_session_id:
        return adapter.build_query(latest_user_message(messages), force_prompt=False)
    return adapter.build_query(messages)


def _conversation_max_query_chars(db: Session) -> int:
    try:
        return max(0, int(get_setting(db, "conversation_max_query_chars", "120000") or "120000"))
    except ValueError:
        return 120000


def _conversation_max_query_scope(db: Session) -> str:
    scope = get_setting(db, "conversation_max_query_scope", "stateless_and_first_turn").lower()
    return scope if scope in {"all", "stateless_and_first_turn", "stateless_only", "off"} else "stateless_and_first_turn"


def _should_fit_query_for_baidu(db: Session, binding: ConversationBinding | None) -> bool:
    scope = _conversation_max_query_scope(db)
    if scope == "off":
        return False
    if scope == "all":
        return True
    if scope == "stateless_only":
        return binding is None
    if scope == "stateless_and_first_turn":
        return binding is None or not binding.baidu_session_id
    return True


def _fit_query_for_baidu(
    db: Session,
    query: str,
    trace_id: str,
    endpoint: str,
    binding: ConversationBinding | None = None,
) -> str:
    if not _should_fit_query_for_baidu(db, binding):
        return query
    max_chars = _conversation_max_query_chars(db)
    if max_chars <= 0 or len(query) <= max_chars:
        return query
    notice = (
        "注意：本次客户端传入的历史上下文过长，项目已为避免百度上游 413 Request Entity Too Large "
        "自动省略较早历史，只保留最近上下文。请优先依据下面保留的最近用户请求、工具结果和对话内容继续完成任务。\n\n"
    )
    keep_chars = max(1000, max_chars - len(notice))
    trimmed = notice + query[-keep_chars:]
    system_log(
        db,
        "WARNING",
        "upstream_payload",
        f"{endpoint} query trimmed from {len(query)} to {len(trimmed)} chars, limit={max_chars}, "
        f"scope={_conversation_max_query_scope(db)}, bound_session={bool(binding and binding.baidu_session_id)}, trace={trace_id}",
    )
    return trimmed


def _metadata_value(metadata: dict[str, Any] | None, *keys: str) -> str:
    if not metadata:
        return ""
    for key in keys:
        value = metadata.get(key)
        if value:
            return str(value)
    return ""


def _resolve_model_info(db: Session, requested_model: str) -> tuple[str, str]:
    model = db.scalar(select(ModelConfig).where(ModelConfig.public_id == requested_model, ModelConfig.enabled.is_(True)))
    if model:
        return model.public_id, model.baidu_model
    fallback = db.scalar(select(ModelConfig).where(ModelConfig.public_id == "smart", ModelConfig.enabled.is_(True)))
    if fallback:
        return fallback.public_id, fallback.baidu_model
    return requested_model, requested_model


def _conversation_key(
    body: ChatCompletionRequest | ResponsesRequest,
    request: Request,
    api_key: ApiKey,
    db: Session,
) -> str:
    canonical_model, _ = _resolve_model_info(db, getattr(body, "model", ""))
    explicit = (
        getattr(body, "conversation_id", None)
        or _metadata_value(getattr(body, "metadata", None), "conversation_id", "chat_id", "session_id", "thread_id")
        or request.headers.get("x-conversation-id")
        or request.headers.get("x-chat-id")
        or getattr(body, "user", None)
    )
    if explicit:
        return str(explicit)[:240]
    fallback = get_setting(db, "conversation_fallback_binding", "false").lower() == "true"
    mode = _conversation_mode(db)
    if mode == "bound":
        fallback = True
    if fallback:
        source_ip = request.client.host if request.client else ""
        strategy = get_setting(db, "conversation_missing_id_strategy", "smart").lower()
        if strategy not in {"smart", "stable", "fallback", "strict", "ephemeral"}:
            strategy = "smart"
        if strategy == "strict":
            raise HTTPException(
                status_code=400,
                detail="conversation_id is required when conversation binding is enabled",
            )
        if strategy == "ephemeral":
            return f"ephemeral:{uuid.uuid4().hex}"[:240]
        if strategy == "smart":
            if hasattr(body, "messages"):
                messages = message_dicts(getattr(body, "messages", []) or [])
            else:
                messages = [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in getattr(body, "input", [])] if isinstance(getattr(body, "input", ""), list) else [{"role": "user", "content": getattr(body, "input", "")}]
            seed = first_user_text(messages) or str(getattr(body, "user", "") or "")
            seed_hash = hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:20]
            return f"smart:{api_key.name}:{source_ip}:{canonical_model}:{seed_hash}"[:240]
        if strategy == "stable":
            return f"stable:{api_key.name}:{canonical_model}"[:240]
        return f"fallback:{api_key.name}:{source_ip}:{canonical_model}"[:240]
    return ""


def _resolve_conversation_local_id(
    body: ChatCompletionRequest | ResponsesRequest,
    request: Request,
    api_key: ApiKey,
    db: Session,
) -> str:
    mode = _conversation_mode(db)
    if mode == "stateless":
        return ""

    source_ip = request.client.host if request.client else ""
    local_id = _conversation_key(body, request, api_key, db)
    if mode == "hybrid" and not local_id:
        return ""
    if not local_id:
        canonical_model, _ = _resolve_model_info(db, getattr(body, "model", ""))
        local_id = f"fallback:{api_key.name}:{source_ip}:{canonical_model}"[:240]
    return local_id


def _tool_cache_key_for_request(
    body: ChatCompletionRequest | ResponsesRequest,
    request: Request,
    api_key: ApiKey,
    db: Session,
    binding: ConversationBinding | None = None,
) -> str:
    if binding and binding.local_conversation_id:
        return f"binding:{binding.local_conversation_id}"
    try:
        key = _conversation_key(body, request, api_key, db)
    except HTTPException:
        key = ""
    if key:
        return f"request:{key}"
    source_ip = request.client.host if request.client else ""
    canonical_model, _ = _resolve_model_info(db, getattr(body, "model", ""))
    return f"stateless:{api_key.name}:{source_ip}:{canonical_model}"


def _prepare_conversation(
    body: ChatCompletionRequest | ResponsesRequest,
    request: Request,
    api_key: ApiKey,
    query: str,
    db: Session,
    local_id: str | None = None,
) -> tuple[ConversationBinding | None, str]:
    mode = _conversation_mode(db)
    if mode == "stateless":
        return None, mode

    source_ip = request.client.host if request.client else ""
    local_id = local_id or _conversation_key(body, request, api_key, db)
    if mode == "hybrid" and not local_id:
        return None, mode
    if not local_id:
        canonical_model, _ = _resolve_model_info(db, getattr(body, "model", ""))
        local_id = f"fallback:{api_key.name}:{source_ip}:{canonical_model}"[:240]

    requested_model = getattr(body, "model", "")
    canonical_model, baidu_model = _resolve_model_info(db, requested_model)
    conversation = db.scalar(select(BaiduConversation).where(BaiduConversation.local_conversation_id == local_id))
    if not conversation:
        conversation = BaiduConversation(
            local_conversation_id=local_id,
            api_key_name=api_key.name,
            source_ip=source_ip,
            model=canonical_model,
            requested_model=requested_model,
            baidu_model=baidu_model,
            mode=mode,
            title=query[:80],
        )
        db.add(conversation)
    else:
        previous_model = conversation.model or conversation.requested_model or conversation.baidu_model
        model_changed = (
            (conversation.model and conversation.model != canonical_model)
            or (conversation.requested_model and conversation.requested_model != requested_model)
            or (conversation.baidu_model and conversation.baidu_model != baidu_model)
        )
        reset_on_model_change = get_setting(db, "conversation_reset_on_model_change", "true").lower() == "true"
        if model_changed and reset_on_model_change:
            old_session = conversation.baidu_session_id
            conversation.baidu_session_id = ""
            conversation.last_qid = ""
            conversation.last_pkg_id = ""
            conversation.cookie_snapshot = ""
            conversation.rank = 0
            conversation.status = "active"
            conversation.last_error = ""
            system_log(
                db,
                "INFO",
                "conversation_binding",
                "model changed; reset bound Baidu window "
                f"local={local_id[:120]} previous_model={previous_model} "
                f"new_model={canonical_model}/{baidu_model} previous_session={old_session or '-'}",
            )
        conversation.model = canonical_model
        conversation.requested_model = requested_model
        conversation.baidu_model = baidu_model
    db.flush()

    ttl_hours = _int_setting(db, "conversation_ttl_hours", 24)
    max_turns = _int_setting(db, "conversation_max_turns", 50)
    last_active_at = conversation.last_active_at or conversation.created_at or datetime.utcnow()
    expired = ttl_hours > 0 and last_active_at < datetime.utcnow() - timedelta(hours=ttl_hours)
    current_rank = conversation.rank or 0
    over_turns = max_turns > 0 and current_rank >= max_turns
    if expired or over_turns or conversation.status != "active":
        conversation.baidu_session_id = ""
        conversation.last_qid = ""
        conversation.last_pkg_id = ""
        conversation.cookie_snapshot = ""
        conversation.rank = 0
        conversation.status = "active"
        conversation.last_error = ""

    next_rank = conversation.rank + 1 if conversation.baidu_session_id else 1
    save_content = get_setting(db, "conversation_save_content", "true").lower() == "true"
    turn = ConversationTurn(
        conversation_id=conversation.id,
        local_conversation_id=local_id,
        rank=next_rank,
        model=canonical_model,
        requested_model=requested_model,
        baidu_model=baidu_model,
        prompt_preview=query[:1000] if save_content else "",
        status="running",
    )
    db.add(turn)
    db.flush()
    binding = ConversationBinding(
        conversation_id=conversation.id,
        turn_id=turn.id,
        local_conversation_id=local_id,
        baidu_session_id=conversation.baidu_session_id,
        rank=next_rank,
        credential_id=conversation.credential_id,
        cookie_snapshot=conversation.cookie_snapshot or "",
    )
    db.commit()
    return binding, mode


def _int_setting(db: Session, key: str, default: int) -> int:
    try:
        return int(get_setting(db, key, str(default)) or default)
    except ValueError:
        return default


def _update_conversation_from_event(
    db: Session,
    binding: ConversationBinding | None,
    event: Any,
    response_preview: str = "",
) -> None:
    if not binding:
        return
    conversation = db.get(BaiduConversation, binding.conversation_id)
    turn = db.get(ConversationTurn, binding.turn_id)
    if not conversation or not turn:
        return
    changed = False
    if event.session_id or event.lid:
        conversation.baidu_session_id = event.session_id or conversation.baidu_session_id or event.lid
        binding.baidu_session_id = conversation.baidu_session_id
        turn.session_id = event.session_id or turn.session_id or event.lid
        changed = True
    if event.qid:
        conversation.last_qid = event.qid
        turn.qid = event.qid
        changed = True
    if event.pkg_id:
        conversation.last_pkg_id = event.pkg_id
        turn.pkg_id = event.pkg_id
        changed = True
    context = (event.raw or {}).get("_baidu_context") if event.raw else None
    if isinstance(context, dict):
        conversation.credential_id = context.get("credential_id")
        conversation.credential_name = str(context.get("credential_name") or "")
        binding.credential_id = conversation.credential_id
        snapshot = str(context.get("cookie_snapshot") or "")
        if snapshot:
            conversation.cookie_snapshot = snapshot
            binding.cookie_snapshot = snapshot
        changed = True
    if response_preview and get_setting(db, "conversation_save_content", "true").lower() == "true":
        turn.response_preview = response_preview[-2000:]
        changed = True
    if changed:
        conversation.rank = max(conversation.rank, turn.rank)
        conversation.last_active_at = datetime.utcnow()
        db.commit()


def _finish_conversation_turn(
    db: Session,
    binding: ConversationBinding | None,
    status: str,
    started_at: float,
    error: str = "",
) -> None:
    if not binding:
        return
    conversation = db.get(BaiduConversation, binding.conversation_id)
    turn = db.get(ConversationTurn, binding.turn_id)
    if not conversation or not turn:
        return
    turn.status = status
    turn.duration_ms = int((time.time() - started_at) * 1000)
    turn.error = error
    conversation.status = "active" if status == "completed" else "error"
    conversation.last_error = error
    conversation.last_active_at = datetime.utcnow()
    db.commit()


def _format_stream_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return f"Upstream request failed: {message[:300]}"


def _binding_cookie_override(binding: ConversationBinding | None) -> str | None:
    if binding and binding.cookie_snapshot and not binding.credential_id:
        return binding.cookie_snapshot
    return None


def _record_request_error(
    db: Session,
    endpoint: str,
    model: str,
    source_ip: str,
    api_key_name: str,
    prompt_chars: int,
    exc: Exception,
) -> None:
    with request_timer(db, endpoint, model, source_ip, api_key_name, prompt_chars) as state:
        state["status_code"] = getattr(exc, "status_code", 500)
        state["error"] = str(getattr(exc, "detail", None) or exc)[:1000]


@router.get("/models")
def list_models(db: Session = Depends(get_db), api_key: ApiKey = Depends(get_api_key)):
    models = db.scalars(select(ModelConfig).where(ModelConfig.enabled.is_(True))).all()
    allowed = [item.strip() for item in api_key.allowed_models.split(",") if item.strip()]
    if "*" not in allowed:
        models = [item for item in models if item.public_id in allowed]
    return {
        "object": "list",
        "data": [
            {
                "id": item.public_id,
                "object": "model",
                "created": int(item.created_at.timestamp()),
                "owned_by": "baidu-chat-web",
            }
            for item in models
        ],
    }


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(get_api_key),
):
    enforce_access_policy(api_key, body.model, request, db)
    adapter = BaiduAdapter(db)
    messages = message_dicts(body.messages)
    recent_tool_fingerprints = _recent_tool_fingerprints(messages, match_mode=_tool_repeat_match_mode(db))
    title_query = adapter.build_query(messages)
    source_ip = request.client.host if request.client else ""
    binding = None
    conversation_lock: asyncio.Lock | None = None
    try:
        if _should_prepare_binding(body, request, api_key, db):
            local_id = _resolve_conversation_local_id(body, request, api_key, db)
            if local_id:
                conversation_lock = _get_conversation_lock(local_id)
                await conversation_lock.acquire()
            binding, _ = _prepare_conversation(body, request, api_key, title_query, db, local_id=local_id)
    except Exception as exc:
        _release_conversation_lock(conversation_lock)
        _record_request_error(db, "/v1/chat/completions", body.model, source_ip, api_key.name, len(title_query), exc)
        system_log(db, "ERROR", "conversation_binding", f"prepare failed model={body.model} ip={source_ip}: {str(exc)[:500]}")
        raise
    tool_cache_key = _tool_cache_key_for_request(body, request, api_key, db, binding)
    query = _build_query_for_request(adapter, db, messages, binding, cache_key=tool_cache_key, tools=body.tools)
    _update_turn_prompt_preview(db, binding, query)
    tool_mode = _tool_mode(db)
    allow_tool_calls = not (
        tool_mode == "off"
        or (has_recent_tool_result(messages) and _force_final_after_tool_result(db))
    )
    if allow_tool_calls:
        query = _append_tool_prompt(
            query,
            body.tools,
            body.tool_choice,
            _tool_client_profile(db),
            has_recent_tool_result(messages),
            _tool_loop_protection(db),
            _output_route_instruction(db, query, body.tools),
            _tokeny_compact_tool_schema_after_result(db),
        )
    query = _fit_query_for_baidu(db, query, "", "/v1/chat/completions", binding)
    _update_turn_prompt_preview(db, binding, query)
    response_mode = _conversation_response_mode(db)
    should_stream = response_mode != "non_stream" and (body.stream or response_mode == "stream")
    force_buffer_all = response_mode == "buffered_stream"
    if should_stream:
        return StreamingResponse(
            _stream_chat(
                body.model,
                query,
                binding,
                source_ip,
                api_key.name,
                body.tools,
                allow_tool_calls=allow_tool_calls,
                force_buffer_all=force_buffer_all,
                recent_tool_fingerprints=recent_tool_fingerprints,
                conversation_lock=conversation_lock,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        with request_timer(db, "/v1/chat/completions", body.model, source_ip, api_key.name, len(query)) as state:
            content = ""
            images: list[str] = []
            reasoning = ""
            output_reasoning = get_setting(db, "output_reasoning", "true").lower() == "true"
            output_image_urls = get_setting(db, "output_image_urls", "true").lower() == "true"
            long_text_strategy = get_setting(db, "long_text_strategy", "stream_delta")
            started_at = time.time()
            try:
                for attempt in range(2):
                    async for event in adapter.stream_conversation(
                        query,
                        body.model,
                        baidu_session_id=binding.baidu_session_id if binding else "",
                        rank=binding.rank if binding else None,
                        credential_id=binding.credential_id if binding else None,
                        cookie_override=_binding_cookie_override(binding),
                    ):
                        content += event.text or ""
                        if long_text_strategy == "stream_delta":
                            content += event.workspace_content_delta or ""
                        if output_reasoning:
                            reasoning += event.reasoning or ""
                        if output_image_urls and event.images:
                            images.extend(event.images)
                        _update_conversation_from_event(db, binding, event, content)
                    if content or reasoning or images:
                        break
                    if attempt == 0 and _reset_binding_for_empty_retry(db, binding, state["request_id"], "/v1/chat/completions"):
                        continue
                    break
                if not content and not reasoning and not images:
                    content = _empty_response_message()
                _finish_conversation_turn(db, binding, "completed", started_at)
            except Exception as exc:
                _finish_conversation_turn(db, binding, "failed", started_at, str(exc)[:1000])
                traced_system_log(db, "ERROR", "conversation_binding", state["request_id"], f"upstream failed local={binding.local_conversation_id if binding else '-'} model={body.model}: {str(exc)[:500]}")
                raise
            if images:
                content += "\n\n" + "\n".join(f"![image]({url})" for url in images)
            state["completion_chars"] = len(content)
            tool_calls = _parse_tool_calls_with_retries(content, body.tools, db) if _should_parse_tools(db, allow_tool_calls) else []
            blocked_repeats: list[str] = []
            if tool_calls:
                tool_calls, blocked_repeats = _filter_repeated_tool_calls(
                    tool_calls,
                    recent_tool_fingerprints,
                    db,
                    state["request_id"],
                    binding,
                )
        if tool_calls:
            summary = _tool_call_summary(tool_calls)
            _log_tool_event(db, "INFO", f"non-stream chat parsed {len(tool_calls)} tool call(s) for model={body.model}: {summary}", state["request_id"])
            _note_conversation_turn(db, binding, f"parsed tool_calls: {summary}")
        elif blocked_repeats:
            content = _repeated_tool_block_text(blocked_repeats)
        elif body.tools and _maybe_tool_call_markup(content):
            _log_tool_event(db, "WARNING", f"non-stream chat saw tool-like markup but parsed 0 tool calls for model={body.model}; raw={content[:500]}", state["request_id"])
            _note_conversation_turn(db, binding, f"unparsed tool-like output: {content[:800]}")
        finish_reason = "tool_calls" if tool_calls else "stop"
        clean_content = content
        if not tool_calls and (_has_tool_call_markup(content) or _maybe_tool_call_markup(content)):
            clean_content = _fallback_tool_text(content, _tool_failure_strategy(db))
        message: dict[str, Any] = {
            "role": "assistant",
            "content": None if tool_calls else clean_content,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        if reasoning and not tool_calls:
            message["reasoning_content"] = reasoning
        return {
            "id": "chatcmpl-" + state["request_id"],
            "object": "chat.completion",
            "created": now_ts(),
            "model": body.model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": len(query),
                "completion_tokens": len(content),
                "total_tokens": len(query) + len(content),
            },
        }
    finally:
        _release_conversation_lock(conversation_lock)


async def _stream_chat(
    model: str,
    query: str,
    binding: ConversationBinding | None = None,
    source_ip: str = "",
    api_key_name: str = "",
    tools: list[dict[str, Any]] | None = None,
    allow_tool_calls: bool = True,
    force_buffer_all: bool = False,
    recent_tool_fingerprints: set[str] | None = None,
    conversation_lock: asyncio.Lock | None = None,
) -> AsyncIterator[str]:
    response_id = "chatcmpl-" + uuid.uuid4().hex
    db = SessionLocal()
    adapter = BaiduAdapter(db)
    output_reasoning = get_setting(db, "output_reasoning", "true").lower() == "true"
    output_image_urls = get_setting(db, "output_image_urls", "true").lower() == "true"
    long_text_strategy = get_setting(db, "long_text_strategy", "stream_delta")
    max_buffer_chars = _tool_max_buffer_chars(db)
    failure_strategy = _tool_failure_strategy(db)
    yield sse_line(
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": now_ts(),
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )
    content_preview = ""
    emitted_tool_calls: list[dict[str, Any]] = []
    tool_buffer_mode = force_buffer_all or bool(tools and _should_parse_tools(db, allow_tool_calls) and _tool_mode(db) == "force_buffer")
    tool_buffer_failed = False
    started_at = time.time()
    try:
        with request_timer(db, "/v1/chat/completions", model, source_ip, api_key_name, len(query)) as state:
            try:
                for attempt in range(2):
                    async for event in adapter.stream_conversation(
                        query,
                        model,
                        baidu_session_id=binding.baidu_session_id if binding else "",
                        rank=binding.rank if binding else None,
                        credential_id=binding.credential_id if binding else None,
                        cookie_override=_binding_cookie_override(binding),
                    ):
                        _update_conversation_from_event(db, binding, event, content_preview)
                        delta = ""
                        if output_reasoning and event.reasoning and not force_buffer_all and not tool_buffer_mode:
                            yield sse_line(
                                {
                                    "id": response_id,
                                    "object": "chat.completion.chunk",
                                    "created": now_ts(),
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {"reasoning_content": event.reasoning}, "finish_reason": None}],
                                }
                            )
                            continue
                        elif event.text:
                            delta = event.text
                        elif long_text_strategy == "stream_delta" and event.workspace_content_delta:
                            delta = event.workspace_content_delta
                        elif output_image_urls and event.images:
                            delta = "\n".join(f"![image]({url})" for url in event.images)
                        if delta:
                            content_preview += delta
                            state["completion_chars"] = len(content_preview)
                            _update_conversation_from_event(db, binding, event, content_preview)
                            if _should_parse_tools(db, allow_tool_calls) and _should_buffer_tool_output(db, tools, content_preview, tool_buffer_mode):
                                tool_buffer_mode = True
                                if len(content_preview) <= max_buffer_chars:
                                    continue
                                clean_content = _fallback_tool_text(content_preview, failure_strategy)
                                yield sse_line(
                                    {
                                        "id": response_id,
                                        "object": "chat.completion.chunk",
                                        "created": now_ts(),
                                        "model": model,
                                        "choices": [{"index": 0, "delta": {"content": clean_content}, "finish_reason": None}],
                                    }
                                )
                                tool_buffer_mode = False
                                tool_buffer_failed = True
                                continue
                            yield sse_line(
                                {
                                    "id": response_id,
                                    "object": "chat.completion.chunk",
                                    "created": now_ts(),
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                                }
                            )
                    if content_preview:
                        break
                    if attempt == 0 and _reset_binding_for_empty_retry(db, binding, state["request_id"], "/v1/chat/completions stream"):
                        continue
                    break
                if not content_preview:
                    content_preview = _empty_response_message()
                    state["completion_chars"] = len(content_preview)
                    yield sse_line(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": now_ts(),
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": content_preview}, "finish_reason": None}],
                        }
                    )
                _finish_conversation_turn(db, binding, "completed", started_at)
                emitted_tool_calls = _parse_tool_calls_with_retries(content_preview, tools, db) if _should_parse_tools(db, allow_tool_calls) and not tool_buffer_failed else []
                blocked_repeats: list[str] = []
                if emitted_tool_calls:
                    emitted_tool_calls, blocked_repeats = _filter_repeated_tool_calls(
                        emitted_tool_calls,
                        recent_tool_fingerprints or set(),
                        db,
                        state["request_id"],
                        binding,
                    )
                if emitted_tool_calls:
                    summary = _tool_call_summary(emitted_tool_calls)
                    _log_tool_event(db, "INFO", f"stream chat parsed {len(emitted_tool_calls)} tool call(s) for model={model}: {summary}", state["request_id"])
                    _note_conversation_turn(db, binding, f"parsed tool_calls: {summary}")
                    stream_tool_calls = [dict(call, index=index) for index, call in enumerate(emitted_tool_calls)]
                    yield sse_line(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": now_ts(),
                            "model": model,
                            "choices": [{"index": 0, "delta": {"tool_calls": stream_tool_calls}, "finish_reason": None}],
                        }
                    )
                elif blocked_repeats:
                    yield sse_line(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": now_ts(),
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": _repeated_tool_block_text(blocked_repeats)}, "finish_reason": None}],
                        }
                    )
                elif tool_buffer_mode and content_preview:
                    if _maybe_tool_call_markup(content_preview):
                        _log_tool_event(db, "WARNING", f"stream chat fallback after unparsed tool-like output for model={model}; raw={content_preview[:500]}", state["request_id"])
                        _note_conversation_turn(db, binding, f"unparsed tool-like output: {content_preview[:800]}")
                    clean_content = _fallback_tool_text(content_preview, failure_strategy) if _has_tool_call_markup(content_preview) or _maybe_tool_call_markup(content_preview) else content_preview
                    yield sse_line(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": now_ts(),
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": clean_content}, "finish_reason": None}],
                        }
                    )
            except Exception as exc:
                state["status_code"] = 500
                state["error"] = str(exc)[:1000]
                _finish_conversation_turn(db, binding, "failed", started_at, str(exc)[:1000])
                traced_system_log(db, "ERROR", "conversation_binding", state["request_id"], f"stream upstream failed local={binding.local_conversation_id if binding else '-'} model={model}: {str(exc)[:500]}")
                yield sse_line(
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": now_ts(),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": _format_stream_error(exc)},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
    finally:
        _release_conversation_lock(conversation_lock)
        yield sse_line(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": now_ts(),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if emitted_tool_calls else "stop"}],
            }
        )
        yield "data: [DONE]\n\n"
        db.close()


@router.post("/responses")
async def responses(
    body: ResponsesRequest,
    request: Request,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(get_api_key),
):
    enforce_access_policy(api_key, body.model, request, db)
    messages = _responses_input_to_messages(body)
    chat_body = ChatCompletionRequest(model=body.model, messages=messages, stream=body.stream)
    chat_body.user = body.user
    chat_body.conversation_id = body.conversation_id
    chat_body.metadata = body.metadata
    chat_body.tools = body.tools
    chat_body.tool_choice = body.tool_choice
    chat_body.parallel_tool_calls = body.parallel_tool_calls
    response_mode = _conversation_response_mode(db)
    should_stream = response_mode != "non_stream" and (body.stream or response_mode == "stream")
    if should_stream:
        adapter = BaiduAdapter(db)
        messages_dict = message_dicts(chat_body.messages)
        recent_tool_fingerprints = _recent_tool_fingerprints(messages_dict, match_mode=_tool_repeat_match_mode(db))
        title_query = adapter.build_query(messages_dict)
        source_ip = request.client.host if request.client else ""
        binding = None
        conversation_lock: asyncio.Lock | None = None
        try:
            if _should_prepare_binding(body, request, api_key, db):
                local_id = _resolve_conversation_local_id(body, request, api_key, db)
                if local_id:
                    conversation_lock = _get_conversation_lock(local_id)
                    await conversation_lock.acquire()
                binding, _ = _prepare_conversation(body, request, api_key, title_query, db, local_id=local_id)
        except Exception as exc:
            _release_conversation_lock(conversation_lock)
            _record_request_error(db, "/v1/responses", body.model, source_ip, api_key.name, len(title_query), exc)
            system_log(db, "ERROR", "conversation_binding", f"responses prepare failed model={body.model} ip={source_ip}: {str(exc)[:500]}")
            raise
        tool_cache_key = _tool_cache_key_for_request(body, request, api_key, db, binding)
        query = _build_query_for_request(adapter, db, messages_dict, binding, cache_key=tool_cache_key, tools=body.tools)
        _update_turn_prompt_preview(db, binding, query)
        tool_mode = _tool_mode(db)
        allow_tool_calls = not (
            tool_mode == "off"
            or (has_recent_tool_result(messages_dict) and _force_final_after_tool_result(db))
        )
        if allow_tool_calls:
            query = _append_tool_prompt(
                query,
                body.tools,
                body.tool_choice,
                _tool_client_profile(db),
                has_recent_tool_result(messages_dict),
                _tool_loop_protection(db),
                _output_route_instruction(db, query, body.tools),
                _tokeny_compact_tool_schema_after_result(db),
            )
        query = _fit_query_for_baidu(db, query, "", "/v1/responses", binding)
        _update_turn_prompt_preview(db, binding, query)
        return StreamingResponse(
            _stream_responses(
                body.model,
                query,
                binding,
                source_ip,
                api_key.name,
                body.tools,
                allow_tool_calls,
                force_buffer_all=response_mode == "buffered_stream",
                recent_tool_fingerprints=recent_tool_fingerprints,
                conversation_lock=conversation_lock,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    chat = await chat_completions(chat_body, request, db, api_key)
    message = chat["choices"][0]["message"]
    if message.get("tool_calls"):
        return {
            "id": "resp-" + uuid.uuid4().hex,
            "object": "response",
            "created_at": now_ts(),
            "status": "requires_action",
            "model": body.model,
            "output": [
                {
                    "id": call["id"],
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                }
                for call in message["tool_calls"]
            ],
        }
    content = message["content"]
    output: list[dict[str, Any]] = []
    if message.get("reasoning_content"):
        output.append(
            {
                "id": "reasoning-" + uuid.uuid4().hex,
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": message["reasoning_content"]}],
            }
        )
    output.append(
        {
            "id": "msg-" + uuid.uuid4().hex,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content}],
        }
    )
    return {
        "id": "resp-" + uuid.uuid4().hex,
        "object": "response",
        "created_at": now_ts(),
        "status": "completed",
        "model": body.model,
        "output": output,
    }


async def _stream_responses(
    model: str,
    query: str,
    binding: ConversationBinding | None = None,
    source_ip: str = "",
    api_key_name: str = "",
    tools: list[dict[str, Any]] | None = None,
    allow_tool_calls: bool = True,
    force_buffer_all: bool = False,
    recent_tool_fingerprints: set[str] | None = None,
    conversation_lock: asyncio.Lock | None = None,
) -> AsyncIterator[str]:
    response_id = "resp-" + uuid.uuid4().hex
    message_item_id = "msg-" + uuid.uuid4().hex
    text_content_index = 0
    text_output_started = False
    text_output_sent = ""
    reasoning_item_id = "rs_" + uuid.uuid4().hex
    reasoning_output_index = 0
    reasoning_summary_index = 0
    reasoning_content_index = 0
    reasoning_started = False
    reasoning_sent = ""
    db = SessionLocal()
    adapter = BaiduAdapter(db)
    output_reasoning = get_setting(db, "output_reasoning", "true").lower() == "true"
    output_image_urls = get_setting(db, "output_image_urls", "true").lower() == "true"
    long_text_strategy = get_setting(db, "long_text_strategy", "stream_delta")
    max_buffer_chars = _tool_max_buffer_chars(db)
    failure_strategy = _tool_failure_strategy(db)
    yield sse_event(
        "response.created",
        {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": now_ts(),
                "status": "in_progress",
                "model": model,
                "output": [],
            },
        },
    )
    yield sse_event(
        "response.in_progress",
        {
            "type": "response.in_progress",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": now_ts(),
                "status": "in_progress",
                "model": model,
                "output": [],
            },
        },
    )

    def response_text_output_index() -> int:
        return 1 if reasoning_started else 0

    def response_text_events(delta: str) -> list[str]:
        nonlocal text_output_started, text_output_sent
        if not delta:
            return []
        events: list[str] = []
        output_index = response_text_output_index()
        if not text_output_started:
            text_output_started = True
            events.append(
                sse_event(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "response_id": response_id,
                        "output_index": output_index,
                        "item": {
                            "id": message_item_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                )
            )
            events.append(
                sse_event(
                    "response.content_part.added",
                    {
                        "type": "response.content_part.added",
                        "response_id": response_id,
                        "item_id": message_item_id,
                        "output_index": output_index,
                        "content_index": text_content_index,
                        "part": {"type": "output_text", "text": ""},
                    },
                )
            )
        text_output_sent += delta
        events.append(
            sse_event(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "response_id": response_id,
                    "item_id": message_item_id,
                    "output_index": output_index,
                    "content_index": text_content_index,
                    "delta": delta,
                },
            )
        )
        return events

    def response_text_done_events() -> list[str]:
        if not text_output_started:
            return []
        output_index = response_text_output_index()
        return [
            sse_event(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "response_id": response_id,
                    "item_id": message_item_id,
                    "output_index": output_index,
                    "content_index": text_content_index,
                    "text": text_output_sent,
                },
            ),
            sse_event(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "response_id": response_id,
                    "item_id": message_item_id,
                    "output_index": output_index,
                    "content_index": text_content_index,
                    "part": {"type": "output_text", "text": text_output_sent},
                },
            ),
            sse_event(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "output_index": output_index,
                    "item": {
                        "id": message_item_id,
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text_output_sent}],
                    },
                },
            ),
        ]

    def response_reasoning_events(delta: str) -> list[str]:
        nonlocal reasoning_started, reasoning_sent
        if not delta:
            return []
        events: list[str] = []
        if not reasoning_started:
            reasoning_started = True
            events.append(
                sse_event(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "response_id": response_id,
                        "output_index": reasoning_output_index,
                        "item": {
                            "id": reasoning_item_id,
                            "type": "reasoning",
                            "status": "in_progress",
                            "summary": [],
                        },
                    },
                )
            )
            events.append(
                sse_event(
                    "response.reasoning_summary_part.added",
                    {
                        "type": "response.reasoning_summary_part.added",
                        "response_id": response_id,
                        "item_id": reasoning_item_id,
                        "output_index": reasoning_output_index,
                        "summary_index": reasoning_summary_index,
                        "part": {"type": "summary_text", "text": ""},
                    },
                )
            )
        reasoning_sent += delta
        events.append(
            sse_event(
                "response.reasoning_summary_text.delta",
                {
                    "type": "response.reasoning_summary_text.delta",
                    "response_id": response_id,
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_output_index,
                    "summary_index": reasoning_summary_index,
                    "delta": delta,
                },
            )
        )
        events.append(
            sse_event(
                "response.reasoning_text.delta",
                {
                    "type": "response.reasoning_text.delta",
                    "response_id": response_id,
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_output_index,
                    "content_index": reasoning_content_index,
                    "delta": delta,
                },
            )
        )
        return events

    def response_reasoning_done_events() -> list[str]:
        if not reasoning_started:
            return []
        return [
            sse_event(
                "response.reasoning_summary_text.done",
                {
                    "type": "response.reasoning_summary_text.done",
                    "response_id": response_id,
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_output_index,
                    "summary_index": reasoning_summary_index,
                    "text": reasoning_sent,
                },
            ),
            sse_event(
                "response.reasoning_summary_part.done",
                {
                    "type": "response.reasoning_summary_part.done",
                    "response_id": response_id,
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_output_index,
                    "summary_index": reasoning_summary_index,
                    "part": {"type": "summary_text", "text": reasoning_sent},
                },
            ),
            sse_event(
                "response.reasoning_text.done",
                {
                    "type": "response.reasoning_text.done",
                    "response_id": response_id,
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_output_index,
                    "content_index": reasoning_content_index,
                    "text": reasoning_sent,
                },
            ),
            sse_event(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "output_index": reasoning_output_index,
                    "item": {
                        "id": reasoning_item_id,
                        "type": "reasoning",
                        "status": "completed",
                        "summary": [{"type": "summary_text", "text": reasoning_sent}],
                    },
                },
            ),
        ]

    content_preview = ""
    tool_buffer_mode = force_buffer_all or bool(tools and _should_parse_tools(db, allow_tool_calls) and _tool_mode(db) == "force_buffer")
    emitted_tool_calls: list[dict[str, Any]] = []
    tool_buffer_failed = False
    started_at = time.time()
    try:
        with request_timer(db, "/v1/responses", model, source_ip, api_key_name, len(query)) as state:
            try:
                for attempt in range(2):
                    async for event in adapter.stream_conversation(
                        query,
                        model,
                        baidu_session_id=binding.baidu_session_id if binding else "",
                        rank=binding.rank if binding else None,
                        credential_id=binding.credential_id if binding else None,
                        cookie_override=_binding_cookie_override(binding),
                    ):
                        _update_conversation_from_event(db, binding, event, content_preview)
                        if output_reasoning and event.reasoning and not force_buffer_all and not tool_buffer_mode:
                            for response_event in response_reasoning_events(event.reasoning):
                                yield response_event
                            continue
                        text = event.text or ""
                        if long_text_strategy == "stream_delta":
                            text += event.workspace_content_delta or ""
                        if output_image_urls and event.images:
                            text += "\n".join(f"![image]({url})" for url in event.images)
                        if text:
                            content_preview += text
                            state["completion_chars"] = len(content_preview)
                            _update_conversation_from_event(db, binding, event, content_preview)
                            if _should_parse_tools(db, allow_tool_calls) and _should_buffer_tool_output(db, tools, content_preview, tool_buffer_mode):
                                tool_buffer_mode = True
                                if len(content_preview) <= max_buffer_chars:
                                    continue
                                clean_content = _fallback_tool_text(content_preview, failure_strategy)
                                for response_event in response_text_events(clean_content):
                                    yield response_event
                                tool_buffer_mode = False
                                tool_buffer_failed = True
                                continue
                            for response_event in response_text_events(text):
                                yield response_event
                    if content_preview:
                        break
                    if attempt == 0 and _reset_binding_for_empty_retry(db, binding, state["request_id"], "/v1/responses stream"):
                        continue
                    break
                if not content_preview:
                    content_preview = _empty_response_message()
                    state["completion_chars"] = len(content_preview)
                    for response_event in response_text_events(content_preview):
                        yield response_event
                _finish_conversation_turn(db, binding, "completed", started_at)
                emitted_tool_calls = _parse_tool_calls_with_retries(content_preview, tools, db) if _should_parse_tools(db, allow_tool_calls) and not tool_buffer_failed else []
                blocked_repeats: list[str] = []
                if emitted_tool_calls:
                    emitted_tool_calls, blocked_repeats = _filter_repeated_tool_calls(
                        emitted_tool_calls,
                        recent_tool_fingerprints or set(),
                        db,
                        state["request_id"],
                        binding,
                    )
                if emitted_tool_calls:
                    summary = _tool_call_summary(emitted_tool_calls)
                    _log_tool_event(db, "INFO", f"stream responses parsed {len(emitted_tool_calls)} tool call(s) for model={model}: {summary}", state["request_id"])
                    _note_conversation_turn(db, binding, f"parsed tool_calls: {summary}")
                    for index, call in enumerate(emitted_tool_calls, start=1 if text_output_started else 0):
                        item = {
                            "id": call["id"],
                            "type": "function_call",
                            "status": "completed",
                            "call_id": call["id"],
                            "name": call["function"]["name"],
                            "arguments": call["function"]["arguments"],
                        }
                        yield sse_event(
                            "response.output_item.added",
                            {
                                "type": "response.output_item.added",
                                "response_id": response_id,
                                "output_index": index,
                                "item": item,
                            },
                        )
                        yield sse_event(
                            "response.output_item.done",
                            {
                                "type": "response.output_item.done",
                                "response_id": response_id,
                                "output_index": index,
                                "item": item,
                            },
                        )
                elif blocked_repeats:
                    for response_event in response_text_events(_repeated_tool_block_text(blocked_repeats)):
                        yield response_event
                elif tool_buffer_mode and content_preview:
                    if _maybe_tool_call_markup(content_preview):
                        _log_tool_event(db, "WARNING", f"stream responses fallback after unparsed tool-like output for model={model}; raw={content_preview[:500]}", state["request_id"])
                        _note_conversation_turn(db, binding, f"unparsed tool-like output: {content_preview[:800]}")
                    clean_content = _fallback_tool_text(content_preview, failure_strategy) if _has_tool_call_markup(content_preview) or _maybe_tool_call_markup(content_preview) else content_preview
                    for response_event in response_text_events(clean_content):
                        yield response_event
            except Exception as exc:
                state["status_code"] = 500
                state["error"] = str(exc)[:1000]
                _finish_conversation_turn(db, binding, "failed", started_at, str(exc)[:1000])
                traced_system_log(db, "ERROR", "conversation_binding", state["request_id"], f"responses stream upstream failed local={binding.local_conversation_id if binding else '-'} model={model}: {str(exc)[:500]}")
                for response_event in response_text_events(_format_stream_error(exc)):
                    yield response_event
    finally:
        final_status = "requires_action" if emitted_tool_calls else "completed"
        _release_conversation_lock(conversation_lock)
        for response_event in response_reasoning_done_events():
            yield response_event
        for response_event in response_text_done_events():
            yield response_event
        yield sse_event(
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": now_ts(),
                    "status": final_status,
                    "model": model,
                    "output": [],
                },
            },
        )
        yield "data: [DONE]\n\n"
        db.close()


def _responses_input_to_messages(body: ResponsesRequest) -> list[ChatMessage]:
    messages: list[dict[str, Any]] = []
    if body.instructions:
        messages.append({"role": "system", "content": body.instructions})
    if isinstance(body.input, str):
        messages.append({"role": "user", "content": body.input})
    else:
        for item in body.input:
            if hasattr(item, "model_dump"):
                messages.append(item.model_dump())
            else:
                messages.append({"role": item.get("role", "user"), "content": item.get("content", "")})
    return [ChatMessage(**message) for message in messages]


@router.post("/images/generations")
async def image_generations(
    body: ImageGenerationRequest,
    request: Request,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(get_api_key),
):
    enforce_access_policy(api_key, body.model, request, db)
    adapter = BaiduAdapter(db)
    source_ip = request.client.host if request.client else ""
    with request_timer(db, "/v1/images/generations", body.model, source_ip, api_key.name, len(body.prompt)):
        urls: list[str] = []
        async for event in adapter.stream_conversation(body.prompt, body.model):
            if event.images:
                urls.extend(event.images)
        return {"created": now_ts(), "data": [{"url": url} for url in urls[: body.n]]}


@router.post("/files")
async def upload_file(
    file: UploadFile = File(...),
    _: ApiKey = Depends(get_api_key),
):
    content = await file.read()
    return {
        "id": "file-" + uuid.uuid4().hex,
        "object": "file",
        "bytes": len(content),
        "created_at": now_ts(),
        "filename": file.filename,
        "purpose": "assistants",
        "status": "processed",
    }


@router.get("/health")
def health():
    return JSONResponse({"status": "ok"})
