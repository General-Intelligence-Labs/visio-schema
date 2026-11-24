"""Scalar tactile scenes use the layout's byte format, not a glove model."""
from __future__ import annotations

import math
import struct

import pytest

from visio_schema import Message, make_channel
from visio_schema.display import hand_geometry as hg
from visio_schema.display import scene_derivers as sd
from visio_schema.foxglove.PackedElementField_pb2 import PackedElementField as Field
from visio_schema.foxglove.SceneUpdate_pb2 import SceneUpdate
from visio_schema.v1.calibration.tactile_pb2 import TactileLayout
from visio_schema.v1.sensor.tactile_raw_pb2 import TactileRaw

TOPIC = "/glove_left/tactile/0"


def _layout(kind=Field.UINT8, *, count=1):
    lay = TactileLayout(precision=TactileLayout.APPROXIMATE)
    lay.groups.add().fields.add(name="raw", type=kind)
    width = {Field.INT16: 2, Field.FLOAT32: 4, Field.FLOAT64: 8}.get(kind, 1)
    for i in range(count):
        lay.points.add(group=0, taxel_id=100 + i, index=i * width,
                       x=0.01 * i, y=0.02, part="palm")
    return lay


def _pair(proto, *, layout=False, ns=1_000_000_000):
    channel = make_channel(TOPIC + ("/layout" if layout else ""),
                           proto.DESCRIPTOR.full_name, stream_id=30 if layout else 31)
    msg = Message(stream_id=channel.id, payload=proto.SerializeToString())
    msg.timestamp.FromNanoseconds(ns)
    return msg, channel


def _install(deriver, lay):
    assert deriver.derive(*_pair(lay, layout=True)) is None


def _scene(deriver, data, *, ns=1_000_000_000):
    result = deriver.derive(*_pair(TactileRaw(data=data), ns=ns))
    if result is None:
        return None
    msg, channel = result
    assert channel.topic == TOPIC + "/scene"
    assert msg.timestamp.ToNanoseconds() == ns
    return SceneUpdate.FromString(msg.payload).entities[0]


def _assert_intensities(scene, expected):
    assert [c.color.r for c in scene.cubes] == pytest.approx(expected)
    assert [c.color.b for c in scene.cubes] == pytest.approx([1 - p for p in expected])
    assert [c.size.z for c in scene.cubes] == pytest.approx(
        [0.004 + p * hg.MAX_H for p in expected])
    assert all(c.size.z >= 0.004 and 0 <= c.color.r <= 1 for c in scene.cubes)


def test_uint8_keeps_legacy_appearance_and_labels():
    deriver = sd.TactileSceneDeriver()
    _install(deriver, _layout(count=4))
    scene = _scene(deriver, bytes([0, 40, 80, 255]))
    _assert_intensities(scene, [0, 0.5, 1, 1])
    assert [t.text for t in scene.texts] == ["100", "101", "102", "103"]
    assert scene.frame_id == "glove_left"
    assert scene.cubes[1].pose.position.x == pytest.approx(0.01)


def test_signed_int16_little_endian_and_no_raw_mutation():
    deriver = sd.TactileSceneDeriver()
    _install(deriver, _layout(Field.INT16, count=5))
    data = b"\x00\x80\xff\xff\x00\x00\xe8\x03\xff\x7f"
    pair = _pair(TactileRaw(data=data))
    original = pair[0].payload
    result = deriver.derive(*pair)
    scene = SceneUpdate.FromString(result[0].payload).entities[0]
    _assert_intensities(scene, [0, 0, 0, 1000 / 32767, 1])
    assert pair[0].payload == original
    assert TactileRaw.FromString(original).data == data


def test_multiple_groups_and_nonzero_field_offset():
    deriver = sd.TactileSceneDeriver()
    lay = _layout()
    lay.groups[0].fields[0].offset = 1
    lay.groups.add().fields.add(name="other_scalar", type=Field.INT16, offset=2)
    lay.points.add(group=1, taxel_id=999, index=3, x=0.02, y=0.02)
    _install(deriver, lay)
    _assert_intensities(_scene(deriver, b"\xff\x28\xff\xff\xff\xe8\x03"),
                        [0.5, 1000 / 32767])


