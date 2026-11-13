"""指标采集：通过 SSH 密码登录，跑一段只读的 POSIX shell，解析 /proc 与 df。

设计原则：
- 不用装 agent，不写远端文件，不依赖 sudo；
- 一次连接、一次 exec 拿到全部指标（CPU 用两次 /proc/stat 采样算差值）；
- 解析失败要能说清是哪一段失败，而不是抛一个空指针。
"""
from __future__ import annotations

import math
import random
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

# 只读取，不写入。用 @@@ 分节，避免和命令输出混在一起。
COLLECT_SCRIPT = r"""
LC_ALL=C; export LC_ALL
echo '@@@STAT1'
grep '^cpu ' /proc/stat 2>/dev/null
echo '@@@MEM'
grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|SReclaimable|Shmem):' /proc/meminfo 2>/dev/null
echo '@@@LOAD'
cat /proc/loadavg 2>/dev/null
echo '@@@UP'
cat /proc/uptime 2>/dev/null
echo '@@@NCPU'
getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null
echo '@@@DF'
DFOUT=$(df -P -k -T 2>/dev/null) || DFOUT=$(df -P -k 2>/dev/null)
printf '%s\n' "$DFOUT" | tail -n +2
echo '@@@DFI'
DIOUT=$(df -P -k -i 2>/dev/null) || DIOUT=""
printf '%s\n' "$DIOUT" | tail -n +2
echo '@@@HOST'
uname -sr 2>/dev/null
hostname 2>/dev/null
echo '@@@STAT2'
sleep 1
grep '^cpu ' /proc/stat 2>/dev/null
echo '@@@END'
"""

DIAG_SCRIPT = r"""
LC_ALL=C; export LC_ALL
echo '@@@TOP'
ps -eo pcpu,pmem,rss,comm --sort=-pcpu 2>/dev/null | head -n 8
echo '@@@DF'
df -h 2>/dev/null | head -n 20
echo '@@@MEM'
free -m 2>/dev/null | head -n 4
echo '@@@END'
"""

# 这些是伪文件系统 / 容器镜像层，统计它们没有运维意义
EXCLUDE_TYPES = {
    "tmpfs", "devtmpfs", "squashfs", "overlay", "aufs", "proc", "sysfs", "cgroup",
    "cgroup2", "ramfs", "devpts", "mqueue", "hugetlbfs", "tracefs", "debugfs",
    "securityfs", "pstore", "bpf", "autofs", "fusectl", "configfs", "efivarfs",
    "nsfs", "none", "fuse.gvfsd-fuse", "fuse.lxcfs", "fuse.portal", "binfmt_misc",
}


class CollectorError(RuntimeError):
    """采集失败，message 直接展示给用户。"""


# --------------------------------------------------------------------------- SSH
def collect_ssh(server: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    try:
        import paramiko
    except Exception as exc:  # pragma: no cover
        raise CollectorError("缺少 paramiko 库，请执行：pip install paramiko") from exc

    cfg = settings["collect"]
    host = server["host"]
    port = int(server.get("port") or 22)
    username = server.get("username") or "root"
    password = server.get("password") or ""

    client = paramiko.SSHClient()
    if cfg.get("host_key_policy") == "strict":
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    started = time.time()
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=int(cfg["connect_timeout_sec"]),
            banner_timeout=int(cfg["connect_timeout_sec"]),
            auth_timeout=int(cfg["connect_timeout_sec"]),
            look_for_keys=False,
            allow_agent=False,
        )
        stdin, stdout, stderr = client.exec_command(
            COLLECT_SCRIPT, timeout=int(cfg["command_timeout_sec"])
        )
        raw = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
    except paramiko.AuthenticationException as exc:
        raise CollectorError("SSH 认证失败：用户名或密码不对，或该账号禁止密码登录") from exc
    except paramiko.BadHostKeyException as exc:
        raise CollectorError("SSH 主机密钥与本地记录不一致，疑似中间人攻击") from exc
    except paramiko.SSHException as exc:
        msg = str(exc)
        if "not found in known_hosts" in msg:
            raise CollectorError("主机密钥不在 known_hosts 中（已设为 strict 模式）") from exc
        raise CollectorError(f"SSH 协议错误：{msg}") from exc
    except socket.timeout as exc:
        raise CollectorError(f"连接超时：检查 {host}:{port} 是否可达、防火墙是否放行") from exc
    except (ConnectionRefusedError, OSError) as exc:
        raise CollectorError(f"无法连接 {host}:{port}（{exc.__class__.__name__}: {exc}）") from exc
    finally:
        try:
            client.close()
        except Exception:
            pass

    latency_ms = int((time.time() - started) * 1000)
    if code != 0 and "@@@STAT2" not in raw:
        hint = (err or "").strip().splitlines()[:2]
        raise CollectorError("远端命令执行失败：" + (" / ".join(hint) or f"exit={code}"))

    metric = parse_output(raw)
    metric["latency_ms"] = latency_ms
    metric["ts"] = int(time.time())
    return metric


