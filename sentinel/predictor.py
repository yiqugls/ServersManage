"""磁盘容量趋势预测：最小二乘拟合，算出“还有多久写满”。

只用最近一段窗口的数据，斜率带 R² 置信度，避免把抖动当成趋势。
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional

GB = 1024 ** 3


def linear_fit(points: List[Dict[str, float]]) -> Optional[Dict[str, float]]:
    """points: [{'t': 小时, 'y': 字节}]，返回斜率(字节/小时)、截距、R²。"""
    n = len(points)
    if n < 3:
        return None
    mean_t = sum(p["t"] for p in points) / n
    mean_y = sum(p["y"] for p in points) / n
    sxx = sum((p["t"] - mean_t) ** 2 for p in points)
    if sxx <= 0:
        return None
    sxy = sum((p["t"] - mean_t) * (p["y"] - mean_y) for p in points)
    slope = sxy / sxx
    intercept = mean_y - slope * mean_t
    ss_tot = sum((p["y"] - mean_y) ** 2 for p in points)
    ss_res = sum((p["y"] - (intercept + slope * p["t"])) ** 2 for p in points)
    r2 = 0.0 if ss_tot <= 0 else max(0.0, min(1.0, 1 - ss_res / ss_tot))
    return {"slope": slope, "intercept": intercept, "r2": r2, "n": n}


def _confidence(r2: float, span_hours: float, n: int) -> str:
    if n >= 12 and span_hours >= 1.0 and r2 >= 0.75:
        return "high"
    if n >= 6 and span_hours >= 0.3 and r2 >= 0.45:
        return "medium"
    return "low"


def forecast(
    history: List[Dict[str, Any]],
    total_bytes: int,
    threshold_pct: float,
    min_slope_gb_day: float,
    now_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """history: 该挂载点的 [(ts, used_bytes)] 序列（升序）。

    返回 {slope_gb_day, eta_hours, status, confidence, ...}
    """
    now_ts = int(now_ts or time.time())
    usable = [(int(ts), int(used)) for ts, used in history if used is not None and total_bytes > 0]
    if len(usable) < 3:
        return {
            "status": "unknown",
            "confidence": "low",
            "slope_gb_day": 0.0,
            "eta_hours": None,
            "eta_at": None,
            "samples": len(usable),
            "r2": 0.0,
        }

    base_t = usable[0][0]
    span_hours = max(0.0, (usable[-1][0] - base_t) / 3600.0)
    points = [{"t": (ts - base_t) / 3600.0, "y": float(used)} for ts, used in usable]
    fit = linear_fit(points)
    if not fit:
        return {
            "status": "unknown",
            "confidence": "low",
            "slope_gb_day": 0.0,
            "eta_hours": None,
            "eta_at": None,
            "samples": len(usable),
            "r2": 0.0,
        }

    slope_bph = fit["slope"]  # 字节/小时
    slope_gb_day = slope_bph * 24 / GB
    used_now = usable[-1][1]
    used_pct = used_now * 100.0 / total_bytes if total_bytes else 0.0
    capacity_line = total_bytes * threshold_pct / 100.0

    out: Dict[str, Any] = {
        "samples": len(usable),
        "span_hours": round(span_hours, 2),
        "slope_gb_day": round(slope_gb_day, 3),
        "used_pct": round(used_pct, 2),
        "r2": round(fit["r2"], 3),
        "confidence": _confidence(fit["r2"], span_hours, len(usable)),
    }

    if used_now >= capacity_line:
        out.update({"status": "full", "eta_hours": 0.0, "eta_at": now_ts})
        return out

    min_slope_bph = max(0.0, min_slope_gb_day) * GB / 24.0
    if slope_bph <= 0 or slope_bph < min_slope_bph:
        eta = None if slope_bph <= 0 else (capacity_line - used_now) / slope_bph
        out.update(
            {
                "status": "stable" if slope_bph <= 0 else "slow",
                "eta_hours": round(eta, 2) if eta else None,
                "eta_at": int(now_ts + eta * 3600) if eta else None,
            }
        )
        return out

    eta_hours = (capacity_line - used_now) / slope_bph
    if not math.isfinite(eta_hours) or eta_hours < 0:
        eta_hours = 0.0
    out.update(
        {
            "status": "filling",
            "eta_hours": round(eta_hours, 2),
            "eta_at": int(now_ts + eta_hours * 3600),
        }
    )
    return out


def build_predictions(
    metrics: List[Dict[str, Any]], settings: Dict[str, Any], now_ts: Optional[int] = None
) -> List[Dict[str, Any]]:
    """对一台服务器最近的历史数据，逐挂载点做预测。"""
    alert_cfg = settings["alerts"]
    threshold = float(alert_cfg["disk_pct"])
    min_slope = float(alert_cfg["disk_min_slope_gb_day"])
    now_ts = int(now_ts or time.time())

    # mount -> [(ts, used)]
    series: Dict[str, List[tuple]] = {}
    totals: Dict[str, int] = {}
    for m in metrics:
        ts = int(m["ts"])
        for disk in m.get("disks") or []:
            mount = disk.get("mount")
            used = disk.get("used")
            total = disk.get("total")
            if not mount or used is None or not total:
                continue
            series.setdefault(mount, []).append((ts, used))
            totals[mount] = int(total)

    out: List[Dict[str, Any]] = []
    for mount, points in series.items():
        points.sort(key=lambda x: x[0])
        result = forecast(points, totals.get(mount, 0), threshold, min_slope, now_ts=now_ts)
        result["mount"] = mount
        result["total"] = totals.get(mount, 0)
        result["used"] = points[-1][1] if points else 0
        out.append(result)
    out.sort(key=lambda r: (r.get("eta_hours") is None, r.get("eta_hours") or 0))
    return out