@pytest.mark.parametrize(("kind", "fmt", "value", "upper"), [
    (Field.UINT8, "B", 40, 80),
    (Field.INT8, "b", 63, 127),
    (Field.UINT16, "H", 12345, 65535),
    (Field.INT16, "h", 1000, 32767),
    (Field.UINT32, "I", 123456789, 4294967295),
    (Field.INT32, "i", 123456789, 2147483647),
    (Field.FLOAT32, "f", 0.25, 1),
    (Field.FLOAT64, "d", 0.75, 1),
])
def test_all_numeric_types(kind, fmt, value, upper):
    deriver = sd.TactileSceneDeriver()
    _install(deriver, _layout(kind))
    _assert_intensities(_scene(deriver, struct.pack("<" + fmt, value)), [value / upper])


@pytest.mark.parametrize("invalid", ["group", "type", "empty_fields", "multi_fields",
                                    "coordinates", "precision", "part", "empty_points"])
def test_invalid_layout_invalidates_old_decoder(invalid):
    deriver = sd.TactileSceneDeriver()
    _install(deriver, _layout())
    lay = _layout(Field.INT16)
    if invalid == "group":
        lay.points[0].group = 9
    elif invalid == "type":
        lay.groups[0].fields[0].type = 999
    elif invalid == "empty_fields":
        lay.groups[0].ClearField("fields")
    elif invalid == "multi_fields":
        lay.groups[0].fields.add(name="shear", type=Field.INT16)
    elif invalid == "coordinates":
        lay.points[0].x = math.nan
    elif invalid == "precision":
        lay.precision = 99
    elif invalid == "part":
        lay.precision = TactileLayout.LOGICAL
        lay.points[0].part = "unknown_part"
    else:
        lay.ClearField("points")
    with pytest.raises(ValueError, match=r"tactile/0.*layout"):
        _install(deriver, lay)
    assert _scene(deriver, b"\x28\x00") is None
    _install(deriver, _layout())
    _assert_intensities(_scene(deriver, b"\x28"), [0.5])


def test_malformed_layout_invalidates_old_decoder():
    deriver = sd.TactileSceneDeriver()
    _install(deriver, _layout())
    msg, ch = _pair(_layout(), layout=True)
    msg.payload = b"\xff"
    with pytest.raises(ValueError, match=r"tactile/0/layout"):
        deriver.derive(msg, ch)
    assert _scene(deriver, b"\x28") is None


def test_truncated_payload_is_diagnostic_not_zero_filled():
    deriver = sd.TactileSceneDeriver()
    _install(deriver, _layout(Field.INT16, count=2))
    with pytest.raises(ValueError, match=r"tactile/0.*(truncated|bytes)"):
        _scene(deriver, b"\xe8\x03\x00")
    _assert_intensities(_scene(deriver, b"\xe8\x03\x00\x00"), [1000 / 32767, 0])


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_readings_are_diagnostic(value):
    deriver = sd.TactileSceneDeriver()
    _install(deriver, _layout(Field.FLOAT32))
    with pytest.raises(ValueError, match=r"tactile/0.*(finite|NaN)"):
        _scene(deriver, struct.pack("<f", value))


def test_layout_switch_rebuilds_decoder_and_default_range():
    deriver = sd.TactileSceneDeriver()
    _install(deriver, _layout())
    _assert_intensities(_scene(deriver, b"\x28"), [0.5])
    _install(deriver, _layout(Field.INT16))
    _assert_intensities(_scene(deriver, b"\xe8\x03", ns=2_000_000_000), [1000 / 32767])
    _install(deriver, _layout())
    _assert_intensities(_scene(deriver, b"\x28", ns=3_000_000_000), [0.5])


