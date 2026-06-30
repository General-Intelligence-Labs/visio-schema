"""Unit tests for the host-runnable pure logic in the packaged `visio_schema.display`
viewer (the `visio-display` command): the quat->FrameTransform derivation, MCAP
replay, the MCAP-source argument guard, and the packaging contract (console
entry point + shipped layout data). The live sinks (Foxglove WS, Rerun viewer)
need a viewer/board and are exercised manually, not here.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve().parent
_EXAMPLES = _THIS.parents[1] / "examples" / "python"

pytest.importorskip("mcap", reason="mcap library not installed")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _vd():
    import visio_schema.display as vd
    return vd


def test_tf_deriver_quat_to_frame_transform() -> None:
    """Quaternion payload -> a world->frame FrameTransform on /tf, with the
    rotation, child frame (from the topic), and timestamp carried through."""
    vd = _vd()
    from visio_schema.foxglove.FrameTransform_pb2 import FrameTransform
    from visio_schema.v1.ros.geometry_msgs.quaternion_pb2 import Quaternion
    from visio_schema.v1.service.device_info.device_info_pb2 import Channel

    q = Quaternion()
    q.x, q.y, q.z, q.w = 0.0, 0.7071, 0.0, 0.7071
    m = vd.Message(stream_id=20, payload=q.SerializeToString(), seq=3)
    m.timestamp.FromNanoseconds(1_700_000_000_000_000_000)
    ch = Channel(id=20, topic="/gripper/imu/0/quat", schema_name=vd._QUAT_SCHEMA)

    out = vd.TfDeriver().derive(m, ch)
    assert out is not None
    tf_msg, tf_ch = out
    assert tf_ch.topic == "/tf"
    assert tf_ch.schema_name == "foxglove.FrameTransform"
    assert len(tf_ch.schema) > 0  # embedded FileDescriptorSet so it self-describes
    ft = FrameTransform()
    ft.ParseFromString(tf_msg.payload)
    assert ft.parent_frame_id == "world"
    assert ft.child_frame_id == "gripper/imu/0"
    assert (ft.rotation.x, ft.rotation.y, ft.rotation.z, ft.rotation.w) == (q.x, q.y, q.z, q.w)
    assert tf_msg.timestamp.ToNanoseconds() == 1_700_000_000_000_000_000

    # A non-quat channel is passed through untouched (no /tf emitted).
    cam = Channel(id=16, topic="/gripper/camera/0", schema_name=vd._VIDEO_SCHEMA)
    assert vd.TfDeriver().derive(m, cam) is None


def _bitrate_msg(vd, stream_id: int, nbytes: int, t_ns: int, seq: int = 0):
    m = vd.Message(stream_id=stream_id, payload=b"\x00" * nbytes, seq=seq)
    m.timestamp.FromNanoseconds(t_ns)
    return m


def test_bitrate_deriver_counts_seq_gap_drops() -> None:
    """A bitrate dip caused by *lost* frames is distinguished from one caused by
    *smaller* frames: gaps in a stream's seq become `drops`/`drop_pct`, while a
    large jump (reconnect/wrap) is not counted as a drop burst."""
    vd = _vd()
    from visio_schema.v1.service.device_info.device_info_pb2 import Channel

    cam = Channel(id=16, topic="/ego/camera/left", schema_name=vd._VIDEO_SCHEMA)
    S = 1_000_000_000

    d = vd.BitrateDeriver(window=2.0)
    # seq 0,1,2 then jump to 5 (3 and 4 lost) then 6 — 2 frames dropped in window.
    for t_ns, seq in [(0, 0), (S // 10, 1), (S // 5, 2), (3 * S // 10, 5)]:
        assert d.feed(_bitrate_msg(vd, 16, 1000, t_ns, seq=seq), cam) == []
    out = d.feed(_bitrate_msg(vd, 16, 1000, S // 2, seq=6), cam)
    assert out

    samples = {ch.topic: json.loads(m.payload) for m, ch in out}
    cam_s = samples["/stats/bitrate/ego/camera/left"]
    assert cam_s["drops"] == 2                       # seq 3 and 4
    assert cam_s["fps"] == pytest.approx(5 / 2.0)    # 5 delivered frames in the window
    assert cam_s["drop_pct"] == pytest.approx(100.0 * 2 / 7)  # 2 lost of 7 expected
    assert samples["/stats/bitrate/_total"]["drops"] == 2

    # An implausible jump (reset/reconnect) is not a drop burst.
    d2 = vd.BitrateDeriver(window=2.0)
    assert d2.feed(_bitrate_msg(vd, 16, 1000, 0, seq=10), cam) == []
    out2 = d2.feed(_bitrate_msg(vd, 16, 1000, S // 2, seq=999_999), cam)
    assert json.loads(out2[0][0].payload)["drops"] == 0


def test_bitrate_deriver_total_and_per_video() -> None:
    """A sliding-window bitrate is published once an emit interval of message-time
    elapses: one json line per video stream plus a /_total carrying every stream
    (video + non-video) and a video-only subtotal — all with exact arithmetic over
    the windowed bytes."""
    vd = _vd()
    from visio_schema.v1.service.device_info.device_info_pb2 import Channel

    cam = Channel(id=16, topic="/ego/camera/left", schema_name=vd._VIDEO_SCHEMA)
    imu = Channel(id=20, topic="/ego/imu/0/raw", schema_name=vd._IMU_RAW_SCHEMA)
    S = 1_000_000_000  # 1 s in ns

    d = vd.BitrateDeriver(window=2.0)
    # First message only primes the message-time clock — nothing emitted.
    assert d.feed(_bitrate_msg(vd, 16, 1000, 0), cam) == []
    # Within the first 0.5 s of message-time: still throttled.
    assert d.feed(_bitrate_msg(vd, 16, 1000, S // 10), cam) == []
    assert d.feed(_bitrate_msg(vd, 16, 1000, S // 5), cam) == []
    assert d.feed(_bitrate_msg(vd, 20, 500, S // 4), imu) == []   # non-video, counts in total
    assert d.feed(_bitrate_msg(vd, 16, 1000, 3 * S // 10), cam) == []
    assert d.feed(_bitrate_msg(vd, 16, 1000, 4 * S // 10), cam) == []
    # t == 0.5 s: emit. Window (2 s) still covers everything fed so far.
    out = d.feed(_bitrate_msg(vd, 16, 1000, S // 2), cam)
    assert out, "a bitrate sample should be due at the emit interval"

    samples = {ch.topic: json.loads(m.payload) for m, ch in out}
    # Only the video stream gets its own line; the IMU folds into the total only.
    assert set(samples) == {"/stats/bitrate/_total", "/stats/bitrate/ego/camera/left"}

    w = 2.0
    tot = samples["/stats/bitrate/_total"]
    assert tot["bytes"] == 6 * 1000 + 500              # 6 video frames + 1 imu
    assert tot["mbps"] == pytest.approx(6500 * 8 / w / 1e6)
    assert tot["video_mbps"] == pytest.approx(6000 * 8 / w / 1e6)
    assert tot["fps"] == pytest.approx(7 / w)

    camrate = samples["/stats/bitrate/ego/camera/left"]
    assert camrate["bytes"] == 6000
    assert camrate["mbps"] == pytest.approx(6000 * 8 / w / 1e6)
    assert camrate["fps"] == pytest.approx(6 / w)


def test_bitrate_channels_are_json_and_in_synthetic_id_space() -> None:
    """Bitrate rides json channels (so it never touches the protobuf wire
    contract) on synthetic stream ids that can't collide with announced ids or
    the /tf stream; the emitted message timestamp is the source message-time."""
    vd = _vd()
    from visio_schema.v1.service.device_info.device_info_pb2 import Channel

    cam = Channel(id=17, topic="/ego/camera/right", schema_name=vd._VIDEO_SCHEMA)
    d = vd.BitrateDeriver(window=2.0)
    assert d.feed(_bitrate_msg(vd, 17, 2048, 0), cam) == []
    out = d.feed(_bitrate_msg(vd, 17, 2048, 600_000_000), cam)  # 0.6 s -> emit
    assert out

    for m, ch in out:
        assert ch.encoding == "json"
        assert ch.schema_encoding == "jsonschema"
        assert ch.schema_name == "visio.stats.Bitrate"
        assert len(ch.schema) > 0                  # self-describing JSON Schema
        assert ch.id >= vd._BITRATE_STREAM_BASE
        assert ch.id != vd._TF_STREAM_ID
        assert m.stream_id == ch.id
        assert m.timestamp.ToNanoseconds() == 600_000_000
    # The per-source channel id is the base offset by the source stream id.
    ids = {ch.topic: ch.id for _, ch in out}
    assert ids["/stats/bitrate/ego/camera/right"] == vd._BITRATE_STREAM_BASE + 17
    assert ids["/stats/bitrate/_total"] == vd._BITRATE_STREAM_BASE


def test_bitrate_deriver_throttles_before_emit_interval() -> None:
    """No sample is emitted until an emit interval of message-time has passed
    since the previous emit (the priming message included)."""
    vd = _vd()
    from visio_schema.v1.service.device_info.device_info_pb2 import Channel

    cam = Channel(id=16, topic="/ego/camera/left", schema_name=vd._VIDEO_SCHEMA)
    d = vd.BitrateDeriver(window=2.0)
    assert d.feed(_bitrate_msg(vd, 16, 1000, 0), cam) == []            # prime
    assert d.feed(_bitrate_msg(vd, 16, 1000, 400_000_000), cam) == []  # 0.4 s < 0.5 s
    assert d.feed(_bitrate_msg(vd, 16, 1000, 600_000_000), cam)        # 0.6 s -> emit


def test_read_mcap_roundtrips(tmp_path) -> None:
    """A recording read back through read_mcap yields (Message, Channel) pairs
    with the topic, schema name + embedded FileDescriptorSet, payload and
    stream_id intact."""
    vd = _vd()
    gen = _load("make_sample_mcap", _EXAMPLES / "make_sample_mcap.py")
    out = tmp_path / "s.mcap"
    gen.generate(str(out), seconds=1.0)

    vd._STOP.clear()
    pairs = list(vd.read_mcap(str(out)))
    assert pairs, "no messages replayed"
    topics = {ch.topic for _, ch in pairs}
    assert gen.IMU_RAW_TOPIC in topics
    for msg, ch in pairs:
        assert ch.id == msg.stream_id
        assert ch.schema_name and ch.schema  # every channel is self-describing
        assert ch.encoding == "protobuf"


def test_parse_tcp_defaults_and_explicit_port() -> None:
    """--tcp HOST uses the device's default preview port; HOST:PORT overrides it."""
    vd = _vd()
    assert vd._DEFAULT_TCP_PORT == 9000
    assert vd._parse_tcp("GILABS-1234.local") == ("GILABS-1234.local", 9000)
    assert vd._parse_tcp("10.0.0.7:50001") == ("10.0.0.7", 50001)


