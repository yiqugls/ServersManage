"""后台调度：按每台服务器自己的周期采集，附带每日汇总与数据保留清理。

调度器只做三件事：算“谁该跑了”、丢进线程池、把最近一次结果缓存在内存里给
HTTP 层直接用（避免每次刷新页面都去扫一遍历史表）。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from . import collector

log = logging.getLogger("sentinel.scheduler")


class Scheduler(threading.Thread):
    def __init__(self, store, monitor, settings_getter):
        super().__init__(name="sentinel-scheduler", daemon=True)
        self.store = store
        self.monitor = monitor
        self.get_settings = settings_getter
        self._stop = threading.Event()
        self._next_run: Dict[int, float] = {}
        self._inflight: set = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="collect")
        self._pool_size = 8
        self._last_prune = 0.0
        self.started_at = int(time.time())
        self.last_tick = 0.0
        # server_id -> 最近一次采集结果，供前端快速读取
        self.runtime: Dict[int, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ 循环
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # 调度线程不能死
                log.exception("调度循环异常: %s", exc)
            self._stop.wait(1.0)

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False)

    def tick(self) -> None:
        settings = self.get_settings()
        now = time.time()
        self.last_tick = now
        self._resize_pool(int(settings["collect"]["parallel"]))

        servers = self.store.list_servers()
        alive = {int(s["id"]) for s in servers}
        for sid in list(self._next_run):
            if sid not in alive:
                self._next_run.pop(sid, None)
                self.runtime.pop(sid, None)

        for server in servers:
            sid = int(server["id"])
            if not server.get("enabled"):
                continue
            if sid in self._inflight:
                continue
            due = self._next_run.get(sid)
            if due is None:
                # 启动后错开几秒，避免所有服务器同时打 SSH
                self._next_run[sid] = now + random.uniform(1.0, 4.0)
                continue
            if now < due:
                continue
            interval = int(server.get("interval_sec") or settings["collect"]["interval_sec"])
            self._next_run[sid] = now + max(10, interval)
            self._submit(sid, server)

        self._maybe_report(settings, now)
        self._maybe_prune(settings, now)

    def _submit(self, sid: int, server: Dict[str, Any]) -> None:
        with self._lock:
            if sid in self._inflight:
                return
            self._inflight.add(sid)
        self._pool.submit(self._job, sid, server)

    def _resize_pool(self, size: int) -> None:
        size = max(1, min(32, size))
        if size == self._pool_size:
            return
        old = self._pool
        self._pool = ThreadPoolExecutor(max_workers=size, thread_name_prefix="collect")
        self._pool_size = size
        old.shutdown(wait=False)

    def _job(self, sid: int, server: Dict[str, Any]) -> None:
        try:
            result = self.run_collect(server)
            self.runtime[sid] = {
                "at": int(time.time()),
                "ok": result.get("ok", False),
                "error": result.get("error", ""),
                "metric": result.get("metric"),
                "predictions": result.get("predictions") or [],
                "findings": result.get("findings") or [],
                "alerts": len(result.get("alerts") or []),
            }
        except Exception as exc:
            log.exception("采集任务异常 %s: %s", server.get("name"), exc)
            self.runtime[sid] = {
                "at": int(time.time()),
                "ok": False,
                "error": f"{exc.__class__.__name__}: {exc}",
                "metric": None,
                "predictions": [],
                "findings": [],
                "alerts": 0,
            }
        finally:
            with self._lock:
                self._inflight.discard(sid)

    # ------------------------------------------------------------------ 采集
    def run_collect(self, server: Dict[str, Any]) -> Dict[str, Any]:
        settings = self.get_settings()
        sid = int(server["id"])
        # 调度器遍历的是 store.list_servers() 的脱敏记录：它有 has_password，但没有 password。
        # 不换回带凭据的记录就会用空密码连 SSH —— 表现就是“手动点立即采集有数据、
        # 自动采集一直认证失败”。手动接口传进来的已经带密码，不会重复查库。
        if "password" not in server:
            server = self.store.server_credentials(sid) or server
        try:
            metric = collector.collect(server, settings)
        except collector.CollectorError as exc:
            streak = self.store.touch_server(sid, False, str(exc))
            extra = self.monitor.on_failure(server, str(exc), streak)
            return {"ok": False, "error": str(exc), "streak": streak, **extra}
        except Exception as exc:
            message = f"{exc.__class__.__name__}: {exc}"
            streak = self.store.touch_server(sid, False, message)
            extra = self.monitor.on_failure(server, message, streak)
            return {"ok": False, "error": message, "streak": streak, **extra}

        self.store.touch_server(sid, True, latency_ms=metric.get("latency_ms"))
        result = self.monitor.on_success(server, metric)
        result["ok"] = True
        result["metric"] = metric
        return result

    # ------------------------------------------------------------------ 定时
    def _maybe_report(self, settings: Dict[str, Any], now: float) -> None:
        cfg = settings["digest"]
        if not cfg.get("enabled"):
            return
        local = time.localtime(now)
        if time.strftime("%H:%M", local) != cfg.get("time"):
            return
        if cfg.get("frequency") == "weekly" and (local.tm_wday + 1) != int(cfg.get("weekday") or 1):
            return
        today = time.strftime("%Y-%m-%d", local)
        if self.store.get_state("last_report", "") == today:
            return
        self.store.set_state("last_report", today)
        self._pool.submit(self._run_report)

    def _run_report(self) -> None:
        try:
            result = self.monitor.send_report()
            log.info("定期汇总已发送: %s", result)
        except Exception as exc:
            log.warning("定期汇总发送失败: %s", exc)

    def _maybe_prune(self, settings: Dict[str, Any], now: float) -> None:
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        try:
            removed = self.store.prune_metrics(int(settings["collect"]["retention_days"]))
            if removed:
                log.info("清理过期指标 %s 条", removed)
        except Exception as exc:
            log.warning("清理过期指标失败: %s", exc)

    # ------------------------------------------------------------------ 查询
    def runtime_of(self, server_id: int) -> Optional[Dict[str, Any]]:
        return self.runtime.get(int(server_id))

    def next_run_of(self, server_id: int) -> Optional[int]:
        due = self._next_run.get(int(server_id))
        return int(due) if due else None

    def is_running(self, server_id: int) -> bool:
        with self._lock:
            return int(server_id) in self._inflight
