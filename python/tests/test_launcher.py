"""Unit tests for the ``visio-display --serve`` launcher: the extracted ``run_bridge``
+ per-bridge stop refactor, device discovery (mocked ``zeroconf`` / ``list_ports``),
and the ``BridgeManager`` connect/switch/disconnect lifecycle (mocked Foxglove sink
+ sources). No hardware, no network, no real Foxglove server.
"""
from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
import types

import pytest
from aiohttp.test_utils import make_mocked_request


def _vd():
    import visio_schema.display as vd
    return vd


def _disc():
    import visio_schema.display.discovery as d
    return d


def _serve():
    import visio_schema.display.serve as s
    return s


# --------------------------------------------------------------------------- #
# run_bridge                                                                   #
# --------------------------------------------------------------------------- #
class _FakeSink:
    def __init__(self) -> None:
        self.writes: list = []
        self.closed = False

    def write(self, msg, ch) -> None:
        self.writes.append((msg, ch))

    def close(self) -> None:
        self.closed = True


def _quat_pair(vd):
    from visio_schema.v1.ros.geometry_msgs.quaternion_pb2 import Quaternion
    from visio_schema.v1.service.device_info.device_info_pb2 import Channel

    q = Quaternion()
    q.x, q.y, q.z, q.w = 0.0, 0.0, 0.0, 1.0
    m = vd.Message(stream_id=20, payload=q.SerializeToString(), seq=0)
    m.timestamp.FromNanoseconds(1)
    ch = Channel(id=20, topic="/g/imu/0/quat", schema_name=vd._QUAT_SCHEMA)
    return m, ch


def test_run_bridge_fans_out_and_derives_tf() -> None:
    vd = _vd()
    m, ch = _quat_pair(vd)
    sink = _FakeSink()
    n = vd.run_bridge(iter([(m, ch)]), [sink], derive_tf=True)
    assert n == 1
    topics = [c.topic for _, c in sink.writes]
    assert "/g/imu/0/quat" in topics  # the source message
    assert "/tf" in topics            # the derived transform
    assert sink.closed is True        # close_sinks defaults True (one-shot semantics)


def test_run_bridge_close_sinks_false_leaves_sink_open() -> None:
    vd = _vd()
    sink = _FakeSink()
    n = vd.run_bridge(iter([]), [sink], close_sinks=False)
    assert n == 0
    assert sink.closed is False       # the launcher's server outlives the source


# --------------------------------------------------------------------------- #
# per-bridge stop threaded through the source (the extraction's key blocker)   #
# --------------------------------------------------------------------------- #
def test_read_tcp_stops_on_event_without_eof() -> None:
    """A per-bridge stop event ends an in-flight TCP source even though the peer
    never closes — the exact thing the module-global _STOP couldn't do per-device."""
    from visio_schema.transport import frame_bytes

    vd = _vd()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    hold = threading.Event()

    def serve() -> None:
        conn, _ = srv.accept()
        m = vd.Message(stream_id=7, payload=b"\x01", seq=0)
        m.timestamp.FromNanoseconds(1)
        conn.sendall(frame_bytes(m))
        hold.wait(5)          # keep the connection open (no EOF) until released
        conn.close()

    t = threading.Thread(target=serve)
    t.start()
    ev = threading.Event()
    try:
        gen = vd.read_tcp(host, port, ev)
        first = next(gen)
        assert first.stream_id == 7
        ev.set()
        assert list(gen) == []   # returns promptly on the stop event, no EOF needed
    finally:
        hold.set()
        t.join(timeout=5)
        srv.close()


# --------------------------------------------------------------------------- #
# discovery                                                                    #
# --------------------------------------------------------------------------- #
class _FakePort:
    def __init__(self, device: str, vid: int, product: str | None = None) -> None:
        self.device = device
        self.vid = vid
        self.product = product


def test_discovery_serial_scan_adds_and_removes() -> None:
    d = _disc()
    svc = d.DiscoveryService()
    changes: list[int] = []
    svc._on_change = lambda: changes.append(len(svc.snapshot()))

    ports = [_FakePort("/dev/ttyACM0", 0x2207, "GILABS-ego"),
             _FakePort("/dev/ttyUSB9", 0x1234)]   # non-GI-Labs vid → ignored
    fake_lp = types.SimpleNamespace(comports=lambda: ports)

    svc._scan_serial(fake_lp)
    devs = svc.snapshot()
    assert len(devs) == 1
    dto = devs[0]
    assert (dto["transport"], dto["device"], dto["id"]) == (
        "usb", "/dev/ttyACM0", "usb:/dev/ttyACM0")
    assert "GILABS-ego" in dto["label"]

    ports.clear()                 # device unplugged
    svc._scan_serial(fake_lp)
    assert svc.snapshot() == []
    assert changes                # on_change fired on add and on remove


