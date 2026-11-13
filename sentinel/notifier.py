"""邮件通知：SMTP 发信 + HTML 模板。

支持 465 隐式 SSL、587 STARTTLS、25 明文三种方式，附件不需要，正文一律
text + html 双份，保证手机端和桌面端都能看。
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any, Dict, List, Optional

from . import __version__
from .predictor import GB

SEV_COLOR = {
    "critical": "#d92d20",
    "high": "#e8590c",
    "warning": "#b7791f",
    "info": "#2f6feb",
    "resolved": "#1a7f37",
}
SEV_TEXT = {
    "critical": "严重",
    "high": "高",
    "warning": "警告",
    "info": "提示",
    "resolved": "已恢复",
}
SEV_RANK = {"info": 1, "warning": 2, "high": 3, "critical": 4, "resolved": 0}


class NotifyError(RuntimeError):
    pass


# --------------------------------------------------------------------- 发送
def send(settings: Dict[str, Any], subject: str, text_body: str, html_body: str) -> None:
    cfg = settings["email"]
    if not cfg.get("host"):
        raise NotifyError("未配置 SMTP 服务器")
    recipients = [x for x in (cfg.get("to_addrs") or []) if "@" in str(x)]
    if not recipients:
        raise NotifyError("未配置收件人")
    sender = cfg.get("from_addr") or cfg.get("username") or ""
    if not sender:
        raise NotifyError("未配置发件人地址")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((str(cfg.get("from_name") or "哨兵监控"), sender))
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["X-Sentinel-Version"] = __version__
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    host = cfg["host"]
    port = int(cfg.get("port") or 465)
    security = cfg.get("security") or "ssl"
    context = ssl.create_default_context()
    timeout = 20

    try:
        if security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        try:
            server.ehlo()
            if security == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if cfg.get("username"):
                server.login(cfg["username"], cfg.get("password") or "")
            server.send_message(msg, from_addr=sender, to_addrs=recipients)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError as exc:
        raise NotifyError("SMTP 认证失败：账号或授权码不正确") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise NotifyError(f"收件人被拒绝：{exc.recipients}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError(f"SMTP 发送失败：{exc}") from exc


def send_test(settings: Dict[str, Any]) -> None:
    send(
        settings,
        "[哨兵] 测试邮件",
        "这是一封测试邮件。如果你看到它，说明 SMTP 配置可用。\n\n—— 哨兵监控",
        _shell(
            "测试邮件",
            "邮件通道已打通",
            '<p style="margin:0 0 12px;color:#374151">这是一封测试邮件。'
            "看到它说明 SMTP 配置可用，后续的异常告警和磁盘预测提醒会发到这个邮箱。</p>",
            accent="#2f6feb",
        ),
    )


# --------------------------------------------------------------------- 模板
def _shell(title: str, subtitle: str, body_html: str, accent: str = "#2f6feb") -> str:
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f4f5f7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0"
       style="width:640px;max-width:100%;background:#ffffff;border-radius:10px;overflow:hidden;
              font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;
              box-shadow:0 1px 3px rgba(16,24,40,.08);">
  <tr><td style="height:4px;background:{accent};font-size:0;line-height:0">&nbsp;</td></tr>
  <tr><td style="padding:22px 28px 6px;">
     <div style="font-size:12px;letter-spacing:.08em;color:#9aa4b2;">SENTINEL OPS · 服务器巡检</div>
     <div style="font-size:20px;font-weight:600;color:#111827;margin-top:6px;">{title}</div>
     <div style="font-size:13px;color:#6b7280;margin-top:4px;">{subtitle}</div>
  </td></tr>
  <tr><td style="padding:14px 28px 26px;">{body_html}</td></tr>
  <tr><td style="padding:14px 28px;background:#fafbfc;border-top:1px solid #eef0f3;
                 font-size:11px;color:#9aa4b2;">
     本邮件由哨兵监控自动发送 · v{__version__} · 请勿直接回复
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def _kv_table(rows: List[tuple]) -> str:
    cells = "".join(
        f'<tr><td style="padding:4px 14px 4px 0;color:#6b7280;font-size:12px;white-space:nowrap">{k}</td>'
        f'<td style="padding:4px 0;color:#111827;font-size:12px;">{v}</td></tr>'
        for k, v in rows
    )
    return f'<table role="presentation" cellpadding="0" cellspacing="0">{cells}</table>'


def _usage_bar(pct: Optional[float], color: str) -> str:
    if pct is None:
        return '<span style="color:#9aa4b2;font-size:12px">—</span>'
    width = max(0.0, min(100.0, float(pct)))
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;">'
        '<tr><td style="width:100%;padding-right:8px;">'
        '<div style="background:#eef0f3;border-radius:3px;height:6px;width:100%;">'
        f'<div style="background:{color};height:6px;border-radius:3px;width:{width:.0f}%;"></div>'
        "</div></td>"
        f'<td style="font-size:12px;color:#111827;white-space:nowrap;width:48px;text-align:right">{pct:.1f}%</td>'
        "</tr></table>"
    )


def _fmt_gb(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.1f} GB"


def _fmt_hours(hours: Optional[float]) -> str:
    if hours is None:
        return "—"
    if hours < 1:
        return f"{hours * 60:.0f} 分钟"
    if hours < 48:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


# --------------------------------------------------------------------- 告警邮件
def build_alert_email(
    settings: Dict[str, Any],
    server: Dict[str, Any],
    metric: Dict[str, Any],
    findings: List[Dict[str, Any]],
    predictions: List[Dict[str, Any]],
    analysis: Optional[Dict[str, Any]] = None,
    diagnosis: str = "",
) -> Dict[str, str]:
    top = max(findings, key=lambda f: SEV_RANK.get(f["severity"], 0)) if findings else None
    severity = (top or {}).get("severity", "warning")
    accent = SEV_COLOR.get(severity, "#2f6feb")
    label = SEV_TEXT.get(severity, severity)
    host = f"{server.get('host')}:{server.get('port', 22)}"

    if len(findings) == 1:
        title = f"{label} · {findings[0]['title']}"
    else:
        title = f"{label} · {server.get('name')} 有 {len(findings)} 项异常"
    subtitle = f"{server.get('name')}（{host}）· {_ts_text(metric.get('ts'))}"

    items = "".join(
        f'<li style="margin:0 0 8px;color:#111827;font-size:13px;line-height:1.55;">'
        f'<span style="display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;'
        f'color:#fff;background:{SEV_COLOR.get(f["severity"], "#6b7280")};margin-right:6px;">'
        f'{SEV_TEXT.get(f["severity"], f["severity"])}</span>'
        f'<b>{f["title"]}</b><br><span style="color:#4b5563;font-size:12px;">{f.get("detail", "")}</span>'
        "</li>"
        for f in findings
    )

    disks = metric.get("disks") or []
    disk_rows = "".join(
        f'<tr><td style="padding:5px 10px 5px 0;font-size:12px;color:#111827;white-space:nowrap">{d.get("mount")}</td>'
        f'<td style="padding:5px 10px 5px 0;width:45%;">{_usage_bar(d.get("pct"), _bar_color(d.get("pct")))}</td>'
        f'<td style="padding:5px 0;font-size:12px;color:#6b7280;white-space:nowrap">'
        f'{_fmt_gb((d.get("used") or 0) / GB)} / {_fmt_gb((d.get("total") or 0) / GB)}</td></tr>'
        for d in disks
    )

    forecast_rows = ""
    risky = [p for p in predictions if p.get("eta_hours") is not None and p.get("status") == "filling"]
    if risky:
        rows = "".join(
            f'<tr><td style="padding:5px 10px 5px 0;font-size:12px;color:#111827;white-space:nowrap">{p["mount"]}</td>'
            f'<td style="padding:5px 10px 5px 0;font-size:12px;color:#4b5563;white-space:nowrap">'
            f'+{p.get("slope_gb_day", 0):.2f} GB/天</td>'
            f'<td style="padding:5px 10px 5px 0;font-size:12px;color:#4b5563;white-space:nowrap">'
            f'{_fmt_hours(p.get("eta_hours"))}后超过阈值</td>'
            f'<td style="padding:5px 0;font-size:11px;color:#9aa4b2;white-space:nowrap">'
            f'置信度 {_conf_text(p.get("confidence"))}</td></tr>'
            for p in risky[:6]
        )
        forecast_rows = (
            '<div style="margin-top:18px;">'
            '<div style="font-size:13px;font-weight:600;color:#111827;margin-bottom:6px;">容量趋势预测</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0">{rows}</table>'
            '<div style="font-size:11px;color:#9aa4b2;margin-top:6px;">'
            "基于最近样本做最小二乘拟合，可作为扩容时间参考；突发写入会让实际时间提前。</div></div>"
        )

    ai_block = ""
    if analysis:
        actions = analysis.get("actions") or []
        action_html = "".join(
            f'<li style="margin:0 0 4px;color:#374151;font-size:12px;line-height:1.6;">{a}</li>'
            for a in actions
        )
        ai_block = (
            '<div style="margin-top:18px;padding:14px 16px;background:#f8fafc;border:1px solid #eef0f3;'
            'border-radius:8px;">'
            '<div style="font-size:12px;font-weight:600;color:#2f6feb;margin-bottom:6px;">'
            "AI 研判（模型辅助，仅供参考）</div>"
            f'<div style="font-size:13px;color:#111827;line-height:1.6;">{analysis.get("summary", "")}</div>'
            + (
                f'<div style="font-size:12px;color:#4b5563;margin-top:6px;line-height:1.6;">'
                f'可能原因：{analysis.get("likely_cause")}</div>'
                if analysis.get("likely_cause")
                else ""
            )
            + (
                f'<div style="font-size:12px;color:#6b7280;margin-top:8px;">建议动作</div>'
                f'<ul style="margin:4px 0 0 18px;padding:0;">{action_html}</ul>'
                if action_html
                else ""
            )
            + "</div>"
        )

    diag_block = ""
    if diagnosis.strip():
        diag_block = (
            '<div style="margin-top:14px;">'
            '<div style="font-size:12px;color:#6b7280;margin-bottom:4px;">现场快照</div>'
            '<pre style="margin:0;padding:10px 12px;background:#0f172a;color:#e2e8f0;border-radius:6px;'
            'font-size:11px;line-height:1.55;white-space:pre-wrap;word-break:break-all;max-height:280px;'
            f'overflow:hidden;">{_escape(diagnosis[:1600])}</pre></div>'
        )

    host_info = metric.get("host_info") or {}
    metric_rows = _kv_table(
        [
            ("CPU", "{:.1f}%".format(float(metric.get("cpu_pct") or 0))),
            ("内存", "{:.1f}%".format(float(metric.get("mem_pct") or 0))),
            ("负载", metric.get("load1") if metric.get("load1") is not None else "—"),
            ("主机", "{} · {}".format(host_info.get("hostname") or "—", host_info.get("kernel") or "—")),
        ]
    )
    body = """
