#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给演示服务器灌一段合成的历史指标，方便一上手就能看到图表和趋势预测。

只写 kind='demo' 的服务器，真实服务器会被跳过。默认补 8 小时、每分钟一个点，
以当前最新一条指标为基准往前推，保证和实时采集的数据能接上。

    python tools/seed_demo.py --hours 8
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sentinel import config  # noqa: E402
from sentinel.secrets import SecretBox  # noqa: E402
from sentinel.store import Store  # noqa: E402

GB = 1024 ** 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.path.join(BASE, "data"))
    parser.add_argument("--server-id", type=int, default=0, help="默认第一个 demo 服务器")
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--interval", type=int, default=60, help="点间隔（秒）")
    args = parser.parse_args()

    box = SecretBox(os.path.join(args.data_dir, "secret.key"))
    store = Store(os.path.join(args.data_dir, "sentinel.db"), box)

    servers = [s for s in store.list_servers() if s.get("kind") == "demo"]
    if args.server_id:
        servers = [s for s in servers if s["id"] == args.server_id]
    if not servers:
        print("没有找到演示服务器（kind=demo），先在页面上添加一台或调用 POST /api/demo")
        return 1

    latest = store.latest_metric(servers[0]["id"])
    if not latest:
        print("演示服务器还没有采到数据，等几秒再试")
        return 1

    now = int(latest["ts"])
    count = int(args.hours * 3600 / args.interval)
    base_disks = {d["mount"]: d for d in latest["disks"]}
    root_rate = 14.4 / 3600.0      # GB/秒
    data_rate = 5.4 / 3600.0

    rows = []
    for i in range(count, 0, -1):
        ts = now - i * args.interval
        drift = i * args.interval
        disks = []
        for mount, disk in base_disks.items():
            rate = root_rate if mount == "/" else (data_rate if mount == "/data" else 0.0)
            used = max(0.02 * disk["total"], disk["used"] - rate * drift * GB)
            disks.append(
                {
                    **disk,
                    "used": int(used),
                    "avail": int(disk["total"] - used),
                    "pct": round(used / disk["total"] * 100, 2),
                }
            )
        phase = (ts % 900) / 900.0
        spike = 55 if 0.66 <= phase < 0.93 else 0
        cpu = max(2.0, min(99.0, 27 + 7 * math.sin(ts / 700) + spike + random.uniform(-5, 5)))
        mem = max(20.0, min(96.0, 58 + 9 * math.sin(ts / 1100) + random.uniform(-3, 3)))
        rows.append(
            (
                servers[0]["id"], ts, round(cpu, 2), 8, round(mem, 2),
                int(16 * GB * mem / 100), 16 * GB, 3.0,
                round(cpu / 100 * 8 * .9, 2), round(cpu / 100 * 8 * .85, 2), round(cpu / 100 * 8 * .8, 2),
                int(86400 * 21 + (ts - now)), json.dumps(disks, ensure_ascii=False),
                json.dumps(latest.get("host_info") or {}, ensure_ascii=False),
            )
        )

    with store._lock:  # noqa: SLF001 - 演示脚本，直接用内部连接更省事
        store._conn.executemany(  # noqa: SLF001
            "INSERT INTO metrics(server_id, ts, cpu_pct, cpu_cores, mem_pct, mem_used, mem_total,"
            " swap_pct, load1, load5, load15, uptime_sec, disks, host_info)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        store._conn.commit()

    print(f"已为 {servers[0]['name']} 写入 {len(rows)} 个历史点（{args.hours} 小时，每 {args.interval}s）")
    settings = config.normalize(store.get_json("settings", {}) or {})
    from sentinel.predictor import build_predictions

    history = store.metrics_since(servers[0]["id"], time.time() - 12 * 3600)
    for pred in build_predictions(history, settings):
        print(
            "  {mount}: 当前 {used_pct}% 增长 {slope_gb_day} GB/天 预计 {eta_hours} 小时写满 "
            "（置信度 {confidence}）".format(**pred)
        )
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
