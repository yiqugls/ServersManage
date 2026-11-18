"""告警编排：规则判定 -> 冷却去重 -> （可选）模型研判 -> 邮件。

分层原则：
- 规则层负责“一定要告诉你”的硬事件（阈值、失联、磁盘写满预测），永远兜底；
- 模型层负责“这次异常到底要不要紧、可能是什么原因”，失败不影响告警送达。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from . import collector, notifier, predictor
from .llm import LLMClient, LLMError

log = logging.getLogger("sentinel.analyzer")

SEV_RANK = {"info": 1, "warning": 2, "high": 3, "critical": 4, "resolved": 0}
GB = 1024 ** 3

RULE_TITLES = {
    "cpu": "CPU 持续高负载",
    "mem": "内存使用率过高",
    "disk": "磁盘使用率过高",
    "inode": "inode 使用率过高",
    "disk_forecast": "磁盘预计将被写满",
    "offline": "服务器失联",
}


class Monitor:
    def __init__(self, store, settings_getter):
        self.store = store
        self.get_settings = settings_getter
        self._active: Dict[str, Dict[str, Any]] = self.store.get_json("alert_state", {}) or {}

    # ------------------------------------------------------------------ 入口
    def on_success(self, server: Dict[str, Any], metric: Dict[str, Any]) -> Dict[str, Any]:
        settings = self.get_settings()
        server_id = int(server["id"])
        if not self.store.get_server(server_id):
            # 采集期间服务器被删了，别再写脏数据
            return {"predictions": [], "findings": [], "alerts": [], "analysis": None, "resolved": []}
        self.store.insert_metric(server_id, metric)

        history = self.store.metrics_since(server_id, int(time.time()) - 12 * 3600, limit=4000)
        predictions = predictor.build_predictions(history, settings, now_ts=int(time.time()))
        findings = self._evaluate(server, metric, predictions, settings)
        resolved = self._resolve_missing(server, findings)

        alerts: List[Dict[str, Any]] = []
        analysis = None
        if findings:
            alerts, analysis = self._dispatch(server, metric, findings, predictions, settings)
        if resolved:
            self._notify_recovery(server, metric, resolved, settings)

        return {
            "predictions": predictions,
            "findings": findings,
            "alerts": alerts,
            "analysis": analysis,
            "resolved": resolved,
        }

    def on_failure(self, server: Dict[str, Any], error: str, streak: int) -> Dict[str, Any]:
        settings = self.get_settings()
        cfg = settings["alerts"]
        alerts: List[Dict[str, Any]] = []
        if streak >= int(cfg["offline_after"]):
            finding = {
                "kind": "offline",
                "fingerprint": "offline",
                "severity": "critical",
                "title": RULE_TITLES["offline"],
                "detail": f"连续 {streak} 次采集失败：{error}",
                "value": float(streak),
            }
            alerts, _ = self._dispatch(server, {}, [finding], [], settings)
        return {"alerts": alerts, "error": error}

    # ------------------------------------------------------------------ 规则
    def _evaluate(
        self,
        server: Dict[str, Any],
        metric: Dict[str, Any],
        predictions: List[Dict[str, Any]],
        settings: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        cfg = settings["alerts"]
        server_id = int(server["id"])
        findings: List[Dict[str, Any]] = []

        if metric.get("cpu_pct") is not None:
            streak = self._consecutive(server_id, "cpu_pct", float(cfg["cpu_pct"]))
            if streak >= int(cfg["cpu_consecutive"]):
                cpu = float(metric["cpu_pct"])
                findings.append(
                    {
                        "kind": "cpu",
                        "fingerprint": "cpu",
                        "severity": "critical" if cpu >= 97 else "high",
                        "title": RULE_TITLES["cpu"],
                        "detail": f"连续 {streak} 个周期 CPU 超过 {cfg['cpu_pct']}%，"
                        f"当前 {cpu:.1f}%，负载 {_num(metric.get('load1'), 2)}",
                        "value": cpu,
                    }
                )

        if metric.get("mem_pct") is not None:
            streak = self._consecutive(server_id, "mem_pct", float(cfg["mem_pct"]))
            if streak >= int(cfg["mem_consecutive"]):
                mem = float(metric["mem_pct"])
                findings.append(
                    {
                        "kind": "mem",
                        "fingerprint": "mem",
                        "severity": "critical" if mem >= 98 else "high",
                        "title": RULE_TITLES["mem"],
                        "detail": f"连续 {streak} 个周期内存超过 {cfg['mem_pct']}%，当前 {mem:.1f}%，"
                        f"可用 {_gb((metric.get('mem_total') or 0) - (metric.get('mem_used') or 0))}",
                        "value": mem,
                    }
                )

        for disk in metric.get("disks") or []:
            mount = disk.get("mount") or "?"
            pct = disk.get("pct")
            if pct is not None and float(pct) >= float(cfg["disk_pct"]):
                findings.append(
                    {
                        "kind": "disk",
                        "fingerprint": f"disk:{mount}",
                        "severity": "critical" if float(pct) >= 97 else "high",
                        "title": f"{RULE_TITLES['disk']}（{mount}）",
                        "detail": f"已用 {float(pct):.1f}%，{_gb(disk.get('used'))} / {_gb(disk.get('total'))}，"
                        f"剩余 {_gb(disk.get('avail'))}",
                        "value": float(pct),
                    }
                )
            inode = disk.get("inode_pct")
            if inode is not None and float(inode) >= 95:
                findings.append(
                    {
                        "kind": "inode",
                        "fingerprint": f"inode:{mount}",
                        "severity": "high",
                        "title": f"{RULE_TITLES['inode']}（{mount}）",
                        "detail": f"inode 已用 {float(inode):.1f}%，即使有空间也无法创建新文件",
                        "value": float(inode),
                    }
                )

        horizon = float(cfg["disk_forecast_hours"])
        # 已经超过阈值的挂载点由上面的 disk 规则负责，不再重复报“预计写满”
        over_threshold = {
            d.get("mount")
            for d in (metric.get("disks") or [])
            if d.get("pct") is not None and float(d["pct"]) >= float(cfg["disk_pct"])
        }
        for pred in predictions:
            if pred.get("mount") in over_threshold:
                continue
            eta = pred.get("eta_hours")
            if pred.get("status") not in ("filling", "full"):
                continue
            if eta is None:
                continue
            if pred.get("confidence") == "low" and eta > 24:
                continue  # 数据太少时，只保留“马上要满”的判断
            if eta > horizon:
                continue
            mount = pred.get("mount")
            if eta <= 6:
                severity = "critical"
            elif eta <= 24:
                severity = "high"
            else:
                severity = "warning"
            findings.append(
                {
                    "kind": "disk_forecast",
                    "fingerprint": f"disk_forecast:{mount}",
                    "severity": severity,
                    "title": f"{RULE_TITLES['disk_forecast']}（{mount}）",
                    "detail": f"按最近 {pred.get('span_hours') or 0:.1f} 小时的趋势，每天增长 "
                    f"{pred.get('slope_gb_day') or 0:.2f} GB，预计 {_hours_text(eta)} 后达到 "
                    f"{cfg['disk_pct']}% 阈值（当前 {pred.get('used_pct') or 0:.1f}%，"
                    f"置信度 {_conf_text(pred.get('confidence'))}）",
                    "value": eta,
                }
            )
        return findings

    def _consecutive(self, server_id: int, field: str, threshold: float) -> int:
        rows = self.store.recent_metrics(server_id, limit=60)
        count = 0
        for row in reversed(rows):
            value = row.get(field)
            if value is not None and float(value) >= threshold:
                count += 1
            else:
                break
        return count

    # ------------------------------------------------------------------ 去重
    def _state_key(self, server: Dict[str, Any], fingerprint: str) -> str:
        return f"{server['id']}:{fingerprint}"

    def _should_emit(
        self, key: str, severity: str, cfg: Dict[str, Any], now: int
    ) -> bool:
        state = self._active.get(key)
        if not state:
            return True
        if SEV_RANK.get(severity, 0) > SEV_RANK.get(state.get("sev", "info"), 0):
            return True
        cooldown = int(cfg.get("cooldown_minutes", 360)) * 60
        return now - int(state.get("last", 0)) >= cooldown

    # ------------------------------------------------------------------ 邮件限速
    def _email_quota_block(self, cfg: Dict[str, Any], now: int) -> str:
        """每天最多 N 封 + 相邻两封最小间隔；返回拦截原因，空串 = 放行。

        计数只认“实际发送成功”的邮件（一封里打包多条异常算 1 封），
        存在 state 表里，重启不丢；自然日 0 点重置。
        """
        quota = self.store.get_json("email_quota", {}) or {}
        max_per_day = int(cfg.get("max_emails_per_day") or 0)
        if max_per_day > 0:
            today = time.strftime("%Y-%m-%d", time.localtime(now))
            if str(quota.get("day") or "") == today and int(quota.get("count") or 0) >= max_per_day:
                return f"已达每日 {max_per_day} 封上限"
        interval = int(cfg.get("min_email_interval_minutes") or 0)
        if interval > 0 and quota.get("last_ts") and now - int(quota["last_ts"]) < interval * 60:
            return f"距上一封邮件不足 {interval} 分钟"
        return ""

    def _record_email_sent(self, now: int) -> None:
        quota = self.store.get_json("email_quota", {}) or {}
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if str(quota.get("day") or "") != today:
            quota["day"] = today
            quota["count"] = 0
        quota["count"] = int(quota.get("count") or 0) + 1
        quota["last_ts"] = now
        self.store.set_json("email_quota", quota)

    # ------------------------------------------------------------------ 分发
    def _dispatch(
        self,
        server: Dict[str, Any],
        metric: Dict[str, Any],
        findings: List[Dict[str, Any]],
        predictions: List[Dict[str, Any]],
        settings: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        cfg = settings["alerts"]
        now = int(time.time())
        summary_mode = settings["digest"].get("alerts_mode") == "summary"
        emitted: List[Dict[str, Any]] = []
        for finding in findings:
            key = self._state_key(server, finding["fingerprint"])
            if self._should_emit(key, finding["severity"], cfg, now):
                emitted.append(finding)
        if not emitted:
            return [], None

        analysis: Optional[Dict[str, Any]] = None
        llm_error = ""
        diagnosis = ""
        # 汇总模式下不再逐条调用模型（汇总邮件里会统一做一次 AI 总结），省钱
        if (
            not summary_mode
            and cfg.get("enabled")
            and settings["llm"].get("enabled")
            and settings["llm"].get("mode") == "on_alert"
        ):
            diagnosis = collector.collect_diagnosis(server, settings)
            try:
                client = LLMClient(settings["llm"], session_id="server-%s" % server.get("id"))
                analysis = client.analyze_incident(
                    build_context(server, metric, findings, predictions, settings, diagnosis)
                )
            except LLMError as exc:
                llm_error = str(exc)
                log.warning("模型研判失败 %s: %s", server.get("name"), exc)
            except Exception as exc:  # 任何意外都不能挡住告警
                llm_error = f"{exc.__class__.__name__}: {exc}"
                log.warning("模型研判异常 %s: %s", server.get("name"), exc)

        email_enabled = bool(cfg.get("enabled")) and bool(settings["email"].get("enabled"))
        is_demo = server.get("kind") == "demo"
        notify_error = ""
        notified = False

        if summary_mode:
            notify_error = "已归入%s汇总，不发即时邮件" % (
                "每周" if settings["digest"].get("frequency") == "weekly" else "每日"
            )
        elif not email_enabled:
            notify_error = "邮件通知未启用"
        elif is_demo:
            notify_error = "演示服务器不发信"
        else:
            quota_error = self._email_quota_block(cfg, now)
            if quota_error:
                notify_error = quota_error
            elif self.store.count_emails_since(now - 3600) >= int(cfg["max_emails_per_hour"]):
                notify_error = f"已达每小时 {cfg['max_emails_per_hour']} 封上限"
            else:
                try:
                    mail = notifier.build_alert_email(
                        settings, server, metric, emitted, predictions, analysis, diagnosis
                    )
                    notifier.send(settings, mail["subject"], mail["text"], mail["html"])
                    notified = True
                    self._record_email_sent(now)
                except notifier.NotifyError as exc:
                    notify_error = str(exc)
                    log.warning("告警邮件发送失败 %s: %s", server.get("name"), exc)
                except Exception as exc:
                    notify_error = f"{exc.__class__.__name__}: {exc}"
                    log.warning("告警邮件异常 %s: %s", server.get("name"), exc)

        llm_payload = analysis or ({"error": llm_error} if llm_error else None)
        alerts: List[Dict[str, Any]] = []
        for finding in emitted:
            log.info(
                "告警 %s · %s（%s）",
                server.get("name"),
                finding["title"],
                finding["severity"],
            )
            alert = {
                "server_id": int(server["id"]),
                "server_name": server.get("name", ""),
                "fingerprint": finding["fingerprint"],
                "kind": finding["kind"],
                "severity": finding["severity"],
                "title": finding["title"],
                "detail": finding["detail"],
                "value": finding.get("value"),
                "ts": now,
                "state": "firing",
                "notified": notified,
                "notify_error": notify_error,
                "llm": json.dumps(llm_payload, ensure_ascii=False) if llm_payload else None,
            }
            alert_id = self.store.insert_alert(alert)
            alert["id"] = alert_id
            alert["llm"] = llm_payload
            alert["notified"] = notified
            alert["notify_error"] = notify_error
            alerts.append(alert)
            self._active[self._state_key(server, finding["fingerprint"])] = {
                "first": self._active.get(self._state_key(server, finding["fingerprint"]), {}).get("first", now),
                "last": now,
                "sev": finding["severity"],
                "title": finding["title"],
            }
        self._save_state()
        return alerts, analysis

    def _notify_recovery(
        self,
        server: Dict[str, Any],
        metric: Dict[str, Any],
        resolved: List[Dict[str, Any]],
        settings: Dict[str, Any],
    ) -> None:
        for item in resolved:
            self._active.pop(item["key"], None)
        self._save_state()

        cfg = settings["alerts"]
        now = int(time.time())
        summary_mode = settings["digest"].get("alerts_mode") == "summary"
        email_enabled = bool(cfg.get("enabled")) and bool(settings["email"].get("enabled"))
        notify_error = ""
        notified = False
        if not cfg.get("notify_recovery"):
            notify_error = "未开启恢复通知"
        elif summary_mode:
            notify_error = "已归入定期汇总，不发即时邮件"
        elif not email_enabled:
            notify_error = "邮件通知未启用"
        elif server.get("kind") == "demo":
            notify_error = "演示服务器不发信"
        else:
            quota_error = self._email_quota_block(cfg, now)
            if quota_error:
                notify_error = quota_error
            else:
                try:
                    mail = notifier.build_recovery_email(server, metric, resolved)
                    notifier.send(settings, mail["subject"], mail["text"], mail["html"])
                    notified = True
                    self._record_email_sent(now)
                except Exception as exc:
                    notify_error = str(exc)
                    log.warning("恢复邮件发送失败 %s: %s", server.get("name"), exc)

        for item in resolved:
            self.store.insert_alert(
                {
                    "server_id": int(server["id"]),
                    "server_name": server.get("name", ""),
                    "fingerprint": item["fingerprint"],
                    "kind": "recovered",
                    "severity": "info",
                    "title": f"已恢复：{item.get('title') or item['fingerprint']}",
                    "detail": "指标已回落到阈值以内",
                    "ts": now,
                    "state": "resolved",
                    "notified": notified,
                    "notify_error": notify_error,
                }
            )

    def _resolve_missing(
        self, server: Dict[str, Any], findings: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        current = {f["fingerprint"] for f in findings}
        prefix = f"{server['id']}:"
        resolved: List[Dict[str, Any]] = []
        for key, state in list(self._active.items()):
            if not key.startswith(prefix):
                continue
            fingerprint = key[len(prefix):]
            if fingerprint in current:
                continue
            resolved.append(
                {
                    "key": key,
                    "fingerprint": fingerprint,
                    "title": state.get("title") or fingerprint,
                }
            )
        return resolved

    def _save_state(self) -> None:
        # 清理已删除服务器的残留
        valid = {str(s["id"]) for s in self.store.list_servers()}
        for key in list(self._active.keys()):
            if key.split(":", 1)[0] not in valid:
                self._active.pop(key, None)
        self.store.set_json("alert_state", self._active)

    # ------------------------------------------------------------------ 汇总
    def build_digest_rows(self, period_sec: int = 86400) -> List[Dict[str, Any]]:
        settings = self.get_settings()
        now = int(time.time())
        servers = self.store.list_servers()
        rows = []
        for server in servers:
            latest = self.store.latest_metric(int(server["id"]))
            # 预测只看最近 12 小时，和报表周期无关
            history = self.store.metrics_since(int(server["id"]), now - 12 * 3600, limit=4000)
            preds = predictor.build_predictions(history, settings, now_ts=now)
            worst = max(preds, key=lambda p: p.get("used_pct") or 0) if preds else None
            alert_count = sum(
                1
                for a in self.store.alerts_since(now - period_sec)
                if a["server_id"] == server["id"] and a.get("kind") != "recovered"
            )
            rows.append(
                {
                    "server": server,
                    "latest": latest or {},
                    "worst_disk": worst or {},
                    "alert_count": alert_count,
                }
            )
        return rows

    def collect_report_alerts(self, since_ts: int) -> List[Dict[str, Any]]:
        """把周期内的告警按「服务器 + 问题」归并，标出当前是否仍未恢复。"""
        rows = self.store.alerts_since(since_ts)
        active = self.store.get_json("alert_state", {}) or {}
        fired: Dict[tuple, Dict[str, Any]] = {}
        resolved: Dict[tuple, Dict[str, Any]] = {}
        counts: Dict[tuple, int] = {}
        for alert in rows:
            key = (alert["server_id"], alert["fingerprint"])
            counts[key] = counts.get(key, 0) + 1
            if alert["kind"] == "recovered":
                resolved[key] = alert
            else:
                fired[key] = alert

        out: List[Dict[str, Any]] = []
        for key in set(fired) | set(resolved):
            base = fired.get(key) or resolved[key]
            title = str(base.get("title") or "")
            if key not in fired and title.startswith("已恢复："):
                title = title[4:]
            out.append(
                {
                    "server_id": key[0],
                    "server_name": base.get("server_name", ""),
                    "fingerprint": key[1],
                    "title": title,
                    "detail": base.get("detail", ""),
                    "severity": base.get("severity", "warning"),
                    "ts": base.get("ts"),
                    "occurrences": counts.get(key, 1),
                    "active": f"{key[0]}:{key[1]}" in active,
                }
            )
        out.sort(key=lambda item: (0 if item["active"] else 1, -(item["ts"] or 0)))
        return out

    def send_report(self) -> Dict[str, Any]:
        """发一封「按天/按周」的汇总：告警 + 体检表 + AI 总结。"""
        settings = self.get_settings()
        cfg = settings["digest"]
        frequency = cfg.get("frequency") or "daily"
        period = 7 * 86400 if frequency == "weekly" else 86400
        now = int(time.time())
        rows = self.build_digest_rows(period)
        alerts = self.collect_report_alerts(now - period)

        digest = None
        if settings["llm"].get("enabled"):
            try:
                client = LLMClient(settings["llm"], session_id="periodic-report")
                digest = client.daily_digest(
                    {
                        "window": "7d" if frequency == "weekly" else "24h",
                        "servers": [
                            {
                                "name": r["server"]["name"],
                                "host": r["server"]["host"],
                                "cpu_pct": r["latest"].get("cpu_pct"),
                                "mem_pct": r["latest"].get("mem_pct"),
                                "disk": {
                                    "mount": r["worst_disk"].get("mount"),
                                    "used_pct": r["worst_disk"].get("used_pct"),
                                    "slope_gb_day": r["worst_disk"].get("slope_gb_day"),
                                    "eta_hours": r["worst_disk"].get("eta_hours"),
                                },
                                "alerts": r["alert_count"],
                            }
                            for r in rows
                        ],
                        "alerts": [
                            {
                                "server": a["server_name"],
                                "title": a["title"],
                                "active": a["active"],
                                "occurrences": a["occurrences"],
                            }
                            for a in alerts[:20]
                        ],
                    }
                )
            except Exception as exc:
                log.warning("汇总模型调用失败: %s", exc)

        date_text = time.strftime("%Y-%m-%d")
        mail = notifier.build_digest_email(
            rows,
            digest,
            date_text,
            alerts=alerts,
            frequency=frequency,
            summary_only=cfg.get("alerts_mode") == "summary",
        )
        error = ""
        if settings["email"].get("enabled"):
            try:
                notifier.send(settings, mail["subject"], mail["text"], mail["html"])
            except Exception as exc:
                error = str(exc)
                log.warning("汇总邮件发送失败: %s", exc)
        else:
            error = "邮件通知未启用"
        return {
            "rows": len(rows),
            "alerts": len(alerts),
            "error": error,
            "subject": mail["subject"],
        }


# --------------------------------------------------------------------- 上下文
def build_context(
    server: Dict[str, Any],
    metric: Dict[str, Any],
    findings: List[Dict[str, Any]],
    predictions: List[Dict[str, Any]],
    settings: Dict[str, Any],
    diagnosis: str = "",
) -> Dict[str, Any]:
    disks = []
    by_mount = {p.get("mount"): p for p in predictions}
    for disk in metric.get("disks") or []:
        pred = by_mount.get(disk.get("mount")) or {}
        disks.append(
            {
                "mount": disk.get("mount"),
                "fs": disk.get("fs"),
                "total_gb": round((disk.get("total") or 0) / GB, 1),
                "used_gb": round((disk.get("used") or 0) / GB, 1),
                "pct": _num(disk.get("pct"), 1),
                "inode_pct": _num(disk.get("inode_pct"), 1),
                "slope_gb_day": pred.get("slope_gb_day"),
                "eta_hours": pred.get("eta_hours"),
                "confidence": pred.get("confidence"),
            }
        )
    context = {
        "server": {
            "name": server.get("name"),
            "host": server.get("host"),
            "note": server.get("note") or "",
        },
        "latest": {
            "cpu_pct": _num(metric.get("cpu_pct"), 1),
            "cores": metric.get("cpu_cores"),
            "mem_pct": _num(metric.get("mem_pct"), 1),
            "swap_pct": _num(metric.get("swap_pct"), 1),
            "load1": _num(metric.get("load1"), 2),
            "load5": _num(metric.get("load5"), 2),
            "uptime_hours": round((metric.get("uptime_sec") or 0) / 3600, 1),
        },
        "disks": disks,
        "findings": [
            {"kind": f["kind"], "severity": f["severity"], "title": f["title"], "detail": f["detail"]}
            for f in findings
        ],
        "thresholds": {
            "cpu_pct": settings["alerts"]["cpu_pct"],
            "mem_pct": settings["alerts"]["mem_pct"],
            "disk_pct": settings["alerts"]["disk_pct"],
        },
    }
    if diagnosis:
        context["diagnosis"] = diagnosis[:1500]
    return context


def _num(value: Any, digits: int) -> Optional[float]:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _gb(value: Any) -> str:
    try:
        return f"{float(value) / GB:.1f} GB"
    except (TypeError, ValueError):
        return "—"


def _hours_text(hours: float) -> str:
    if hours < 1:
        return f"{hours * 60:.0f} 分钟"
    if hours < 48:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def _conf_text(conf: Optional[str]) -> str:
    return {"high": "高", "medium": "中", "low": "低"}.get(conf or "low", "低")
