#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""不依赖网络的单元测试：解析、预测、配置、加密、模型输出提取、邮件模板。

    python tests/test_units.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import collect_output  # noqa: E402

from sentinel import collector, config, notifier, predictor  # noqa: E402
from sentinel.llm import LLMClient, extract_json, extract_message, normalize_analysis  # noqa: E402
from sentinel.secrets import SecretBox  # noqa: E402

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


def test_collector():
    print("\n[采集解析]")
    m = collector.parse_output(collect_output(cpu_pct=95, disk_pct=90))
    check("CPU 使用率 95%", abs(m["cpu_pct"] - 95) <= 1, m["cpu_pct"])
    check("内存 64%", abs(m["mem_pct"] - 64) <= 1, m["mem_pct"])
    check("核数 8", m["cpu_cores"] == 8, m["cpu_cores"])
    check("负载解析", m["load1"] == 1.2, m["load1"])
    check("运行时长", m["uptime_sec"] == 1814400, m["uptime_sec"])
    check("过滤 tmpfs", [d["mount"] for d in m["disks"]] == ["/", "/data"], m["disks"])
    check("inode 合并", m["disks"][0]["inode_pct"] == 5.0, m["disks"][0].get("inode_pct"))
    check("主机信息", m["host_info"]["hostname"] == "fake-host-01")

    # busybox 风格：df 没有 -T，列数少一列
    raw = """@@@STAT1
cpu  1000 0 1000 8000 0 0 0 0
@@@MEM
MemTotal: 1000000 kB
MemFree: 100000 kB
@@@DF
/dev/sda1 1000000 900000 100000 90% /
@@@DFI
/dev/sda1 65536 32768 32768 50% /
@@@HOST
Linux 4.19.0
box
@@@STAT2
cpu  1100 0 1100 8800 0 0 0 0
@@@END
"""
    m2 = collector.parse_output(raw)
    check("无 -T 的 df 也能解析", len(m2["disks"]) == 1 and m2["disks"][0]["pct"] == 90.0, m2["disks"])
    check("无 MemAvailable 时回退计算", m2["mem_pct"] == 90.0, m2["mem_pct"])

    try:
        collector.parse_output("@@@STAT1\nnot linux\n@@@END\n")
        check("非 Linux 内容应报错", False)
    except collector.CollectorError:
        check("非 Linux 内容应报错", True)


def test_predictor():
    print("\n[容量预测]")
    now = int(time.time())
    total = 100 * 1024 ** 3
    # 每天 +10GB
    points = [(now - i * 3600, int((60 + (10 / 24) * (24 - i)) * 1024 ** 3)) for i in range(0, 25)]
    points.sort()
    res = predictor.forecast(points, total, 90.0, 0.2, now_ts=now)
    check("识别为增长中", res["status"] == "filling", res)
    check("斜率约 10 GB/天", abs(res["slope_gb_day"] - 10) < 0.3, res["slope_gb_day"])
    check("预计时间约 48 小时", abs(res["eta_hours"] - 48) < 2, res["eta_hours"])
    check("高置信度", res["confidence"] == "high", res["confidence"])

    flat = [(now - i * 3600, 70 * 1024 ** 3) for i in range(12, -1, -1)]
    check("平稳盘不误报", predictor.forecast(flat, total, 90.0, 0.2)["status"] == "stable")

    full = [(now - i * 3600, 95 * 1024 ** 3) for i in range(6, -1, -1)]
    check("已超阈值标记 full", predictor.forecast(full, total, 90.0, 0.2)["status"] == "full")

    check("样本不足标记 unknown", predictor.forecast([(now, 1)], total, 90.0, 0.2)["status"] == "unknown")

    # 增长很慢时不预测（0.24 GB/天 < 5 GB/天）
    slow = [(now - i * 3600, int((50 + (24 - i) * 0.01) * 1024 ** 3)) for i in range(24, -1, -1)]
    check("低于最小斜率不预测", predictor.forecast(slow, total, 90.0, 5.0)["status"] == "slow")