def collect_diagnosis(server: Dict[str, Any], settings: Dict[str, Any]) -> str:
    """异常时抓一把 top / df，给模型和邮件做上下文。失败不影响主流程。"""
    try:
        import paramiko
    except Exception:
        return ""
    cfg = settings["collect"]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=server["host"],
            port=int(server.get("port") or 22),
            username=server.get("username") or "root",
            password=server.get("password") or "",
            timeout=int(cfg["connect_timeout_sec"]),
            banner_timeout=int(cfg["connect_timeout_sec"]),
            auth_timeout=int(cfg["connect_timeout_sec"]),
            look_for_keys=False,
            allow_agent=False,
        )
        _, stdout, _ = client.exec_command(DIAG_SCRIPT, timeout=int(cfg["command_timeout_sec"]))
        raw = stdout.read().decode("utf-8", "replace")
    except Exception:
        return ""
    finally:
        try:
            client.close()
        except Exception:
            pass

    parts: List[str] = []
    for name, title in (("TOP", "CPU 占用 Top"), ("DF", "磁盘现状"), ("MEM", "内存现状")):
        body = _section(raw, name)
        if body.strip():
            parts.append(f"# {title}\n{body.strip()}")
    return "\n\n".join(parts)


# ------------------------------------------------------------------------ 解析
def _section(raw: str, name: str) -> str:
    marker = "@@@" + name
    lines = raw.splitlines()
    out: List[str] = []
    inside = False
    for line in lines:
        if line.startswith("@@@"):
            inside = line.strip() == marker
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def _parse_sections(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    current: Optional[str] = None
    buf: List[str] = []
    for line in raw.splitlines():
        if line.startswith("@@@"):
            if current:
                out[current] = "\n".join(buf)
            current = line.strip()[3:]
            buf = []
        elif current:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf)
    return out


def _cpu_sample(text: str) -> Optional[Tuple[int, int]]:
    """返回 (total, idle_total)，idle 含 iowait。"""
    line = text.strip().splitlines()[0] if text.strip() else ""
    parts = line.split()
    if len(parts) < 5:
        return None
    try:
        nums = [int(x) for x in parts[1:9]]
    except ValueError:
        return None
    while len(nums) < 8:
        nums.append(0)
    user, nice, system, idle, iowait, irq, softirq, steal = nums[:8]
    total = user + nice + system + idle + iowait + irq + softirq + steal
    return total, idle + iowait


def _parse_mem(text: str) -> Dict[str, float]:
    fields: Dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            fields[key.strip()] = int(parts[0])  # kB
        except ValueError:
            continue
    total = fields.get("MemTotal", 0)
    avail = fields.get("MemAvailable")
    if avail is None:
        avail = (
            fields.get("MemFree", 0)
            + fields.get("Buffers", 0)
            + fields.get("Cached", 0)
            + fields.get("SReclaimable", 0)
            - fields.get("Shmem", 0)
        )
    used = max(0, total - max(0, avail))
    swap_total = fields.get("SwapTotal", 0)
    swap_free = fields.get("SwapFree", 0)
    return {
        "mem_total": total * 1024,
        "mem_used": used * 1024,
        "mem_pct": round(used * 100.0 / total, 2) if total else 0.0,
        "swap_pct": round((swap_total - swap_free) * 100.0 / swap_total, 2) if swap_total else 0.0,
    }


