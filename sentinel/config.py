"""默认配置、字段清洗与合并逻辑。

所有可变配置都存在 SQLite 的 state 表里（key = "settings"），
本模块只负责给出一份完整的默认值，并把用户存下来的部分与默认值深合并，
这样以后新增配置项时老库也能自动补齐。
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict

DEFAULT_SETTINGS: Dict[str, Any] = {
    "collect": {
        # 采集周期（秒）
        "interval_sec": 60,
        # SSH 建连超时
        "connect_timeout_sec": 8,
        # 单次命令执行超时
        "command_timeout_sec": 25,
        # 指标保留天数
        "retention_days": 14,
        # 同时采集的服务器数量
        "parallel": 4,
        # auto = 自动接受未知主机指纹（方便，但非严格）；strict = 只信 known_hosts
        "host_key_policy": "auto",
    },
    "alerts": {
        "enabled": True,
        "cpu_pct": 90.0,
        "cpu_consecutive": 3,
        "mem_pct": 95.0,
        "mem_consecutive": 3,
        "disk_pct": 90.0,
        # 预测多少小时内写满就预警
        "disk_forecast_hours": 72.0,
        # 增长斜率低于该值就不做预测（抖动噪声）
        "disk_min_slope_gb_day": 0.2,
        # 连续失败几次判定为失联
        "offline_after": 2,
        # 同一告警的重复提醒冷却时间（分钟）
        "cooldown_minutes": 360,
        "notify_recovery": True,
        # 每小时的邮件上限，防止配置错误把自己淹没
        "max_emails_per_hour": 20,
        # 每天最多发几封（自然日 0 点重置，0 = 不限制）
        "max_emails_per_day": 2,
        # 相邻两封邮件的最小间隔（分钟，0 = 不限制）
        "min_email_interval_minutes": 60,
    },
    "email": {
        "enabled": False,
        "host": "",
        "port": 465,
        "security": "ssl",  # ssl | starttls | none
        "username": "",
        "password": "",  # 落盘加密
        "from_addr": "",
        "from_name": "哨兵监控",
        "to_addrs": [],
        "subject_prefix": "[哨兵]",
    },
    "llm": {
        "enabled": False,
        # 预设：opencode-go（DeepSeek 套餐） | deepseek（官方） | custom
        "provider": "opencode-go",
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "model": "deepseek-v4.1-flash",
        "api_key": "",  # 落盘加密
        # off | on_alert（仅异常时调用）
        "mode": "on_alert",
        "timeout_sec": 40,
        "max_tokens": 1000,
        "temperature": 0.2,
    },
    "digest": {
        # 定期汇总邮件：把「什么时候提醒」从秒级/实时改成按天、按周
        "enabled": False,
        "frequency": "daily",  # daily | weekly
        "time": "08:00",
        "weekday": 1,  # 1=周一 … 7=周日，仅 weekly 生效
        # immediate = 异常立即发信（汇总只附体检表）
        # summary   = 异常不发即时信，只记录，统一进汇总
        "alerts_mode": "immediate",
    },
    "web": {
        # 设置后 Web 端需要 HTTP Basic 认证；留空则不校验
        "basic_auth_user": "",
        "basic_auth_password": "",
    },
}

# 前端可以读到的密钥字段一律打码，只回传“是否已配置”
SECRET_PATHS = (
    ("email", "password"),
    ("llm", "api_key"),
    ("web", "basic_auth_password"),
)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 合进 base 的副本；只递归 dict，其它类型直接覆盖。"""
    out = copy.deepcopy(base)
    if not isinstance(override, dict):
        return out
    for key, value in override.items():
        if key not in out:
            out[key] = copy.deepcopy(value)
        elif isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _coerce(value: Any, default: Any) -> Any:
    """按默认值的类型做一次轻量纠偏，避免前端传字符串导致计算报错。"""
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes", "是")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, list):
        if isinstance(value, str):
            value = [x.strip() for x in value.replace(";", ",").split(",")]
        if isinstance(value, (list, tuple)):
            return [str(x).strip() for x in value if str(x).strip()]
        return []
    if isinstance(default, str):
        return "" if value is None else str(value)
    return value


def sanitize(payload: Dict[str, Any], defaults: Dict[str, Any] = None) -> Dict[str, Any]:
    """只保留默认结构里存在的字段，并按默认值类型纠偏。"""
    defaults = defaults or DEFAULT_SETTINGS
    out: Dict[str, Any] = {}
    for key, default in defaults.items():
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(default, dict):
            if isinstance(value, dict):
                out[key] = sanitize(value, default)
        else:
            out[key] = _coerce(value, default)
    return out


