"""OpenAI 兼容的大模型调用（默认指向 OpenCode Go 的 DeepSeek 套餐）。

只用标准库 urllib，避免为了一个 HTTP 请求引入依赖。
模型只负责“解释和分级”，不负责决定要不要发邮件（规则仍然兜底）。
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("sentinel.llm")

USER_AGENT = "sentinel-ops/1.0"

SYSTEM_PROMPT = (
    "你是一名资深 Linux 运维工程师，负责判断监控告警是否值得立即打扰值班人员。"
    "你会收到一台服务器的指标摘要、磁盘增长预测、以及已命中的规则。"
    "请判断异常的真实严重性，给出最可能的根因假设和可执行的处置建议。"
    "严格只输出一个 JSON 对象，不要 markdown 代码块、不要任何前后解释。"
    "JSON 字段："
    "severity 取 critical/high/warning/info 之一；"
    "summary 一句话中文结论（40 字内）；"
    "likely_cause 中文根因假设（80 字内）；"
    "actions 字符串数组，最多 4 条，每条都是可直接执行的命令或具体检查项；"
    "need_alert 布尔，是否值得立即通知人；"
    "confidence 0 到 1 的小数，表示你对该判断的把握。"
)

DIGEST_PROMPT = (
    "你是一名资深 Linux 运维工程师。你会收到一组服务器过去 24 小时的指标摘要。"
    "请用中文写一份简短的巡检结论：整体是否健康、哪几台需要关注、最值得做的一件事。"
    "严格只输出一个 JSON 对象，字段：health 取 good/warn/bad 之一；"
    "summary 一句话中文结论（50 字内）；highlights 字符串数组，最多 5 条，每条一句中文。"
)


class LLMError(RuntimeError):
    pass


def resolve_endpoint(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise LLMError("未配置 base URL")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def _content_text(value: Any) -> str:
    """content 可能是字符串，也可能是 [{"type":"text","text":"..."}] 这种分段数组。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "".join(parts)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return str(value)


def extract_message(data: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """从 chat/completions 返回里取正文，兼容几种常见变体。

    返回 (文本, finish_reason, usage)。文本可能为空——调用方需要处理。
    """
    choices = data.get("choices") or []
    if not isinstance(choices, list) or not choices:
        raise LLMError("返回里没有 choices，可能不是 OpenAI 兼容的 chat 接口")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}

    text = _content_text(message.get("content"))
    if not text.strip():
        text = _content_text(choice.get("text"))  # 少数网关走 completions 风格
    if not text.strip():
        # 推理型模型：答案可能只落在 reasoning 字段里
        text = _content_text(message.get("reasoning_content")) or _content_text(
            message.get("reasoning")
        )
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return text.strip(), str(choice.get("finish_reason") or ""), usage


