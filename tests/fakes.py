# -*- coding: utf-8 -*-
"""测试用的假服务：SSH 服务器、SMTP 收信端、OpenAI 兼容端点。

只用于本地端到端验证，不参与线上逻辑。
"""
from __future__ import annotations

import json
import logging
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paramiko

logging.getLogger("paramiko").setLevel(logging.WARNING)

# --------------------------------------------------------------------- SSH
STAT_TEMPLATE = "cpu  {u} {n} {s} {idle} 0 0 0 0\n"


def _stat_pair(cpu_pct: float):
    """回放两次 /proc/stat，让解析端算出的 CPU 使用率约等于 cpu_pct。"""
    total_delta = 1000
    idle_delta = int(round(total_delta * (100 - cpu_pct) / 100))
    busy_delta = total_delta - idle_delta
    base = 100000
    s1 = STAT_TEMPLATE.format(u=base, n=0, s=base, idle=base * 5)
    s2 = STAT_TEMPLATE.format(
        u=base + busy_delta, n=0, s=base, idle=base * 5 + idle_delta
    )
    return s1, s2


def collect_output(cpu_pct: float = 95.0, disk_pct: float = 90.0) -> str:
    s1, s2 = _stat_pair(cpu_pct)
    total_kb = 104857600
    used_kb = int(total_kb * disk_pct / 100)
    avail_kb = total_kb - used_kb
    mem_total = 16384000
    mem_avail = int(mem_total * 0.36)
    return f"""@@@STAT1
{s1.strip()}
@@@MEM
MemTotal:       {mem_total} kB
MemFree:         1000000 kB
MemAvailable:    {mem_avail} kB
Buffers:          200000 kB
Cached:          3000000 kB
SwapTotal:       2097152 kB
SwapFree:        2097152 kB
@@@LOAD
1.20 0.90 0.70 1/234 5678
@@@UP
1814400.12 123456.78
@@@NCPU
8
@@@DF
Filesystem     Type  1K-blocks      Used Available Use% Mounted on
/dev/sda1      ext4   {total_kb}   {used_kb}   {avail_kb}  {int(disk_pct)}% /
tmpfs          tmpfs    1048576         0   1048576   0% /run
/dev/sdb1      xfs   2097152000 1048576000 1048576000  50% /data
@@@DFI
Filesystem     Inodes IUsed IFree IUse% Mounted on
/dev/sda1      6553600 327680 6225920    5% /
tmpfs           262144      1  262143    1% /run
/dev/sdb1      13107200 262144 12845056    2% /data
@@@HOST
Linux 5.15.0-test-x86_64
fake-host-01
@@@STAT2
{s2.strip()}
@@@END
"""


DIAG_OUTPUT = """@@@TOP
%CPU %MEM   RSS COMMAND
88.0 12.4 204800 java
 3.2  1.1  18432 nginx
@@@DF
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1        100G   91G  9.0G  91% /
@@@MEM
              total        used        free
Mem:          16000       10200        5800
@@@END
"""


class _FakeSSHServer(paramiko.ServerInterface):
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.command = ""
        self.event = threading.Event()

    def check_auth_password(self, username, password):
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.command = command.decode("utf-8", "replace")
        self.event.set()
        return True


class FakeSSH:
    def __init__(self, host="127.0.0.1", port=2222, username="root", password="secret"):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.host_key = paramiko.RSAKey.generate(2048)
        self.cpu_pct = 95.0
        self.disk_pct = 90.0
        self.sock = None
        self.thread = None
        self.running = False

    def start(self):
        import socket

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(8)
        self.running = True
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        while self.running:
            try:
                client, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        transport = None
        try:
            transport = paramiko.Transport(client)
            transport.add_server_key(self.host_key)
            server = _FakeSSHServer(self.username, self.password)
            transport.start_server(server=server)
            chan = transport.accept(15)
            if chan is None:
                return
            server.event.wait(10)
            output = DIAG_OUTPUT if "@@@TOP" in server.command else collect_output(
                self.cpu_pct, self.disk_pct
            )
            time.sleep(0.05)
            chan.sendall(output.encode("utf-8"))
            chan.send_exit_status(0)
            chan.close()
        except Exception:
            pass
        finally:
            try:
                if transport:
                    transport.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------- SMTP
