"""Cross-language golden wire vectors (Python side).

Loads the committed byte fixtures in ../../tests/golden/wire_vectors.txt and
asserts the Python (libprotobuf) codec encodes the mirrored inputs to exactly
those bytes and decodes them back. The C++ test (cpp/tests/test_golden_vectors.cc)
pins the SAME bytes from the SAME file with mirrored inputs, so green on both
sides proves nanopb and libprotobuf wire output are byte-identical.

To regenerate after an intentional change: run the snippet in the repo's
generator note, then update this file's mirrored inputs and the C++ test.
"""
from __future__ import annotations

from pathlib import Path

from google.protobuf.timestamp_pb2 import Timestamp
from visio_schema.service.device_info.v1.device_info_pb2 import DeviceInfo
from visio_schema.wire.message import Message, decode_message, encode_message
from visio_schema.wire.v1.header_pb2 import Header

_GOLDEN = Path(__file__).resolve().parents[1].parent / "tests" / "golden" / "wire_vectors.txt"

# Mirrored inputs — MUST match cpp/tests/test_golden_vectors.cc.
STREAM_ID, SEQ, TS_S, TS_N = 16, 7, 1234, 5678
PAYLOAD = b"golden-payload"
DEVICE = "gripper_left"
FIRMWARE = "1.2.3"
CH_ID, CH_TOPIC = 16, "/gripper_left/imus/2/raw"
CH_SCHEMA = "visio_schema.sensor.v1.ImuRaw"


def _load() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for line in _GOLDEN.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, hexbytes = line.split("=", 1)
        out[key] = bytes.fromhex(hexbytes)
    return out


VEC = _load()


def _ts() -> Timestamp:
    return Timestamp(seconds=TS_S, nanos=TS_N)


def test_header_golden() -> None:
    h = Header(stream_id=STREAM_ID, seq=SEQ)
    h.timestamp.CopyFrom(_ts())
    assert h.SerializeToString() == VEC["header"]

    d = Header()
    d.ParseFromString(VEC["header"])
    assert (d.stream_id, d.seq) == (STREAM_ID, SEQ)
    assert (d.timestamp.seconds, d.timestamp.nanos) == (TS_S, TS_N)


def test_frame_golden() -> None:
    m = Message(stream_id=STREAM_ID, seq=SEQ, payload=PAYLOAD)
    m.timestamp.CopyFrom(_ts())
    assert encode_message(m) == VEC["frame"]

    dm = decode_message(VEC["frame"])
    assert (dm.stream_id, dm.seq, dm.payload) == (STREAM_ID, SEQ, PAYLOAD)
    assert (dm.timestamp.seconds, dm.timestamp.nanos) == (TS_S, TS_N)


def test_device_info_golden() -> None:
    di = DeviceInfo(device_name=DEVICE, firmware_version=FIRMWARE)
    c = di.channels.add()
    c.id = CH_ID
    c.topic = CH_TOPIC
    c.encoding = "protobuf"
    c.schema_name = CH_SCHEMA
    c.schema_encoding = "protobuf"
    assert di.SerializeToString() == VEC["device_info"]

    dd = DeviceInfo()
    dd.ParseFromString(VEC["device_info"])
    assert dd.device_name == DEVICE and dd.firmware_version == FIRMWARE
    assert len(dd.channels) == 1
    assert dd.channels[0].id == CH_ID
    assert dd.channels[0].topic == CH_TOPIC
    assert dd.channels[0].schema_name == CH_SCHEMA