<div style="font-size:13px;color:#111827;margin-bottom:12px;">
  <ul style="margin:0 0 4px 18px;padding:0;">""" + items + """</ul>
</div>
<div style="margin-top:6px;">""" + metric_rows + """</div>
<div style="margin-top:16px;">
  <div style="font-size:13px;font-weight:600;color:#111827;margin-bottom:6px;">磁盘使用</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">""" + disk_rows + """</table>
</div>
""" + forecast_rows + ai_block + diag_block
    html = _shell(title, subtitle, body, accent=accent)
    text = _text_version(title, subtitle, findings, metric, predictions, analysis)
    prefix = str(settings["email"].get("subject_prefix") or "").strip()
    short = findings[0]["title"] if len(findings) == 1 else f"{len(findings)} 项异常"
    subject = f'{prefix}[{label}] {server.get("name")} · {short}'
    return {"subject": subject, "html": html, "text": text, "severity": severity}



def build_recovery_email(
    server: Dict[str, Any], metric: Dict[str, Any], resolved: List[Dict[str, Any]]
) -> Dict[str, str]:
    names = "".join(
        f'<li style="margin:0 0 4px;color:#111827;font-size:13px;">{r.get("title", r.get("fingerprint"))}</li>'
        for r in resolved
    )
    body = (
        f'<ul style="margin:0 0 12px 18px;padding:0;">{names}</ul>'
        '<p style="margin:0;color:#4b5563;font-size:13px;line-height:1.6;">'
        "上述指标已回落到阈值以内，本次告警关闭。</p>"
        + '<div style="margin-top:14px;">'
        + _kv_table(
            [
                ("CPU", f'{metric.get("cpu_pct", 0):.1f}%'),
                ("内存", f'{metric.get("mem_pct", 0):.1f}%'),
                ("时间", _ts_text(metric.get("ts"))),
            ]
        )
        + "</div>"
    )
    html = _shell(
        "已恢复 · " + server.get("name", ""),
        f'{server.get("host")} · {len(resolved)} 项告警解除',
        body,
        accent=SEV_COLOR["resolved"],
    )
    text = f"[已恢复] {server.get('name')} {server.get('host')}\n" + "\n".join(
        f"- {r.get('title')}" for r in resolved
    )
    return {"subject": f"[恢复] {server.get('name')}", "html": html, "text": text, "severity": "resolved"}


