"""HTTP 层：JSON API + 静态页面。

用标准库 ThreadingHTTPServer，够用且没有额外依赖；绑定地址默认只监听 127.0.0.1，
需要远程访问时再开 --host 0.0.0.0 并建议同时设置 Basic 认证。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import socket
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from . import __version__, collector, config, notifier
from .llm import LLMClient, LLMError

log = logging.getLogger("sentinel.api")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 2 * 1024 * 1024

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}

ROUTES: Tuple[Tuple[str, re.Pattern, str], ...] = (
    ("GET", re.compile(r"^/api/health$"), "_r_health"),
    ("GET", re.compile(r"^/api/bootstrap$"), "_r_bootstrap"),
    ("GET", re.compile(r"^/api/overview$"), "_r_overview"),
    ("GET", re.compile(r"^/api/status$"), "_r_status"),
    ("GET", re.compile(r"^/api/servers$"), "_r_servers"),
    ("POST", re.compile(r"^/api/servers$"), "_r_server_create"),
    ("POST", re.compile(r"^/api/servers/probe$"), "_r_probe"),
    ("GET", re.compile(r"^/api/servers/(\d+)$"), "_r_server"),
    ("PUT", re.compile(r"^/api/servers/(\d+)$"), "_r_server_update"),
    ("DELETE", re.compile(r"^/api/servers/(\d+)$"), "_r_server_delete"),
    ("POST", re.compile(r"^/api/servers/(\d+)/collect$"), "_r_server_collect"),
    ("POST", re.compile(r"^/api/servers/(\d+)/test$"), "_r_server_test"),
    ("GET", re.compile(r"^/api/servers/(\d+)/series$"), "_r_server_series"),
    ("GET", re.compile(r"^/api/alerts$"), "_r_alerts"),
    ("GET", re.compile(r"^/api/settings$"), "_r_settings"),
    ("PUT", re.compile(r"^/api/settings$"), "_r_settings_update"),
    ("POST", re.compile(r"^/api/settings/test-email$"), "_r_test_email"),
    ("POST", re.compile(r"^/api/settings/test-llm$"), "_r_test_llm"),
    ("POST", re.compile(r"^/api/digest/send$"), "_r_digest"),
    ("POST", re.compile(r"^/api/demo$"), "_r_demo"),
)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "Sentinel/" + __version__
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ 基础
    @property
    def app(self):
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                if not self._check_auth():
                    return
                body: Dict[str, Any] = {}
                if method in ("POST", "PUT"):
                    body = self._read_json()
                for route_method, pattern, name in ROUTES:
                    if route_method != method:
                        continue
                    match = pattern.match(path)
                    if match:
                        getattr(self, name)(match, query, body)
                        return
                self._json({"error": f"没有这个接口：{method} {path}"}, 404)
                return

            if method != "GET":
                self._json({"error": "method not allowed"}, 405)
                return
            if not self._check_auth():
                return
            self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # 兜底，避免连接被直接掐断
            log.exception("处理请求失败 %s %s", method, path)
            try:
                self._json({"error": f"{exc.__class__.__name__}: {exc}"}, 500)
            except Exception:
                pass

    def _check_auth(self) -> bool:
        settings = self.app.get_settings()
        user = str(settings["web"].get("basic_auth_user") or "")
        password = str(settings["web"].get("basic_auth_password") or "")
        if not user:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8", "replace")
            except Exception:
                raw = ""
            if raw == f"{user}:{password}":
                return True
        body = "需要登录".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Sentinel Ops", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求体不是合法 JSON")
        return data if isinstance(data, dict) else {}

    def _json(self, data: Any, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = os.path.abspath(os.path.join(WEB_DIR, rel))
        if not target.startswith(os.path.abspath(WEB_DIR)):
            self._json({"error": "forbidden"}, 403)
            return
        if not os.path.isfile(target):
            target = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(target):
                self._json({"error": "前端文件缺失"}, 500)
                return
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ 接口
    def _r_health(self, match, query, body) -> None:
        self._json({"ok": True, "version": __version__, "ts": int(time.time())})

    def _r_bootstrap(self, match, query, body) -> None:
        self._json(
            {
                "version": __version__,
                "servers": self.app.overview(),
                "settings": self.app.public_settings(),
                "presets": config.LLM_PRESETS,
                "status": self._status_payload(),
            }
        )

    def _r_overview(self, match, query, body) -> None:
        self._json({"servers": self.app.overview(), "status": self._status_payload()})

    def _r_status(self, match, query, body) -> None:
        self._json(self._status_payload())

    def _status_payload(self) -> Dict[str, Any]:
        settings = self.app.get_settings()
        scheduler = self.app.scheduler
        email_cfg = settings["email"]
        llm_cfg = settings["llm"]
        return {
            "version": __version__,
            "uptime": int(time.time()) - scheduler.started_at,
            "last_tick": int(scheduler.last_tick) or None,
            "stats": self.app.store.stats(),
            "interval_sec": settings["collect"]["interval_sec"],
            "email_ready": bool(email_cfg.get("host") and email_cfg.get("to_addrs")),
            "llm_ready": bool(llm_cfg.get("api_key") and llm_cfg.get("model")),
            "llm_enabled": bool(llm_cfg.get("enabled")),
        }

    def _r_servers(self, match, query, body) -> None:
        self._json({"servers": self.app.store.list_servers()})

    def _r_server(self, match, query, body) -> None:
        server_id = int(match.group(1))
        hours = _q_float(query, "hours", 6.0)
        data = self.app.server_detail(server_id, hours)
        if not data:
            self._json({"error": "服务器不存在"}, 404)
            return
        data["alerts"] = self.app.store.list_alerts(limit=20, server_id=server_id)
        self._json(data)

    def _r_server_create(self, match, query, body) -> None:
        error = _validate_server(body, require_password=True)
        if error:
            self._json({"error": error}, 400)
            return
        server_id = self.app.store.create_server(body)
        log.info("新增服务器 %s (%s)", body.get("name"), body.get("host"))
        self._json({"id": server_id, "server": self.app.store.get_server(server_id)}, 201)

    def _r_server_update(self, match, query, body) -> None:
        server_id = int(match.group(1))
        existing = self.app.store.get_server(server_id)
        if not existing:
            self._json({"error": "服务器不存在"}, 404)
            return
        # 允许只传要改的字段：缺的用原值补齐再校验
        merged = {k: existing.get(k) for k in ("name", "host", "port", "username", "note", "kind")}
        merged.update({k: v for k, v in body.items() if v is not None})
        error = _validate_server(merged, require_password=False)
        if error:
            self._json({"error": error}, 400)
            return
        self.app.store.update_server(server_id, body)
        self._json({"server": self.app.store.get_server(server_id)})

    def _r_server_delete(self, match, query, body) -> None:
        server_id = int(match.group(1))
        if not self.app.store.get_server(server_id):
            self._json({"error": "服务器不存在"}, 404)
            return
        self.app.store.delete_server(server_id)
        log.info("删除服务器 #%s", server_id)
        self._json({"ok": True})

    def _r_probe(self, match, query, body) -> None:
        host = str(body.get("host") or "").strip()
        username = str(body.get("username") or "").strip()
        if not host or not username:
            self._json({"error": "IP 和用户名必填"}, 400)
            return
        try:
            result = collector.probe(
                host,
                int(body.get("port") or 22),
                username,
                str(body.get("password") or ""),
                self.app.get_settings(),
            )
        except collector.CollectorError as exc:
            self._json({"error": str(exc)}, 400)
            return
        self._json({"ok": True, "probe": result})

    def _r_server_test(self, match, query, body) -> None:
        server_id = int(match.group(1))
        credentials = self.app.store.server_credentials(server_id)
        if not credentials:
            self._json({"error": "服务器不存在"}, 404)
            return
        if credentials.get("kind") == "demo":
            self._json({"error": "演示服务器是合成数据，没有真实 SSH 连接可测"}, 400)
            return
        try:
            result = collector.probe(
                credentials["host"],
                credentials["port"],
                credentials["username"],
                credentials["password"],
                self.app.get_settings(),
            )
        except collector.CollectorError as exc:
            self._json({"error": str(exc)}, 400)
            return
        self._json({"ok": True, "probe": result})

    def _r_server_collect(self, match, query, body) -> None:
        server_id = int(match.group(1))
        credentials = self.app.store.server_credentials(server_id)
        if not credentials:
            self._json({"error": "服务器不存在"}, 404)
            return
        result = self.app.scheduler.run_collect(credentials)
        self._json(
            {
                "ok": result.get("ok", False),
                "error": result.get("error", ""),
                "metric": result.get("metric"),
                "predictions": result.get("predictions") or [],
                "findings": result.get("findings") or [],
                "alerts": result.get("alerts") or [],
                "analysis": result.get("analysis"),
                "resolved": result.get("resolved") or [],
            }
        )

    def _r_server_series(self, match, query, body) -> None:
        server_id = int(match.group(1))
        hours = _q_float(query, "hours", 6.0)
        self._json(self.app.series(server_id, hours))

    def _r_alerts(self, match, query, body) -> None:
        limit = int(_q_float(query, "limit", 100))
        server_id = None
        if query.get("server_id"):
            try:
                server_id = int(query["server_id"][0])
            except ValueError:
                server_id = None
        self._json({"alerts": self.app.store.list_alerts(limit=max(1, min(500, limit)), server_id=server_id)})

    def _r_settings(self, match, query, body) -> None:
        self._json({"settings": self.app.public_settings(), "presets": config.LLM_PRESETS})

    def _r_settings_update(self, match, query, body) -> None:
        settings = self.app.update_settings(body)
        log.info("配置已更新")
        self._json({"settings": settings})

    def _r_test_email(self, match, query, body) -> None:
        settings = self.app.get_settings()
        try:
            notifier.send_test(settings)
        except notifier.NotifyError as exc:
            self._json({"error": str(exc)}, 400)
            return
        self._json({"ok": True, "message": "测试邮件已发送，请查收"})

    def _r_test_llm(self, match, query, body) -> None:
        settings = self.app.get_settings()
        if body:
            # 允许用表单里还没保存的配置直接试
            settings = config.normalize(
                config.deep_merge(settings, config.sanitize(body))
            )
        try:
            result = LLMClient(settings["llm"]).test()
        except LLMError as exc:
            self._json({"error": str(exc)}, 400)
            return
        self._json({"ok": True, "result": result})

    def _r_digest(self, match, query, body) -> None:
        result = self.app.monitor.send_report()
        self._json(result)

    def _r_demo(self, match, query, body) -> None:
        server_id = self.app.seed_demo()
        self._json({"id": server_id}, 201)


def _q_float(query: Dict[str, list], key: str, default: float) -> float:
    try:
        return float(query[key][0])
    except (KeyError, IndexError, ValueError):
        return default


def _validate_server(body: Dict[str, Any], require_password: bool) -> Optional[str]:
    host = str(body.get("host") or "").strip()
    username = str(body.get("username") or "").strip()
    kind = str(body.get("kind") or "ssh")
    if not host:
        return "服务器 IP / 主机名必填"
    if len(host) > 255 or any(ch.isspace() for ch in host):
        return "服务器地址格式不对"
    if kind != "demo":
        if not username:
            return "SSH 用户名必填"
        if require_password and not body.get("password"):
            return "SSH 密码必填（本工具使用密码登录，暂不支持密钥）"
    try:
        port = int(body.get("port") or 22)
    except (TypeError, ValueError):
        return "端口必须是数字"
    if not 1 <= port <= 65535:
        return "端口范围应为 1-65535"
    try:
        interval = body.get("interval_sec")
        if interval not in (None, "", 0, "0"):
            value = int(interval)
            if value < 10:
                return "采集间隔不能小于 10 秒"
    except (TypeError, ValueError):
        return "采集间隔必须是数字"
    return None


def serve(app, host: str, port: int) -> ThreadingHTTPServer:
    httpd = _Server((host, port), ApiHandler)
    httpd.daemon_threads = True
    httpd.app = app  # type: ignore[attr-defined]
    return httpd


class _Server(ThreadingHTTPServer):
    """Windows 上必须避免 SO_REUSEADDR：否则第二个实例能抢同一个端口，
    两个进程同时采集、同时写一个 SQLite，行为会变得莫名其妙。
    """

    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except (AttributeError, OSError):
                pass
        super().server_bind()