def _parse_df(text: str) -> Tuple[List[Dict[str, Any]], bool]:
    """返回 (磁盘列表, 是否带文件系统类型列)。"""
    rows: List[Dict[str, Any]] = []
    has_type = False
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        with_type = not parts[1].isdigit()
        if with_type:
            if len(parts) < 7:
                continue
            fs, fstype = parts[0], parts[1]
            size_s, used_s, avail_s, pct_s = parts[2], parts[3], parts[4], parts[5]
            mount = " ".join(parts[6:])
            has_type = True
        else:
            fs, fstype = parts[0], ""
            size_s, used_s, avail_s, pct_s = parts[1], parts[2], parts[3], parts[4]
            mount = " ".join(parts[5:])
        try:
            total_kb = int(size_s)
            used_kb = int(used_s)
            avail_kb = int(avail_s)
        except ValueError:
            continue
        if total_kb <= 0:
            continue
        pct = _eff_pct(used_kb, avail_kb, pct_s)
        rows.append(
            {
                "mount": mount,
                "fs": fstype or "",
                "total": total_kb * 1024,
                "used": used_kb * 1024,
                "avail": avail_kb * 1024,
                "pct": pct,
                "inode_pct": None,
            }
        )
    return rows, has_type


def _eff_pct(used: int, avail: int, pct_text: str) -> float:
    """df 的 Use% 用的是 used/(used+avail)，会比 used/total 更贴近真实。"""
    if used + avail > 0:
        return round(used * 100.0 / (used + avail), 2)
    try:
        return float(pct_text.rstrip("%"))
    except ValueError:
        return 0.0


def _parse_df_inode(text: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        mount = " ".join(parts[5:])
        try:
            out[mount] = float(parts[4].rstrip("%"))
        except ValueError:
            continue
    return out


def _filter_disks(disks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    kept = [d for d in disks if d.get("fs", "") not in EXCLUDE_TYPES]
    if not kept:  # 容器里 / 是 overlay，全被过滤掉时退回全量
        kept = list(disks)
    # 同一挂载点只留一条（重复挂载/绑定挂载）
    seen = {}
    for d in kept:
        seen.setdefault(d["mount"], d)
    return sorted(seen.values(), key=lambda d: d["mount"])


def parse_output(raw: str) -> Dict[str, Any]:
    sec = _parse_sections(raw)
    if "STAT1" not in sec or "STAT2" not in sec:
        if "STAT1" not in sec:
            raise CollectorError("远端没有返回 /proc/stat，可能不是 Linux 系统")
    s1 = _cpu_sample(sec.get("STAT1", ""))
    s2 = _cpu_sample(sec.get("STAT2", ""))
    cpu_pct = None
    if s1 and s2:
        d_total = s2[0] - s1[0]
        d_idle = s2[1] - s1[1]
        if d_total > 0:
            cpu_pct = round(max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total))), 2)

    mem = _parse_mem(sec.get("MEM", ""))

    load1 = load5 = load15 = None
    load_parts = sec.get("LOAD", "").split()
    if len(load_parts) >= 3:
        try:
            load1, load5, load15 = (float(load_parts[0]), float(load_parts[1]), float(load_parts[2]))
        except ValueError:
            pass

    uptime = None
    up_parts = sec.get("UP", "").split()
    if up_parts:
        try:
            uptime = int(float(up_parts[0]))
        except ValueError:
            pass

    cores = None
    ncpu = sec.get("NCPU", "").strip().splitlines()
    if ncpu:
        try:
            cores = int(ncpu[0])
        except ValueError:
            cores = None

    disks, _ = _parse_df(sec.get("DF", ""))
    inodes = _parse_df_inode(sec.get("DFI", ""))
    for d in disks:
        if d["mount"] in inodes:
            d["inode_pct"] = inodes[d["mount"]]
    disks = _filter_disks(disks)

    host_lines = [x for x in sec.get("HOST", "").splitlines() if x.strip()]
    host_info = {
        "kernel": host_lines[0].strip() if len(host_lines) > 0 else "",
        "hostname": host_lines[1].strip() if len(host_lines) > 1 else "",
    }

    if cpu_pct is None and not disks and not mem.get("mem_total"):
        raise CollectorError("指标解析失败：返回内容与 Linux 格式不符")

    return {
        "cpu_pct": cpu_pct,
        "cpu_cores": cores,
        "mem_pct": mem.get("mem_pct"),
        "mem_used": mem.get("mem_used"),
        "mem_total": mem.get("mem_total"),
        "swap_pct": mem.get("swap_pct"),
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "uptime_sec": uptime,
        "disks": disks,
        "host_info": host_info,
    }