def test_discovery_mdns_add_and_remove() -> None:
    d = _disc()
    svc = d.DiscoveryService()

    class FakeInfo:
        port = 50001
        def parsed_addresses(self):
            return ["10.0.0.5"]

    class FakeZC:
        def get_service_info(self, type_, name, timeout=1500):
            return FakeInfo()

    listener = d.DiscoveryService._MdnsListener(svc)
    name = "GILABS-abc123._umi-protocol._tcp.local."
    listener.add_service(FakeZC(), d._MDNS_SERVICE, name)

    devs = svc.snapshot()
    assert len(devs) == 1
    dto = devs[0]
    assert dto["transport"] == "sta"
    assert (dto["host"], dto["port"]) == ("10.0.0.5", 50001)
    assert dto["id"] == "tcp:10.0.0.5:50001"     # keyed by address, not serial
    assert dto["label"] == "GILABS-abc123"       # instance name, suffix stripped

    listener.remove_service(FakeZC(), d._MDNS_SERVICE, name)
    assert svc.snapshot() == []


def test_discovery_add_manual_probes_then_adds(monkeypatch) -> None:
    d = _disc()
    svc = d.DiscoveryService()
    monkeypatch.setattr(d.socket, "create_connection",
                        lambda addr, timeout=None: contextlib.nullcontext())

    dto = svc.add_manual("192.168.4.1")
    assert dto["transport"] == "ap"
    assert dto["id"] == "tcp:192.168.4.1:50001"   # default bus port
    assert svc.snapshot() == [dto]


