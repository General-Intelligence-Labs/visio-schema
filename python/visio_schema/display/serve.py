#!/usr/bin/env python3
"""The ``visio-display --serve`` launcher: a local web app that discovers devices and
opens the selected one in Foxglove.

It serves one page on ``http://127.0.0.1:<serve_port>`` (auto-opened in the browser):
a live device list on the left (usb / sta / ap, from :mod:`.discovery`) and, on
select, it starts a bridge — the same :class:`~visio_schema.display.FoxgloveSink`
path the one-shot ``--foxglove`` uses, re-publishing the device over a Foxglove
WebSocket server at ``ws://localhost:<ws_port>`` — then opens Foxglove pointed at it.

Foxglove is opened **externally**, not embedded:

* **desktop** (default, offline-capable): the OS opens the ``foxglove://open?ds=...``
  deep link, launching Foxglove Studio directly — works on the device AP with no
  internet. (This is *not* the SDK's ``app_url(open_in_desktop=True)``, which is a
  ``?openIn=desktop`` web bounce that needs internet.) Fails *silently* if the app
  isn't installed, hence the browser fallback.
* **browser**: a tab at ``https://app.foxglove.dev?ds=...`` — zero-install but needs
  internet + a Foxglove login. Offered as a page button (opened client-side so it can
  reuse one named tab).

One Foxglove server lives for the whole session; switching devices swaps the *source*
feeding it and resets stale channels (:meth:`FoxgloveSink.reset`), so the viewer stays
connected across a switch. Only one bridge (one bus reader) runs at a time.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

from aiohttp import web
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError

from visio_schema import ChannelRegistry, command_message
from visio_schema.transport import extract_frames, frame_bytes, serial_endpoint
from visio_schema.transport.framed_fd import FramedFdEndpoint
from visio_schema.v1.control import command_pb2, command_result_pb2

from . import FoxgloveSink, VideoDecodeSink, dial_tcp, run_bridge
from .discovery import DEFAULT_BUS_PORT, USB, DiscoveryService

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# A CommandResult reply times out if the device doesn't answer within this window.
# ScanWifi is the slow one (the device scans for a couple seconds), so keep it roomy.
_COMMAND_TIMEOUT_S = 10.0
# The device answers a Command with a CommandResult published on its
# ``/<device>/command_result`` DATA channel (NOT the COMMAND control stream — see the
# firmware's VisioPublisher::declare(command_result_topic_)), so replies arrive as normal
# resolved rows in the streaming fan-out and we pick them off by schema.
_COMMAND_RESULT_SCHEMA = "visio_schema.v1.control.CommandResult"


class _CommandResultSink:
    """A display sink that routes CommandResult rows to the pending-command table. It sits
    in the same run_bridge fan-out as the Foxglove/status sinks, so it sees every resolved
    ``(message, channel)`` and forwards the ones on the command_result channel. (Those also
    reach Foxglove as a ``/…/command_result`` topic, which is harmless.)"""

    def __init__(self, deliver) -> None:
        self._deliver = deliver

    def write(self, msg: object, ch) -> None:
        if ch.schema_name == _COMMAND_RESULT_SCHEMA:
            self._deliver(msg.payload)

    def close(self) -> None:
        pass


# CDC-ACM is a virtual UART, so the baud is ignored by the device — but pyserial (the
# Windows read path) requires one.
_WIN_BAUD = 921600


class _WinEndpoint:
    """A bidirectional endpoint for Windows, where the POSIX-fd transport
    (:class:`FramedFdEndpoint`: ``os.pipe`` self-pipe + ``select``/``os.read`` on a raw fd,
    and ``os.O_NOCTTY``) doesn't apply. A daemon thread reads + COBS-de-frames from a
    blocking, timeout-bounded byte source; ``send`` frames + writes on the caller's thread.
    Matches the Endpoint contract BridgeManager uses: ``start(on_inbound, on_closed)`` /
    ``send(msg)`` / ``stop()``. ``read_chunk(n)`` returns bytes (``b""`` = idle tick to
    re-check stop, ``None`` = EOF/link dropped)."""

    def __init__(self, read_chunk, write_bytes, close) -> None:
        self._read_chunk = read_chunk
        self._write_bytes = write_bytes
        self._close = close
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_inbound, on_closed) -> None:
        self._thread = threading.Thread(
            target=self._run, args=(on_inbound, on_closed),
            name="visio-win-ep", daemon=True)
        self._thread.start()

    def _run(self, on_inbound, on_closed) -> None:
        rx = bytearray()
        try:
            while not self._stop.is_set():
                chunk = self._read_chunk(4096)
                if chunk is None:       # EOF: link dropped / device unplugged
                    break
                if not chunk:           # timeout tick
                    continue
                rx.extend(chunk)
                for msg in extract_frames(rx):
                    on_inbound(msg, self)
        finally:
            if on_closed is not None:
                on_closed()

    def send(self, msg) -> None:
        self._write_bytes(frame_bytes(msg))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        self._close()


def _open_endpoint(dto: dict):
    """Open a bidirectional `Endpoint` to a discovered device — the same connection
    both streams to Foxglove and carries config commands (the bus is single-reader, so
    there is exactly one). On POSIX, USB uses the (native-preferring) serial endpoint and
    sta/ap wrap a `dial_tcp` fd in a `FramedFdEndpoint`. On Windows those POSIX-fd paths
    don't work (no select()-able serial fd, `os.read` can't read a socket handle), so we
    read via pyserial / selectors+recv through a :class:`_WinEndpoint`."""
    if dto["transport"] == USB:
        if sys.platform == "win32":
            import serial
            ser = serial.Serial(dto["device"], _WIN_BAUD, timeout=0.2)

            def _read(n):
                try:
                    return ser.read(n)          # b"" on timeout, bytes on data
                except serial.SerialException:
                    return None                 # device gone
            return _WinEndpoint(_read, ser.write, ser.close)
        return serial_endpoint(dto["device"])

    sock = dial_tcp(dto["host"], dto["port"])
    if sys.platform != "win32":
        return FramedFdEndpoint(sock.detach())   # POSIX: adopts + owns the fd
    # Windows: os.read can't read a socket handle; poll with selectors + recv (which do
    # support sockets on Windows), mirroring the read-only _read_sock_win path.
    import selectors
    sock.setblocking(False)
    sel = selectors.DefaultSelector()
    sel.register(sock, selectors.EVENT_READ)

    def _read(n):
        if not sel.select(timeout=0.2):
            return b""                           # idle tick
        try:
            chunk = sock.recv(n)
        except BlockingIOError:
            return b""
        except OSError:
            return None
        return chunk or None                     # recv returns b"" on EOF

    def _close():
        sel.close()
        sock.close()
    return _WinEndpoint(_read, sock.sendall, _close)


def _md(m) -> dict:
    # snake_case field names + INTEGER enums (the page compares wifi_state against ints,
    # and integers survive an unknown future enum value that a name lookup would choke on).
    return MessageToDict(m, preserving_proto_field_name=True, use_integers_for_enums=True)


def _result_to_dict(res: command_result_pb2.CommandResult) -> dict:
    """Flatten a CommandResult into the JSON the page consumes: ok + error, plus the
    typed payload (DeviceState / WifiScanResults) under a stable key when present."""
    out: dict = {"ok": res.ok, "command_id": res.command_id}
    if not res.ok:
        out["error"] = res.error_message or res.error_code or "device reported failure"
    which = res.WhichOneof("payload")
    if which == "state":
        out["state"] = _md(res.state)
    elif which == "scan":
        out["scan"] = [_md(r) for r in res.scan.results]
    elif which == "recordings":
        out["recordings"] = [_md(r) for r in res.recordings.recordings]
    return out


def _scan_host_wifi() -> list[dict]:
    """Scan for Wi-Fi networks visible to THIS host (the operator's PC), so they can pick
    one to provision onto the device. We scan host-side, NOT via the device's ScanWifi,
    because the device usually can't scan while it's in single-radio AP-fallback — and the
    PC is co-located with the device, so it sees the same networks the device could join.
    Returns ``[{ssid, signal(0-100), security}]`` strongest-first; raises if no host
    scanner is available (the page then falls back to manual SSID entry)."""
    if sys.platform == "darwin":
        return _scan_macos()
    if sys.platform == "win32":
        return _scan_windows()
    return _scan_linux()


def _dedup_strongest(nets: list[dict]) -> list[dict]:
    """One row per SSID, keeping the strongest signal, sorted strongest-first."""
    best: dict[str, dict] = {}
    for n in nets:
        if not n["ssid"]:
            continue   # hidden network
        cur = best.get(n["ssid"])
        if cur is None or n["signal"] > cur["signal"]:
            best[n["ssid"]] = n
    return sorted(best.values(), key=lambda n: -n["signal"])


def _scan_linux() -> list[dict]:
    # nmcli -t: terse, ':'-separated, ':' inside a field escaped as '\:'. SIGNAL is 0-100.
    out = subprocess.run(
        ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"],
        capture_output=True, text=True, timeout=20, check=True)
    nets = []
    for line in out.stdout.splitlines():
        fields = [f.replace("\\:", ":") for f in re.split(r"(?<!\\):", line)]
        if len(fields) < 3:
            continue
        ssid, signal, security = fields[0], fields[1], fields[2]
        nets.append({"ssid": ssid, "signal": int(signal) if signal.isdigit() else 0,
                     "security": security or "OPEN"})
    return _dedup_strongest(nets)


def _scan_windows() -> list[dict]:
    # `netsh wlan show networks mode=bssid` groups: "SSID N : name", then "Authentication :
    # ...", then per-BSSID "Signal : NN%". Take the strongest Signal seen under each SSID.
    out = subprocess.run(["netsh", "wlan", "show", "networks", "mode=bssid"],
                         capture_output=True, text=True, timeout=20, check=True)
    nets, cur = [], None
    for raw in out.stdout.splitlines():
        line = raw.strip()
        if line.startswith("SSID ") and " : " in line:
            cur = {"ssid": line.split(" : ", 1)[1].strip(), "signal": 0, "security": "OPEN"}
            nets.append(cur)
        elif cur is not None and line.startswith("Authentication"):
            cur["security"] = line.split(":", 1)[1].strip()
        elif cur is not None and line.startswith("Signal"):
            pct = line.split(":", 1)[1].strip().rstrip("%")
            cur["signal"] = max(cur["signal"], int(pct) if pct.isdigit() else 0)
    return _dedup_strongest(nets)


def _scan_macos() -> list[dict]:
    # The private `airport -s` CLI was removed in macOS 14+, so parse
    # `system_profiler SPAirPortDataType` (no sudo). Under "Other Local Wi-Fi Networks:"
    # each network is an indented "SSID:" block with "Security:" and "Signal / Noise:".
    out = subprocess.run(["system_profiler", "SPAirPortDataType"],
                         capture_output=True, text=True, timeout=25, check=True)
    lines = out.stdout.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip() == "Other Local Wi-Fi Networks:"), None)
    if start is None:
        return []
    base = len(lines[start]) - len(lines[start].lstrip())
    nets, cur = [], None
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= base:
            break   # dedented out of the section
        s = line.strip()
        if s.endswith(":") and ": " not in s:            # a network name ("MySSID:")
            cur = {"ssid": s[:-1], "signal": 0, "security": "OPEN"}
            nets.append(cur)
        elif cur is not None and s.startswith("Security:"):
            cur["security"] = s.split(":", 1)[1].strip()
        elif cur is not None and s.startswith("Signal / Noise:"):
            m = re.search(r"(-?\d+)\s*dBm", s)
            if m:
                cur["signal"] = max(0, min(100, 2 * (int(m.group(1)) + 100)))   # dBm→~%
    return _dedup_strongest(nets)


def _open_deep_link(url: str) -> bool:
    """Open a URL/URI via the OS handler (the ``foxglove://`` desktop deep link).
    Cross-platform: ``open`` (macOS), ``os.startfile`` (Windows), ``xdg-open`` (Linux).
    Returns whether the open call itself succeeded — it can't tell whether Foxglove
    Desktop is actually installed (an absent handler fails silently), which is exactly
    why the page also offers the browser fallback."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url], check=False)
        elif os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]  # Windows-only
        else:
            subprocess.run(["xdg-open", url], check=False)
        return True
    except OSError:
        return False


class _StatusSink:
    """A passive display sink that observes the stream so the launcher can report
    liveness (message count + distinct topics) without decoding payloads. ``write``
    runs on the bridge reader thread while ``snapshot`` is read from the aiohttp
    thread, so both are serialized by a lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages = 0
        self._topics: set[str] = set()

    def reset(self) -> None:
        with self._lock:
            self._messages = 0
            self._topics = set()

    def write(self, msg, ch) -> None:
        with self._lock:
            self._messages += 1
            self._topics.add(ch.topic)

    def snapshot(self) -> tuple[int, list[str]]:
        with self._lock:
            return self._messages, sorted(self._topics)

    def close(self) -> None:
        pass


class BridgeManager:
    """Owns the one long-lived Foxglove server and the single active bus reader.

    ``connect(dto)`` stops any current reader, resets the server's channels, and
    starts a new reader thread pumping the chosen device into the shared server;
    ``disconnect()`` stops the reader but leaves the server up. All transitions are
    serialized by ``_lock`` so only ever one reader (one bus connection) runs."""

    def __init__(self, *, ws_port: int = 8765, viewer: str = "both",
                 baud: int = 921600, bitrate: bool = True,
                 bitrate_window: float = 2.0) -> None:
        self._viewer = viewer
        self._baud = baud
        self._bitrate = bitrate
        self._bitrate_window = bitrate_window
        # long-lived; started here. ws_port=0 (the launcher default) lets foxglove pick a
        # free port — the viewer is always pointed at self._sink.port, so nothing needs a
        # fixed one and startup can never collide with a stale launcher.
        self._sink = FoxgloveSink(ws_port)
        self._status = _StatusSink()
        # _connect_lock serializes connect/disconnect/shutdown (only ONE reader ever).
        # _lock guards the short state reads/writes. They are split so a transition can
        # join() the old reader thread WITHOUT holding _lock — the reader touches _lock
        # in its own teardown, so joining under _lock would deadlock.
        self._connect_lock = threading.Lock()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._current: dict | None = None
        self._error: str | None = None
        self._viewer_opened = False
        # Per-session: transcode H.265 → JPEG on this PC so browsers without HEVC support can still
        # see video (from the page's WebCodecs probe on each connect). ``_video_sink`` is the live
        # transcode sink, kept so status() can report hardware-vs-software decode.
        self._video_sink: VideoDecodeSink | None = None
        # Command/reply plumbing for device config. The active bridge's Endpoint is
        # bidirectional, so config commands ride the SAME connection as the stream;
        # replies (CommandResult) are matched to their request by command_id.
        self._endpoint = None                # visio_schema.transport Endpoint | None
        self._cmd_lock = threading.Lock()
        self._command_seq = 0
        self._pending: dict[int, dict] = {}   # command_id -> {event, result}
        self._cmd_sink = _CommandResultSink(self._deliver_result)   # in the run_bridge fan-out

    @property
    def ws_url(self) -> str:
        return f"ws://localhost:{self._sink.port}/"      # the actually-bound port

    # -- bridge lifecycle (call the following only under _connect_lock) ------ #
    def _stop_current(self) -> None:
        """Signal + join the active reader thread. Grabs the thread/stop refs under
        _lock, then joins OUTSIDE it (the reader's teardown takes _lock, so joining
        while holding it would deadlock). The idle source rechecks its stop event
        every 0.2 s, so a bounded join suffices once any blocking open has completed."""
        with self._lock:
            thread, stop = self._thread, self._stop
            self._thread = None
            self._stop = None
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        self._fail_pending("device disconnected")   # the connection they'd answer on is gone

    def _fail_pending(self, reason: str) -> None:
        """Release every in-flight command waiter with a failure — the endpoint they were
        waiting on is going away, so their reply will never arrive."""
        with self._cmd_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot["result"] = {"ok": False, "error": reason}
            slot["event"].set()

    def connect(self, dto: dict, decode: bool = False) -> dict:
        with self._connect_lock:
            self._stop_current()      # joins the old reader outside _lock
            self._sink.reset()        # drop the previous device's channels/session
            self._status.reset()
            with self._lock:
                self._error = None
                self._current = dto
                self._video_sink = None
                self._stop = threading.Event()
                self._thread = threading.Thread(
                    target=self._run, args=(dto, self._stop, decode),
                    name="visio-bridge", daemon=True)
                self._thread.start()
        opened = self.open_viewer(force=False)
        return {**self.status(), **opened}

    def disconnect(self) -> dict:
        with self._connect_lock:
            self._stop_current()
            with self._lock:
                self._current = None
                self._video_sink = None
        return self.status()

    def _run(self, dto: dict, stop: threading.Event, decode: bool) -> None:
        # Open the bidirectional endpoint here on the reader thread — never on the async
        # handler that called connect() — since the serial/TCP open blocks. on_inbound
        # runs on the endpoint's OWN thread: it peels off CommandResult replies (they
        # ride the COMMAND control stream, which resolved() would drop) and queues
        # everything else for the streaming path. This thread then drains that queue
        # through the same run_bridge fan-out the read-only mode used.
        try:
            ep = _open_endpoint(dto)
        except Exception as exc:  # connect refused, device unplugged mid-open...
            traceback.print_exc(file=sys.stderr)
            with self._lock:
                if self._stop is stop:
                    self._error = f"{type(exc).__name__}: {exc}"
            return

        inbox: queue.Queue = queue.Queue(maxsize=8192)
        link_closed = threading.Event()   # EOF signal — a flag, not a queue item, so a
                                          # backed-up inbox can never swallow the disconnect

        def on_inbound(msg, _ep) -> None:
            # The native reader hands out a ZERO-COPY memoryview payload valid only for
            # THIS callback (its buffer is reused for the next frame), and Foxglove's
            # channel.log rejects a memoryview outright. Materialize to bytes before the
            # message crosses the queue to the reader thread or reaches a sink. (Command
            # replies are demuxed downstream by _CommandResultSink, not here — they ride a
            # data channel, not the COMMAND stream.)
            msg.payload = bytes(msg.payload)
            with contextlib.suppress(queue.Full):
                inbox.put_nowait(msg)         # streaming is lossy-ok under backpressure

        ep.start(on_inbound, link_closed.set)
        with self._lock:
            if self._stop is not stop:        # a newer connect() already superseded us
                ep.stop()
                return
            self._endpoint = ep

        def raw_frames():
            while not stop.is_set():
                try:
                    yield inbox.get(timeout=0.2)
                except queue.Empty:
                    if link_closed.is_set():   # EOF, and the backlog is drained
                        return
                    # else idle tick: re-check stop / EOF

        # When the operator's browser can't decode HEVC, transcode video (H.265 → JPEG) on the way
        # to Foxglove — the raw H.265 is dropped and the JPEG rides the same topic. The wrapper only
        # fronts the Foxglove sink; _status/bitrate still see the raw video for correct link stats.
        # It does the decode+encode on its own per-camera worker threads, so this reader thread
        # stays free to keep draining the USB link.
        fox = VideoDecodeSink(self._sink) if decode else self._sink
        with self._lock:
            self._video_sink = fox if decode else None
        try:
            source = ChannelRegistry().resolved(raw_frames())
            run_bridge(source, [fox, self._status, self._cmd_sink], derive_tf=True,
                       derive_bitrate=self._bitrate,
                       bitrate_window=self._bitrate_window, close_sinks=False)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            with self._lock:
                if self._stop is stop:
                    self._error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                if self._endpoint is ep:   # don't clobber a superseding connect's endpoint
                    self._endpoint = None
            ep.stop()
            if fox is not self._sink:      # a transcode wrapper: stop its worker threads (they
                fox.close()                # hold decoders + write to the shared FoxgloveSink)

    # -- device config: send a Command, await its CommandResult ------------- #
    def send_command(self, cmd: command_pb2.Command, *,
                     timeout: float = _COMMAND_TIMEOUT_S) -> dict:
        """Send a Command on the active bridge connection and block until its
        CommandResult (matched by command_id) or ``timeout``. Returns the flattened
        result dict (see :func:`_result_to_dict`), or an ``ok=False`` error if no device
        is connected / the send fails / it times out. Called from an executor thread by
        the config routes, so blocking here is fine."""
        with self._lock:
            ep = self._endpoint
        if ep is None:
            return {"ok": False, "error": "no device connected"}
        with self._cmd_lock:
            self._command_seq += 1
            cid = self._command_seq
            cmd.command_id = cid
            slot = {"event": threading.Event(), "result": None}
            self._pending[cid] = slot
        try:
            ep.send(command_message(cmd))
        except Exception as exc:
            with self._cmd_lock:
                self._pending.pop(cid, None)
            return {"ok": False, "error": f"send failed: {exc}"}
        if not slot["event"].wait(timeout):
            with self._cmd_lock:
                self._pending.pop(cid, None)
            return {"ok": False, "error": "timed out waiting for the device"}
        return slot["result"]

    def _deliver_result(self, payload) -> None:
        """Match a CommandResult payload to its waiter by command_id and hand it over.
        Called from the streaming reader thread by :class:`_CommandResultSink` for each row
        on the command_result channel. A decode failure is a real anomaly (corruption /
        version skew) — log and drop rather than crash the reader; the waiter falls back to
        its timeout."""
        res = command_result_pb2.CommandResult()
        try:
            res.ParseFromString(payload)
        except DecodeError as exc:
            print(f"visio-display: undecodable CommandResult dropped: {exc}", file=sys.stderr)
            return
        with self._cmd_lock:
            slot = self._pending.pop(res.command_id, None)
        if slot is None:
            return   # unsolicited / already-timed-out reply
        slot["result"] = _result_to_dict(res)
        slot["event"].set()

    # -- viewer ------------------------------------------------------------- #
    def viewer_urls(self) -> dict:
        q = "ds=foxglove-websocket&ds.url=" + urllib.parse.quote(self.ws_url, safe="")
        return {
            "ws_url": self.ws_url,
            "desktop_url": f"foxglove://open?{q}",
            "browser_url": f"https://app.foxglove.dev?{q}",
        }

    def open_viewer(self, *, force: bool) -> dict:
        """Open Foxglove Desktop via the deep link (once per session, or on ``force``).
        The browser tab is intentionally *not* opened server-side — the page opens it
        client-side so it can reuse one named tab and avoid the custom-scheme prompt."""
        urls = self.viewer_urls()
        opened = False
        if self._viewer in ("desktop", "both") and (force or not self._viewer_opened):
            opened = _open_deep_link(urls["desktop_url"])
            if opened:                          # a failed open stays retryable
                self._viewer_opened = True
        return {**urls, "desktop_opened": opened}

    # -- status / shutdown -------------------------------------------------- #
    def status(self) -> dict:
        with self._lock:
            dto = self._current
            error = self._error
        messages, topics = self._status.snapshot()
        if dto is None:
            state = "idle"
        elif error is not None:
            state = "error"
        elif messages > 0:
            state = "streaming"
        else:
            state = "connecting"
        ident = {"connected_id": None, "label": None, "transport": None}
        if dto is not None:
            ident = {"connected_id": dto["id"], "label": dto["label"],
                     "transport": dto["transport"]}
        sink = self._video_sink
        return {
            **ident,
            "state": state,
            "messages": messages,
            "topics": topics,
            "error": error,
            "ws_url": self.ws_url,
            # Host-side transcode status: None = not transcoding (browser decodes the raw H.265),
            # else "hardware"/"software" once the first video frame has opened a decoder.
            "decode_hw": sink.decode_mode() if (dto is not None and sink is not None) else None,
        }

    def shutdown(self) -> None:
        with self._connect_lock:
            self._stop_current()
            with self._lock:
                self._current = None
        self._sink.close()


# --------------------------------------------------------------------------- #
# aiohttp app — handlers read their deps from ``request.app`` (typed AppKeys) so   #
# they're testable without a running server (see tests/test_launcher.py).          #
# --------------------------------------------------------------------------- #
_BRIDGE: web.AppKey[BridgeManager] = web.AppKey("bridge", BridgeManager)
_DISCOVERY: web.AppKey[DiscoveryService] = web.AppKey("discovery", DiscoveryService)
_SUBSCRIBERS: web.AppKey[set] = web.AppKey("subscribers", set)


def _snapshot_event(discovery: DiscoveryService) -> bytes:
    return b"data: " + json.dumps(discovery.snapshot()).encode() + b"\n\n"


async def _index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(_STATIC_DIR / "index.html")


async def _devices_sse(request: web.Request) -> web.StreamResponse:
    discovery = request.app[_DISCOVERY]
    subscribers = request.app[_SUBSCRIBERS]
    resp = web.StreamResponse()
    resp.headers["Content-Type"] = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    await resp.prepare(request)
    ev = asyncio.Event()
    subscribers.add(ev)
    try:
        await resp.write(_snapshot_event(discovery))   # prime with the current list
        while True:
            await ev.wait()
            ev.clear()
            await resp.write(_snapshot_event(discovery))
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        subscribers.discard(ev)
    return resp


async def _connect(request: web.Request) -> web.Response:
    body = await request.json()
    dev_id = body.get("id")
    dto = next((d for d in request.app[_DISCOVERY].snapshot() if d["id"] == dev_id), None)
    if dto is None:
        return web.json_response({"error": f"unknown device {dev_id!r}"}, status=404)
    # `decode` (from the page's WebCodecs probe): when the browser can't render H.265, transcode
    # it to JPEG on this PC. connect() blocks (stop+join the old reader, then a subprocess to open
    # Foxglove) — keep it off the event loop so SSE + status stay responsive during a switch.
    decode = bool(body.get("decode"))
    bridge = request.app[_BRIDGE]
    result = await asyncio.get_running_loop().run_in_executor(
        None, lambda: bridge.connect(dto, decode))
    return web.json_response(result)


async def _disconnect(request: web.Request) -> web.Response:
    bridge = request.app[_BRIDGE]
    result = await asyncio.get_running_loop().run_in_executor(None, bridge.disconnect)
    return web.json_response(result)


async def _manual(request: web.Request) -> web.Response:
    body = await request.json()
    host_in = (body.get("host") or "").strip()
    if not host_in:
        return web.json_response({"error": "host required"}, status=400)
    try:
        port_in = int(body.get("port") or DEFAULT_BUS_PORT)
    except (TypeError, ValueError):
        return web.json_response({"error": "port must be a number"}, status=400)
    discovery = request.app[_DISCOVERY]
    loop = asyncio.get_running_loop()
    try:
        # add_manual() blocks up to 1 s on a reachability probe — off the loop.
        dto = await loop.run_in_executor(None, discovery.add_manual, host_in, port_in)
    except OSError as exc:
        return web.json_response(
            {"error": f"{host_in}:{port_in} unreachable: {exc}"}, status=400)
    return web.json_response(dto)


async def _open_viewer(request: web.Request) -> web.Response:
    bridge = request.app[_BRIDGE]
    result = await asyncio.get_running_loop().run_in_executor(
        None, lambda: bridge.open_viewer(force=True))
    return web.json_response(result)


async def _status(request: web.Request) -> web.Response:
    return web.json_response(request.app[_BRIDGE].status())


# -- device config (send a Command on the connected device's bridge) --------- #
async def _send_command(request: web.Request, cmd: command_pb2.Command) -> web.Response:
    """Run the blocking send+await off the event loop; 200 on ok, 502 on a device/comm
    failure (so the page can show the device's own error_message)."""
    bridge = request.app[_BRIDGE]
    result = await asyncio.get_running_loop().run_in_executor(None, bridge.send_command, cmd)
    return web.json_response(result, status=200 if result.get("ok") else 502)


async def _config_state(request: web.Request) -> web.Response:
    return await _send_command(request, command_pb2.Command(get_state=command_pb2.GetState()))


async def _config_identify(request: web.Request) -> web.Response:
    return await _send_command(request, command_pb2.Command(identify=command_pb2.Identify()))


async def _config_wifi_scan(request: web.Request) -> web.Response:
    # Scan HOST-side (see _scan_host_wifi) — the device usually can't scan while it's in
    # AP-fallback, and the PC sees the same nearby networks the device could join. No device
    # round-trip; the operator picks an SSID here, then Connect provisions it to the device.
    try:
        nets = await asyncio.get_running_loop().run_in_executor(None, _scan_host_wifi)
    except Exception as exc:
        return web.json_response(
            {"ok": False, "error": f"Wi-Fi scan unavailable on this computer: {exc}"}, status=502)
    return web.json_response({"ok": True, "scan": nets})


async def _config_wifi(request: web.Request) -> web.Response:
    body = await request.json()
    ssid = (body.get("ssid") or "").strip()
    if not ssid:
        return web.json_response({"ok": False, "error": "ssid required"}, status=400)
    cmd = command_pb2.Command(connect_wifi=command_pb2.ConnectWifi(
        ssid=ssid, passphrase=body.get("passphrase") or ""))
    return await _send_command(request, cmd)


async def _config_time(request: web.Request) -> web.Response:
    # The launcher runs on the operator's machine, so the server's own clock IS the host
    # time to push — no input needed. RV1106 boards boot to 1970, so this fixes recording
    # timestamps in one click.
    now = datetime.now().astimezone()
    offset_min = int(now.utcoffset().total_seconds() // 60) if now.utcoffset() else 0
    cmd = command_pb2.Command(set_time=command_pb2.SetTime(
        unix_us=int(time.time() * 1_000_000), utc_offset_min=offset_min))
    return await _send_command(request, cmd)


async def _config_bitrate(request: web.Request) -> web.Response:
    body = await request.json()
    try:
        kbps = int(body.get("bitrate_kbps"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "bitrate_kbps must be a number"},
                                 status=400)
    if kbps <= 0:
        return web.json_response({"ok": False, "error": "bitrate_kbps must be positive"},
                                 status=400)
    cmd = command_pb2.Command(set_bitrate=command_pb2.SetBitrate(bitrate_kbps=kbps))
    return await _send_command(request, cmd)


async def _config_meta(request: web.Request) -> web.Response:
    body = await request.json()
    cmd = command_pb2.Command(set_recording_meta=command_pb2.SetRecordingMeta(
        task=body.get("task") or "", location=body.get("location") or "",
        message=body.get("message") or "", capturer=body.get("capturer") or ""))
    return await _send_command(request, cmd)


async def _config_format(request: web.Request) -> web.Response:
    # Destructive: the page gates this behind a typed confirmation. fs_type "" = keep the
    # card's current filesystem; an explicit ext4/exfat/vfat forces one.
    body = await request.json()
    cmd = command_pb2.Command(format_storage=command_pb2.FormatStorage(
        fs_type=(body.get("fs_type") or "").strip()))
    return await _send_command(request, cmd)


async def _shutdown(request: web.Request) -> web.Response:
    # The page's Quit button: a double-clicked app has no terminal to Ctrl-C, so this
    # is how the operator stops it. Flush the response, then hard-exit — the daemon
    # reader thread + the Foxglove server die with the process; nothing to persist.
    asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
    return web.json_response({"stopped": True})


def _build_app(bridge: BridgeManager, discovery: DiscoveryService) -> web.Application:
    """Wire the routes onto an app carrying the bridge + discovery. Split out from
    :func:`run_serve` so the handlers can be exercised with a test client."""
    app = web.Application()
    app[_BRIDGE] = bridge
    app[_DISCOVERY] = discovery
    app[_SUBSCRIBERS] = set()
    app.router.add_get("/", _index)
    app.router.add_get("/api/devices", _devices_sse)
    app.router.add_post("/api/connect", _connect)
    app.router.add_post("/api/disconnect", _disconnect)
    app.router.add_post("/api/manual", _manual)
    app.router.add_post("/api/open-viewer", _open_viewer)
    app.router.add_post("/api/shutdown", _shutdown)
    app.router.add_get("/api/status", _status)
    # device config — each sends a Command on the connected device's bridge
    app.router.add_post("/api/config/state", _config_state)
    app.router.add_post("/api/config/identify", _config_identify)
    app.router.add_post("/api/config/wifi/scan", _config_wifi_scan)
    app.router.add_post("/api/config/wifi", _config_wifi)
    app.router.add_post("/api/config/time", _config_time)
    app.router.add_post("/api/config/bitrate", _config_bitrate)
    app.router.add_post("/api/config/meta", _config_meta)
    app.router.add_post("/api/config/format", _config_format)
    app.router.add_static("/static/", _STATIC_DIR)
    return app


def _free_port(host: str) -> int:
    """An OS-assigned free port on ``host``. The launcher opens the browser itself, so its
    web-UI port needn't be fixed — a free one just works and can't collide with a stale
    launcher. We resolve a concrete number up front (rather than binding port 0 in run_app)
    because we need it in the URL we open the browser at. Tiny TOCTOU window before the real
    bind, fine for a local single-user tool."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def run_serve(*, serve_port: int = 0, ws_port: int = 0, viewer: str = "both",
              baud: int = 921600, bitrate: bool = True, bitrate_window: float = 2.0,
              host: str = "127.0.0.1", open_browser: bool = True) -> None:
    """Run the launcher web app (blocks until Ctrl-C / SIGTERM). Both ports default to 0
    (auto): the web UI takes a free port and the Foxglove WS server (ws_port=0) lets foxglove
    pick one. A caller can still pin either by passing an explicit port."""
    serve_port = serve_port or _free_port(host)
    bridge = BridgeManager(ws_port=ws_port, viewer=viewer, baud=baud,
                           bitrate=bitrate, bitrate_window=bitrate_window)
    discovery = DiscoveryService()
    app = _build_app(bridge, discovery)

    async def on_startup(app: web.Application) -> None:
        loop = asyncio.get_running_loop()
        subscribers = app[_SUBSCRIBERS]

        def wake_all() -> None:
            for ev in subscribers:
                ev.set()

        # on_change fires on a discovery thread → marshal onto the loop.
        discovery.set_on_change(lambda: loop.call_soon_threadsafe(wake_all))
        discovery.start()
        url = f"http://{host}:{serve_port}/"
        print(f"visio-display launcher: {url}", file=sys.stderr)
        if open_browser:
            loop.call_later(0.3, lambda: webbrowser.open(url))

    async def on_cleanup(app: web.Application) -> None:
        bridge.shutdown()
        discovery.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host=host, port=serve_port, print=None)