def test_read_tcp_roundtrips() -> None:
    """read_tcp dials a TCP server, de-frames the same COBS core frames the serial
    path uses, and yields the Messages — then returns cleanly on peer EOF (the
    device closing the preview connection)."""
    import socket
    import threading

    from visio_schema.transport import frame_bytes

    vd = _vd()
    vd._STOP.clear()

    sent = []
    for i in range(3):
        m = vd.Message(stream_id=7, payload=bytes([i, i + 1, i + 2]), seq=i)
        m.timestamp.FromNanoseconds(1_700_000_000_000_000_000 + i)
        sent.append(m)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()

    def serve() -> None:
        conn, _ = srv.accept()
        with conn:
            for m in sent:
                conn.sendall(frame_bytes(m))
        # conn closing => client sees EOF => read_tcp returns.

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        got = list(vd.read_tcp(host, port))
    finally:
        thread.join(timeout=5)
        srv.close()

    assert len(got) == len(sent)
    for out, exp in zip(got, sent, strict=True):
        assert (out.stream_id, out.seq, out.payload) == (exp.stream_id, exp.seq, exp.payload)


def test_mcap_source_with_foxglove_is_rejected(capsys) -> None:
    """MCAP-file -> Foxglove is unsupported; with no other sink the tool exits
    with the open-it-in-Studio guidance rather than serving the file over WS."""
    vd = _vd()
    with pytest.raises(SystemExit):
        vd.main(["--mcap-in", "/nonexistent.mcap", "--foxglove"])
    err = capsys.readouterr().err
    assert "Foxglove Studio" in err  # the user is pointed at File > Open local file