# ------------------------------------------------------------------------ 演示
_DEMO_STATE: Dict[int, Dict[str, float]] = {}


def collect_demo(server: Dict[str, Any]) -> Dict[str, Any]:
    """合成数据，用于首次启动就能看到图表和告警效果；不会发邮件。"""
    sid = int(server["id"])
    st = _DEMO_STATE.setdefault(
        sid,
        {"cpu": 24.0, "disk_root": 150.0, "disk_data": 620.0, "started": time.time()},
    )
    t = time.time()
    elapsed = t - st["started"]

    # CPU：日常波动 + 每 15 分钟一次持续 4 分钟的尖峰
    phase = elapsed % 900
    if 600 <= phase < 840:
        target = 95.0 + random.uniform(-2, 2)
    else:
        target = 26.0 + 6 * math.sin(elapsed / 600) + random.uniform(-4, 4)
    st["cpu"] = st["cpu"] * 0.7 + target * 0.3
    cpu = round(max(1.0, min(99.5, st["cpu"])), 2)

    # 磁盘：慢速增长，制造出“几十小时后写满”的预测场景
    st["disk_root"] = min(198.0, st["disk_root"] + 0.004)   # ~14 GB/h
    st["disk_data"] = min(985.0, st["disk_data"] + 0.0015)  # ~5.4 GB/h

    disks = [
        {
            "mount": "/",
            "fs": "ext4",
            "total": 200 * 1024 ** 3,
            "used": int(st["disk_root"] * 1024 ** 3),
            "avail": int((200 - st["disk_root"]) * 1024 ** 3),
            "pct": round(st["disk_root"] / 200 * 100, 2),
            "inode_pct": 12.0,
        },
        {
            "mount": "/data",
            "fs": "xfs",
            "total": 1000 * 1024 ** 3,
            "used": int(st["disk_data"] * 1024 ** 3),
            "avail": int((1000 - st["disk_data"]) * 1024 ** 3),
            "pct": round(st["disk_data"] / 1000 * 100, 2),
            "inode_pct": 3.0,
        },
        {
            "mount": "/boot",
            "fs": "ext4",
            "total": 1024 ** 3,
            "used": int(0.42 * 1024 ** 3),
            "avail": int(0.58 * 1024 ** 3),
            "pct": 42.0,
            "inode_pct": 1.0,
        },
    ]
    mem_total = 16 * 1024 ** 3
    mem_pct = round(58 + 8 * math.sin(elapsed / 900) + random.uniform(-2, 2), 2)
    mem_pct = max(20.0, min(97.0, mem_pct))
    load = round(cpu / 100 * 8 * 0.9 + random.uniform(-0.3, 0.3), 2)

    return {
        "ts": int(t),
        "cpu_pct": cpu,
        "cpu_cores": 8,
        "mem_pct": mem_pct,
        "mem_used": int(mem_total * mem_pct / 100),
        "mem_total": mem_total,
        "swap_pct": 3.0,
        "load1": max(0.0, load),
        "load5": max(0.0, load * 0.9),
        "load15": max(0.0, load * 0.8),
        "uptime_sec": int(86400 * 21 + elapsed),
        "disks": disks,
        "host_info": {"kernel": "Linux 5.15.0-122-generic", "hostname": "demo-node-01"},
        "latency_ms": random.randint(48, 120),
    }


def collect(server: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    if server.get("kind") == "demo":
        return collect_demo(server)
    return collect_ssh(server, settings)


def probe(host: str, port: int, username: str, password: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """表单里的“测试连接”，不落库。"""
    fake = {
        "host": host,
        "port": int(port or 22),
        "username": username,
        "password": password,
        "kind": "ssh",
        "id": 0,
    }
    metric = collect_ssh(fake, settings)
    return {
        "cpu_pct": metric.get("cpu_pct"),
        "mem_pct": metric.get("mem_pct"),
        "disks": metric.get("disks", []),
        "host_info": metric.get("host_info", {}),
        "cpu_cores": metric.get("cpu_cores"),
        "latency_ms": metric.get("latency_ms"),
    }