def test_duplicate_layout_is_cached_and_scene_is_throttled(monkeypatch):
    deriver = sd.TactileSceneDeriver()
    lay = _layout(Field.INT16)
    _install(deriver, lay)
    first = _scene(deriver, b"\xe8\x03")
    monkeypatch.setattr(hg, "build_placement", lambda *_: pytest.fail("recompiled placement"))
    monkeypatch.setattr(sd, "_compile_tactile", lambda *_: pytest.fail("recompiled decoder"))
    _install(deriver, lay)
    assert _scene(deriver, b"\xe8\x03", ns=1_032_999_999) is None
    second = _scene(deriver, b"\xe8\x03", ns=1_033_000_000)
    _assert_intensities(first, [1000 / 32767])
    _assert_intensities(second, [1000 / 32767])


@pytest.mark.parametrize("value_range", [(0, 1000), (-1000, 1000)])
def test_configured_display_range_and_clipping(value_range):
    deriver = sd.TactileSceneDeriver(value_range=value_range)
    _install(deriver, _layout(Field.INT16, count=5))
    values = [-32768, -1, 0, 1000, 32767]
    lo, hi = value_range
    expected = [max(0, min(1, (v - lo) / (hi - lo))) for v in values]
    _assert_intensities(_scene(deriver, struct.pack("<5h", *values)), expected)


@pytest.mark.parametrize("value_range", [(0, 0), (2, 1), (math.nan, 1), (0, math.inf),
                                        (-1e308, 1e308)])
def test_invalid_display_range_is_rejected(value_range):
    with pytest.raises(ValueError, match="range"):
        sd.TactileSceneDeriver(value_range=value_range)


def test_bridge_forwards_raw_unchanged_and_uses_range():
    from visio_schema.display import run_bridge

    class Sink:
        def __init__(self):
            self.rows = []
            self.closed = False

        def write(self, msg, ch):
            self.rows.append((msg, ch))

        def close(self):
            self.closed = True

    layout = _pair(_layout(Field.INT16), layout=True)
    raw = _pair(TactileRaw(data=b"\xe8\x03"))
    sink = Sink()
    assert run_bridge(iter([layout, raw]), [sink], derive_scene=True,
                      tactile_range=(0, 2000)) == 2
    assert sink.rows[:2] == [layout, raw]
    assert sink.closed
    _assert_intensities(SceneUpdate.FromString(sink.rows[-1][0].payload).entities[0], [0.5])


@pytest.mark.parametrize("launcher", [False, True])
@pytest.mark.parametrize("scene", [False, True])
def test_cli_forwards_range_without_enabling_scene(monkeypatch, launcher, scene):
    import visio_schema.display as vd

    captured = {}
    if launcher:
        pytest.importorskip("aiohttp")
        from visio_schema.display import serve
        monkeypatch.setattr(serve, "run_serve", lambda **kw: captured.update(kw))
        args = ["--serve"]
    else:
        monkeypatch.setattr(vd, "McapWriter", lambda _: object())
        monkeypatch.setattr(vd, "_replay", lambda _: iter(()))
        monkeypatch.setattr(vd.signal, "signal", lambda *_: None)

        def bridge(_source, _sinks, **kw):
            captured.update(kw)
            return 0

        monkeypatch.setattr(vd, "run_bridge", bridge)
        args = ["--mcap-in", "unused.mcap", "--out", "unused-out.mcap"]
    args += ["--tactile-range", "0", "2000"]
    if scene:
        args.append("--scene")
    assert vd.main(args) == 0
    assert captured["tactile_range"] == (0.0, 2000.0)
    assert captured["derive_scene"] is scene


@pytest.mark.parametrize("limits", [("1", "1"), ("2", "1"), ("nan", "1"), ("0", "inf")])
def test_cli_rejects_invalid_range_before_starting_source(limits, capsys):
    from visio_schema.display import main

    with pytest.raises(SystemExit) as exc:
        main(["--serve", "--tactile-range", *limits])
    assert exc.value.code == 2
    assert "range" in capsys.readouterr().err