def test_run_discards_message_count_and_exits_clean(monkeypatch) -> None:
    """`run` is the console entry (not `main`) precisely so a successful N-message
    run exits 0 instead of leaking the message count as the process exit code.
    Guard that: `run` calls `main`, drops its int return, and raises no SystemExit."""
    vd = _vd()
    monkeypatch.setattr(vd, "main", lambda argv=None: 7)  # pretend 7 messages processed
    assert vd.run() is None  # not SystemExit(7), not the count


def test_help_exits_zero() -> None:
    """The CLI parser is wired up: `--help` prints usage and exits 0."""
    vd = _vd()
    with pytest.raises(SystemExit) as ei:
        vd.main(["--help"])
    assert ei.value.code == 0


def test_layout_data_is_shipped() -> None:
    """The Foxglove starter layout ships as package data beside the module, so the
    installed command can point users at its absolute path."""
    import json

    vd = _vd()
    assert vd._LAYOUT_PATH.exists()
    json.loads(vd._LAYOUT_PATH.read_text())  # parses as JSON


def test_pyproject_declares_console_script_and_default_deps() -> None:
    """Guard the packaging contract: the `visio-display` console script stays
    declared, and the viewer + MCAP deps ship as base dependencies (installed by
    default) rather than behind feature extras."""
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        tomllib = pytest.importorskip("tomli")

    pyproject = _THIS.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    assert data["project"]["scripts"]["visio-display"] == "visio_schema.display:run"
    deps = " ".join(data["project"]["dependencies"])
    for pkg in ("mcap", "pyserial", "foxglove-sdk", "rerun-sdk", "av"):
        assert pkg in deps, f"{pkg} should be a default dependency"
    # No feature-gating extras — they were folded into the default install.
    assert set(data["project"].get("optional-dependencies", {})) == {"dev"}
