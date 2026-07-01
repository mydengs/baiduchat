import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

import httpx
import orjson
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.init_db import get_setting
from app.db.models import Credential, ModelConfig, PromptConfig
from app.services.logging_service import system_log


AI_TAB_RE = re.compile(
    r'<script[^>]+name=["\']aiTabFrameBaseData["\'][^>]*>(.*?)</script>',
    re.S,
)


@dataclass
class BaiduContext:
    token: str
    searchframe_lid: str
    cookie: str
    credential_id: int | None = None
    credential_name: str = "anonymous"


@dataclass
class ParsedEvent:
    component: str
    text: str = ""
    reasoning: str = ""
    images: list[str] | None = None
    workspace_file_id: str | None = None
    workspace_content_delta: str = ""
    raw: dict[str, Any] | None = None
    lid: str = ""
    qid: str = ""
    session_id: str = ""
    pkg_id: str = ""
    seq_id: int | None = None
    finished: bool = False


class BaiduAdapter:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()

    def get_model(self, public_id: str) -> ModelConfig:
        model = self.db.scalar(
            select(ModelConfig).where(ModelConfig.public_id == public_id, ModelConfig.enabled.is_(True))
        )
        if not model:
            model = self.db.scalar(select(ModelConfig).where(ModelConfig.public_id == "smart"))
        if not model:
            raise RuntimeError("No enabled model config found")
        return model

    def get_prompt_config(self) -> PromptConfig | None:
        return self.db.scalar(select(PromptConfig).where(PromptConfig.name == "default"))

    def build_query(self, messages: list[dict[str, Any]], force_prompt: bool = True) -> str:
        parts: list[str] = []
        prompt = self.get_prompt_config()
        if force_prompt and prompt and prompt.enabled and prompt.content:
            parts.append(f"系统提示词：\n{prompt.content}")
        for item in messages:
            role = item.get("role", "user")
            content = item.get("content", "")
            if role == "tool":
                tool_name = item.get("name") or item.get("tool_call_id") or "tool"
                parts.append(f"工具 {tool_name} 的执行结果：\n{content}")
                continue
            if item.get("tool_calls"):
                parts.append(f"助手请求调用工具：\n{json.dumps(item.get('tool_calls'), ensure_ascii=False)}")
                continue
            if isinstance(content, list):
                content_text = "\n".join(str(part.get("text", part)) for part in content)
            else:
                content_text = str(content)
            if role == "system":
                parts.append(f"系统：{content_text}")
            elif role == "assistant":
                parts.append(f"助手：{content_text}")
            else:
                parts.append(f"用户：{content_text}")
        return "\n\n".join(parts).strip()

    async def init_context(self, cookie_override: str | None = None, credential_id: int | None = None) -> BaiduContext:
        if cookie_override is not None:
            return await self._load_context(cookie_override, None)

        if credential_id is not None:
            credential = self.db.get(Credential, credential_id)
            if not credential:
                system_log(self.db, "ERROR", "credential", f"bound credential missing id={credential_id}")
                raise RuntimeError(f"Bound credential {credential_id} no longer exists")
            if not credential.enabled:
                system_log(self.db, "ERROR", "credential", f"bound credential disabled id={credential.id} name={credential.name}")
                raise RuntimeError(f"Bound credential {credential.name} is disabled")
            try:
                return await self._load_context(credential.cookie, credential)
            except Exception as exc:
                self._mark_credential_failed(credential, exc)
                system_log(self.db, "ERROR", "credential", f"bound credential failed id={credential.id} name={credential.name}: {str(exc)[:500]}")
                raise

        mode = get_setting(self.db, "credential_mode", "auto").lower()
        if mode not in {"auto", "pool", "anonymous"}:
            mode = "auto"
        if mode == "anonymous":
            return await self._load_context("", None)

        credentials = self.db.scalars(
            select(Credential)
            .where(Credential.enabled.is_(True))
            .order_by(Credential.failure_count.asc(), Credential.last_used_at.asc(), Credential.id.asc())
        ).all()
        if not credentials:
            if mode == "pool":
                raise RuntimeError("Credential mode is pool, but no enabled credentials are available")
            return await self._load_context("", None)

        last_error: Exception | None = None
        for credential in credentials:
            try:
                return await self._load_context(credential.cookie, credential)
            except Exception as exc:
                last_error = exc
                self._mark_credential_failed(credential, exc)
                system_log(self.db, "WARNING", "credential", f"credential failed id={credential.id} name={credential.name}: {str(exc)[:500]}")
        if mode == "auto":
            return await self._load_context("", None)
        raise RuntimeError(f"All enabled credentials failed: {last_error}")

    def _mark_credential_failed(self, credential: Credential, exc: Exception) -> None:
        credential.failure_count += 1
        credential.last_status = f"failed: {str(exc)[:52]}"
        threshold = self._credential_failure_threshold()
        if threshold > 0 and credential.failure_count >= threshold:
            credential.enabled = False
            credential.last_status = f"disabled after {credential.failure_count} failures: {str(exc)[:36]}"
        self.db.commit()

    def _credential_failure_threshold(self) -> int:
        raw = get_setting(self.db, "credential_failure_disable_threshold", "3")
        try:
            return max(0, int(raw))
        except ValueError:
            return 3

    async def _load_context(self, cookie: str, credential: Credential | None) -> BaiduContext:
        headers = self._browser_headers(cookie)
        async with httpx.AsyncClient(timeout=self.settings.default_upstream_timeout, follow_redirects=True) as client:
            resp = await client.get(f"{self.settings.baidu_base_url}/", headers=headers)
            resp.raise_for_status()
        merged_cookie = self._merge_response_cookies(cookie, resp.cookies)
        match = AI_TAB_RE.search(resp.text)
        if not match:
            raise RuntimeError("Could not find aiTabFrameBaseData in homepage")
        data = json.loads(match.group(1))
        token = str(data.get("token") or data.get("context", {}).get("token") or "")
        lid = str(data.get("lid") or data.get("context", {}).get("searchframeLid") or "")
        if not token or not lid:
            raise RuntimeError("Homepage did not include token/lid")
        if credential:
            today = datetime.utcnow().date().isoformat()
            if credential.use_count_date != today:
                credential.use_count_date = today
                credential.use_count_today = 0
            credential.use_count_total += 1
            credential.use_count_today += 1
            credential.failure_count = 0
            credential.last_status = f"ok token={token[:6]} lid={lid}"
            credential.last_used_at = datetime.utcnow()
            self.db.commit()
        return BaiduContext(
            token=token,
            searchframe_lid=lid,
            cookie=merged_cookie,
            credential_id=credential.id if credential else None,
            credential_name=credential.name if credential else "anonymous",
        )

    def _merge_response_cookies(self, cookie_header: str, response_cookies: httpx.Cookies) -> str:
        pairs: dict[str, str] = {}
        for part in (cookie_header or "").split(";"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if name:
                pairs[name] = value.strip()
        for cookie in response_cookies.jar:
            if cookie.name:
                pairs[cookie.name] = cookie.value
        return "; ".join(f"{name}={value}" for name, value in pairs.items())

    def make_chat_token(self, context: BaiduContext, query: str) -> str:
        md5_query = hashlib.md5(query.encode()).hexdigest()
        ts = int(time.time() * 1000)
        raw = f"{context.token}|{md5_query}|{ts}|{context.searchframe_lid}"
        encoded = base64.b64encode(raw.encode()).decode()
        return f"{encoded}-{context.searchframe_lid}-3"

    def build_payload(
        self,
        query: str,
        model: ModelConfig,
        context: BaiduContext,
        rank: int = 1,
        direct_answer: bool = False,
        baidu_session_id: str = "",
    ) -> dict[str, Any]:
        chat_token = self.make_chat_token(context, query)
        search_info: dict[str, Any] = {
            "srcid": "",
            "order": "",
            "tplname": "",
            "dqaKey": "",
            "re_rank": str(rank),
            "ori_lid": baidu_session_id,
            "sa": "ai_directans" if direct_answer else "bkb",
            "enter_type": "chat_url",
            "chatParams": {
                "setype": model.setype,
                "chat_samples": "WISE_NEW_CSAITAB",
                "chat_token": chat_token,
                "scene": "",
            },
            "isPrivateChat": False,
            "usedModel": {
                "modelName": model.baidu_model,
                "modelFunction": {"deepSearch": model.deep_search, "thinkMode": model.think_mode},
            },
            "landingPageSwitch": "",
            "landingPage": "aitab",
            "ecomFrom": "",
            "hasLocPermission": "",
            "isInnovate": 2,
            "applid": "",
            "a_lid": "",
            "showMindMap": False,
            "deepDecisionInfo": {"isDeepDecision": 0},
        }
        if direct_answer:
            search_info["interaction_type"] = 20
        payload: dict[str, Any] = {
            "message": {
                "inputMethod": "chat_search",
                "isRebuild": False,
                "content": {
                    "query": "",
                    "agentInfo": {"agent_id": [""], "params": json.dumps({"agt_rk": rank, "agt_sess_cnt": 1})},
                    "agentInfoList": [],
                    "qtype": 0,
                    "extData": {},
                },
                "searchInfo": search_info,
                "from": "",
                "source": model.source,
                "query": [{"type": "TEXT", "data": {"text": {"query": query, "extData": "{}", "text_type": ""}}}],
                "anti_ext": {"inputT": None, "ck1": 126, "ck9": 332, "ck10": 180},
            },
            "setype": model.setype,
            "rank": rank,
        }
        if direct_answer:
            payload["sa"] = "ai_directans"
        return payload

    def _browser_headers(self, cookie: str = "") -> dict[str, str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
        }
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _conversation_headers(self, context: BaiduContext, query: str, model: ModelConfig, rank: int) -> dict[str, str]:
        return self._conversation_headers_for_session(context, query, model, rank, "")

    def _conversation_headers_for_session(
        self,
        context: BaiduContext,
        query: str,
        model: ModelConfig,
        rank: int,
        baidu_session_id: str = "",
    ) -> dict[str, str]:
        anti = json.dumps({"inputT": None, "ck1": 126, "ck9": 332, "ck10": 180}, separators=(",", ":"))
        encoded_query = quote(query, safe="")
        encoded_anti = quote(anti, safe="")
        headers = self._browser_headers(context.cookie)
        headers.update(
            {
                "Accept": "text/event-stream",
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": self.settings.baidu_base_url,
                "Referer": (
                    f"{self.settings.baidu_base_url}/search/{baidu_session_id}?enter_type=chat_url"
                    if baidu_session_id
                    else f"{self.settings.baidu_base_url}/"
                ),
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "source": model.source,
                "isDeepseek": "1" if "DeepSeek" in model.baidu_model else "0",
                "personifiedSwitch": "0",
                "X-Chat-Message": f"query:{encoded_query},anti_ext:{encoded_anti},enter_type:chat_url,re_rank:{rank},modelName:{model.baidu_model}",
            }
        )
        return headers

    async def stream_conversation(
        self,
        query: str,
        public_model: str,
        direct_answer: Optional[bool] = None,
        baidu_session_id: str = "",
        rank: int | None = None,
        credential_id: int | None = None,
        cookie_override: str | None = None,
    ) -> AsyncIterator[ParsedEvent]:
        model = self.get_model(public_model)
        direct = direct_answer
        if direct is None:
            direct = get_setting(self.db, "auto_direct_answer", "false").lower() == "true"
        context = await self.init_context(cookie_override=cookie_override, credential_id=credential_id)
        request_rank = rank if rank and rank > 0 else int(time.time()) % 1000
        payload = self.build_payload(
            query,
            model,
            context,
            rank=request_rank,
            direct_answer=direct,
            baidu_session_id=baidu_session_id,
        )
        headers = self._conversation_headers_for_session(context, query, model, request_rank, baidu_session_id)
        if get_setting(self.db, "log_upstream_model", "true").lower() == "true":
            used_model = payload.get("message", {}).get("searchInfo", {}).get("usedModel", {})
            system_log(
                self.db,
                "INFO",
                "upstream_model",
                "conversation model summary "
                f"public_model={public_model} "
                f"baidu_model={model.baidu_model} "
                f"usedModel={json.dumps(used_model, ensure_ascii=False, separators=(',', ':'))} "
                f"source={model.source} setype={model.setype} "
                f"isDeepseek={headers.get('isDeepseek')} "
                f"rank={request_rank} bound_session={baidu_session_id or '-'} "
                f"credential={context.credential_name}",
            )
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{self.settings.baidu_base_url}/aichat/api/conversation",
                headers=headers,
                content=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
            ) as resp:
                resp.raise_for_status()
                buffer = ""
                async for chunk in resp.aiter_bytes():
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip("\r")
                        if not line:
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        event = self.parse_event_line(line)
                        if event:
                            if event and context:
                                event.raw = event.raw or {}
                                event.raw["_baidu_context"] = {
                                    "credential_id": context.credential_id,
                                    "credential_name": context.credential_name,
                                    "searchframe_lid": context.searchframe_lid,
                                    "cookie_snapshot": context.cookie if context.credential_id is None else "",
                                }
                            yield event
                if buffer.strip():
                    line = buffer.strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    event = self.parse_event_line(line)
                    if event:
                        if event and context:
                            event.raw = event.raw or {}
                            event.raw["_baidu_context"] = {
                                "credential_id": context.credential_id,
                                "credential_name": context.credential_name,
                                "searchframe_lid": context.searchframe_lid,
                                "cookie_snapshot": context.cookie if context.credential_id is None else "",
                            }
                        yield event

    async def canvas_search(
        self,
        lid: str,
        qid: str,
        file_id: str,
        token: str,
        cookie: str = "",
        is_private_chat: bool = False,
    ) -> dict[str, Any]:
        params = {
            "tk": token,
            "lid": lid,
            "qid": qid,
            "knowledgeId": "",
            "isPrivateChat": str(is_private_chat).lower(),
            "fileId": file_id,
        }
        headers = self._browser_headers(cookie)
        async with httpx.AsyncClient(timeout=self.settings.default_upstream_timeout) as client:
            resp = await client.get(f"{self.settings.baidu_base_url}/aichat/api/canvas/search", params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def download_workspace_file(
        self,
        file_type: str,
        file_name: str,
        lid: str,
        qid: str,
        file_id: str,
        token: str,
        cookie: str = "",
        is_private_chat: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "type": "writing",
            "fileType": file_type,
            "fileName": file_name,
            "data": {"fileId": file_id, "lid": lid, "qid": qid},
            "token": token,
            "isPrivateChat": is_private_chat,
        }
        headers = self._browser_headers(cookie)
        headers.update({"Content-Type": "application/json;charset=UTF-8", "Origin": self.settings.baidu_base_url, "Referer": f"{self.settings.baidu_base_url}/"})
        async with httpx.AsyncClient(timeout=self.settings.default_upstream_timeout) as client:
            resp = await client.post(
                f"{self.settings.baidu_base_url}/aichat/api/download",
                headers=headers,
                content=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
            )
            resp.raise_for_status()
            return resp.json()

    def parse_event_line(self, line: str) -> ParsedEvent | None:
        try:
            data = orjson.loads(line)
        except Exception:
            return None
        basedata = data if "lid" in data and "user" in data else {}
        message = data.get("data", {}).get("message", {})
        meta = message.get("metaData", {})
        generator = message.get("content", {}).get("generator", {})
        component = generator.get("component", "")
        gdata = generator.get("data", {}) or {}
        event = ParsedEvent(component=component, raw=data, finished=bool(generator.get("isFinished") or meta.get("endTurn")))
        event.lid = str(basedata.get("lid") or "")
        event.qid = str(data.get("qid") or "")
        event.session_id = str(data.get("sessionId") or "")
        event.pkg_id = str(data.get("pkgId") or "")
        event.seq_id = data.get("seq_id") if isinstance(data.get("seq_id"), int) else None
        if component == "markdown-yiyan":
            event.text = self._fix_text_encoding(str(gdata.get("value") or generator.get("text") or ""))
        elif component == "thinkingSteps":
            arr = gdata.get("reasoningContentArr") or []
            event.reasoning = self._fix_text_encoding("".join(str(x) for x in arr) or str(gdata.get("reasoningContent") or ""))
        elif component == "image-generate":
            items = gdata.get("items") or []
            event.images = [i.get("originUrl") or i.get("previewUrl") for i in items if i.get("originUrl") or i.get("previewUrl")]
        elif component == "imageScroll":
            items = gdata.get("items") or []
            event.images = [i.get("objUrl") or i.get("originUrl") or i.get("thumbUrl") for i in items if i.get("objUrl") or i.get("originUrl") or i.get("thumbUrl")]
        elif component == "editor-workspace-viewer":
            value = gdata.get("value") or {}
            update = value.get("updateFile") or {}
            event.workspace_file_id = update.get("fileId") or value.get("id")
            event.workspace_content_delta = self._fix_text_encoding(str(update.get("content") or ""))
        return event

    def _fix_text_encoding(self, text: str) -> str:
        if not text:
            return text
        mojibake_markers = ("ä", "å", "æ", "ç", "è", "é", "ï¼", "ã")
        if not any(marker in text for marker in mojibake_markers):
            return text
        try:
            return text.encode("latin1").decode("utf-8")
        except UnicodeError:
            return text