@pytest.mark.parametrize("scene", [False, True])
def test_launcher_passes_scene_settings_to_manager(monkeypatch, scene):
    pytest.importorskip("aiohttp")
    from visio_schema.display import serve

    captured = {}

    def make_manager(**kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(serve, "BridgeManager", make_manager)
    monkeypatch.setattr(serve, "DiscoveryService", object)
    monkeypatch.setattr(serve, "_build_app", lambda *_: serve.web.Application())
    monkeypatch.setattr(serve.web, "run_app", lambda *_a, **_kw: None)
    serve.run_serve(serve_port=12345, derive_scene=scene, tactile_range=(0, 2000))
    assert captured["tactile_range"] == (0, 2000)
    assert captured["derive_scene"] is scene


@pytest.mark.parametrize("scene", [False, True])
def test_manager_passes_scene_settings_to_bridge(monkeypatch, scene):
    import threading

    pytest.importorskip("aiohttp")
    from visio_schema.display import serve

    class Endpoint:
        def start(self, *_):
            pass

        def stop(self):
            pass

    captured = {}

    def bridge(_source, _sinks, **kw):
        captured.update(kw)

    monkeypatch.setattr(serve, "FoxgloveSink", lambda _: object())
    monkeypatch.setattr(serve, "_open_endpoint", lambda _: Endpoint())
    monkeypatch.setattr(serve, "run_bridge", bridge)
    manager = serve.BridgeManager(derive_scene=scene, tactile_range=(0, 2000))
    manager._stop = threading.Event()
    manager._run({"transport": serve.MCAP}, manager._stop, decode=False)
    assert manager._error is None
    assert captured["tactile_range"] == (0, 2000)
    assert captured["derive_scene"] is scene


def test_truncation_is_diagnosed_even_inside_throttle_period():
    deriver = sd.TactileSceneDeriver()
    _install(deriver, _layout(Field.INT16))
    assert _scene(deriver, b"\xe8\x03") is not None
    with pytest.raises(ValueError, match="truncated"):
        _scene(deriver, b"\xe8", ns=1_000_000_001)


def test_bad_raw_protobuf_has_topic_diagnostic():
    deriver = sd.TactileSceneDeriver()
    _install(deriver, _layout(Field.INT16))
    msg, ch = _pair(TactileRaw(data=b"\xe8\x03"))
    msg.payload = b"\xff"
    with pytest.raises(ValueError, match=r"tactile/0:"):
        deriver.derive(msg, ch)


class _CollectSink:
    def __init__(self):
        self.rows = []
        self.closed = False

    def write(self, msg, channel):
        assert not self.closed
        self.rows.append((msg, channel))

    def close(self):
        self.closed = True


@pytest.mark.parametrize("failure", ["layout", "truncated", "nonfinite"])
@pytest.mark.parametrize("close_sinks", [False, True])
def test_bridge_isolates_tactile_errors_and_recovers(failure, close_sinks, capsys):
    from visio_schema.display import run_bridge
    from visio_schema.foxglove.FrameTransforms_pb2 import FrameTransforms

    kind = Field.FLOAT32 if failure == "nonfinite" else Field.INT16
    lay = _layout(kind)
    good_data = struct.pack("<f", 0.5) if failure == "nonfinite" else b"\xe8\x03"
    if failure == "layout":
        bad = _layout(kind)
        bad.points[0].group = 99
        bad_pair = _pair(bad, layout=True)
    else:
        bad_data = struct.pack("<f", math.nan) if failure == "nonfinite" else b"\xe8"
        bad_pair = _pair(TactileRaw(data=bad_data), ns=1_040_000_000)
    fts = FrameTransforms()
    fts.transforms.add(child_frame_id="lh_palm").translation.x = 0.1
    skeleton_ch = make_channel("/quest/skeleton/hand_left", sd._TF_SCHEMA, stream_id=50)
    skeleton = Message(stream_id=50, payload=fts.SerializeToString())
    camera_ch = make_channel("/ego/camera/0", "foxglove.CompressedImage", stream_id=51)
    camera = Message(stream_id=51, payload=b"unchanged camera data")
    camera.timestamp.FromNanoseconds(1_100_000_000)
    skeleton.timestamp.FromNanoseconds(1_200_000_000)
    gap = [_pair(TactileRaw(data=good_data), ns=1_050_000_000)] if failure == "layout" else []
    rows = [
        _pair(lay, layout=True),
        _pair(TactileRaw(data=good_data)),
        bad_pair, *gap, bad_pair,
        (camera, camera_ch), (skeleton, skeleton_ch),
        _pair(lay, layout=True, ns=1_500_000_000),
        _pair(TactileRaw(data=good_data), ns=2_000_000_000),
    ]
    sink = _CollectSink()
    assert run_bridge(iter(rows), [sink], derive_scene=True, derive_bitrate=True,
                      close_sinks=close_sinks) == len(rows)
    assert sink.closed is close_sinks
    input_topics = {ch.topic for _, ch in rows}
    assert [(m, ch) for m, ch in sink.rows if ch.topic in input_topics] == rows
    assert any(ch.topic == "/stats/bitrate/_total" and m.timestamp.ToNanoseconds() == 2_000_000_000
               for m, ch in sink.rows)
    tactile = [m for m, ch in sink.rows if ch.topic == TOPIC + "/scene"]
    assert len(tactile) == 2
    intensity = 0.5 if failure == "nonfinite" else 1000 / 32767
    for msg in tactile:
        _assert_intensities(SceneUpdate.FromString(msg.payload).entities[0], [intensity])
    assert any(ch.topic == skeleton_ch.topic + "/scene" for _, ch in sink.rows)
    warnings = capsys.readouterr().err
    assert warnings.count("warning:") == 1
    assert TOPIC in warnings and "raw stream continues" in warnings


def test_bridge_reports_new_error_after_a_valid_frame(capsys):
    from visio_schema.display import run_bridge

    rows = [
        _pair(_layout(Field.INT16), layout=True),
        _pair(TactileRaw(data=b"\xe8\x03")),
        _pair(TactileRaw(data=b"\xe8"), ns=1_000_000_001),
        _pair(TactileRaw(data=b"\xe8\x03"), ns=1_000_000_002),  # throttled, not recovery
        _pair(TactileRaw(data=b"\xe8"), ns=1_000_000_003),
        _pair(TactileRaw(data=b"\xe8\x03"), ns=2_000_000_000),
        _pair(TactileRaw(data=b"\xe8"), ns=3_000_000_000),
    ]
    sink = _CollectSink()
    assert run_bridge(iter(rows), [sink], derive_scene=True) == len(rows)
    assert capsys.readouterr().err.count("warning:") == 2


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, KeyError])
@pytest.mark.parametrize("site", ["compile", "geometry", "decode", "render"])
def test_bridge_does_not_swallow_scene_programming_errors(monkeypatch, error_type, site):
    from visio_schema.display import run_bridge
    from visio_schema.display.tactile_scalars import ScalarLayout

    def broken_scene(*_args, **_kwargs):
        raise error_type("unexpected rendering bug")

    obj, name = {"compile": (sd, "_compile_tactile"),
                 "geometry": (hg, "build_placement"),
                 "decode": (ScalarLayout, "intensities"),
                 "render": (sd, "_tactile_scene")}[site]
    monkeypatch.setattr(obj, name, broken_scene)
    rows = [_pair(_layout(Field.INT16), layout=True), _pair(TactileRaw(data=b"\xe8\x03"))]
    sink = _CollectSink()
    with pytest.raises(error_type, match="unexpected rendering bug"):
        run_bridge(iter(rows), [sink], derive_scene=True)
    assert sink.closed