class SMTPRecord:
    def __init__(self, mail_from, rcpt_to, data):
        self.mail_from = mail_from
        self.rcpt_to = rcpt_to
        self.data = data

    @property
    def text(self):
        return self.data.decode("utf-8", "replace")


class SMTPSink:
    """够 smtplib 用的最小 SMTP 服务，收到的邮件存在 self.messages 里。"""

    def __init__(self, host="127.0.0.1", port=2525):
        self.host = host
        self.port = port
        self.messages = []
        self._lock = threading.Lock()
        self.server = None
        self.thread = None

    def start(self):
        sink = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"220 fake-smtp ESMTP\r\n")
                mail_from = ""
                rcpt = []
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    cmd = line.decode("utf-8", "replace").strip()
                    upper = cmd.upper()
                    if upper.startswith("EHLO") or upper.startswith("HELO"):
                        self.wfile.write(b"250-fake-smtp\r\n250-SIZE 10485760\r\n250 HELP\r\n")
                    elif upper.startswith("MAIL FROM"):
                        mail_from = cmd
                        self.wfile.write(b"250 OK\r\n")
                    elif upper.startswith("RCPT TO"):
                        rcpt.append(cmd)
                        self.wfile.write(b"250 OK\r\n")
                    elif upper == "DATA":
                        self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                        chunks = []
                        while True:
                            chunk = self.rfile.readline()
                            if not chunk:
                                break
                            if chunk in (b".\r\n", b".\n"):
                                break
                            chunks.append(chunk)
                        with sink._lock:
                            sink.messages.append(
                                SMTPRecord(mail_from, rcpt, b"".join(chunks))
                            )
                        self.wfile.write(b"250 OK queued\r\n")
                    elif upper == "QUIT":
                        self.wfile.write(b"221 Bye\r\n")
                        return
                    elif upper == "RSET":
                        self.wfile.write(b"250 OK\r\n")
                    elif upper == "NOOP":
                        self.wfile.write(b"250 OK\r\n")
                    else:
                        self.wfile.write(b"250 OK\r\n")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass

    def wait_for(self, count=1, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if len(self.messages) >= count:
                    return True
            time.sleep(0.1)
        return False


# ------------------------------------------------------------------- OpenAI
class FakeOpenAI:
    def __init__(self, port=8765):
        self.port = port
        self.requests = []
        self.reply = {
            "severity": "high",
            "summary": "根分区写入速度异常，疑似日志未轮转",
            "likely_cause": "应用日志按天写满且没有 logrotate 清理",
            "actions": ["du -sh /var/log/* | sort -h | tail", "配置 logrotate 并压缩旧日志"],
            "need_alert": True,
            "confidence": 0.82,
        }
        self.server = None

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace")
                fake.requests.append(
                    {
                        "path": self.path,
                        "auth": self.headers.get("Authorization", ""),
                        "session": self.headers.get("x-opencode-session", ""),
                        "ua": self.headers.get("User-Agent", ""),
                        "body": body,
                    }
                )
                if "巡检结论" in body:
                    content = json.dumps(
                        {
                            "health": "warn",
                            "summary": "整体可控，web-1 的根分区需要关注",
                            "highlights": ["web-1 / 预计 3 小时写满", "其余指标正常"],
                        },
                        ensure_ascii=False,
                    )
                else:
                    content = json.dumps(fake.reply, ensure_ascii=False)
                payload = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 320, "completion_tokens": 96, "total_tokens": 416},
                }
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Server(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