def test_rules():
    print("\n[告警规则]")

    class FakeStore:
        def __init__(self):
            self.rows = []

        def recent_metrics(self, sid, limit=60):
            return self.rows[-limit:]

        def list_servers(self):
            return [{"id": 1}]

        def get_json(self, key, default=None):
            return default

        def set_json(self, key, value):
            pass

    from sentinel.analyzer import Monitor

    store = FakeStore()
    settings = config.normalize({"alerts": {"cpu_pct": 80, "cpu_consecutive": 3, "disk_pct": 85}})
    monitor = Monitor(store, lambda: settings)
    server = {"id": 1, "name": "t", "kind": "ssh"}
    used = 90 * 1024 ** 3
    metric = {
        "ts": int(time.time()), "cpu_pct": 95.0, "mem_pct": 50.0,
        "disks": [{"mount": "/", "total": 100 * 1024 ** 3, "used": used, "avail": 10 * 1024 ** 3,
                   "pct": 90.0, "inode_pct": 3.0}],
    }
    store.rows = [metric, metric]
    findings = monitor._evaluate(server, metric, [], settings)
    kinds = [f["kind"] for f in findings]
    check("CPU 连续 3 次才报（当前 2 次）", "cpu" not in kinds, kinds)
    store.rows = [metric, metric, metric]
    findings = monitor._evaluate(server, metric, [], settings)
    kinds = [f["kind"] for f in findings]
    check("CPU 连续 3 次告警", "cpu" in kinds, kinds)
    check("磁盘超阈值告警", "disk" in kinds, kinds)

    preds = [{"mount": "/", "status": "full", "eta_hours": 0.0, "used_pct": 90.0, "confidence": "high"},
             {"mount": "/data", "status": "filling", "eta_hours": 5.0, "used_pct": 60.0,
              "slope_gb_day": 9.0, "confidence": "high", "span_hours": 6}]
    metric2 = dict(metric, disks=[metric["disks"][0], {"mount": "/data", "total": 100 * 1024 ** 3,
                                                       "used": 60 * 1024 ** 3, "avail": 40 * 1024 ** 3,
                                                       "pct": 60.0, "inode_pct": 2.0}])
    findings = monitor._evaluate(server, metric2, preds, settings)
    fps = [f["fingerprint"] for f in findings]
    check("超阈值的盘不再报预测", "disk_forecast:/" not in fps, fps)
    check("未满的盘仍报预测", "disk_forecast:/data" in fps, fps)


def test_config():
    print("\n[配置]")
    s = config.normalize({"collect": {"interval_sec": 1, "parallel": 999},
                          "alerts": {"cpu_pct": 200, "cooldown_minutes": -5},
                          "llm": {"provider": "nope", "mode": "weird"}})
    check("间隔下限 10s", s["collect"]["interval_sec"] == 10, s["collect"]["interval_sec"])
    check("并发上限 32", s["collect"]["parallel"] == 32, s["collect"]["parallel"])
    check("阈值上限 100", s["alerts"]["cpu_pct"] == 100.0, s["alerts"]["cpu_pct"])
    check("冷却不能为负", s["alerts"]["cooldown_minutes"] == 0)
    check("未知 provider 回退 custom", s["llm"]["provider"] == "custom")
    check("未知 mode 回退 on_alert", s["llm"]["mode"] == "on_alert")
    masked = config.mask_secrets(config.normalize({"llm": {"api_key": "sk-abc"}}))
    check("打码后不含量明文", masked["llm"]["api_key"] == "" and masked["llm"]["api_key_set"] is True)

    d = config.normalize({"digest": {"frequency": "weekly", "weekday": 9,
                                     "alerts_mode": "summary", "enabled": False}})["digest"]
    check("周报星期夹到 7", d["weekday"] == 7, d["weekday"])
    check("只进汇总时自动开启", d["enabled"] is True)
    check("周报频率保留", d["frequency"] == "weekly")
    d2 = config.normalize({"digest": {"frequency": "monthly", "alerts_mode": "x"}})["digest"]
    check("未知频率回退 daily", d2["frequency"] == "daily", d2["frequency"])
    check("未知投递方式回退 immediate", d2["alerts_mode"] == "immediate", d2["alerts_mode"])


def test_secrets():
    print("\n[加密]")
    with tempfile.TemporaryDirectory() as tmp:
        box = SecretBox(os.path.join(tmp, "k.key"))
        token = box.encrypt("p@ssw0rd-中文")
        check("密文不含明文", "p@ssw0rd" not in token)
        check("可解密", box.decrypt(token) == "p@ssw0rd-中文")
        check("空值安全", box.encrypt("") == "" and box.decrypt("") == "")
        box2 = SecretBox(os.path.join(tmp, "k.key"))
        check("重启后仍可解密", box2.decrypt(token) == "p@ssw0rd-中文")
        other = SecretBox(os.path.join(tmp, "other.key"))
        check("换 key 解不出来", other.decrypt(token) == "")
        # HMAC 分支
        import sentinel.secrets as sec

        saved = sec._HAS_FERNET
        try:
            sec._HAS_FERNET = False
            box3 = SecretBox(os.path.join(tmp, "h.key"))
            t3 = box3.encrypt("fallback-测试")
            check("HMAC 分支可用", t3.startswith("h1:") and box3.decrypt(t3) == "fallback-测试")
            check("HMAC 篡改能发现", box3.decrypt(t3[:-4] + "AAAA") != "fallback-测试")
        finally:
            sec._HAS_FERNET = saved


