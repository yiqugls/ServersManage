#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证“按天/按周汇总”模式：异常不即时发信，到点一封汇总。

    python tests/test_summary.py
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

PORT = 8096
TMP = os.path.join(BASE, "tests", "_tmp_summary")
URL = f"http://127.0.0.1:{PORT}"

passed = 0
failed = 0


def check(name, ok, extra=""):
    global passed, failed
    if ok:
        passed += 1
        print("  PASS  " + name)
    else:
        failed += 1
        print("  FAIL  " + name + (f"  [{extra}]" if extra else ""))
    return ok


def request(path, method="GET", body=None, timeout=40):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        URL + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8", "replace"))


def wait_health(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if request("/api/health", timeout=2)[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def parse_mail(record):
    from email import message_from_bytes
    from email.header import decode_header, make_header

    msg = message_from_bytes(record.data)
    subject = str(make_header(decode_header(msg.get("Subject", ""))))
    html = ""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html = (part.get_payload(decode=True) or b"").decode(
                part.get_content_charset() or "utf-8", "replace"
            )
            break
    return subject, html


def main():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    ssh = FakeSSH()
    ssh.start()
    smtp = SMTPSink()
    smtp.start()
    llm = FakeOpenAI(port=8765)
    llm.start()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "run.py"), "--port", str(PORT),
         "--data-dir", TMP, "--no-demo", "--log-level", "WARNING"],
        cwd=BASE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not check("服务启动", wait_health()):
            return 1

        code, data = request("/api/settings", "PUT", {
            "alerts": {"disk_pct": 85, "cooldown_minutes": 0},
            "email": {"enabled": True, "host": "127.0.0.1", "port": 2525, "security": "none",
                      "from_addr": "s@test.local", "to_addrs": ["ops@test.local"]},
            "llm": {"enabled": True, "provider": "custom", "base_url": "http://127.0.0.1:8765/v1",
                    "model": "test-model", "api_key": "sk-x", "mode": "on_alert"},
            # 关键：告警只进汇总，时间设在一小时后，避免测试期间自动触发
            "digest": {"enabled": True, "frequency": "daily",
                       "time": time.strftime("%H:%M", time.localtime(time.time() + 3600)),
                       "weekday": 1, "alerts_mode": "summary"},
        })
        check("保存汇总配置", code == 200 and data["settings"]["digest"]["alerts_mode"] == "summary",
              str(data.get("error", "")))

        code, data = request("/api/servers", "POST", {
            "name": "summary-web", "host": "127.0.0.1", "port": 2222,
            "username": "root", "password": "secret",
        })
        sid = data.get("id")
        check("创建服务器", code == 201 and bool(sid), str(data.get("error", "")))

        code, data = request(f"/api/servers/{sid}/collect", "POST", {})
        check("采集命中磁盘告警", any(f.get("kind") == "disk" for f in data.get("findings") or []),
              str([f.get("kind") for f in data.get("findings") or []]))

        time.sleep(1.2)
        check("汇总模式下不发即时邮件", len(smtp.messages) == 0, f"{len(smtp.messages)} 封")
        check("汇总模式下不逐条调用模型", len(llm.requests) == 0, str(len(llm.requests)))

        code, data = request("/api/alerts?limit=10")
        alert = (data.get("alerts") or [{}])[0]
        check("告警仍然被记录", bool(alert.get("title")), str(alert)[:60])
        check("记录里注明归入汇总", "汇总" in str(alert.get("notify_error", "")),
              str(alert.get("notify_error")))

        code, data = request("/api/digest/send", "POST", {})
        check("手动触发汇总", code == 200 and data.get("alerts") == 1, json.dumps(data, ensure_ascii=False)[:80])
        check("收到一封汇总邮件", smtp.wait_for(1, 10), f"{len(smtp.messages)} 封")

        if smtp.messages:
            subject, html = parse_mail(smtp.messages[0])
            check("主题是告警汇总", "告警汇总" in subject, subject[:60])
            check("邮件含未恢复告警", "未恢复" in html and "磁盘使用率过高" in html)
            check("邮件含服务器名", "summary-web" in html)
            check("邮件含体检表", "本期告警" in html and "CPU" in html)
            check("邮件含 AI 结论", "AI 巡检结论" in html and "整体可控" in html)

        check("汇总时调用了一次模型", len(llm.requests) == 1, str(len(llm.requests)))
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
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