def build_digest_email(
    rows: List[Dict[str, Any]],
    digest: Optional[Dict[str, Any]],
    date_text: str,
    alerts: Optional[List[Dict[str, Any]]] = None,
    frequency: str = "daily",
    summary_only: bool = False,
) -> Dict[str, str]:
    """定期汇总：告警 + 体检表 + AI 结论。frequency 为 daily / weekly。"""
    alerts = alerts or []
    weekly = frequency == "weekly"
    title = "告警汇总" if (summary_only and alerts) else ("每周巡检汇总" if weekly else "每日巡检汇总")
    subtitle = f"{date_text} · {len(rows)} 台服务器" + (f" · {len(alerts)} 条告警" if alerts else "")

    trs = ""
    for row in rows:
        s = row["server"]
        latest = row.get("latest") or {}
        pred = row.get("worst_disk") or {}
        trs += (
            "<tr>"
            f'<td style="padding:7px 8px;border-bottom:1px solid #eef0f3;font-size:12px;color:#111827;">'
            f'{s.get("name")}<br><span style="color:#9aa4b2;font-size:11px;">{s.get("host")}</span></td>'
            f'<td style="padding:7px 8px;border-bottom:1px solid #eef0f3;font-size:12px;">'
            f'{_usage_bar(latest.get("cpu_pct"), _bar_color(latest.get("cpu_pct")))}</td>'
            f'<td style="padding:7px 8px;border-bottom:1px solid #eef0f3;font-size:12px;">'
            f'{_usage_bar(latest.get("mem_pct"), _bar_color(latest.get("mem_pct")))}</td>'
            f'<td style="padding:7px 8px;border-bottom:1px solid #eef0f3;font-size:12px;color:#4b5563;">'
            f'{pred.get("mount", "—")} {pred.get("pct", 0):.0f}%'
            + (
                f'<br><span style="color:#b7791f;font-size:11px;">{_fmt_hours(pred.get("eta_hours"))}后满</span>'
                if pred.get("eta_hours")
                else ""
            )
            + "</td>"
            f'<td style="padding:7px 8px;border-bottom:1px solid #eef0f3;font-size:12px;color:#4b5563;">'
            f'{row.get("alert_count", 0)}</td>'
            "</tr>"
        )

    alert_block = ""
    if alerts:
        shown = alerts[:20]
        rows_html = ""
        for a in shown:
            active = a.get("active")
            color = "#d92d20" if active else "#1a7f37"
            state = "未恢复" if active else "已恢复"
            detail = _escape(str(a.get("detail") or ""))[:160]
            rows_html += (
                "<tr>"
                f'<td style="padding:7px 8px;border-bottom:1px solid #eef0f3;white-space:nowrap;font-size:11px;">'
                f'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:{color};'
                f'margin-right:5px;"></span><span style="color:{color};">{state}</span></td>'
                f'<td style="padding:7px 8px;border-bottom:1px solid #eef0f3;font-size:12px;color:#111827;'
                f'white-space:nowrap;">{_escape(str(a.get("server_name") or ""))}</td>'
                f'<td style="padding:7px 8px;border-bottom:1px solid #eef0f3;font-size:12px;color:#111827;">'
                f'{_escape(str(a.get("title") or ""))}'
                + (f'<br><span style="color:#9aa4b2;font-size:11px;">{detail}</span>' if detail else "")
                + "</td>"
                f'<td style="padding:7px 8px;border-bottom:1px solid #eef0f3;font-size:11px;color:#6b7280;'
                f'white-space:nowrap;text-align:right;">{a.get("occurrences", 1)} 次</td>'
                "</tr>"
            )
        more = (
            f'<div style="font-size:11px;color:#9aa4b2;margin-top:6px;">另有 {len(alerts) - len(shown)} 条未列出</div>'
            if len(alerts) > len(shown)
            else ""
        )
        alert_block = (
            '<div style="margin:0 0 18px;">'
            f'<div style="font-size:13px;font-weight:600;color:#111827;margin-bottom:6px;">'
            f'本期告警（{len(alerts)} 条，未恢复优先）</div>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f"{rows_html}</table>{more}</div>"
        )
    elif summary_only:
        alert_block = (
            '<div style="margin:0 0 18px;padding:12px 14px;background:#f0fdf4;border:1px solid #dcfce7;'
            'border-radius:8px;font-size:13px;color:#166534;">本期没有告警，各项指标正常。</div>'
        )

    ai = ""
    if digest:
        hl = "".join(
            f'<li style="margin:0 0 3px;color:#374151;font-size:12px;">{_escape(str(h))}</li>'
            for h in digest.get("highlights") or []
        )
        ai = (
            '<div style="margin-top:16px;padding:12px 14px;background:#f8fafc;border:1px solid #eef0f3;'
            'border-radius:8px;">'
            '<div style="font-size:12px;font-weight:600;color:#2f6feb;margin-bottom:5px;">AI 巡检结论</div>'
            f'<div style="font-size:13px;color:#111827;">{_escape(str(digest.get("summary", "")))}</div>'
            + (f'<ul style="margin:6px 0 0 18px;padding:0;">{hl}</ul>' if hl else "")
            + "</div>"
        )

    body = (
        alert_block
        + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        '<tr><th align="left" style="padding:0 8px 6px;font-size:11px;color:#9aa4b2;font-weight:500;">服务器</th>'
        '<th align="left" style="padding:0 8px 6px;font-size:11px;color:#9aa4b2;font-weight:500;">CPU</th>'
        '<th align="left" style="padding:0 8px 6px;font-size:11px;color:#9aa4b2;font-weight:500;">内存</th>'
        '<th align="left" style="padding:0 8px 6px;font-size:11px;color:#9aa4b2;font-weight:500;">磁盘</th>'
        '<th align="left" style="padding:0 8px 6px;font-size:11px;color:#9aa4b2;font-weight:500;">本期告警</th>'
        f"</tr>{trs}</table>{ai}"
    )
    html = _shell(title, subtitle, body)
    lines = [title, subtitle, ""]
    for a in alerts[:20]:
        lines.append(f"- [{'未恢复' if a.get('active') else '已恢复'}] {a.get('server_name')} {a.get('title')}")
    if alerts:
        lines.append("")
    for r in rows:
        latest = r.get("latest") or {}
        lines.append(
            f"{r['server'].get('name')} CPU {latest.get('cpu_pct')}% 内存 {latest.get('mem_pct')}%"
        )
    return {
        "subject": f"[{'告警汇总' if summary_only and alerts else '汇总'}] {date_text} "
        + ("周报" if weekly else "日报"),
        "html": html,
        "text": "\n".join(lines),
        "severity": "info",
    }


