#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tiny dependency-free headless-Chrome client for end-to-end tests.

The repository deliberately has no pytest / selenium / playwright, so the
reader E2E tests drive a REAL headless Chrome over the Chrome DevTools
Protocol using only the standard library:

* a minimal RFC 6455 WebSocket client (no Origin header, so
  ``--remote-allow-origins=*`` is not even required in most builds);
* launch Chrome with ``--remote-debugging-port=0`` and parse the actual port
  from its stderr line ``DevTools listening on ws://127.0.0.1:<port>/...``;
* a small CDP command layer (``Runtime.evaluate`` with ``awaitPromise``,
  ``DOM.setFileInputFiles`` + a synthetic ``change`` event, and
  ``Input.dispatchMouseEvent`` for genuine element clicks).

Usage::

    from headless_chrome import Chrome, find_chrome_path

    with Chrome.open("about:blank") as chrome:
        chrome.navigate("http://127.0.0.1:8000/reader.html")
        chrome.wait_for("dictRows.length > 0")
        value = chrome.evaluate("1 + 1")

Windows, macOS and Linux are supported (see ``find_chrome_path``).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

__all__ = ["Chrome", "find_chrome_path", "WebSocket", "ChromeError"]


class ChromeError(RuntimeError):
    """Raised for anything that goes wrong while driving Chrome."""


# --------------------------------------------------------------------------
# locating Chrome
# --------------------------------------------------------------------------
_CHROME_CANDIDATES = (
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


def find_chrome_path() -> str:
    """Return a path to the Chrome/Chromium executable (or raise)."""
    override = os.environ.get("CHROME_PATH")
    if override:
        if not Path(override).is_file():
            raise ChromeError(f"CHROME_PATH points to a missing file: {override}")
        return override
    for name in ("chrome", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    for cand in _CHROME_CANDIDATES:
        expanded = os.path.expandvars(cand)
        if Path(expanded).is_file():
            return expanded
    raise ChromeError(
        "Chrome not found. Install Google Chrome or set the CHROME_PATH "
        "environment variable."
    )


# --------------------------------------------------------------------------
# RFC 6455 WebSocket (client, masked frames, ping/pong, fragmentation)
# --------------------------------------------------------------------------
class WebSocket:
    """Minimal WebSocket client speaking the DevTools protocol."""

    def __init__(self, host: str, port: int, path: str, timeout: float = 60.0):
        self._sock = socket.create_connection((host, port), timeout=10)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ChromeError("connection closed during handshake")
            response += chunk
        status = response.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 101 " not in status:
            raise ChromeError(f"WebSocket handshake failed: {status}")

    # -- low-level framing ------------------------------------------------
    @staticmethod
    def _mask(data: bytes, mask: bytes) -> bytes:
        return bytes(b ^ mask[i % 4] for i, b in enumerate(data))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        head = bytes([0x80 | opcode])          # FIN + opcode
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])          # MASK bit + length
        elif n < 0x10000:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self._sock.sendall(head + mask + self._mask(payload, mask))

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ChromeError("WebSocket closed by peer")
            data += chunk
        return data

    def recv_message(self) -> str:
        """Read one complete text message (handles fragmentation + ping)."""
        fragments: list[bytes] = []
        while True:
            hdr = self._recv_exact(2)
            fin = bool(hdr[0] & 0x80)
            opcode = hdr[0] & 0x0F
            masked = bool(hdr[1] & 0x80)
            length = hdr[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length)
            if mask:
                payload = self._mask(payload, mask)

            if opcode == 0x8:                 # close
                raise ChromeError("WebSocket closed")
            if opcode == 0x9:                 # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                 # pong, ignore
                continue
            fragments.append(payload)
            if fin:
                break
        return b"".join(fragments).decode("utf-8")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# CDP client
