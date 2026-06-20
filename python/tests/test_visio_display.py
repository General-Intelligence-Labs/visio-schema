"""Unit tests for the host-runnable pure logic in the packaged `visio_schema.display`
viewer (the `visio-display` command): the quat->FrameTransform derivation, MCAP
replay, the MCAP-source argument guard, and the packaging contract (console
entry point + shipped layout data). The live sinks (Foxglove WS, Rerun viewer)
need a viewer/board and are exercised manually, not here.
"""
from __future__ import annotations

import importlib.util
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