def test_discovery_add_manual_raises_when_unreachable(monkeypatch) -> None:
    d = _disc()
    svc = d.DiscoveryService()

    def boom(addr, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(d.socket, "create_connection", boom)
    with pytest.raises(OSError):
        svc.add_manual("1.2.3.4", 50001)
    assert svc.snapshot() == []


# --------------------------------------------------------------------------- #
# viewer opener + URLs                                                         #
# --------------------------------------------------------------------------- #
def test_open_deep_link_dispatch_per_platform(monkeypatch) -> None:
    s = _serve()
    seen: dict = {}
    monkeypatch.setattr(s.subprocess, "run",
                        lambda cmd, check=False: seen.__setitem__("cmd", cmd))

    monkeypatch.setattr(s.os, "name", "posix")
    monkeypatch.setattr(s.sys, "platform", "linux")
    assert s._open_deep_link("foxglove://open?x=1") is True
    assert seen["cmd"] == ["xdg-open", "foxglove://open?x=1"]

    monkeypatch.setattr(s.sys, "platform", "darwin")
    assert s._open_deep_link("foxglove://open?x=1") is True
    assert seen["cmd"] == ["open", "foxglove://open?x=1"]


def _fake_fox_factory():
    def make(port):
        return types.SimpleNamespace(
            port=port,
            write=lambda m, c: None,
            reset=lambda: None,
            close=lambda: None,
        )
    return make


def test_viewer_urls_encode_ws(monkeypatch) -> None:
    s = _serve()
    monkeypatch.setattr(s, "FoxgloveSink", _fake_fox_factory())
    mgr = s.BridgeManager(ws_port=8765, viewer="both")
    urls = mgr.viewer_urls()
    assert urls["desktop_url"].startswith(
        "foxglove://open?ds=foxglove-websocket&ds.url=")
    assert "ws%3A%2F%2Flocalhost%3A8765%2F" in urls["desktop_url"]
    assert urls["browser_url"].startswith(
        "https://app.foxglove.dev?ds=foxglove-websocket&ds.url=")


# --------------------------------------------------------------------------- #
# BridgeManager lifecycle — single-reader invariant across a device switch     #
# --------------------------------------------------------------------------- #
class _FakeEndpoint:
    """Fake bidirectional Endpoint for the bridge tests. Feeds ``frames`` (raw Messages)
    to the bridge on start() to drive the streaming path, answers a sent Command via
    ``on_cmd(msg)->reply`` to script CommandResult replies, and (with ``live``) tracks
    that it's alive only between start() and stop() — the single-reader invariant."""

    def __init__(self, *, frames=(), on_cmd=None, live=None, lock=None, started=None):
        self._frames = list(frames)
        self._on_cmd = on_cmd
        self._live = live
        self._lock = lock
        self._started = started
        self._cb = None

    def start(self, on_inbound, on_closed) -> None:
        self._cb = on_inbound
        if self._live is not None:
            with self._lock:
                self._live["n"] += 1
                self._live["max"] = max(self._live["max"], self._live["n"])
        for m in self._frames:
            on_inbound(m, self)
        if self._started is not None:
            self._started.set()

    def send(self, msg) -> None:
        reply = self._on_cmd(msg) if self._on_cmd is not None else None
        if reply is not None:
            self._cb(reply, self)      # simulate the device's CommandResult

    def stop(self) -> None:
        if self._live is not None:
            with self._lock:
                self._live["n"] -= 1


def test_bridge_manager_switch_keeps_single_reader(monkeypatch) -> None:
    s = _serve()

    class FakeFox:
        def __init__(self, port):
            self.port = port
            self.reset_calls = 0
            self.closed = False

        def write(self, m, c):
            pass

        def reset(self):
            self.reset_calls += 1

        def close(self):
            self.closed = True

    monkeypatch.setattr(s, "FoxgloveSink", FakeFox)
    monkeypatch.setattr(s, "_open_deep_link", lambda url: True)

    lock = threading.Lock()
    live = {"n": 0, "max": 0}
    started = threading.Event()

    # a fresh live endpoint per connect; alive start()..stop(), and it never feeds EOF, so
    # the bridge's drain loop blocks until the per-bridge stop event fires on a switch.
    monkeypatch.setattr(s, "_open_endpoint", lambda dto: _FakeEndpoint(
        live=live, lock=lock, started=started))

    mgr = s.BridgeManager(ws_port=0, viewer="desktop")
    try:
        mgr.connect({"id": "usb:/dev/a", "label": "A",
                     "transport": "usb", "device": "/dev/a"})
        assert started.wait(2)
        started.clear()
        mgr.connect({"id": "tcp:h:1", "label": "B",
                     "transport": "sta", "host": "h", "port": 1})
        assert started.wait(2)

        assert live["max"] == 1               # only ever one bus reader at a time
        assert mgr._sink.reset_calls >= 2     # channels reset on each (re)connect
        st = mgr.status()
        assert st["connected_id"] == "tcp:h:1"
    finally:
        mgr.shutdown()
    assert mgr._sink.closed is True           # server torn down only on shutdown
    assert live["n"] == 0                     # no reader left running


# --------------------------------------------------------------------------- #
# CLI wiring                                                                    #
# --------------------------------------------------------------------------- #
def test_serve_flag_mutually_exclusive_with_source() -> None:
    vd = _vd()
    with pytest.raises(SystemExit):
        vd.main(["--serve", "--serial", "/dev/ttyACM0"])


def test_serve_dispatches_to_run_serve(monkeypatch) -> None:
    vd = _vd()
    import visio_schema.display.serve as serve_mod

    captured: dict = {}
    monkeypatch.setattr(serve_mod, "run_serve", lambda **kw: captured.update(kw))
    assert vd.main(["--serve", "--serve-port", "9999", "--viewer", "browser"]) == 0
    assert captured["serve_port"] == 9999
    assert captured["viewer"] == "browser"
    assert captured["ws_port"] == 8765        # reuses --port default


# --------------------------------------------------------------------------- #
# BridgeManager.status() state machine                                         #
# --------------------------------------------------------------------------- #
class _FakeFox:
    def __init__(self, port):
        self.port = port
        self.closed = False

    def write(self, m, c):
        pass

    def reset(self):
        pass

    def close(self):
        self.closed = True


def _install_bridge(monkeypatch, open_endpoint):
    s = _serve()
    monkeypatch.setattr(s, "FoxgloveSink", _FakeFox)
    monkeypatch.setattr(s, "_open_deep_link", lambda url: True)
    monkeypatch.setattr(s, "_open_endpoint", open_endpoint)
    return s


def test_status_streaming_once_messages_flow(monkeypatch) -> None:
    vd = _vd()
    from visio_schema.v1.service.device_info.device_info_pb2 import DeviceInfo
    from visio_schema.wire.control import DEVICE_INFO

    # the device announces a channel, then sends one data frame on it → one resolved row,
    # which flips status to "streaming". _quat_pair gives a valid quat msg + its channel.
    m, ch = _quat_pair(vd)
    announce = vd.Message(stream_id=DEVICE_INFO,
                          payload=DeviceInfo(device_name="t", channels=[ch]).SerializeToString())

    s = _install_bridge(monkeypatch, lambda dto: _FakeEndpoint(frames=[announce, m]))
    mgr = s.BridgeManager(ws_port=0, viewer="desktop")
    try:
        mgr.connect({"id": "usb:/dev/a", "label": "A",
                     "transport": "usb", "device": "/dev/a"})
        deadline = time.time() + 2
        while mgr.status()["state"] != "streaming" and time.time() < deadline:
            time.sleep(0.01)
        st = mgr.status()
        assert st["state"] == "streaming"
        assert st["messages"] >= 1
    finally:
        mgr.shutdown()


def test_status_error_on_open_failure(monkeypatch) -> None:
    def boom(dto):
        raise ConnectionRefusedError("no route to host")

    s = _install_bridge(monkeypatch, boom)
    mgr = s.BridgeManager(ws_port=0, viewer="desktop")
    try:
        mgr.connect({"id": "tcp:h:1", "label": "B",
                     "transport": "sta", "host": "h", "port": 1})
        deadline = time.time() + 2
        while mgr.status()["state"] != "error" and time.time() < deadline:
            time.sleep(0.01)
        st = mgr.status()
        assert st["state"] == "error"
        assert "ConnectionRefusedError" in st["error"]
    finally:
        mgr.shutdown()


def test_status_idle_after_disconnect(monkeypatch) -> None:
    s = _install_bridge(monkeypatch, lambda dto: _FakeEndpoint())
    mgr = s.BridgeManager(ws_port=0, viewer="desktop")
    try:
        mgr.connect({"id": "usb:/dev/a", "label": "A",
                     "transport": "usb", "device": "/dev/a"})
        st = mgr.disconnect()
        assert st["state"] == "idle"
        assert st["connected_id"] is None
    finally:
        mgr.shutdown()


# --------------------------------------------------------------------------- #
# device config — send_command correlation + config HTTP routes                #
# --------------------------------------------------------------------------- #
def _scripted_device(monkeypatch, on_cmd):
    """A bridge whose endpoint announces a ``/dev/command_result`` channel and answers each
    Command via ``on_cmd(Command)->CommandResult`` published on it — the real reply path
    (the device publishes replies on that data channel, not the COMMAND control stream)."""
    s = _serve()
    from visio_schema import make_channel
    from visio_schema.v1.control import command_pb2
    from visio_schema.v1.service.device_info.device_info_pb2 import DeviceInfo
    from visio_schema.wire.control import DEVICE_INFO

    cr_id = 100
    cr_ch = make_channel("/dev/command_result", s._COMMAND_RESULT_SCHEMA, stream_id=cr_id)
    di = DeviceInfo(device_name="dev", channels=[cr_ch]).SerializeToString()
    announce = _vd().Message(stream_id=DEVICE_INFO, payload=di)

    def open_endpoint(dto):
        def reply(sent_msg):
            cmd = command_pb2.Command()
            cmd.ParseFromString(sent_msg.payload)
            res = on_cmd(cmd)
            return _vd().Message(stream_id=cr_id, payload=res.SerializeToString())
        return _FakeEndpoint(frames=[announce], on_cmd=reply)

    monkeypatch.setattr(s, "FoxgloveSink", _FakeFox)
    monkeypatch.setattr(s, "_open_deep_link", lambda url: True)
    monkeypatch.setattr(s, "_open_endpoint", open_endpoint)
    return s


def _connected(mgr) -> None:
    mgr.connect({"id": "usb:/dev/a", "label": "A", "transport": "usb", "device": "/dev/a"})
    for _ in range(200):
        if mgr._endpoint is not None:
            return
        time.sleep(0.005)
    raise AssertionError("endpoint never came up")


def test_send_command_matches_reply_by_id(monkeypatch) -> None:
    from visio_schema.v1.control import command_pb2, command_result_pb2

    def on_cmd(cmd):
        res = command_result_pb2.CommandResult(command_id=cmd.command_id, ok=True)
        if cmd.WhichOneof("body") == "scan_wifi":
            w = res.scan.results.add()
            w.ssid, w.rssi, w.security = "GiLabs", -42, "WPA2"
        return res

    s = _scripted_device(monkeypatch, on_cmd)
    mgr = s.BridgeManager(ws_port=0, viewer="desktop")
    try:
        _connected(mgr)
        r = mgr.send_command(command_pb2.Command(scan_wifi=command_pb2.ScanWifi()))
        assert r["ok"] is True
        assert r["scan"] == [{"ssid": "GiLabs", "rssi": -42, "security": "WPA2"}]
    finally:
        mgr.shutdown()


def test_send_command_surfaces_device_error(monkeypatch) -> None:
    from visio_schema.v1.control import command_pb2, command_result_pb2

    def on_cmd(cmd):
        return command_result_pb2.CommandResult(
            command_id=cmd.command_id, ok=False, error_message="no such fs")

    s = _scripted_device(monkeypatch, on_cmd)
    mgr = s.BridgeManager(ws_port=0, viewer="desktop")
    try:
        _connected(mgr)
        r = mgr.send_command(command_pb2.Command(format_storage=command_pb2.FormatStorage()))
        assert r["ok"] is False and r["error"] == "no such fs"
    finally:
        mgr.shutdown()


def test_send_command_without_device_is_graceful() -> None:
    s = _serve()
    from visio_schema.v1.control import command_pb2
    mgr = s.BridgeManager(ws_port=0, viewer="desktop")
    # never connected → no endpoint; must return an error, not raise
    r = mgr.send_command(command_pb2.Command(identify=command_pb2.Identify()))
    assert r == {"ok": False, "error": "no device connected"}
    mgr.shutdown()


# _post / _StubBridge / _StubDiscovery are defined in the HTTP-handlers section below;
# Python resolves them at call time, so these config-route tests can use them here.
def test_config_wifi_requires_ssid() -> None:
    s = _serve()
    app = s._build_app(_StubBridge(), _StubDiscovery())
    resp = _post(s._config_wifi, app, {"ssid": "  "})
    assert resp.status == 400
    assert "ssid required" in json.loads(resp.body)["error"]


def test_config_bitrate_rejects_non_numeric() -> None:
    s = _serve()
    app = s._build_app(_StubBridge(), _StubDiscovery())
    resp = _post(s._config_bitrate, app, {"bitrate_kbps": "fast"})
    assert resp.status == 400


def test_config_state_calls_bridge_send() -> None:
    s = _serve()
    sent = {}

    class _Bridge:
        def send_command(self, cmd, **kw):
            sent["body"] = cmd.WhichOneof("body")
            return {"ok": True, "state": {"wifi_ssid": "GiLabs"}}

    app = s._build_app(_Bridge(), _StubDiscovery())
    resp = _post(s._config_state, app, {})
    assert resp.status == 200
    assert sent["body"] == "get_state"
    assert json.loads(resp.body)["state"]["wifi_ssid"] == "GiLabs"


def test_send_command_times_out_and_cleans_up(monkeypatch) -> None:
    from visio_schema.v1.control import command_pb2
    # a silent endpoint (default on_cmd=None → send() never replies)
    s = _install_bridge(monkeypatch, lambda dto: _FakeEndpoint())
    mgr = s.BridgeManager(ws_port=0, viewer="desktop")
    try:
        _connected(mgr)
        r = mgr.send_command(command_pb2.Command(identify=command_pb2.Identify()), timeout=0.05)
        assert r == {"ok": False, "error": "timed out waiting for the device"}
        assert mgr._pending == {}            # slot cleaned up — no leak
    finally:
        mgr.shutdown()


def test_disconnect_releases_in_flight_command(monkeypatch) -> None:
    from visio_schema.v1.control import command_pb2
    s = _install_bridge(monkeypatch, lambda dto: _FakeEndpoint())
    mgr = s.BridgeManager(ws_port=0, viewer="desktop")
    try:
        _connected(mgr)
        holder: dict = {}
        t = threading.Thread(target=lambda: holder.update(
            r=mgr.send_command(command_pb2.Command(identify=command_pb2.Identify()), timeout=5)))
        t.start()
        for _ in range(400):                 # wait until the command is registered + waiting
            if mgr._pending:
                break
            time.sleep(0.005)
        assert mgr._pending
        mgr.disconnect()                     # _stop_current → _fail_pending releases the waiter
        t.join(2)
        assert holder["r"] == {"ok": False, "error": "device disconnected"}
        assert mgr._pending == {}
    finally:
        mgr.shutdown()


def test_result_to_dict_all_payloads() -> None:
    s = _serve()
    from visio_schema.v1.control import command_result_pb2

    r = command_result_pb2.CommandResult(command_id=1, ok=True)
    r.state.wifi_ssid = "GiLabs"
    r.state.disk_free_pct = 42
    assert s._result_to_dict(r) == {
        "ok": True, "command_id": 1, "state": {"wifi_ssid": "GiLabs", "disk_free_pct": 42}}

    r2 = command_result_pb2.CommandResult(command_id=2, ok=True)
    w = r2.scan.results.add()
    w.ssid, w.rssi, w.security = "AP", -30, "OPEN"
    assert s._result_to_dict(r2)["scan"] == [{"ssid": "AP", "rssi": -30, "security": "OPEN"}]

    r3 = command_result_pb2.CommandResult(command_id=3, ok=True)
    e = r3.recordings.recordings.add()   # double-nested field
    e.name, e.size_bytes = "sess1", 100
    assert s._result_to_dict(r3)["recordings"] == [{"name": "sess1", "size_bytes": "100"}]

    r4 = command_result_pb2.CommandResult(command_id=4, ok=False, error_message="boom")
    assert s._result_to_dict(r4) == {"ok": False, "command_id": 4, "error": "boom"}


# --------------------------------------------------------------------------- #
# FoxgloveSink.reset() — real channel-close behavior (fake foxglove module)     #
# --------------------------------------------------------------------------- #
def test_foxglove_sink_reset_closes_channels_and_bumps_session(monkeypatch) -> None:
    import sys

    vd = _vd()
    from visio_schema.v1.service.device_info.device_info_pb2 import Channel

    closed: list[str] = []

    class FakeChannel:
        def __init__(self, topic, **kw):
            self.topic = topic

        def log(self, *a, **k):
            pass

        def close(self):
            closed.append(self.topic)

    class FakeServer:
        def __init__(self):
            self.port = 8765
            self.cleared = 0

        def app_url(self):
            return "http://app.example"

        def clear_session(self, session_id=None):
            self.cleared += 1

        def stop(self):
            pass

    server = FakeServer()
    fake_fg = types.SimpleNamespace(
        start_server=lambda port=0: server,
        Channel=FakeChannel,
        Schema=lambda **kw: object(),
    )
    monkeypatch.setitem(sys.modules, "foxglove", fake_fg)

    sink = vd.FoxgloveSink(8765)
    for sid, topic in ((1, "/a"), (2, "/b")):
        m = vd.Message(stream_id=sid, payload=b"x", seq=0)
        m.timestamp.FromNanoseconds(sid)
        sink.write(m, Channel(id=sid, topic=topic, schema_name="X"))
    assert len(sink._channels) == 2

    sink.reset()
    assert sink._channels == {}          # table cleared
    assert set(closed) == {"/a", "/b"}   # every stale channel closed
    assert server.cleared == 1           # viewers told to reset (session bumped)


# --------------------------------------------------------------------------- #
# open_viewer gating                                                           #
# --------------------------------------------------------------------------- #
def test_open_viewer_once_per_session_and_force(monkeypatch) -> None:
    s = _serve()
    monkeypatch.setattr(s, "FoxgloveSink", _fake_fox_factory())
    opened: list[str] = []
    monkeypatch.setattr(s, "_open_deep_link", lambda url: (opened.append(url), True)[1])
    mgr = s.BridgeManager(ws_port=8765, viewer="desktop")
    mgr.open_viewer(force=False)
    mgr.open_viewer(force=False)
    assert len(opened) == 1               # opened once per session
    mgr.open_viewer(force=True)
    assert len(opened) == 2               # force re-opens


def test_open_viewer_browser_mode_never_opens_desktop(monkeypatch) -> None:
    s = _serve()
    monkeypatch.setattr(s, "FoxgloveSink", _fake_fox_factory())
    opened: list[str] = []
    monkeypatch.setattr(s, "_open_deep_link", lambda url: opened.append(url) or True)
    mgr = s.BridgeManager(ws_port=8765, viewer="browser")
    res = mgr.open_viewer(force=True)
    assert opened == []
    assert res["desktop_opened"] is False


def test_open_viewer_failed_open_stays_retryable(monkeypatch) -> None:
    s = _serve()
    monkeypatch.setattr(s, "FoxgloveSink", _fake_fox_factory())
    opened: list[str] = []
    monkeypatch.setattr(s, "_open_deep_link",
                        lambda url: (opened.append(url), False)[1])  # always "fails"
    mgr = s.BridgeManager(ws_port=8765, viewer="desktop")
    mgr.open_viewer(force=False)
    mgr.open_viewer(force=False)          # not gated, since the first open failed
    assert len(opened) == 2


# --------------------------------------------------------------------------- #
# _open_deep_link — Windows + failure                                          #
# --------------------------------------------------------------------------- #
def test_open_deep_link_windows_and_failure(monkeypatch) -> None:
    s = _serve()
    seen: dict = {}
    monkeypatch.setattr(s.os, "name", "nt")
    monkeypatch.setattr(s.sys, "platform", "win32")
    monkeypatch.setattr(s.os, "startfile",
                        lambda url: seen.__setitem__("startfile", url), raising=False)
    assert s._open_deep_link("foxglove://x") is True
    assert seen["startfile"] == "foxglove://x"

    monkeypatch.setattr(s.os, "name", "posix")
    monkeypatch.setattr(s.sys, "platform", "linux")

    def boom(cmd, check=False):
        raise OSError("no xdg-open")

    monkeypatch.setattr(s.subprocess, "run", boom)
    assert s._open_deep_link("foxglove://x") is False


# --------------------------------------------------------------------------- #
# discovery lifecycle + degradation + mDNS rebind                              #
# --------------------------------------------------------------------------- #
def test_discovery_serial_loop_degrades_without_pyserial(monkeypatch) -> None:
    import sys

    d = _disc()
    svc = d.DiscoveryService()
    monkeypatch.setitem(sys.modules, "serial", None)        # force ImportError
    monkeypatch.setitem(sys.modules, "serial.tools", None)
    svc._serial_loop()                                       # returns, no exception
    assert svc.snapshot() == []


def test_discovery_stop_joins_serial_thread(monkeypatch) -> None:
    import sys

    d = _disc()
    svc = d.DiscoveryService()
    fake_serial = types.ModuleType("serial")
    fake_tools = types.ModuleType("serial.tools")
    fake_tools.list_ports = types.SimpleNamespace(comports=lambda: [])
    fake_serial.tools = fake_tools
    monkeypatch.setitem(sys.modules, "serial", fake_serial)
    monkeypatch.setitem(sys.modules, "serial.tools", fake_tools)

    # This test is about the serial-poll thread's start/join, NOT mDNS. Stub out
    # _start_mdns so start() never constructs a real Zeroconf: zeroconf IS installed
    # (it's a base dep), and binding real multicast on a locked-down CI runner (e.g.
    # GitHub's macOS hosts) can block indefinitely in Zeroconf()/close() — which has
    # no join timeout — hanging the whole wheel-test job. Keep the unit hermetic.
    monkeypatch.setattr(svc, "_start_mdns", lambda: None)

    svc.start()
    assert svc._serial_thread.is_alive()
    svc.stop()
    assert not svc._serial_thread.is_alive()


def test_discovery_mdns_rebind_replaces_stale_address() -> None:
    d = _disc()
    svc = d.DiscoveryService()

    class Info:
        def __init__(self, ip):
            self._ip = ip
            self.port = 50001

        def parsed_addresses(self):
            return [self._ip]

    class ZC:
        def __init__(self, ip):
            self._ip = ip

        def get_service_info(self, type_, name, timeout=1500):
            return Info(self._ip)

    listener = d.DiscoveryService._MdnsListener(svc)
    name = "GILABS-x._umi-protocol._tcp.local."
    listener.add_service(ZC("10.0.0.5"), d._MDNS_SERVICE, name)
    listener.add_service(ZC("10.0.0.9"), d._MDNS_SERVICE, name)   # re-resolved address
    devs = svc.snapshot()
    assert len(devs) == 1                # the stale 10.0.0.5 row was dropped
    assert devs[0]["host"] == "10.0.0.9"


# --------------------------------------------------------------------------- #
# HTTP handlers (tested against a built app, no running server)                #
# --------------------------------------------------------------------------- #
class _StubBridge:
    def __init__(self):
        self.connected = None

    def connect(self, dto):
        self.connected = dto
        return {"connected_id": dto["id"], "state": "connecting"}

    def disconnect(self):
        self.connected = None
        return {"connected_id": None, "state": "idle"}


class _StubDiscovery:
    def __init__(self, devices=(), manual_raises=False):
        self._devices = list(devices)
        self._manual_raises = manual_raises

    def snapshot(self):
        return self._devices

    def add_manual(self, host, port):
        if self._manual_raises:
            raise OSError("connection refused")
        return {"id": f"tcp:{host}:{port}", "transport": "ap"}


def _post(handler, app, body):
    req = make_mocked_request("POST", "/x", app=app)

    async def _json():
        return body

    req.json = _json
    import asyncio
    return asyncio.run(handler(req))


def test_http_connect_unknown_id_returns_404() -> None:
    s = _serve()
    app = s._build_app(_StubBridge(), _StubDiscovery(devices=[]))
    resp = _post(s._connect, app, {"id": "nope"})
    assert resp.status == 404
    assert "unknown device" in json.loads(resp.body)["error"]


def test_http_connect_known_id_calls_bridge() -> None:
    s = _serve()
    dto = {"id": "usb:/dev/a", "label": "A", "transport": "usb", "device": "/dev/a"}
    bridge = _StubBridge()
    app = s._build_app(bridge, _StubDiscovery(devices=[dto]))
    resp = _post(s._connect, app, {"id": "usb:/dev/a"})
    assert resp.status == 200
    assert bridge.connected == dto


def test_http_manual_empty_host_returns_400() -> None:
    s = _serve()
    app = s._build_app(_StubBridge(), _StubDiscovery())
    resp = _post(s._manual, app, {"host": "  "})
    assert resp.status == 400
    assert "host required" in json.loads(resp.body)["error"]


def test_http_manual_non_numeric_port_returns_400() -> None:
    s = _serve()
    app = s._build_app(_StubBridge(), _StubDiscovery())
    resp = _post(s._manual, app, {"host": "1.2.3.4", "port": "abc"})
    assert resp.status == 400
    assert "port" in json.loads(resp.body)["error"]


def test_http_manual_unreachable_returns_400() -> None:
    s = _serve()
    app = s._build_app(_StubBridge(), _StubDiscovery(manual_raises=True))
    resp = _post(s._manual, app, {"host": "1.2.3.4"})
    assert resp.status == 400
    assert "unreachable" in json.loads(resp.body)["error"]


def test_http_shutdown_returns_ok(monkeypatch) -> None:
    s = _serve()
    app = s._build_app(_StubBridge(), _StubDiscovery())
    # os._exit is scheduled 0.3 s out; asyncio.run closes the loop before it fires, but
    # guard it anyway so a test can never take the process down.
    monkeypatch.setattr(s.os, "_exit", lambda code: None)
    resp = _post(s._shutdown, app, {})
    assert resp.status == 200
    assert json.loads(resp.body)["stopped"] is True


def test_snapshot_event_framing() -> None:
    s = _serve()
    dto = {"id": "usb:/dev/a", "transport": "usb"}
    ev = s._snapshot_event(_StubDiscovery(devices=[dto]))
    assert ev.startswith(b"data: ") and ev.endswith(b"\n\n")
    assert json.loads(ev[len(b"data: "):-2]) == [dto]


def test_sse_primes_and_unregisters_subscriber() -> None:
    import asyncio

    s = _serve()
    dto = {"id": "usb:/dev/a", "label": "A", "transport": "usb", "device": "/dev/a"}
    app = s._build_app(_StubBridge(), _StubDiscovery(devices=[dto]))

    async def run():
        req = make_mocked_request("GET", "/api/devices", app=app)
        task = asyncio.ensure_future(s._devices_sse(req))
        await asyncio.sleep(0.05)                 # let it prime + register
        assert len(app[s._SUBSCRIBERS]) == 1
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert len(app[s._SUBSCRIBERS]) == 0      # finally discarded on cancel

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# discovery: change-detection + update_service dedup                          #
# --------------------------------------------------------------------------- #
def test_discovery_upsert_no_change_does_not_notify() -> None:
    d = _disc()
    svc = d.DiscoveryService()
    fires: list[int] = []
    svc._on_change = lambda: fires.append(1)

    ports = [_FakePort("/dev/ttyACM0", 0x2207, "GILABS-ego")]
    lp = types.SimpleNamespace(comports=lambda: ports)
    svc._scan_serial(lp)
    assert len(fires) == 1                # first add fires
    svc._scan_serial(lp)                  # identical rescan
    assert len(fires) == 1                # ...but an unchanged device stays silent
    ports.clear()
    svc._scan_serial(lp)
    assert len(fires) == 2                # removal fires


def test_discovery_update_service_skips_known_instance() -> None:
    d = _disc()
    svc = d.DiscoveryService()

    class Info:
        def __init__(self, ip):
            self._ip = ip
            self.port = 50001

        def parsed_addresses(self):
            return [self._ip]

    class ZC:
        def __init__(self, ip):
            self._ip = ip

        def get_service_info(self, type_, name, timeout=1500):
            return Info(self._ip)

    listener = d.DiscoveryService._MdnsListener(svc)
    name = "GILABS-x._umi-protocol._tcp.local."
    listener.add_service(ZC("10.0.0.5"), d._MDNS_SERVICE, name)
    # update_service on a KNOWN instance must NOT re-resolve (that would block the
    # zeroconf thread in get_service_info) — the row keeps its original address.
    listener.update_service(ZC("10.0.0.9"), d._MDNS_SERVICE, name)
    devs = svc.snapshot()
    assert len(devs) == 1
    assert devs[0]["host"] == "10.0.0.5"


def test_run_bridge_derives_bitrate() -> None:
    vd = _vd()
    from visio_schema.v1.service.device_info.device_info_pb2 import Channel

    cam = Channel(id=16, topic="/ego/camera/left", schema_name=vd._VIDEO_SCHEMA)
    S = 1_000_000_000

    def _vid(t_ns, seq):
        m = vd.Message(stream_id=16, payload=b"\x00" * 1000, seq=seq)
        m.timestamp.FromNanoseconds(t_ns)
        return (m, cam)

    sink = _FakeSink()
    # two frames spaced past the 0.5 s emit interval → run_bridge fans a bitrate sample
    vd.run_bridge(iter([_vid(0, 0), _vid(6 * S // 10, 1)]), [sink], derive_bitrate=True)
    topics = {c.topic for _, c in sink.writes}
    assert any(t.startswith("/stats/bitrate") for t in topics)
