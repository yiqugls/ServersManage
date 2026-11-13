#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端验证：假 SSH + 假 SMTP + 假 OpenAI + 真实 Sentinel 进程。

跑法：
    python tests/e2e.py

会使用 8099 端口和 tests/_tmp 数据目录，不影响 data/ 里的正式数据。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakeOpenAI, FakeSSH, SMTPSink  # noqa: E402

PORT = 8099
TMP = os.path.join(BASE, "tests", "_tmp")
BASE_URL = f"http://127.0.0.1:{PORT}"

results = []


def check(name, ok, extra=""):
    results.append((name, bool(ok), extra))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    return ok


def request(path, method="GET", body=None, timeout=30):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw}


def wait_health(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, _ = request("/api/health", timeout=2)
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def parse_mail(record):
    """解出 (主题, HTML 正文)；邮件是 MIME 多部分，必须解码 base64。"""
    from email import message_from_bytes
    from email.header import decode_header, make_header

    msg = message_from_bytes(record.data)
    try:
        subject = str(make_header(decode_header(msg.get("Subject", ""))))
    except Exception:
        subject = msg.get("Subject", "")
    html = ""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True) or b""
            html = payload.decode(part.get_content_charset() or "utf-8", "replace")
            break
    return subject, html


def main():
    print("启动假服务…")
    ssh = FakeSSH()
    ssh.start()
    smtp = SMTPSink()
    smtp.start()
    llm = FakeOpenAI()
    llm.start()

    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "run.py"), "--port", str(PORT),
         "--data-dir", TMP, "--no-demo", "--log-level", "WARNING"],
        cwd=BASE, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    ok_all = True
    try:
        if not check("服务启动 /api/health", wait_health()):
            return 1

        print("\n配置 SMTP 与模型…")
        code, data = request("/api/settings", "PUT", {
            "collect": {"interval_sec": 30, "connect_timeout_sec": 5, "command_timeout_sec": 15},
            "alerts": {"disk_pct": 85, "cooldown_minutes": 0},
            "email": {
                "enabled": True, "host": "127.0.0.1", "port": 2525, "security": "none",
                "username": "", "password": "", "from_addr": "sentinel@test.local",
                "from_name": "哨兵", "to_addrs": ["ops@test.local"], "subject_prefix": "[哨兵]",
            },
            "llm": {
                "enabled": True, "provider": "custom", "base_url": "http://127.0.0.1:8765/v1",
                "model": "test-model", "api_key": "sk-test-key", "mode": "on_alert",
                "timeout_sec": 10, "max_tokens": 500,
            },
        })
        ok_all &= check("保存配置", code == 200 and data["settings"]["email"]["enabled"] is True)
        ok_all &= check("密钥不回显", data["settings"]["llm"]["api_key"] == ""
                        and data["settings"]["llm"]["api_key_set"] is True)
        ok_all &= check("明文密钥未落盘",
                        b"sk-test-key" not in open(os.path.join(TMP, "sentinel.db"), "rb").read())

        print("\n错误密码应当被拒绝…")
        code, data = request("/api/servers/probe", "POST", {
            "host": "127.0.0.1", "port": 2222, "username": "root", "password": "wrong",
        })
        ok_all &= check("错误密码探活失败", code == 400 and "认证失败" in data.get("error", ""),
                        data.get("error", "")[:60])

        print("\n绑定服务器并采集…")
        code, data = request("/api/servers", "POST", {
            "name": "fake-web-01", "host": "127.0.0.1", "port": 2222,
            "username": "root", "password": "secret", "note": "e2e",
        })
        server_id = data.get("id")
        ok_all &= check("创建服务器", code == 201 and bool(server_id), str(data.get("error", "")))

        code, data = request(f"/api/servers/{server_id}/collect", "POST", {})
        metric = data.get("metric") or {}
        disks = metric.get("disks") or []
        ok_all &= check("SSH 采集成功", code == 200 and data.get("ok") is True,
                        str(data.get("error", ""))[:80])
        ok_all &= check("CPU 解析 ≈95%", abs((metric.get("cpu_pct") or 0) - 95) <= 1.5,
                        str(metric.get("cpu_pct")))
        ok_all &= check("内存解析 64%", abs((metric.get("mem_pct") or 0) - 64) <= 1,
                        str(metric.get("mem_pct")))
        ok_all &= check("过滤 tmpfs，保留 2 个挂载点", len(disks) == 2,
                        ",".join(d.get("mount", "") for d in disks))
        ok_all &= check("主机名解析", (metric.get("host_info") or {}).get("hostname") == "fake-host-01",
                        str((metric.get("host_info") or {}).get("hostname")))
        ok_all &= check("命中磁盘告警", any(f.get("kind") == "disk" for f in data.get("findings") or []),
                        str([f.get("kind") for f in data.get("findings") or []]))
        ok_all &= check("模型返回被解析", (data.get("analysis") or {}).get("severity") == "high",
                        json.dumps(data.get("analysis"), ensure_ascii=False)[:70])

        ok_all &= check("收到告警邮件", smtp.wait_for(1, 10), f"{len(smtp.messages)} 封")
        if smtp.messages:
            subject, html = parse_mail(smtp.messages[0])
            ok_all &= check("邮件主题正确", "[哨兵]" in subject and "磁盘" in subject, subject[:60])
            ok_all &= check("邮件含 AI 研判", "AI 研判" in html and "logrotate" in html)
            ok_all &= check("邮件含磁盘明细", "fake-host-01" in html and "/data" in html)

        ok_all &= check("模型被调用一次", len(llm.requests) == 1, str(len(llm.requests)))
        if llm.requests:
            req = llm.requests[0]
            ok_all &= check("Authorization 头正确", req["auth"] == "Bearer sk-test-key", req["auth"][:24])
            ok_all &= check("请求包含现场快照", "du -sh /var/log" in req["body"] or "java" in req["body"])

        print("\n指标恢复正常，应触发恢复通知…")
        ssh.cpu_pct = 20.0
        ssh.disk_pct = 40.0
        code, data = request(f"/api/servers/{server_id}/collect", "POST", {})
        ok_all &= check("本次无新告警", not (data.get("findings") or []),
                        str([f.get("kind") for f in data.get("findings") or []]))
        ok_all &= check("恢复被识别", bool(data.get("resolved")),
                        str([r.get("fingerprint") for r in data.get("resolved") or []]))
        ok_all &= check("收到恢复邮件", smtp.wait_for(2, 10), f"{len(smtp.messages)} 封")

        print("\n其它接口…")
        code, data = request(f"/api/servers/{server_id}?hours=1")
        ok_all &= check("详情接口", code == 200 and bool(data["series"]["points"]))
        code, data = request("/api/alerts?limit=50")
        ok_all &= check("告警列表", code == 200 and len(data["alerts"]) >= 2, str(len(data["alerts"])))
        code, data = request("/api/servers")
        ok_all &= check("列表不泄露密码", all("password" not in s for s in data["servers"]))
        code, data = request("/api/overview")
        ok_all &= check("总览接口", code == 200 and len(data["servers"]) == 1)

        code, data = request("/api/servers/" + str(server_id), "DELETE")
        ok_all &= check("删除服务器", code == 200)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        ssh.stop()
        smtp.stop()
        llm.stop()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n结果：{passed}/{len(results)} 通过")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
