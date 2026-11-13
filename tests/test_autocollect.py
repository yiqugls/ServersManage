#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自动采集回归测试：不点「立即采集」，只靠调度线程也要拿到指标。

背景：调度器遍历的是 store.list_servers() 的脱敏记录（没有密码），
如果直接拿它去采集，就会用空密码连 SSH —— 表现为“只有手动采集才生效”。
这个用例专门盯这条路径，避免以后改存储层又把它带回来。

    python tests/test_autocollect.py
    SENTINEL_TEST_DIR=D:\\tmp\\auto python tests/test_autocollect.py   # 换数据目录
"""
from __future__ import annotations

import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakeSSH  # noqa: E402

from sentinel.app import SentinelApp  # noqa: E402

PORT = 2298
DATA_DIR = os.environ.get("SENTINEL_TEST_DIR") or os.path.join(BASE, "tests", "_tmp_auto")
WAIT_SEC = 25.0

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS  " + name)
    else:
        failed += 1
        print("  FAIL  " + name + (f"  [{extra}]" if extra else ""))


def wait_for(fn, timeout=WAIT_SEC, interval=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return fn()


def main() -> int:
    ssh = FakeSSH(port=PORT)
    ssh.start()
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    app = SentinelApp(DATA_DIR, seed_demo=False)
    try:
        # 周期调到最小值，测试才跑得快；关掉告警避免发信
        app.update_settings({"collect": {"interval_sec": 10}, "alerts": {"enabled": False}})
        server_id = app.store.create_server(
            {
                "name": "auto-collect",
                "host": "127.0.0.1",
                "port": PORT,
                "username": "root",
                "password": "secret",
                "enabled": True,
            }
        )
        app.start()
        print("已启动调度器，等待自动采集（不调用任何手动接口）…")

        metric = wait_for(lambda: app.store.latest_metric(server_id))
        check("自动采集写出了指标", metric is not None)
        check(
            "CPU 来自假 SSH（≈95%）",
            metric is not None and abs((metric.get("cpu_pct") or 0) - 95) <= 1.5,
            (metric or {}).get("cpu_pct"),
        )

        server = app.store.get_server(server_id) or {}
        check("没有连续失败", (server.get("fail_streak") or 0) == 0, server.get("fail_streak"))
        check("last_error 为空", not (server.get("last_error") or ""), server.get("last_error"))
        check("last_ok_at 已写入", bool(server.get("last_ok_at")))

        runtime = app.scheduler.runtime_of(server_id) or {}
        check("调度器缓存标记成功", runtime.get("ok") is True, runtime.get("error"))
        check("给下一次采集排了期", bool(app.scheduler.next_run_of(server_id)))

        # 再等一个周期，确认是“持续自动采”，而不是只跑了一次
        rows = wait_for(
            lambda: (
                app.store.recent_metrics(server_id, limit=5)
                if len(app.store.recent_metrics(server_id, limit=5)) >= 2
                else None
            ),
            timeout=20.0,
        )
        check("至少自动采集了 2 次", len(rows or []) >= 2, len(rows or []))
    finally:
        app.stop()
        try:
            app.store.close()
        except Exception:
            pass
        ssh.stop()
        shutil.rmtree(DATA_DIR, ignore_errors=True)

    print(f"\n结果：{passed}/{passed + failed} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
