#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证“重启后配置仍在、密钥还能解密、模型仍可用”。

    python tests/test_restart.py
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

from fakes import FakeOpenAI  # noqa: E402

PORT = 8097
TMP = os.path.join(BASE, "tests", "_tmp_restart")
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


def request(path, method="GET", body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        URL + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8", "replace"))


def start(env):
    return subprocess.Popen(
        [sys.executable, os.path.join(BASE, "run.py"), "--port", str(PORT),
         "--data-dir", TMP, "--no-demo", "--log-level", "WARNING"],
        cwd=BASE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_health(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if request("/api/health")[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def main():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    llm = FakeOpenAI(port=8766)
    llm.start()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = start(env)
    try:
        if not wait_health():
            print("服务没起来")
            return 1
        code, _ = request("/api/settings", "PUT", {
            "email": {"enabled": True, "host": "127.0.0.1", "port": 2525,
                      "to_addrs": ["a@b.c"], "from_addr": "x@y.z"},
            "llm": {"enabled": True, "provider": "custom", "base_url": "http://127.0.0.1:8766/v1",
                    "model": "test-model", "api_key": "sk-persist", "mode": "on_alert"},
        })
        check("首次保存配置", code == 200)

        proc.terminate()
        proc.wait(timeout=10)
        time.sleep(0.6)

        proc = start(env)
        if not wait_health():
            print("重启后服务没起来")
            return 1

        code, data = request("/api/settings")
        settings = data["settings"]
        check("重启后配置仍在", settings["email"]["host"] == "127.0.0.1"
              and settings["llm"]["model"] == "test-model")
        check("密钥已解密（标记为已配置）", settings["llm"]["api_key_set"] is True)
        check("接口不返回明文密钥", settings["llm"]["api_key"] == "")

        code, data = request("/api/settings/test-llm", "POST", {})
        check("重启后仍能调用模型", code == 200 and data.get("ok") is True,
              str(data.get("error", ""))[:60])
        check("确实带上了原 API Key",
              bool(llm.requests) and llm.requests[-1]["auth"] == "Bearer sk-persist",
              llm.requests[-1]["auth"] if llm.requests else "no request")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        llm.stop()
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
