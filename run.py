#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""哨兵 / Sentinel Ops 启动入口。

用法：
    python run.py                     # 默认 http://127.0.0.1:8088
    python run.py --port 9000         # 换端口
    python run.py --host 0.0.0.0      # 允许远程访问（建议同时设置 Basic 认证）
    python run.py --no-demo           # 不创建演示服务器
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from sentinel import __version__, api  # noqa: E402
from sentinel.app import SentinelApp  # noqa: E402

BANNER = r"""
   _____            __  _            __
  / ___/___  ____  / /_(_)___  ___  / /
  \__ \/ _ \/ __ \/ __/ / __ \/ _ \/ /
 ___/ /  __/ / / / /_/ / / / /  __/ /
/____/\___/_/ /_/\__/_/_/ /_/\___/_/
"""


def setup_logging(data_dir: str, level: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    handlers: list = [logging.StreamHandler(sys.stdout)]
    try:
        from logging.handlers import RotatingFileHandler

        handlers.append(
            RotatingFileHandler(
                os.path.join(data_dir, "sentinel.log"),
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )
    except Exception:
        pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    # paramiko 每次连接都会打两行 INFO，服务器多了以后日志会被刷满；
    # 采集失败的原因已经写进 UI 的 last_error，排查时用 --log-level DEBUG 再看细节。
    if getattr(logging, level.upper(), logging.INFO) > logging.DEBUG:
        logging.getLogger("paramiko").setLevel(logging.WARNING)


def port_in_use(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            # Windows 上 SO_REUSEADDR 会允许“抢端口”，必须用独占方式试绑
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except (AttributeError, OSError):
                pass
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return False
        except OSError:
            return True


def main() -> int:
    parser = argparse.ArgumentParser(description="哨兵：轻量服务器巡检与告警")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    parser.add_argument("--port", type=int, default=8088, help="监听端口，默认 8088")
    parser.add_argument("--data-dir", default=os.path.join(BASE_DIR, "data"), help="数据目录")
    parser.add_argument("--no-demo", action="store_true", help="不自动创建演示服务器")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    setup_logging(args.data_dir, args.log_level)
    log = logging.getLogger("sentinel")

    if port_in_use(args.host, args.port):
        print(f"端口 {args.port} 已被占用，请换一个：python run.py --port 8090")
        return 2

    app = SentinelApp(args.data_dir, seed_demo=not args.no_demo)
    try:
        httpd = api.serve(app, args.host, args.port)
    except OSError as exc:
        print(f"无法监听 {args.host}:{args.port}（{exc}）")
        return 2
    app.start()

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(BANNER)
    print(f"  哨兵 v{__version__}  已启动")
    print(f"  控制台   {url}")
    print(f"  数据目录 {os.path.abspath(args.data_dir)}")
    print(f"  采集周期 {app.get_settings()['collect']['interval_sec']} 秒（可在设置里改）")
    print("  停止运行 Ctrl+C\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass

    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        app.stop()
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            app.store.close()
        except Exception:
            pass
        log.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
