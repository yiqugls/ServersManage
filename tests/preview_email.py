#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成三封示例邮件的 HTML，用来快速检查排版（不需要跑服务）。

    python tests/preview_email.py
    # 输出到 tests/shots/mail-*.html
"""
from __future__ import annotations

import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sentinel import config, notifier  # noqa: E402
from sentinel.predictor import GB  # noqa: E402

OUT = os.path.join(BASE, "tests", "shots")
os.makedirs(OUT, exist_ok=True)

settings = config.normalize({})
server = {"name": "prod-web-01", "host": "10.0.0.21", "port": 22, "note": "订单服务"}
now = int(time.time())
metric = {
    "ts": now,
    "cpu_pct": 93.4,
    "mem_pct": 71.2,
    "load1": 7.42,
    "disks": [
        {"mount": "/", "fs": "ext4", "total": 100 * GB, "used": 62 * GB, "avail": 38 * GB, "pct": 62.0, "inode_pct": 8.0},
        {"mount": "/data", "fs": "xfs", "total": 1000 * GB, "used": 917 * GB, "avail": 83 * GB, "pct": 91.7, "inode_pct": 4.0},
    ],
    "host_info": {"hostname": "prod-web-01", "kernel": "Linux 5.15.0-122-generic"},
}
findings = [
    {
        "kind": "disk",
        "severity": "critical",
        "title": "磁盘使用率过高（/data）",
        "detail": "已用 91.7%，917.0 GB / 1000.0 GB，剩余 83.0 GB",
        "value": 91.7,
    },
    {
        "kind": "disk_forecast",
        "severity": "high",
        "title": "磁盘预计将被写满（/data）",
        "detail": "按最近 6.0 小时的趋势，每天增长 12.40 GB，预计 6.7 小时后达到 90% 阈值（当前 91.7%，置信度 高）",
        "value": 6.7,
    },
]
predictions = [
    {"mount": "/data", "status": "filling", "eta_hours": 6.7, "slope_gb_day": 12.4, "used_pct": 91.7, "confidence": "high", "samples": 360},
    {"mount": "/", "status": "stable", "eta_hours": None, "slope_gb_day": 0.02, "used_pct": 62.0, "confidence": "medium", "samples": 360},
]
analysis = {
    "severity": "critical",
    "summary": "/data 写入速度异常，约 6.7 小时后会写满",
    "likely_cause": "应用日志未做轮转，单个日志文件持续追加",
    "actions": ["du -sh /data/* | sort -h | tail -n 5", "配置 logrotate 并压缩 7 天前的日志", "临时扩容或清理旧备份"],
    "need_alert": True,
    "confidence": 0.86,
}
diagnosis = """# CPU 占用 Top
%CPU %MEM   RSS COMMAND
88.0 12.4 204800 java
 3.2  1.1  18432 nginx

# 磁盘现状
Filesystem      Size  Used Avail Use% Mounted on
/dev/sdb1      1000G  917G   83G  92% /data"""

mail = notifier.build_alert_email(settings, server, metric, findings, predictions, analysis, diagnosis)
with open(os.path.join(OUT, "mail-alert.html"), "w", encoding="utf-8") as fh:
    fh.write(mail["html"])
print("alert:", mail["subject"])

recovery = notifier.build_recovery_email(
    server, metric, [{"fingerprint": "disk:/data", "title": "磁盘使用率过高（/data）"}]
)
with open(os.path.join(OUT, "mail-recovery.html"), "w", encoding="utf-8") as fh:
    fh.write(recovery["html"])
print("recovery:", recovery["subject"])

rows = [
    {"server": server, "latest": metric,
     "worst_disk": {"mount": "/data", "pct": 91.7, "eta_hours": 6.7}, "alert_count": 2},
    {"server": {"name": "prod-db-01", "host": "10.0.0.31"},
     "latest": {"cpu_pct": 22.1, "mem_pct": 48.3},
     "worst_disk": {"mount": "/", "pct": 55.0, "eta_hours": None}, "alert_count": 0},
]
digest = notifier.build_digest_email(
    rows,
    {"health": "warn", "summary": "整体可控，prod-web-01 的 /data 需要今天处理",
     "highlights": ["prod-web-01 /data 预计 6.7 小时写满", "其余服务器指标正常"]},
    time.strftime("%Y-%m-%d"),
    alerts=[
        {"server_name": "prod-web-01", "title": "磁盘使用率过高（/data）",
         "detail": "已用 91.7%，917.0 GB / 1000.0 GB，剩余 83.0 GB",
         "active": True, "occurrences": 3, "ts": now},
        {"server_name": "prod-web-01", "title": "磁盘预计将被写满（/data）",
         "detail": "按最近 6.0 小时的趋势，每天增长 12.40 GB",
         "active": True, "occurrences": 1, "ts": now},
        {"server_name": "prod-db-01", "title": "CPU 持续高负载",
         "detail": "连续 3 个周期 CPU 超过 90%，当前 93.2%",
         "active": False, "occurrences": 2, "ts": now - 3600},
    ],
    frequency="daily",
    summary_only=True,
)
with open(os.path.join(OUT, "mail-digest.html"), "w", encoding="utf-8") as fh:
    fh.write(digest["html"])
print("digest:", digest["subject"])
print("输出目录:", OUT)
