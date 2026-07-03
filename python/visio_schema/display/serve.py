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
import json
import os
import subprocess
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from pathlib import Path

from aiohttp import web

from . import (
    FoxgloveSink,
    read_serial_resolved,
    read_tcp_resolved,
    run_bridge,
)
from .discovery import DEFAULT_BUS_PORT, USB, DiscoveryService

_STATIC_DIR = Path(__file__).resolve().parent / "static"


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
        self._sink = FoxgloveSink(ws_port)   # long-lived; started here
        self._status = _StatusSink()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._current: dict | None = None
        self._error: str | None = None
        self._viewer_opened = False

    @property
    def ws_url(self) -> str:
        return f"ws://localhost:{self._sink.port}/"      # the actually-bound port

    # -- bridge lifecycle --------------------------------------------------- #
    def _stop_current(self) -> None:
        """Stop + join the active reader thread. The idle source rechecks its stop
        event every 0.2 s, so a bounded join is enough once any in-flight blocking
        open (serial/TCP) has completed."""
        if self._thread is not None and self._thread.is_alive() and self._stop is not None:
            self._stop.set()
            self._thread.join(timeout=1.5)
        self._thread = None
        self._stop = None

    def connect(self, dto: dict) -> dict:
        with self._lock:
            self._stop_current()
            self._sink.reset()      # drop the previous device's channels/session
            self._status.reset()
            self._error = None
            self._current = dto
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(dto, self._stop),
                name="visio-bridge", daemon=True)
            self._thread.start()
        opened = self.open_viewer(force=False)
        return {**self.status(), **opened}

    def disconnect(self) -> dict:
        with self._lock:
            self._stop_current()
            self._current = None
        return self.status()

    def _run(self, dto: dict, stop: threading.Event) -> None:
        # The source generators open lazily on first iteration, so the blocking
        # serial/TCP open happens here on the reader thread — never on the async
        # handler that called connect(). The source stops when `stop` is set.
        try:
            if dto["transport"] == USB:
                source = read_serial_resolved(dto["device"], self._baud, stop)
            else:
                source = read_tcp_resolved(dto["host"], dto["port"], stop)
            run_bridge(source, [self._sink, self._status], derive_tf=True,
                       derive_bitrate=self._bitrate,
                       bitrate_window=self._bitrate_window, close_sinks=False)
        except Exception as exc:  # connect refused, device unplugged mid-open, a bug...
            traceback.print_exc(file=sys.stderr)   # full detail for debugging
            with self._lock:
                if self._stop is stop:   # only report if we're still the current bridge
                    self._error = f"{type(exc).__name__}: {exc}"

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
        return {
            **ident,
            "state": state,
            "messages": messages,
            "topics": topics,
            "error": error,
            "ws_url": self.ws_url,
        }

    def shutdown(self) -> None:
        with self._lock:
            self._stop_current()
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
    # connect() blocks (stop+join the old reader, then a subprocess to open Foxglove)
    # — keep it off the event loop so SSE + status stay responsive during a switch.
    bridge = request.app[_BRIDGE]
    result = await asyncio.get_running_loop().run_in_executor(None, bridge.connect, dto)
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
    app.router.add_static("/static/", _STATIC_DIR)
    return app


def run_serve(*, serve_port: int = 8770, ws_port: int = 8765, viewer: str = "both",
              baud: int = 921600, bitrate: bool = True, bitrate_window: float = 2.0,
              host: str = "127.0.0.1", open_browser: bool = True) -> None:
    """Run the launcher web app (blocks until Ctrl-C / SIGTERM)."""
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
