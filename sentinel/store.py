"""SQLite 存储层：服务器、指标、告警、运行状态。

所有写入都走同一把可重入锁，采集线程 / 调度线程 / HTTP 线程共用连接。
场景是“每分钟几次写”，不值得引入连接池。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from .secrets import SecretBox

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    host            TEXT    NOT NULL,
    port            INTEGER NOT NULL DEFAULT 22,
    username        TEXT    NOT NULL,
    password_enc    TEXT    NOT NULL DEFAULT '',
    kind            TEXT    NOT NULL DEFAULT 'ssh',
    enabled         INTEGER NOT NULL DEFAULT 1,
    interval_sec    INTEGER,
    note            TEXT    NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    last_attempt_at INTEGER,
    last_ok_at      INTEGER,
    last_error      TEXT    NOT NULL DEFAULT '',
    fail_streak     INTEGER NOT NULL DEFAULT 0,
    last_latency_ms INTEGER
);

CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id   INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    cpu_pct     REAL,
    cpu_cores   INTEGER,
    mem_pct     REAL,
    mem_used    INTEGER,
    mem_total   INTEGER,
    swap_pct    REAL,
    load1       REAL,
    load5       REAL,
    load15      REAL,
    uptime_sec  INTEGER,
    disks       TEXT,
    host_info   TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_server_ts ON metrics(server_id, ts);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id    INTEGER NOT NULL,
    server_name  TEXT    NOT NULL DEFAULT '',
    fingerprint  TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    severity     TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    detail       TEXT    NOT NULL DEFAULT '',
    value        REAL,
    ts           INTEGER NOT NULL,
    state        TEXT    NOT NULL DEFAULT 'firing',
    notified     INTEGER NOT NULL DEFAULT 0,
    notify_error TEXT    NOT NULL DEFAULT '',
    llm          TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_server ON alerts(server_id, ts);

CREATE TABLE IF NOT EXISTS state (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def now() -> int:
    return int(time.time())


class Store:
    def __init__(self, db_path: str, box: SecretBox):
        self.path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.box = box
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ 通用
    def _all(self, sql: str, args: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(args)).fetchall())

    def _one(self, sql: str, args: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            self._conn.commit()
            return cur.lastrowid

    # ------------------------------------------------------------------ 状态
    def get_state(self, key: str, default: str = "") -> str:
        row = self._one("SELECT v FROM state WHERE k = ?", (key,))
        return row["v"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO state(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_state(key, "")
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.set_state(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    # ------------------------------------------------------------------ 服务器
    @staticmethod
    def _server_public(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        encrypted = item.pop("password_enc", "") or ""
        item["enabled"] = bool(item.get("enabled", 1))
        item["has_password"] = bool(encrypted)
        return item

    def list_servers(self) -> List[Dict[str, Any]]:
        rows = self._all("SELECT * FROM servers ORDER BY id")
        return [self._server_public(r) for r in rows]

    def get_server(self, server_id: int) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM servers WHERE id = ?", (server_id,))
        return self._server_public(row) if row else None

    def server_credentials(self, server_id: int) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM servers WHERE id = ?", (server_id,))
        if not row:
            return None
        item = dict(row)
        item["password"] = self.box.decrypt(item.pop("password_enc", "") or "")
        item["enabled"] = bool(item.get("enabled", 1))
        return item

    def create_server(self, data: Dict[str, Any]) -> int:
        ts = now()
        return self._exec(
            "INSERT INTO servers(name, host, port, username, password_enc, kind, enabled,"
            " interval_sec, note, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(data.get("name") or data["host"]).strip(),
                str(data["host"]).strip(),
                int(data.get("port") or 22),
                str(data.get("username") or "root").strip(),
                self.box.encrypt(data.get("password", "")),
                str(data.get("kind") or "ssh"),
                1 if data.get("enabled", True) else 0,
                data.get("interval_sec") or None,
                str(data.get("note") or ""),
                ts,
                ts,
            ),
        )

    def update_server(self, server_id: int, data: Dict[str, Any]) -> bool:
        row = self._one("SELECT * FROM servers WHERE id = ?", (server_id,))
        if not row:
            return False
        fields: List[str] = []
        args: List[Any] = []
        for key in ("name", "host", "username", "note", "kind"):
            if key in data and data[key] is not None:
                fields.append(f"{key} = ?")
                args.append(str(data[key]).strip())
        if "port" in data and data["port"] is not None:
            fields.append("port = ?")
            args.append(int(data["port"]))
        if "enabled" in data and data["enabled"] is not None:
            fields.append("enabled = ?")
            args.append(1 if data["enabled"] else 0)
        if "interval_sec" in data:
            fields.append("interval_sec = ?")
            value = data["interval_sec"]
            fields_args = None if not value else max(10, int(value))
            args.append(fields_args)
        if data.get("password"):
            fields.append("password_enc = ?")
            args.append(self.box.encrypt(str(data["password"])))
        if not fields:
            return True
        fields.append("updated_at = ?")
        args.append(now())
        args.append(server_id)
        self._exec(f"UPDATE servers SET {', '.join(fields)} WHERE id = ?", args)
        return True

    def delete_server(self, server_id: int) -> None:
        self._exec("DELETE FROM servers WHERE id = ?", (server_id,))
        self._exec("DELETE FROM metrics WHERE server_id = ?", (server_id,))
        self._exec("DELETE FROM alerts WHERE server_id = ?", (server_id,))
        state = self.get_json("alert_state", {}) or {}
        prefix = f"{server_id}:"
        changed = False
        for key in [k for k in state if k.startswith(prefix)]:
            state.pop(key, None)
            changed = True
        if changed:
            self.set_json("alert_state", state)

    def touch_server(
        self,
        server_id: int,
        ok: bool,
        error: str = "",
        latency_ms: Optional[int] = None,
    ) -> int:
        """记录一次采集结果，返回新的连续失败次数。"""
        ts = now()
        with self._lock:
            row = self._one("SELECT fail_streak FROM servers WHERE id = ?", (server_id,))
            streak = (row["fail_streak"] if row else 0) or 0
            streak = 0 if ok else streak + 1
            if ok:
                self._exec(
                    "UPDATE servers SET last_attempt_at = ?, last_ok_at = ?, last_error = '',"
                    " fail_streak = 0, last_latency_ms = ? WHERE id = ?",
                    (ts, ts, latency_ms, server_id),
                )
            else:
                self._exec(
                    "UPDATE servers SET last_attempt_at = ?, last_error = ?, fail_streak = ?"
                    " WHERE id = ?",
                    (ts, error[:500], streak, server_id),
                )
            return streak

    # ------------------------------------------------------------------ 指标
    def insert_metric(self, server_id: int, m: Dict[str, Any]) -> int:
        return self._exec(
            "INSERT INTO metrics(server_id, ts, cpu_pct, cpu_cores, mem_pct, mem_used,"
            " mem_total, swap_pct, load1, load5, load15, uptime_sec, disks, host_info)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                server_id,
                int(m.get("ts") or now()),
                m.get("cpu_pct"),
                m.get("cpu_cores"),
                m.get("mem_pct"),
                m.get("mem_used"),
                m.get("mem_total"),
                m.get("swap_pct"),
                m.get("load1"),
                m.get("load5"),
                m.get("load15"),
                m.get("uptime_sec"),
                json.dumps(m.get("disks") or [], ensure_ascii=False),
                json.dumps(m.get("host_info") or {}, ensure_ascii=False),
            ),
        )

    def latest_metric(self, server_id: int) -> Optional[Dict[str, Any]]:
        row = self._one(
            "SELECT * FROM metrics WHERE server_id = ? ORDER BY ts DESC LIMIT 1", (server_id,)
        )
        return self._metric_row(row) if row else None

    def recent_metrics(self, server_id: int, limit: int = 30) -> List[Dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM metrics WHERE server_id = ? ORDER BY ts DESC LIMIT ?",
            (server_id, limit),
        )
        return [self._metric_row(r) for r in reversed(rows)]

    def metrics_since(self, server_id: int, since_ts: int, limit: int = 20000) -> List[Dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM metrics WHERE server_id = ? AND ts >= ? ORDER BY ts ASC LIMIT ?",
            (server_id, since_ts, limit),
        )
        return [self._metric_row(r) for r in rows]

    @staticmethod
    def _metric_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        try:
            item["disks"] = json.loads(item.get("disks") or "[]")
        except Exception:
            item["disks"] = []
        try:
            item["host_info"] = json.loads(item.get("host_info") or "{}")
        except Exception:
            item["host_info"] = {}
        return item

    def prune_metrics(self, retention_days: int) -> int:
        cutoff = now() - max(1, retention_days) * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff - 86400 * 30,))
            self._conn.commit()
            return cur.rowcount or 0

    # ------------------------------------------------------------------ 告警
    def insert_alert(self, alert: Dict[str, Any]) -> int:
        return self._exec(
            "INSERT INTO alerts(server_id, server_name, fingerprint, kind, severity, title,"
            " detail, value, ts, state, notified, notify_error, llm)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(alert["server_id"]),
                alert.get("server_name", ""),
                alert["fingerprint"],
                alert["kind"],
                alert["severity"],
                alert["title"],
                alert.get("detail", ""),
                alert.get("value"),
                int(alert.get("ts") or now()),
                alert.get("state", "firing"),
                1 if alert.get("notified") else 0,
                alert.get("notify_error", ""),
                alert.get("llm"),
            ),
        )

    def list_alerts(self, limit: int = 100, server_id: Optional[int] = None) -> List[Dict[str, Any]]:
        if server_id is None:
            rows = self._all("SELECT * FROM alerts ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        else:
            rows = self._all(
                "SELECT * FROM alerts WHERE server_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
                (server_id, limit),
            )
        out = []
        for row in rows:
            item = dict(row)
            if item.get("llm"):
                try:
                    item["llm"] = json.loads(item["llm"])
                except Exception:
                    pass
            out.append(item)
        return out

    def alerts_since(self, since_ts: int) -> List[Dict[str, Any]]:
        rows = self._all("SELECT * FROM alerts WHERE ts >= ? ORDER BY ts ASC", (since_ts,))
        return [dict(r) for r in rows]

    def count_emails_since(self, since_ts: int) -> int:
        row = self._one(
            "SELECT COUNT(*) AS c FROM alerts WHERE ts >= ? AND notified = 1", (since_ts,)
        )
        return int(row["c"]) if row else 0

    def mark_notified(self, alert_id: int, ok: bool, error: str = "") -> None:
        self._exec(
            "UPDATE alerts SET notified = ?, notify_error = ? WHERE id = ?",
            (1 if ok else 0, (error or "")[:500], alert_id),
        )

    def stats(self) -> Dict[str, int]:
        """用 MAX(id) 估算行数：指标表可能有上百万行，COUNT(*) 每次刷新都全扫太浪费。"""
        servers = self._one("SELECT COUNT(*) AS c FROM servers")
        metrics = self._one("SELECT MAX(id) AS m FROM metrics")
        alerts = self._one("SELECT MAX(id) AS m FROM alerts")
        return {
            "servers": int(servers["c"]) if servers else 0,
            "metrics": int(metrics["m"] or 0) if metrics else 0,
            "alerts": int(alerts["m"] or 0) if alerts else 0,
        }
