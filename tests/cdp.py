"""A minimal Chrome DevTools Protocol client, stdlib only.

WHY THIS EXISTS RATHER THAN playwright/selenium. This suite runs inside
ingest.py's publish gate and the project deliberately keeps its runtime
dependencies to duckdb, requests and python-dotenv (see tests/harness.py).
Adding a browser-automation framework to requirements.txt so that two design
checks can run would make the data pipeline depend on it too.

CDP needs a WebSocket for Runtime.evaluate, and there is no WebSocket in the
stdlib, so this speaks just enough of RFC 6455 to send one text frame and read
one back: a client-masked frame out, an unmasked frame in, no extensions, no
continuation, no compression. That is all the protocol surface a short
`Runtime.evaluate` round trip touches.

Everything here raises on trouble; callers turn that into harness.Skip, because
a machine without Chrome should skip these checks rather than fail them.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from contextlib import closing

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def find_chrome() -> str | None:
    env = os.environ.get("CHROME_BIN")
    if env and os.path.isfile(env):
        return env
    for p in CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WS:
    """One WebSocket connection, text frames only."""

    def __init__(self, url: str, timeout: float = 20.0):
        # ws://127.0.0.1:PORT/devtools/page/ID
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("socket closed during the websocket handshake")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"handshake refused: {buf.split(chr(13).encode())[0][:80]!r}")
        self._rest = buf.split(b"\r\n\r\n", 1)[1]

    def _recv(self, n: int) -> bytes:
        out, self._rest = self._rest[:n], self._rest[n:]
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise RuntimeError("socket closed mid-frame")
            out += chunk
        return out

    def send(self, obj) -> None:
        payload = json.dumps(obj).encode()
        header = bytearray([0x81])                 # FIN + text
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self):
        while True:
            b0, b1 = self._recv(2)
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv(8))[0]
            if b1 & 0x80:                          # server frames are unmasked
                self._recv(4)
            data = self._recv(n)
            if opcode == 0x8:
                raise RuntimeError("websocket closed by Chrome")
            if opcode in (0x1, 0x2):
                return json.loads(data.decode())
            # 0x9 ping / 0xA pong: ignore and keep reading

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Browser:
    """A headless Chrome, started and stopped by this object."""

    def __init__(self, chrome: str):
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="nba-cdp-")
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", f"--remote-debugging-port={self.port}",
             f"--user-data-dir={self.profile}", "--no-first-run",
             "--no-default-browser-check", "--disable-gpu", "--hide-scrollbars",
             "--disable-extensions", "--disable-background-networking",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.ws = None
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                info = json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/version", timeout=2))
                self.ws = WS(info["webSocketDebuggerUrl"])
                break
            except Exception:
                time.sleep(0.25)
        if self.ws is None:
            self.stop()
            raise RuntimeError("Chrome did not expose a debugging port in 25s")
        self._id = 0
        self.target = None

    def call(self, method, params=None, session=None):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(msg)
        while True:
            m = self.ws.recv()
            if m.get("id") == self._id:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def new_page(self, width: int, height: int) -> str:
        t = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        sess = self.call("Target.attachToTarget",
                         {"targetId": t, "flatten": True})["sessionId"]
        self.call("Page.enable", session=sess)
        self.call("Runtime.enable", session=sess)
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": 1,
                   "mobile": False}, session=sess)
        return sess

    def goto(self, session: str, url: str, settle: float = 2.5):
        self.call("Page.navigate", {"url": url}, session=session)
        time.sleep(settle)

    def eval(self, session: str, expr: str):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": True}, session=session)
        if r.get("exceptionDetails"):
            raise RuntimeError(f"page threw: {r['exceptionDetails'].get('text')}")
        return r["result"].get("value")

    def print_pdf(self, session: str, path: str, **opts):
        params = {"printBackground": True, "preferCSSPageSize": True}
        params.update(opts)
        data = self.call("Page.printToPDF", params, session=session)["data"]
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))
        return path

    def stop(self):
        if getattr(self, "ws", None):
            self.ws.close()
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        import shutil
        shutil.rmtree(self.profile, ignore_errors=True)


class Server:
    """The app under uvicorn on a free port, started and stopped by this object."""

    def __init__(self, project_root, python=None):
        self.port = free_port()
        py = python or os.path.join(project_root, ".venv", "bin", "python")
        if not os.path.isfile(py):
            import sys
            py = sys.executable
        self.proc = subprocess.Popen(
            [py, "-m", "uvicorn", "api:app", "--port", str(self.port),
             "--log-level", "error"],
            cwd=str(project_root),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/", timeout=2).read()
                return
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("uvicorn exited before serving")
                time.sleep(0.4)
        self.stop()
        raise RuntimeError("uvicorn did not come up in 40s")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