# --------------------------------------------------------------------------
class Chrome:
    """A connected, headed/headless Chrome tab driven over CDP."""

    def __init__(self, proc: subprocess.Popen, port: int, ws_url: str,
                 profile_dir: str, ws: WebSocket):
        self._proc = proc
        self._port = port
        self._ws_url = ws_url
        self._profile_dir = profile_dir
        self._ws = ws
        self._next_id = 0

    # -- lifecycle --------------------------------------------------------
    @classmethod
    def open(cls, url: str = "about:blank") -> "Chrome":
        exe = find_chrome_path()
        profile_dir = tempfile.mkdtemp(prefix="chrome-cdp-")
        args = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile_dir}",
            "--window-size=1400,1000",
            url,
        ]
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,      # read by a thread so it cannot fill
            text=True,
        )
        stderr_lines: list[str] = []
        def _drain() -> None:
            for line in proc.stderr or []:
                stderr_lines.append(line)
        threading.Thread(target=_drain, daemon=True).start()

        port: int | None = None
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise ChromeError(
                    "Chrome exited during startup:\n" + "".join(stderr_lines)
                )
            for line in stderr_lines:
                match = re.search(r"DevTools listening on ws://127\.0\.0\.1:(\d+)", line)
                if match:
                    port = int(match.group(1))
                    break
            if port is not None:
                break
            time.sleep(0.05)
        if port is None:
            raise ChromeError("DevTools endpoint never appeared:\n" + "".join(stderr_lines))

        ws_url = cls._page_ws_url(port)
        match = re.match(r"ws://([^:/]+):(\d+)(/.*)", ws_url)
        if not match:
            raise ChromeError(f"unparseable ws URL: {ws_url}")
        ws = WebSocket(match.group(1), int(match.group(2)), match.group(3))
        return cls(proc, port, ws_url, profile_dir, ws)

    @staticmethod
    def _page_ws_url(port: int) -> str:
        last_error: Exception | None = None
        for _ in range(40):                 # target list may take a moment
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=5
                ) as resp:
                    targets = json.load(resp)
                for target in targets:
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        return target["webSocketDebuggerUrl"]
                raise ChromeError("no page target on /json/list")
            except Exception as exc:         # noqa: BLE001 - retry while Chrome boots
                last_error = exc
                time.sleep(0.1)
        raise ChromeError(f"could not fetch /json/list: {last_error}")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:                    # noqa: BLE001
            pass
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except Exception:                    # noqa: BLE001
            pass
        try:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
        except Exception:                    # noqa: BLE001
            pass

    def __enter__(self) -> "Chrome":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- protocol ---------------------------------------------------------
    def send(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        message = {"id": self._next_id, "method": method}
        if params:
            message["params"] = params
        self._ws.send_text(json.dumps(message))
        while True:
            raw = self._ws.recv_message()
            reply = json.loads(raw)
            if reply.get("id") == self._next_id:        # events have no id
                break
        if "error" in reply:
            err = reply["error"]
            raise ChromeError(f"CDP {method}: {err.get('code')} {err.get('message')}")
        return reply.get("result", {})

    def evaluate(self, expression: str, timeout_ms: int = 30000):
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "timeout": timeout_ms,
            },
        )
        if "exceptionDetails" in result:
            raise ChromeError(
                "page exception: " + json.dumps(result["exceptionDetails"], ensure_ascii=False)
            )
        value = result.get("result", {})
        if value.get("subtype") == "error":
            raise ChromeError("page error: " + str(value.get("description")))
        return value.get("value") if "value" in value else None

    def wait_for(self, expression: str, timeout: float = 60.0, interval: float = 0.1):
        """Poll ``expression`` until it is truthy; return its value."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.evaluate(expression)
            if last:
                return last
            time.sleep(interval)
        raise ChromeError(f"condition not met within {timeout}s: {expression} (last={last!r})")

    def navigate(self, url: str) -> None:
        self.send("Page.enable")
        self.send("Page.navigate", {"url": url})
        self.wait_for("document.readyState === 'complete'", timeout=60)

    def set_file_input_files(self, selector: str, paths) -> None:
        """Set real files on an <input type=file> via CDP (not JS)."""
        self.send("DOM.enable")
        document = self.send("DOM.getDocument", {"depth": -1})
        root_id = document.get("root", {}).get("nodeId")
        node = self.send(
            "DOM.querySelector",
            {"nodeId": root_id, "selector": selector},
        )
        node_id = node.get("nodeId")
        if not node_id:
            raise ChromeError(f"file input not found: {selector}")
        self.send("DOM.setFileInputFiles", {
            "files": [str(Path(p).resolve()) for p in paths],
            "nodeId": node_id,
        })

    def dispatch_event(self, selector: str, event: str = "change") -> None:
        self.evaluate(
            f"document.querySelector({json.dumps(selector)})"
            f".dispatchEvent(new Event({json.dumps(event)}, {{bubbles: true}}))"
        )

    def click(self, x: float, y: float) -> None:
        """Real mouse press+release at viewport CSS coordinates."""
        for kind, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self.send("Input.dispatchMouseEvent", {
                "type": kind,
                "x": x,
                "y": y,
                "button": "left",
                "buttons": buttons,
                "clickCount": 1,
            })

    def element_center(self, selector: str) -> tuple[float, float]:
        """Center of the first element matching ``selector`` (viewport coords)."""
        point = self.evaluate(
            "(() => {"
            "  const el = document.querySelector(" + json.dumps(selector) + ");"
            "  if (!el) return null;"
            "  const r = el.getBoundingClientRect();"
            "  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };"
            "})()"
        )
        if not point:
            raise ChromeError(f"no element for selector {selector!r}")
        return float(point["x"]), float(point["y"])