def mask_secrets(settings: Dict[str, Any]) -> Dict[str, Any]:
    """返回给前端用的副本：密钥换成是否已配置。"""
    out = copy.deepcopy(settings)
    for section, field in SECRET_PATHS:
        node = out.get(section)
        if isinstance(node, dict):
            raw = node.get(field) or ""
            node[field] = ""
            node[field + "_set"] = bool(raw)
    return out


def normalize(settings: Dict[str, Any]) -> Dict[str, Any]:
    """合并 + 纠偏 + 边界限制。"""
    merged = deep_merge(DEFAULT_SETTINGS, sanitize(settings))
    c = merged["collect"]
    c["interval_sec"] = max(10, min(86400, int(c["interval_sec"])))
    c["connect_timeout_sec"] = max(3, min(120, int(c["connect_timeout_sec"])))
    c["command_timeout_sec"] = max(5, min(300, int(c["command_timeout_sec"])))
    c["retention_days"] = max(1, min(365, int(c["retention_days"])))
    c["parallel"] = max(1, min(32, int(c["parallel"])))
    if c["host_key_policy"] not in ("auto", "strict"):
        c["host_key_policy"] = "auto"

    a = merged["alerts"]
    for key in ("cpu_pct", "mem_pct", "disk_pct"):
        a[key] = max(1.0, min(100.0, float(a[key])))
    a["cpu_consecutive"] = max(1, min(1440, int(a["cpu_consecutive"])))
    a["mem_consecutive"] = max(1, min(1440, int(a["mem_consecutive"])))
    a["disk_forecast_hours"] = max(1.0, min(24.0 * 90, float(a["disk_forecast_hours"])))
    a["disk_min_slope_gb_day"] = max(0.0, min(10000.0, float(a["disk_min_slope_gb_day"])))
    a["offline_after"] = max(1, min(100, int(a["offline_after"])))
    a["cooldown_minutes"] = max(0, min(10080, int(a["cooldown_minutes"])))
    a["max_emails_per_hour"] = max(1, min(500, int(a["max_emails_per_hour"])))
    a["max_emails_per_day"] = max(0, min(500, int(a["max_emails_per_day"])))
    a["min_email_interval_minutes"] = max(0, min(1440, int(a["min_email_interval_minutes"])))

    e = merged["email"]
    if e["security"] not in ("ssl", "starttls", "none"):
        e["security"] = "ssl"
    e["port"] = max(1, min(65535, int(e["port"])))
    if not e["from_addr"] and e["username"]:
        e["from_addr"] = e["username"]

    l = merged["llm"]
    if l["provider"] not in ("opencode-go", "deepseek", "custom"):
        l["provider"] = "custom"
    if l["mode"] not in ("off", "on_alert"):
        l["mode"] = "on_alert"
    l["timeout_sec"] = max(5, min(180, int(l["timeout_sec"])))
    l["max_tokens"] = max(120, min(4000, int(l["max_tokens"])))
    l["temperature"] = max(0.0, min(2.0, float(l["temperature"])))

    d = merged["digest"]
    if d["frequency"] not in ("daily", "weekly"):
        d["frequency"] = "daily"
    if d["alerts_mode"] not in ("immediate", "summary"):
        d["alerts_mode"] = "immediate"
    if not _valid_hhmm(d["time"]):
        d["time"] = "08:00"
    try:
        d["weekday"] = max(1, min(7, int(d["weekday"])))
    except (TypeError, ValueError):
        d["weekday"] = 1
    # 选了“只进汇总”却没开汇总，就等于永远收不到信，这里强制打开
    if d["alerts_mode"] == "summary" and not d["enabled"]:
        d["enabled"] = True
    return merged


def _valid_hhmm(text: str) -> bool:
    try:
        hh, mm = str(text).split(":")
        return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
    except Exception:
        return False


def dump(settings: Dict[str, Any]) -> str:
    return json.dumps(settings, ensure_ascii=False, separators=(",", ":"))


LLM_PRESETS = {
    "opencode-go": {
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "model": "deepseek-v4.1-flash",
        "note": "OpenCode Go 订阅（DeepSeek V4.1 Flash / V4 Pro），OpenAI 兼容端点",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "note": "DeepSeek 官方 API，按量付费",
    },
    "custom": {
        "base_url": "",
        "model": "",
        "note": "任意 OpenAI 兼容端点（vLLM / Ollama / 硅基流动等）",
    },
}
