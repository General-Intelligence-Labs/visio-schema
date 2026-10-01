"""Host tests for mcap_rate_check.py's CameraFrameInfo contract checks.

Each test writes a small MCAP with real embedded descriptors, reads it through
the tool's own `read_file`, and asserts on `verdict`, so the decode, the
running summary and the wording are exercised together.
"""
import importlib.util
import os

from google.protobuf import descriptor_pb2
from mcap.writer import Writer
from visio_schema.reader.rows import _dynamic_message_class
from visio_schema.wire.schema import file_descriptor_set

_SCRIPT = os.path.join(os.path.dirname(__file__), "mcap_rate_check.py")
_spec = importlib.util.spec_from_file_location("mcap_rate_check", _SCRIPT)
mrc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mrc)

FRAME_INFO = "visio_schema.v1.sensor.CameraFrameInfo"
TOPIC = "/ego/camera/0/frame_info"
T0 = 1_700_000_000 * 10**9
DT = 33_333_333


def _retired_schema() -> bytes:
    """The current descriptor set with CameraFrameInfo's fields swapped for the
    retired raw layout (no exposure_us)."""
    fds = descriptor_pb2.FileDescriptorSet.FromString(file_descriptor_set(FRAME_INFO))
    msg = next(m for f in fds.file for m in f.message_type if m.name == "CameraFrameInfo")
    F = descriptor_pb2.FieldDescriptorProto
    del msg.field[:]
    del msg.enum_type[:]
    del msg.reserved_range[:]
    del msg.reserved_name[:]
    msg.field.add(name="timestamp", number=1, type=F.TYPE_MESSAGE, label=F.LABEL_OPTIONAL,
                  type_name=".google.protobuf.Timestamp")
    msg.field.add(name="exposure_time_s", number=5, type=F.TYPE_FLOAT, label=F.LABEL_OPTIONAL)
    return fds.SerializeToString()


def _write(tmp_path, entries, schema_data=None):
    """entries: dicts of CameraFrameInfo fields, one per frame, 33 ms apart."""
    data = schema_data or file_descriptor_set(FRAME_INFO)
    klass = _dynamic_message_class(FRAME_INFO, data)
    path = str(tmp_path / "fi.mcap")
    with open(path, "wb") as f:
        w = Writer(f)
        w.start()
        sid = w.register_schema(name=FRAME_INFO, encoding="protobuf", data=data)
        cid = w.register_channel(topic=TOPIC, message_encoding="protobuf", schema_id=sid)
        for i, fields in enumerate(entries):
            t = T0 + i * DT
            m = klass(**fields)
            m.timestamp.FromNanoseconds(t)
            w.add_message(channel_id=cid, log_time=t, publish_time=t, sequence=i,
                          data=m.SerializeToString())
        w.finish()
    return path


def _verdict(path):
    st = mrc.read_file(path, gap_factor=1.5)["streams"][TOPIC]
    return st, mrc.verdict(st, st["kind"], warn_loss=0.01, fail_loss=0.5)


GOOD = {"exposure_us": 9951, "exposure_mid_offset_us": 10895, "gain": 2.5,
        "line_delay_ns": 24510, "readout_direction": 2}


def test_a_clean_stream_raises_no_frame_info_verdict_and_summarises_ranges(tmp_path):
    entries = [GOOD] * 20 + [dict(GOOD, exposure_us=29828, exposure_mid_offset_us=956)] * 20
    st, bad = _verdict(_write(tmp_path, entries))
    assert not [b for b in bad if "frame_info" in b or "CameraFrameInfo" in b]
    fi = st["frame_info"]
    assert fi["exposure_us"] == (9951, 29828)
    assert fi["mid_offset_us"] == (956, 10895)
    assert fi["line_delay_ns"] == [24510]
    assert fi["readout_direction"] == [2]


def test_zero_exposure_fails(tmp_path):
    _, bad = _verdict(_write(tmp_path, [GOOD] * 10 + [dict(GOOD, exposure_us=0)] + [GOOD] * 10))
    assert "FAIL 1 frame_info entr(ies) with exposure_us == 0" in bad


def test_line_delay_without_direction_fails(tmp_path):
    _, bad = _verdict(_write(tmp_path, [dict(GOOD, readout_direction=0)] * 12))
    assert "FAIL 12 frame_info entr(ies) with a line delay but no readout direction" in bad


def test_global_shutter_without_direction_is_fine(tmp_path):
    gs = dict(GOOD, exposure_mid_offset_us=-2215, line_delay_ns=0, readout_direction=0)
    _, bad = _verdict(_write(tmp_path, [gs] * 12))
    assert not [b for b in bad if "frame_info" in b]


def test_unknown_readout_direction_fails(tmp_path):
    _, bad = _verdict(_write(tmp_path, [dict(GOOD, readout_direction=7)] * 12))
    assert "FAIL 12 frame_info entr(ies) with an unknown readout direction" in bad


def test_retired_layout_warns_and_reports_no_ranges(tmp_path):
    path = _write(tmp_path, [{"exposure_time_s": 0.004}] * 12, schema_data=_retired_schema())
    st, bad = _verdict(path)
    assert any(b.startswith("WARN retired CameraFrameInfo layout") for b in bad)
    assert st["frame_info"]["exposure_us"] is None
    assert not any(b.startswith("FAIL") and "frame_info" in b for b in bad)