class LLMClient:
    def __init__(self, cfg: Dict[str, Any], session_id: str = ""):
        self.cfg = cfg or {}
        self.base_url = self.cfg.get("base_url") or ""
        self.model = self.cfg.get("model") or ""
        self.api_key = self.cfg.get("api_key") or ""
        self.timeout = int(self.cfg.get("timeout_sec") or 40)
        self.provider = self.cfg.get("provider") or "custom"
        self.session_id = session_id or ""

    # ------------------------------------------------------------------ HTTP
    def chat(self, system: str, user: str, max_tokens: Optional[int] = None) -> str:
        if not self.model:
            raise LLMError("未配置模型名")
        budget = int(max_tokens or self.cfg.get("max_tokens") or 1000)
        text, finish, usage = self._post(system, user, budget)
        # 推理型模型可能把预算全花在思考上，正文为空：自动加大预算重试一次
        if not text and finish == "length" and budget < 3000:
            bigger = min(max(budget * 2, 800), 3000)
            log.warning(
                "模型返回空内容（finish_reason=length），max_tokens %s → %s 重试", budget, bigger
            )
            text, finish, usage = self._post(system, user, bigger)
            budget = bigger
        if not text:
            raise LLMError(self._empty_hint(finish, usage, budget))
        return text

    def _post(self, system: str, user: str, budget: int) -> Tuple[str, str, Dict[str, Any]]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": float(self.cfg.get("temperature") or 0.2),
            "max_tokens": int(budget),
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        if self.provider == "opencode-go":
            # OpenCode Go 用这个头做路由和提示缓存，官方建议每个会话一个稳定 ID
            headers["x-opencode-session"] = self._session_uuid()

        req = urllib.request.Request(
            resolve_endpoint(self.base_url), data=body, headers=headers, method="POST"
        )
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise LLMError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"网络错误：{exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMError(f"请求超时（{self.timeout}s）") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"返回不是 JSON：{raw[:200]}") from exc

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise LLMError(f"接口报错：{msg}")

        if not isinstance(data, dict):
            raise LLMError(f"返回结构异常：{raw[:200]}")
        try:
            text, finish, usage = extract_message(data)
        except LLMError:
            raise
        except Exception as exc:  # 兜底，避免把解析异常抛成 500
            raise LLMError(f"返回结构异常：{raw[:200]}") from exc
        if not text:
            log.warning("模型正文为空：finish=%s usage=%s raw=%s", finish, usage, raw[:800])
        return text, finish, usage

    @staticmethod
    def _empty_hint(finish: str, usage: Dict[str, Any], budget: int) -> str:
        detail = f"finish_reason={finish or '未知'}"
        if isinstance(usage, dict) and usage:
            detail += "，completion_tokens=%s" % usage.get("completion_tokens")
        if finish == "length":
            return (
                f"模型返回空内容（{detail}）。max_tokens 已自动加到 {budget} 仍不够，"
                "说明预算被思考过程吃光了：请在设置里把「最大输出 token」调到 1500 以上，"
                "或者换一个非推理模型。"
            )
        if finish == "content_filter":
            return f"模型返回空内容（{detail}）。请求被内容过滤拦截，换个说法或换模型试试。"
        return (
            f"模型返回空内容（{detail}）。该模型/网关可能不支持当前请求格式，"
            "或返回体里没有正文；可以先用「测试模型」确认端点是否可用。"
        )

    # ------------------------------------------------------------------ 业务
    def _session_uuid(self) -> str:
        if self.session_id:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, "sentinel:" + self.session_id))
        return str(uuid.uuid4())

    def analyze_incident(self, context: Dict[str, Any]) -> Dict[str, Any]:
        text = self.chat(SYSTEM_PROMPT, json.dumps(context, ensure_ascii=False, indent=None))
        parsed = extract_json(text)
        if not parsed:
            return {
                "severity": "unknown",
                "summary": text.strip()[:120],
                "likely_cause": "",
                "actions": [],
                "need_alert": True,
                "confidence": None,
                "raw": text[:1000],
            }
        return normalize_analysis(parsed)

    def daily_digest(self, context: Dict[str, Any]) -> Dict[str, Any]:
        text = self.chat(DIGEST_PROMPT, json.dumps(context, ensure_ascii=False), max_tokens=500)
        parsed = extract_json(text) or {}
        return {
            "health": parsed.get("health") or "warn",
            "summary": parsed.get("summary") or text.strip()[:200],
            "highlights": [str(x) for x in (parsed.get("highlights") or [])][:5],
        }

    def test(self) -> Dict[str, Any]:
        # 用真实配置的预算来测，避免“测试通过但正式调用不够用”
        budget = max(512, int(self.cfg.get("max_tokens") or 0))
        text = self.chat(
            "你是一个连通性测试助手。只输出 JSON：{\"ok\":true,\"msg\":\"一句中文问候\"}",
            "请回一句 20 字以内的问候，并说明你已就绪。",
            max_tokens=budget,
        )
        return {"text": text.strip()[:500], "model": self.model, "endpoint": resolve_endpoint(self.base_url)}


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从可能带 ``` 围栏或前后废话的回复里抠出第一个 JSON 对象。"""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


SEVERITIES = ("critical", "high", "warning", "info")


def normalize_analysis(parsed: Dict[str, Any]) -> Dict[str, Any]:
    severity = str(parsed.get("severity") or "warning").lower()
    if severity not in SEVERITIES:
        severity = "warning"
    actions: List[str] = []
    for item in parsed.get("actions") or []:
        text = str(item).strip()
        if text:
            actions.append(text)
        if len(actions) >= 4:
            break
    try:
        confidence = float(parsed.get("confidence"))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = None
    need_alert = parsed.get("need_alert")
    return {
        "severity": severity,
        "summary": str(parsed.get("summary") or "").strip()[:200],
        "likely_cause": str(parsed.get("likely_cause") or "").strip()[:300],
        "actions": actions,
        "need_alert": True if need_alert is None else bool(need_alert),
        "confidence": confidence,
    }