# --------------------------------------------------------------------- 小工具
def _bar_color(pct: Optional[float]) -> str:
    if pct is None:
        return "#9aa4b2"
    if pct >= 90:
        return "#d92d20"
    if pct >= 78:
        return "#e8590c"
    return "#1a7f37"


def _conf_text(conf: Optional[str]) -> str:
    return {"high": "高", "medium": "中", "low": "低"}.get(conf or "low", "低")


def _ts_text(ts: Any) -> str:
    import datetime

    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _text_version(
    title: str,
    subtitle: str,
    findings: List[Dict[str, Any]],
    metric: Dict[str, Any],
    predictions: List[Dict[str, Any]],
    analysis: Optional[Dict[str, Any]],
) -> str:
    lines = [title, subtitle, ""]
    for f in findings:
        lines.append(f"- [{f['severity']}] {f['title']}: {f.get('detail', '')}")
    lines.append("")
    lines.append(f"CPU {metric.get('cpu_pct')}%  内存 {metric.get('mem_pct')}%")
    for p in predictions:
        if p.get("eta_hours") is not None:
            lines.append(
                f"磁盘 {p['mount']} {p.get('used_pct')}% 增长 {p.get('slope_gb_day')} GB/天 "
                f"预计 {p.get('eta_hours')} 小时后到阈值"
            )
    if analysis:
        lines.append("")
        lines.append("AI 研判：" + str(analysis.get("summary")))
        for a in analysis.get("actions") or []:
            lines.append("  · " + str(a))
    return "\n".join(lines)
