"""应用装配：配置读写、密钥解密、演示数据播种。"""
from __future__ import annotations

import copy
import os
import threading
from typing import Any, Dict, List, Optional

from . import config
from .analyzer import Monitor
from .scheduler import Scheduler
from .secrets import SecretBox
from .store import Store


class SentinelApp:
    def __init__(self, data_dir: str, seed_demo: bool = True):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.box = SecretBox(os.path.join(self.data_dir, "secret.key"))
        self.store = Store(os.path.join(self.data_dir, "sentinel.db"), self.box)
        self._lock = threading.RLock()
        self._settings = self._load_settings()
        self.monitor = Monitor(self.store, self.get_settings)
        self.scheduler = Scheduler(self.store, self.monitor, self.get_settings)
        if seed_demo and not self.store.list_servers():
            self.seed_demo()

    # ------------------------------------------------------------------ 配置
    def _load_settings(self) -> Dict[str, Any]:
        raw = self.store.get_json("settings", {}) or {}
        for section, field in config.SECRET_PATHS:
            node = raw.get(section)
            if isinstance(node, dict) and node.get(field):
                node[field] = self.box.decrypt(node[field])
        return config.normalize(raw)

    def _persist_settings(self, settings: Dict[str, Any]) -> None:
        payload = copy.deepcopy(settings)
        for section, field in config.SECRET_PATHS:
            node = payload.get(section)
            if isinstance(node, dict):
                node[field] = self.box.encrypt(node.get(field) or "") if node.get(field) else ""
        self.store.set_json("settings", payload)

    def get_settings(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._settings)

    def update_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            patch = config.sanitize(payload or {})
            # 密钥字段留空 = 保持原值（前端不回显明文）
            for section, field in config.SECRET_PATHS:
                node = patch.get(section)
                if isinstance(node, dict) and field in node and not str(node.get(field) or "").strip():
                    node.pop(field, None)
            merged = config.deep_merge(self._settings, patch)
            self._settings = config.normalize(merged)
            self._persist_settings(self._settings)
            return config.mask_secrets(self._settings)

    def public_settings(self) -> Dict[str, Any]:
        return config.mask_secrets(self.get_settings())

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.stop()

    # ------------------------------------------------------------------ 演示
    def seed_demo(self) -> int:
        return self.store.create_server(
            {
                "name": "演示服务器",
                "host": "127.0.0.1",
                "port": 22,
                "username": "demo",
                "password": "",
                "kind": "demo",
                "note": "合成数据，用来展示图表和告警；可直接删除",
                "enabled": True,
            }
        )

    # ------------------------------------------------------------------ 视图
    def overview(self) -> List[Dict[str, Any]]:
        servers = self.store.list_servers()
        active = self.store.get_json("alert_state", {}) or {}
        out: List[Dict[str, Any]] = []
        for server in servers:
            sid = int(server["id"])
            runtime = self.scheduler.runtime_of(sid) or {}
            latest = runtime.get("metric") or self.store.latest_metric(sid)
            prefix = f"{sid}:"
            open_alerts = sum(1 for key in active if key.startswith(prefix))
            out.append(
                {
                    "server": server,
                    "latest": latest,
                    "predictions": runtime.get("predictions") or [],
                    "findings": runtime.get("findings") or [],
                    "ok": runtime.get("ok"),
                    "error": runtime.get("error", "") or server.get("last_error", ""),
                    "collected_at": runtime.get("at") or server.get("last_ok_at"),
                    "open_alerts": open_alerts,
                    "next_run": self.scheduler.next_run_of(sid),
                    "running": self.scheduler.is_running(sid),
                }
            )
        return out

    def server_detail(self, server_id: int, hours: float = 6.0) -> Optional[Dict[str, Any]]:
        server = self.store.get_server(server_id)
        if not server:
            return None
        runtime = self.scheduler.runtime_of(server_id) or {}
        latest = runtime.get("metric") or self.store.latest_metric(server_id)
        predictions = runtime.get("predictions")
        if predictions is None:
            history = self.store.metrics_since(server_id, self._now() - 12 * 3600, limit=4000)
            from .predictor import build_predictions

            predictions = build_predictions(history, self.get_settings(), now_ts=self._now())
        return {
            "server": server,
            "latest": latest,
            "predictions": predictions,
            "findings": runtime.get("findings") or [],
            "series": self.series(server_id, hours),
            "next_run": self.scheduler.next_run_of(server_id),
            "running": self.scheduler.is_running(server_id),
        }

    def series(self, server_id: int, hours: float = 6.0) -> Dict[str, Any]:
        now = self._now()
        since = now - int(max(0.2, hours) * 3600)
        rows = self.store.metrics_since(server_id, since, limit=30000)
        if not rows:
            return {"points": [], "start": since, "end": now, "step": 60, "disks": []}
        span = max(60, rows[-1]["ts"] - rows[0]["ts"])
        step = max(1, int(span / 360))
        cpu: Dict[int, List[float]] = {}
        mem: Dict[int, List[float]] = {}
        disks: Dict[str, Dict[int, float]] = {}
        for row in rows:
            bucket = int(row["ts"] // step)
            if row.get("cpu_pct") is not None:
                cpu.setdefault(bucket, []).append(float(row["cpu_pct"]))
            if row.get("mem_pct") is not None:
                mem.setdefault(bucket, []).append(float(row["mem_pct"]))
            for disk in row.get("disks") or []:
                mount = disk.get("mount")
                if mount and disk.get("pct") is not None:
                    disks.setdefault(mount, {})[bucket] = float(disk["pct"])
        points = [
            {
                "t": bucket * step,
                "cpu": round(sum(cpu.get(bucket, [0])) / max(1, len(cpu.get(bucket, []))), 2)
                if cpu.get(bucket)
                else None,
                "mem": round(sum(mem.get(bucket, [0])) / max(1, len(mem.get(bucket, []))), 2)
                if mem.get(bucket)
                else None,
            }
            for bucket in sorted(set(cpu) | set(mem))
        ]
        disk_series = [
            {
                "mount": mount,
                "points": [{"t": b * step, "v": round(v, 2)} for b, v in sorted(values.items())],
            }
            for mount, values in sorted(disks.items())
        ]
        return {
            "points": points,
            "disks": disk_series,
            "start": since,
            "end": now,
            "step": step,
        }

    @staticmethod
    def _now() -> int:
        import time

        return int(time.time())