def test_llm_parse():
    print("\n[模型输出解析]")
    check("纯 JSON", extract_json('{"a":1}')["a"] == 1)
    check("带围栏", extract_json('```json\n{"a":2}\n```')["a"] == 2)
    check("带废话", extract_json('好的，结果如下：{"a":3} 以上')["a"] == 3)
    check("坏 JSON 返回 None", extract_json("not json") is None)
    norm = normalize_analysis({"severity": "HUGE", "actions": ["a", "b", "c", "d", "e"], "confidence": 3})
    check("未知严重级别归一", norm["severity"] == "warning", norm["severity"])
    check("动作最多 4 条", len(norm["actions"]) == 4)
    check("置信度夹逼", norm["confidence"] == 1.0)

    text, finish, usage = extract_message(
        {"choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
         "usage": {"completion_tokens": 3}}
    )
    check("普通字符串正文", text == "你好" and finish == "stop" and usage["completion_tokens"] == 3)

    text, _, _ = extract_message(
        {"choices": [{"message": {"content": [{"type": "text", "text": "分段"},
                                             {"type": "text", "text": "正文"}]}}]}
    )
    check("content 为分段数组", text == "分段正文", text)

    text, _, _ = extract_message(
        {"choices": [{"message": {"content": "", "reasoning_content": "只在推理里"},
                      "finish_reason": "stop"}]}
    )
    check("空正文回退 reasoning", text == "只在推理里", text)

    text, finish, _ = extract_message(
        {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}
    )
    check("空正文保留 finish_reason", text == "" and finish == "length")

    try:
        extract_message({"choices": []})
        check("没有 choices 要报错", False)
    except Exception:
        check("没有 choices 要报错", True)

    # 空内容 + length 时自动加大预算重试
    client = LLMClient({"model": "m", "base_url": "http://127.0.0.1:1/v1", "max_tokens": 100})
    calls = []

    def fake_post(system, user, budget):
        calls.append(budget)
        if len(calls) == 1:
            return "", "length", {"completion_tokens": 100}
        return "ok", "stop", {}

    client._post = fake_post
    check("空内容自动加大预算重试", client.chat("s", "u") == "ok" and calls == [100, 800], calls)

    client2 = LLMClient({"model": "m", "base_url": "http://127.0.0.1:1/v1", "max_tokens": 100})
    client2._post = lambda s, u, b: ("", "length", {"completion_tokens": b})
    try:
        client2.chat("s", "u")
        check("仍然为空时给出可操作提示", False)
    except Exception as exc:
        msg = str(exc)
        check("仍然为空时给出可操作提示", "1500" in msg and "思考" in msg, msg[:80])


def test_email():
    print("\n[邮件模板]")
    settings = config.normalize({"email": {"subject_prefix": "[测试]"}})
    server = {"name": "web-1", "host": "1.2.3.4", "port": 22}
    metric = {"ts": int(time.time()), "cpu_pct": 99.0, "mem_pct": 88.0, "load1": 9.9,
              "disks": [{"mount": "/", "total": 100 * 1024 ** 3, "used": 95 * 1024 ** 3,
                         "avail": 5 * 1024 ** 3, "pct": 95.0}],
              "host_info": {"hostname": "web-1", "kernel": "Linux"}}
    findings = [{"kind": "disk", "severity": "critical", "title": "磁盘使用率过高（/）",
                 "detail": "已用 95%", "fingerprint": "disk:/"}]
    mail = notifier.build_alert_email(settings, server, metric, findings, [], None, "")
    check("主题带前缀", mail["subject"].startswith("[测试][严重]"), mail["subject"])
    check("HTML 含主机名", "web-1" in mail["html"])
    check("文本版可用", "磁盘使用率过高" in mail["text"])
    rec = notifier.build_recovery_email(server, metric, [{"fingerprint": "disk:/", "title": "磁盘使用率过高（/）"}])
    check("恢复邮件主题", "恢复" in rec["subject"], rec["subject"])

    digest = notifier.build_digest_email(
        [{"server": server, "latest": metric,
          "worst_disk": {"mount": "/", "pct": 95.0, "eta_hours": 3}, "alert_count": 2}],
        {"health": "warn", "summary": "有一台需要注意", "highlights": ["/ 快满了"]},
        "2026-11-13",
        alerts=[{"server_name": "web-1", "title": "磁盘使用率过高（/）", "detail": "已用 95%",
                 "active": True, "occurrences": 3, "ts": int(time.time())}],
        frequency="daily", summary_only=True,
    )
    check("告警汇总主题", "告警汇总" in digest["subject"], digest["subject"])
    check("汇总含未恢复告警", "未恢复" in digest["html"] and "磁盘使用率过高" in digest["html"])
    check("汇总含 AI 结论", "AI 巡检结论" in digest["html"])

    weekly = notifier.build_digest_email(
        [{"server": server, "latest": metric, "worst_disk": {}, "alert_count": 0}],
        None, "2026-11-13", alerts=[], frequency="weekly", summary_only=True,
    )
    check("周报主题", "周报" in weekly["subject"], weekly["subject"])
    check("无告警时给出安静提示", "没有告警" in weekly["html"])


def test_email_quota():
    print("\n[邮件限速]")
    from sentinel import analyzer

    class QuotaStore:
        def __init__(self):
            self.kv = {}
            self.alert_rows = []

        def get_json(self, key, default=None):
            return self.kv.get(key, default)

        def set_json(self, key, value):
            self.kv[key] = value

        def list_servers(self):
            return [{"id": 1}]

        def insert_alert(self, alert):
            self.alert_rows.append(dict(alert))
            return len(self.alert_rows)

        def count_emails_since(self, since_ts):
            return sum(1 for a in self.alert_rows if a.get("notified") and a.get("ts", 0) >= since_ts)

    def one(kind, fp, title):
        return [{"kind": kind, "fingerprint": fp, "severity": "critical",
                 "title": title, "detail": "x", "value": 1.0}]

    store = QuotaStore()
    settings = config.normalize({
        "alerts": {"cooldown_minutes": 0, "max_emails_per_day": 2, "min_email_interval_minutes": 60},
        "email": {"enabled": True},
    })
    monitor = analyzer.Monitor(store, lambda: settings)
    server = {"id": 1, "name": "quota", "host": "1.2.3.4", "port": 22, "kind": "ssh"}
    metric = {"ts": int(time.time()), "cpu_pct": 95.0, "mem_pct": 50.0, "disks": []}

    sent = []
    real_send = analyzer.notifier.send
    analyzer.notifier.send = lambda s, subj, text, html: sent.append(subj)
    try:
        a1, _ = monitor._dispatch(server, metric, one("cpu", "cpu", "CPU 高"), [], settings)
        check("第一封邮件放行", len(sent) == 1 and a1[0]["notified"] is True,
              (len(sent), a1[0].get("notify_error")))

        a2, _ = monitor._dispatch(server, metric, one("mem", "mem", "内存高"), [], settings)
        check("间隔不足被拦截", len(sent) == 1 and "不足 60 分钟" in (a2[0].get("notify_error") or ""),
              a2[0].get("notify_error"))

        # 模拟过了 1 小时：第 2 封放行，日计数到 2
        store.kv["email_quota"]["last_ts"] = int(time.time()) - 3601
        a3, _ = monitor._dispatch(server, metric, one("disk", "disk:/", "磁盘高"), [], settings)
        check("过了间隔放行第 2 封", len(sent) == 2 and a3[0]["notified"] is True,
              (len(sent), a3[0].get("notify_error")))
        check("日计数达到 2", store.kv["email_quota"]["count"] == 2, store.kv["email_quota"])

        a4, _ = monitor._dispatch(server, metric, one("inode", "inode:/", "inode 高"), [], settings)
        check("超过每日上限被拦截", len(sent) == 2 and "已达每日 2 封上限" in (a4[0].get("notify_error") or ""),
              a4[0].get("notify_error"))

        # 模拟第二天：额度按自然日重置（时间也前进了 1 小时以上）
        store.kv["email_quota"].update({"day": "2000-01-01", "last_ts": int(time.time()) - 3700})
        a5, _ = monitor._dispatch(server, metric, one("offline", "offline", "失联"), [], settings)
        check("次日额度重置", len(sent) == 3 and a5[0]["notified"] is True,
              (len(sent), a5[0].get("notify_error")))

        # 恢复邮件同样受限速约束
        monitor._notify_recovery(server, metric,
                                 [{"key": "1:cpu", "fingerprint": "cpu", "title": "CPU 高"}], settings)
        check("恢复邮件也受间隔限制", len(sent) == 3, len(sent))

        # 0 = 不限制
        settings["alerts"]["max_emails_per_day"] = 0
        settings["alerts"]["min_email_interval_minutes"] = 0
        a6, _ = monitor._dispatch(server, metric, one("cpu2", "cpu2", "又高了"), [], settings)
        check("0 表示不限制", len(sent) == 4 and a6[0]["notified"] is True,
              (len(sent), a6[0].get("notify_error")))
    finally:
        analyzer.notifier.send = real_send


def main():
    test_collector()
    test_predictor()
    test_rules()
    test_config()
    test_secrets()
    test_llm_parse()
    test_email()
    test_email_quota()
    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
